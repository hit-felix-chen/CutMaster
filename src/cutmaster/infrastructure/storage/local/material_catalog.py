"""Local persistent catalog for immutable source media.

Material Names are type-scoped public selectors.  Each record also owns an
opaque, immutable Material ID used for managed storage.  SHA-256 fingerprints
guard source consistency; they are neither public selectors nor global
deduplication keys.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Sequence
from uuid import uuid4

from cutmaster.application.ports.material_catalog import (
    MaterialBinding,
    MaterialRecord,
    MaterialReferenceChecker,
)
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import (
    Material,
    MaterialCondition,
    MaterialFingerprint,
    MaterialType,
)


MANIFEST_SCHEMA_VERSION = 2
ANALYSIS_RESULT_SCHEMA_VERSION = "3.0"
_VIDEO_ANALYSIS_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "material_id",
        "material_type",
        "material_name",
        "material_fingerprint",
        "memory_schema_version",
        "elapsed_sec",
        "material_reused",
        "analysis_reused",
        "model_usage_summary",
        "model_usage_cumulative_summary",
    }
)
_MUSIC_ANALYSIS_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "material_id",
        "material_type",
        "material_name",
        "material_fingerprint",
        "memory_schema_version",
        "elapsed_sec",
        "material_reused",
        "analysis_reused",
    }
)
_MANIFEST_NAME = "manifest.json"
_MANIFEST_LOCK_NAME = ".manifest.lock"
_SIDECAR_DIRECTORY_NAME = "sidecars"
_SUBTITLE_FILE_NAME = "subtitle.srt"
_SUBTITLE_METADATA_NAME = "subtitle.json"
_SUBTITLE_METADATA_SCHEMA_VERSION = "1.0"
_WHITESPACE = re.compile(r"\s+")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_MATERIAL_ID_ALLOCATION_ATTEMPTS = 100


class MaterialCatalogError(RuntimeError):
    """Base error raised by the local Material Catalog."""


class InvalidMaterialNameError(MaterialCatalogError, ValueError):
    """Raised when a candidate Material Name cannot be normalized."""


class InvalidManifestError(MaterialCatalogError):
    """Raised when the on-disk manifest is malformed or unsafe."""


class MaterialNotFoundError(MaterialCatalogError):
    """Raised when a canonical ID does not identify a Material."""


class MaterialNameCollisionError(MaterialCatalogError):
    """Raised when a type-scoped public Material Name already exists."""


class MaterialInconsistentError(MaterialCatalogError):
    """Raised when managed source bytes no longer match their fingerprint."""


class MaterialReferencedError(MaterialCatalogError):
    """Raised when deletion is attempted for a referenced Material."""

    def __init__(self, material_id: MaterialId, references: Sequence[str]) -> None:
        self.material_id = material_id
        self.references = tuple(references)
        super().__init__(
            f"Material {material_id} is still referenced by "
            f"{len(self.references)} record(s)"
        )


class MaterialConsumedError(MaterialCatalogError):
    """Raised when deletion cannot acquire its exclusive Material lease."""

    def __init__(self, material_id: MaterialId) -> None:
        self.material_id = material_id
        super().__init__(f"Material {material_id} has active workflow consumers")


class InvalidAnalysisResultError(MaterialCatalogError):
    """Raised when a staged result is not the leased Material's current result."""


class MaterialLeaseRequiredError(MaterialCatalogError):
    """Raised when publication is attempted without this Catalog's live lease."""


class InvalidSubtitleSidecarError(MaterialCatalogError, ValueError):
    """Raised when a proposed subtitle is not a safe regular file."""


class SubtitleSidecarConflictError(MaterialCatalogError):
    """Raised when immutable subtitle ownership would be changed."""


class _NoMaterialReferences:
    def references(self, material_id: MaterialId) -> Sequence[str]:
        del material_id
        return ()


@dataclass(frozen=True)
class _StoredMaterial:
    material: Material
    source_path: Path
    memory_root: Path


def _parse_material_type(value: MaterialType | str) -> MaterialType:
    if isinstance(value, MaterialType):
        return value
    try:
        return MaterialType(str(value).strip().lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in MaterialType)
        raise ValueError(
            f"Unsupported material type {value!r}; expected one of: {choices}"
        ) from exc


def normalize_material_name(value: str) -> str:
    """Normalize a proposed Material Name without changing its meaning."""

    if not isinstance(value, str):
        raise InvalidMaterialNameError("Material name must be a string")
    name = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if not name:
        raise InvalidMaterialNameError("Material name cannot be empty")
    if name in {".", ".."}:
        raise InvalidMaterialNameError(f"Material name {name!r} is reserved")
    if any(unicodedata.category(character) == "Cc" for character in name):
        raise InvalidMaterialNameError(
            "Material name cannot contain control characters"
        )
    return name


def fingerprint_file(path: Path | str, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the complete SHA-256 digest of a file's bytes."""

    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _exclusive_file_lock(
    path: Path,
    *,
    create_parent: bool = True,
) -> Iterator[None]:
    """Serialize registry and per-Material mutations across processes."""

    with _file_lock(path, create_parent=create_parent):
        yield


@contextmanager
def _file_lock(
    path: Path,
    *,
    shared: bool = False,
    nonblocking: bool = False,
    create_parent: bool = True,
) -> Iterator[None]:
    """Acquire one cross-process shared or exclusive advisory file lock."""

    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir() or path.parent.is_symlink():
        raise FileNotFoundError(f"Lock directory is unavailable: {path.parent}")
    with path.open("a+b") as stream:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if nonblocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(stream.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class MaterialCatalog:
    """Persistent catalog backed by managed source-file copies and file locks."""

    def __init__(
        self,
        root: Path | str,
        *,
        reference_checker: MaterialReferenceChecker | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.manifest_path = self.root / _MANIFEST_NAME
        self._manifest_lock_path = self.root / _MANIFEST_LOCK_NAME
        self._reference_checker = reference_checker or _NoMaterialReferences()
        self._active_bindings: dict[int, MaterialBinding] = {}
        self._consumption_bindings: dict[int, MaterialBinding] = {}
        self._active_bindings_lock = RLock()
        root_created = not self.root.exists()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root_created:
            self.root.chmod(0o700)
        with _exclusive_file_lock(self._manifest_lock_path):
            if self.manifest_path.exists():
                self._load_manifest()
            else:
                self._write_manifest(self._empty_manifest())
            self._secure_library_directories()

    def add(
        self,
        source: Path | str,
        material_type: MaterialType | str,
        name: str | None = None,
    ) -> MaterialRecord:
        """Add ``source`` under one available type-scoped Material Name.

        An occupied name always raises :class:`MaterialNameCollisionError`.
        The caller must explicitly select the existing Material, choose another
        name, or cancel; this method never overwrites or renames a Material.
        """

        return self._ingest(source, material_type, name, reuse_exact=False)

    def ensure(
        self,
        source: Path | str,
        material_type: MaterialType | str,
        name: str | None = None,
    ) -> MaterialRecord:
        """Idempotently resolve or add the exact name/fingerprint binding.

        This operation supports repeatable CLI and automated analysis.  It
        reuses only the record with the same type, normalized name, and bound
        fingerprint.  Equal bytes under another available name remain a
        distinct Material, and an occupied name with different bytes is still
        a collision.
        """

        return self._ingest(source, material_type, name, reuse_exact=True)

    def _ingest(
        self,
        source: Path | str,
        material_type: MaterialType | str,
        name: str | None,
        *,
        reuse_exact: bool,
    ) -> MaterialRecord:
        kind = _parse_material_type(material_type)
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Material source is not a file: {source_path}")
        material_name = normalize_material_name(
            source_path.stem if name is None else name
        )
        fingerprint = fingerprint_file(source_path)
        with _exclusive_file_lock(self._manifest_lock_path):
            return self._add_locked(
                source_path,
                kind,
                material_name,
                fingerprint,
                reuse_exact=reuse_exact,
            )

    def _add_locked(
        self,
        source_path: Path,
        kind: MaterialType,
        material_name: str,
        fingerprint: str,
        *,
        reuse_exact: bool,
    ) -> MaterialRecord:
        manifest = self._load_manifest()
        records = self._records(manifest)

        # A managed source fed back through the CLI must never be treated as a
        # new upload after tampering.  Its recorded identity wins and the
        # inconsistency is surfaced immediately.
        for record in records:
            stored = self._stored_from_record(record)
            if (
                stored.source_path == source_path
                and str(stored.material.fingerprint) != fingerprint
            ):
                raise MaterialInconsistentError(
                    f"Material {stored.material.material_type.value}/"
                    f"{stored.material.name!r} "
                    "is inconsistent"
                )

        for record in records:
            if (
                record["type"] == kind.value
                and record["name"] == material_name
            ):
                stored = self._stored_from_record(record)
                if not self._verify_stored(stored):
                    raise MaterialInconsistentError(
                        f"Material {kind.value}/{stored.material.name!r} "
                        "is inconsistent"
                    )
                if reuse_exact and str(stored.material.fingerprint) == fingerprint:
                    return MaterialRecord(stored.material, reused=True)
                raise MaterialNameCollisionError(
                    f"A {kind.value} Material named {material_name!r} already exists"
                )

        type_directory = self._secure_type_directory(kind)
        material_id = self._allocate_material_id(records)
        material_directory = type_directory / material_id
        suffix = source_path.suffix
        managed_source = material_directory / f"source{suffix}"
        analysis_directory = material_directory / "analysis"

        material_directory_created = False
        try:
            material_directory.mkdir(mode=0o700, exist_ok=False)
            material_directory_created = True
            self._require_contained_directory(material_directory)
            self._copy_exclusive(source_path, managed_source)
            if fingerprint_file(managed_source) != fingerprint:
                raise MaterialCatalogError(
                    f"Source changed while Material {material_name!r} was being added"
                )
            managed_source.chmod(stat.S_IRUSR)
            analysis_directory.mkdir(mode=0o700)
            record = {
                "material_id": material_id,
                "type": kind.value,
                "name": material_name,
                "fingerprint": fingerprint,
                "source": self._relative_to_root(managed_source),
                "analysis": self._relative_to_root(analysis_directory),
            }
            records.append(record)
            self._write_manifest(manifest)
        except Exception:
            if material_directory_created:
                self._remove_created_directory(material_directory)
            raise

        return MaterialRecord(self._stored_from_record(record).material)

    @contextmanager
    def lease(self, material_id: MaterialId) -> Iterator[MaterialBinding]:
        """Yield verified paths under exclusive analysis/mutation access.

        The manifest is checked before and after the per-Material lock is
        acquired. Deletion uses the same per-Material lock, so the binding stays
        valid until this context exits while unrelated Materials remain
        concurrent.
        """

        material_id = self._require_material_id(material_id)
        stack = ExitStack()
        try:
            with _exclusive_file_lock(self._manifest_lock_path):
                stored = self._require_stored_unlocked(material_id)
            stack.enter_context(self._material_lock(stored))
            with _exclusive_file_lock(self._manifest_lock_path):
                current = self._require_stored_unlocked(material_id)
            if not self._verify_stored(current):
                raise MaterialInconsistentError(
                    f"Material {material_id} is inconsistent"
                )
            binding = MaterialBinding(
                material=current.material,
                source_path=current.source_path,
                memory_root=current.memory_root,
                manifest_path=self.manifest_path,
            )
            with self._active_bindings_lock:
                self._active_bindings[id(binding)] = binding
            try:
                yield binding
            finally:
                with self._active_bindings_lock:
                    registered = self._active_bindings.get(id(binding))
                    if registered is binding:
                        del self._active_bindings[id(binding)]
        finally:
            stack.close()

    @contextmanager
    def consume_lease(self, material_id: MaterialId) -> Iterator[MaterialBinding]:
        """Yield verified paths under a shared workflow-consumption lease."""

        material_id = self._require_material_id(material_id)
        stack = ExitStack()
        try:
            with _exclusive_file_lock(self._manifest_lock_path):
                stored = self._require_stored_unlocked(material_id)
            stack.enter_context(self._material_lock(stored, shared=True))
            with _exclusive_file_lock(self._manifest_lock_path):
                current = self._require_stored_unlocked(material_id)
            if not self._verify_stored(current):
                raise MaterialInconsistentError(
                    f"Material {material_id} is inconsistent"
                )
            binding = MaterialBinding(
                material=current.material,
                source_path=current.source_path,
                memory_root=current.memory_root,
                manifest_path=self.manifest_path,
            )
            with self._active_bindings_lock:
                self._consumption_bindings[id(binding)] = binding
            try:
                yield binding
            finally:
                with self._active_bindings_lock:
                    registered = self._consumption_bindings.get(id(binding))
                    if registered is binding:
                        del self._consumption_bindings[id(binding)]
        finally:
            stack.close()

    @contextmanager
    def read_lease(self, material_id: MaterialId) -> Iterator[MaterialBinding]:
        """Yield lock-free inspection paths without a full content digest.

        This boundary is for Material cards, Memory inspection, and byte-range
        playback.  It validates the managed path shape and regular-file state,
        but deliberately does not register a publication-capable binding or
        recompute SHA-256.  Workflow consumption continues to use ``lease``.
        """

        material_id = self._require_material_id(material_id)
        with _exclusive_file_lock(self._manifest_lock_path):
            current = self._require_stored_unlocked(material_id)
        if not self._source_is_regular(current):
            raise MaterialInconsistentError(
                f"Material {material_id} source is unavailable"
            )
        yield MaterialBinding(
            material=current.material,
            source_path=current.source_path,
            memory_root=current.memory_root,
            manifest_path=self.manifest_path,
        )

    def publish_analysis_result(
        self,
        binding: MaterialBinding,
        staged_result_path: Path | str,
    ) -> MaterialRecord:
        """Validate and atomically publish a result through a live lease.

        Publication reads the staged bytes once before validation.  The same
        operation therefore remains safe when the canonical result itself is
        supplied as ``staged_result_path``.
        """

        if not isinstance(binding, MaterialBinding):
            raise TypeError("binding must be a MaterialBinding")
        with self._active_bindings_lock:
            if self._active_bindings.get(id(binding)) is not binding:
                raise MaterialLeaseRequiredError(
                    "Analysis results can only be published through this "
                    "Catalog's active Material lease"
                )
            with _exclusive_file_lock(self._manifest_lock_path):
                current = self._require_stored_unlocked(
                    binding.material.material_id
                )
            self._require_matching_binding(binding, current)
            payload = self._read_staged_result(staged_result_path)
            if not self._valid_analysis_result_payload(payload, current.material):
                raise InvalidAnalysisResultError(
                    "Staged analysis result does not match the leased Material"
                )
            destination = self._analysis_result_path(current)
            self._atomic_write_result(destination, payload)
            if not self._valid_analysis_result(current):
                raise MaterialCatalogError(
                    "Published analysis result could not be validated"
                )
            with _exclusive_file_lock(self._manifest_lock_path):
                published = self._require_stored_unlocked(
                    binding.material.material_id
                )
            if published.material.condition is not MaterialCondition.READY:
                raise MaterialCatalogError(
                    "Published analysis result did not make the Material ready"
                )
            return MaterialRecord(published.material)

    def ensure_subtitle(
        self,
        binding: MaterialBinding,
        source: Path | str,
    ) -> Path:
        """Bind an immutable, private subtitle sidecar to a video Material.

        The caller must hold this Catalog's active Material lease.  The
        sidecar is copied into Material-owned storage and its SHA-256 digest is
        recorded in private metadata.  Identical bytes are idempotent; neither
        replacement nor first-time attachment after completed analysis is
        allowed.
        """

        if not isinstance(binding, MaterialBinding):
            raise TypeError("binding must be a MaterialBinding")
        if not isinstance(source, (Path, str)):
            raise TypeError("source must be a path or string")
        source_path = Path(source).expanduser()
        with self._active_bindings_lock:
            if self._active_bindings.get(id(binding)) is not binding:
                raise MaterialLeaseRequiredError(
                    "Subtitle sidecars can only be managed through this "
                    "Catalog's active Material lease"
                )
            with _exclusive_file_lock(self._manifest_lock_path):
                current = self._require_stored_unlocked(
                    binding.material.material_id
                )
            self._require_matching_binding(binding, current)
            self._require_video_subtitle_owner(current)

            existing = self._resolved_subtitle(current)
            if existing is not None:
                proposed_fingerprint = self._fingerprint_subtitle_source(
                    source_path
                )
                existing_fingerprint = self._read_subtitle_fingerprint(current)
                if proposed_fingerprint != existing_fingerprint:
                    raise SubtitleSidecarConflictError(
                        "Video Material already owns a different subtitle "
                        "sidecar; subtitle replacement is not supported"
                    )
                return existing

            if current.material.condition is MaterialCondition.READY:
                raise SubtitleSidecarConflictError(
                    "Cannot attach the first subtitle sidecar after video "
                    "analysis is complete; create a new video Material"
                )

            sidecar_directory, created = self._secure_sidecar_directory(
                current,
                create=True,
            )
            destination = sidecar_directory / _SUBTITLE_FILE_NAME
            metadata_path = sidecar_directory / _SUBTITLE_METADATA_NAME
            if (
                os.path.lexists(destination)
                or os.path.lexists(metadata_path)
            ):
                raise MaterialInconsistentError(
                    "Video Material contains an unrecorded subtitle sidecar"
                )
            try:
                fingerprint = self._atomic_copy_subtitle(
                    source_path,
                    destination,
                )
                metadata = {
                    "schema_version": _SUBTITLE_METADATA_SCHEMA_VERSION,
                    "file": _SUBTITLE_FILE_NAME,
                    "sha256": fingerprint,
                }
                self._atomic_write_result(
                    metadata_path,
                    (
                        json.dumps(metadata, ensure_ascii=False, indent=2)
                        + "\n"
                    ).encode("utf-8"),
                )
                metadata_path.chmod(stat.S_IRUSR)
                resolved = self._resolved_subtitle(current)
                if resolved is None:
                    raise MaterialCatalogError(
                        "Published subtitle sidecar could not be validated"
                    )
                return resolved
            except Exception:
                destination.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                if created:
                    try:
                        sidecar_directory.rmdir()
                    except OSError:
                        pass
                raise

    def resolve_subtitle(self, binding: MaterialBinding) -> Path | None:
        """Resolve and verify private subtitle bytes through a video lease."""

        if not isinstance(binding, MaterialBinding):
            raise TypeError("binding must be a MaterialBinding")
        with self._active_bindings_lock:
            is_mutation = self._active_bindings.get(id(binding)) is binding
            is_consumption = (
                self._consumption_bindings.get(id(binding)) is binding
            )
            if not (is_mutation or is_consumption):
                raise MaterialLeaseRequiredError(
                    "Subtitle sidecars can only be resolved through this "
                    "Catalog's active Material lease"
                )
            with _exclusive_file_lock(self._manifest_lock_path):
                current = self._require_stored_unlocked(
                    binding.material.material_id
                )
            self._require_matching_binding(binding, current)
            self._require_video_subtitle_owner(current)
            return self._resolved_subtitle(current)

    def validate_analysis_result(self, material_id: MaterialId) -> bool:
        """Validate one canonical v2 result while holding its Material lease."""

        material_id = self._require_material_id(material_id)
        with self.lease(material_id) as binding:
            with _exclusive_file_lock(self._manifest_lock_path):
                current = self._require_stored_unlocked(binding.material.material_id)
            self._require_matching_binding(binding, current)
            return self._valid_analysis_result(current)

    def get(self, material_id: MaterialId) -> MaterialRecord | None:
        material_id = self._require_material_id(material_id)
        with _exclusive_file_lock(self._manifest_lock_path):
            stored = self._get_stored_unlocked(material_id)
            return MaterialRecord(stored.material) if stored is not None else None

    def find_by_name(
        self,
        material_type: MaterialType | str,
        name: str,
    ) -> MaterialRecord | None:
        kind = _parse_material_type(material_type)
        normalized_name = normalize_material_name(name)
        with _exclusive_file_lock(self._manifest_lock_path):
            for record in self._records(self._load_manifest()):
                if (
                    record["type"] == kind.value
                    and record["name"] == normalized_name
                ):
                    return MaterialRecord(self._stored_from_record(record).material)
        return None

    def list(
        self,
        material_type: MaterialType | str | None = None,
    ) -> list[MaterialRecord]:
        """List Materials, optionally restricting the result to one type."""

        kind = _parse_material_type(material_type) if material_type is not None else None
        with _exclusive_file_lock(self._manifest_lock_path):
            return [
                MaterialRecord(self._stored_from_record(record).material)
                for record in self._records(self._load_manifest())
                if kind is None or record["type"] == kind.value
            ]

    def verify(self, material_id: MaterialId) -> bool:
        """Check that a Material's managed source is present and unchanged."""

        material_id = self._require_material_id(material_id)
        with _exclusive_file_lock(self._manifest_lock_path):
            return self._verify_stored(self._require_stored_unlocked(material_id))

    @staticmethod
    def _verify_stored(stored: _StoredMaterial) -> bool:
        if not MaterialCatalog._source_is_regular(stored):
            return False
        try:
            return fingerprint_file(stored.source_path) == str(
                stored.material.fingerprint
            )
        except OSError:
            return False

    @staticmethod
    def _source_is_regular(stored: _StoredMaterial) -> bool:
        return stored.source_path.is_file() and not stored.source_path.is_symlink()

    def require_consistent(self, material_id: MaterialId) -> MaterialRecord:
        """Resolve a Material, raising when its managed bytes are inconsistent."""

        material_id = self._require_material_id(material_id)
        with _exclusive_file_lock(self._manifest_lock_path):
            stored = self._require_stored_unlocked(material_id)
            if not self._verify_stored(stored):
                raise MaterialInconsistentError(
                    f"Material {material_id} is inconsistent"
                )
            return MaterialRecord(stored.material)

    def _require_matching_binding(
        self,
        binding: MaterialBinding,
        stored: _StoredMaterial,
    ) -> None:
        material = stored.material
        bound = binding.material
        if (
            bound.material_id != material.material_id
            or bound.material_type is not material.material_type
            or bound.name != material.name
            or bound.fingerprint != material.fingerprint
            or binding.source_path != stored.source_path
            or binding.memory_root != stored.memory_root
            or binding.manifest_path != self.manifest_path
        ):
            raise MaterialLeaseRequiredError(
                "Material binding does not match its active Catalog lease"
            )
        if not self._verify_stored(stored):
            raise MaterialInconsistentError(
                f"Material {material.material_id} is inconsistent"
            )

    @staticmethod
    def _analysis_result_name(material_type: MaterialType) -> str:
        return (
            "analysis_result.json"
            if material_type is MaterialType.VIDEO
            else "music_analysis_result.json"
        )

    def _analysis_result_path(self, stored: _StoredMaterial) -> Path:
        return stored.memory_root / self._analysis_result_name(
            stored.material.material_type
        )

    @staticmethod
    def _require_video_subtitle_owner(stored: _StoredMaterial) -> None:
        if stored.material.material_type is not MaterialType.VIDEO:
            raise ValueError(
                "Subtitle sidecars are only valid for video Materials"
            )

    def _secure_sidecar_directory(
        self,
        stored: _StoredMaterial,
        *,
        create: bool,
    ) -> tuple[Path, bool]:
        material_directory = stored.source_path.parent
        self._require_contained_directory(material_directory)
        directory = material_directory / _SIDECAR_DIRECTORY_NAME
        if directory.is_symlink():
            raise MaterialInconsistentError(
                "Video Material subtitle directory cannot be a symlink"
            )
        created = False
        if not directory.exists():
            if not create:
                return directory, False
            directory.mkdir(mode=0o700, exist_ok=False)
            created = True
        if not directory.is_dir():
            raise MaterialInconsistentError(
                "Video Material subtitle path is not a directory"
            )
        self._require_contained_directory(directory)
        directory.chmod(0o700)
        return directory, created

    def _resolved_subtitle(self, stored: _StoredMaterial) -> Path | None:
        directory, _ = self._secure_sidecar_directory(stored, create=False)
        if not directory.exists():
            return None
        subtitle_path = directory / _SUBTITLE_FILE_NAME
        metadata_path = directory / _SUBTITLE_METADATA_NAME
        subtitle_present = os.path.lexists(subtitle_path)
        metadata_present = os.path.lexists(metadata_path)
        if not subtitle_present and not metadata_present:
            return None
        if not subtitle_present or not metadata_present:
            raise MaterialInconsistentError(
                "Video Material subtitle sidecar is incomplete"
            )
        fingerprint = self._read_subtitle_fingerprint(stored)
        if subtitle_path.is_symlink() or not subtitle_path.is_file():
            raise MaterialInconsistentError(
                "Video Material subtitle sidecar is not a regular file"
            )
        try:
            actual_fingerprint = fingerprint_file(subtitle_path)
        except OSError as exc:
            raise MaterialInconsistentError(
                "Video Material subtitle sidecar cannot be read"
            ) from exc
        if actual_fingerprint != fingerprint:
            raise MaterialInconsistentError(
                "Video Material subtitle sidecar does not match its fingerprint"
            )
        return subtitle_path.resolve(strict=True)

    def _read_subtitle_fingerprint(self, stored: _StoredMaterial) -> str:
        directory, _ = self._secure_sidecar_directory(stored, create=False)
        metadata_path = directory / _SUBTITLE_METADATA_NAME
        payload = self._read_regular_result(metadata_path)
        if payload is None:
            raise MaterialInconsistentError(
                "Video Material subtitle metadata is not a regular file"
            )
        try:
            metadata = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MaterialInconsistentError(
                "Video Material subtitle metadata is invalid"
            ) from exc
        expected_fields = {"schema_version", "file", "sha256"}
        if (
            not isinstance(metadata, dict)
            or set(metadata) != expected_fields
            or metadata.get("schema_version")
            != _SUBTITLE_METADATA_SCHEMA_VERSION
            or metadata.get("file") != _SUBTITLE_FILE_NAME
            or not isinstance(metadata.get("sha256"), str)
            or not _FINGERPRINT.fullmatch(metadata["sha256"])
        ):
            raise MaterialInconsistentError(
                "Video Material subtitle metadata is invalid"
            )
        return metadata["sha256"]

    @staticmethod
    def _fingerprint_subtitle_source(path: Path) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except (OSError, ValueError) as exc:
            raise InvalidSubtitleSidecarError(
                f"Subtitle must be a regular non-symlink file: {path}"
            ) from exc
        digest = hashlib.sha256()
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise InvalidSubtitleSidecarError(
                    f"Subtitle must be a regular non-symlink file: {path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise InvalidSubtitleSidecarError(
                f"Subtitle could not be read safely: {path}"
            ) from exc
        finally:
            os.close(descriptor)
        return digest.hexdigest()

    def _atomic_copy_subtitle(self, source: Path, destination: Path) -> str:
        self._require_contained_directory(destination.parent)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            source_descriptor = os.open(source, flags)
        except (OSError, ValueError) as exc:
            raise InvalidSubtitleSidecarError(
                f"Subtitle must be a regular non-symlink file: {source}"
            ) from exc
        temporary_descriptor = -1
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        try:
            source_metadata = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_metadata.st_mode):
                raise InvalidSubtitleSidecarError(
                    f"Subtitle must be a regular non-symlink file: {source}"
                )
            temporary_descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with (
                os.fdopen(source_descriptor, "rb", closefd=False) as source_stream,
                os.fdopen(
                    temporary_descriptor,
                    "wb",
                    closefd=False,
                ) as target_stream,
            ):
                for chunk in iter(
                    lambda: source_stream.read(1024 * 1024),
                    b"",
                ):
                    digest.update(chunk)
                    target_stream.write(chunk)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            temporary_path.chmod(stat.S_IRUSR)
            if os.path.lexists(destination):
                raise MaterialInconsistentError(
                    "Video Material already contains an unrecorded subtitle"
                )
            temporary_path.replace(destination)
            temporary_path = None
            if fingerprint_file(destination) != digest.hexdigest():
                raise MaterialCatalogError(
                    "Subtitle sidecar changed during atomic publication"
                )
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(destination.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return digest.hexdigest()
        except OSError as exc:
            raise InvalidSubtitleSidecarError(
                f"Subtitle could not be copied safely: {source}"
            ) from exc
        finally:
            os.close(source_descriptor)
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _read_regular_result(path: Path) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except (OSError, ValueError):
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read()
        except OSError:
            return None
        finally:
            os.close(descriptor)

    def _read_staged_result(self, value: Path | str) -> bytes:
        if not isinstance(value, (Path, str)):
            raise TypeError("staged_result_path must be a path or string")
        path = Path(value).expanduser()
        payload = self._read_regular_result(path)
        if payload is None:
            raise InvalidAnalysisResultError(
                f"Staged analysis result must be a regular file: {path}"
            )
        return payload

    @staticmethod
    def _valid_analysis_result_payload(
        payload: bytes,
        material: Material,
    ) -> bool:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict):
            return False
        expected = {
            "schema_version": ANALYSIS_RESULT_SCHEMA_VERSION,
            "status": "success",
            "material_id": str(material.material_id),
            "material_type": material.material_type.value,
            "material_name": material.name,
            "material_fingerprint": str(material.fingerprint),
        }
        expected_fields = (
            _VIDEO_ANALYSIS_RESULT_FIELDS
            if material.material_type is MaterialType.VIDEO
            else _MUSIC_ANALYSIS_RESULT_FIELDS
        )
        return set(value) == expected_fields and all(
            value.get(name) == expected_value
            for name, expected_value in expected.items()
        )

    def _valid_analysis_result(self, stored: _StoredMaterial) -> bool:
        payload = self._read_regular_result(self._analysis_result_path(stored))
        return payload is not None and self._valid_analysis_result_payload(
            payload,
            stored.material,
        )

    def _atomic_write_result(self, destination: Path, payload: bytes) -> None:
        self._require_contained_directory(destination.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(destination)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(destination.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def delete(self, material_id: MaterialId) -> None:
        """Delete one Material after the injected reference check succeeds."""

        material_id = self._require_material_id(material_id)
        with _exclusive_file_lock(self._manifest_lock_path):
            stored = self._require_stored_unlocked(material_id)
        references = tuple(self._reference_checker.references(material_id))
        if references:
            raise MaterialReferencedError(material_id, references)
        try:
            lease = self._material_lock(
                stored,
                missing_ok=True,
                nonblocking=True,
            )
            lease.__enter__()
        except BlockingIOError as exc:
            raise MaterialConsumedError(material_id) from exc
        try:
            # Recheck after exclusion so a Project reference committed between
            # the optimistic check and the lock can never be deleted.
            references = tuple(self._reference_checker.references(material_id))
            if references:
                raise MaterialReferencedError(material_id, references)
            with _exclusive_file_lock(self._manifest_lock_path):
                current = self._require_stored_unlocked(material_id)
                manifest = self._load_manifest()
                records = self._records(manifest)
                manifest["materials"] = [
                    record
                    for record in records
                    if record["material_id"] != str(material_id)
                ]
                material_directory = current.source_path.parent
                deleting_directory = material_directory.with_name(
                    f".{material_directory.name}.deleting"
                )
                if deleting_directory.exists():
                    raise MaterialCatalogError(
                        "Cannot delete while staging path exists: "
                        f"{deleting_directory}"
                    )
                if material_directory.exists():
                    material_directory.replace(deleting_directory)
                try:
                    self._write_manifest(manifest)
                except Exception:
                    if deleting_directory.exists():
                        deleting_directory.replace(material_directory)
                    raise
                if deleting_directory.exists():
                    shutil.rmtree(deleting_directory)
        finally:
            lease.__exit__(None, None, None)

    @staticmethod
    def _empty_manifest() -> dict[str, Any]:
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "materials": []}

    def _allocate_material_id(self, records: list[dict[str, str]]) -> str:
        occupied_ids = {record["material_id"] for record in records}
        type_directories = [
            self._secure_type_directory(kind) for kind in MaterialType
        ]
        for _ in range(_MATERIAL_ID_ALLOCATION_ATTEMPTS):
            material_id = f"mat_{uuid4()}"
            if material_id in occupied_ids:
                continue
            if any(
                (type_directory / material_id).exists()
                for type_directory in type_directories
            ):
                continue
            return material_id
        raise MaterialCatalogError("Unable to allocate a unique Material ID")

    def _secure_library_directories(self) -> None:
        if self.root.is_symlink() or not self.root.is_dir():
            raise MaterialCatalogError(
                f"Material Catalog root is not a directory: {self.root}"
            )
        self.root.chmod(0o700)
        for kind in MaterialType:
            self._secure_type_directory(kind)

    def _secure_type_directory(self, kind: MaterialType) -> Path:
        directory = self.root / kind.value
        if directory.is_symlink():
            raise MaterialCatalogError(
                f"Material type directory cannot be a symlink: {directory}"
            )
        directory.mkdir(mode=0o700, exist_ok=True)
        self._require_contained_directory(directory)
        directory.chmod(0o700)
        return directory

    def _require_contained_directory(self, directory: Path) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise MaterialCatalogError(
                f"Managed path is not a regular directory: {directory}"
            )
        try:
            directory.resolve().relative_to(self.root)
        except ValueError as exc:
            raise MaterialCatalogError(
                f"Managed directory escapes the Material Catalog: {directory}"
            ) from exc

    @contextmanager
    def _material_lock(
        self,
        stored: _StoredMaterial,
        *,
        missing_ok: bool = False,
        shared: bool = False,
        nonblocking: bool = False,
    ) -> Iterator[None]:
        material = stored.material
        expected_directory = (
            self.root / material.material_type.value / str(material.material_id)
        )
        material_directory = stored.source_path.parent
        if material_directory != expected_directory:
            raise MaterialCatalogError(
                f"Material {material.material_id!r} has an invalid managed path"
            )
        if not material_directory.exists():
            if missing_ok:
                yield
                return
            raise MaterialInconsistentError(
                f"Material {material.material_type.value}/{material.name!r} "
                "directory is missing"
            )
        self._require_contained_directory(material_directory)
        with _file_lock(
            material_directory / ".analysis.lock",
            shared=shared,
            nonblocking=nonblocking,
            create_parent=False,
        ):
            yield

    def _remove_created_directory(self, directory: Path) -> None:
        """Remove a failed new directory only when it is still safely contained."""

        try:
            self._require_contained_directory(directory)
        except MaterialCatalogError:
            return
        shutil.rmtree(directory)

    @staticmethod
    def _copy_exclusive(source: Path, destination: Path) -> None:
        with source.open("rb") as source_stream, destination.open("xb") as target:
            shutil.copyfileobj(source_stream, target, length=1024 * 1024)

    def _relative_to_root(self, path: Path) -> str:
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise MaterialCatalogError(
                f"Managed path escapes the Material Catalog: {path}"
            ) from exc
        return path.relative_to(self.root).as_posix()

    def _managed_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise InvalidManifestError("Managed paths must be non-empty strings")
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise InvalidManifestError(
                f"Managed path escapes the Material Catalog: {relative!r}"
            ) from exc
        return path

    def _stored_from_record(self, record: dict[str, str]) -> _StoredMaterial:
        material_type = MaterialType(record["type"])
        memory_root = self._managed_path(record["analysis"])
        material = Material(
            material_id=MaterialId.parse(record["material_id"]),
            material_type=material_type,
            name=record["name"],
            fingerprint=MaterialFingerprint(record["fingerprint"]),
            condition=MaterialCondition.QUEUED,
        )
        stored = _StoredMaterial(
            material=material,
            source_path=self._managed_path(record["source"]),
            memory_root=memory_root,
        )
        if self._valid_analysis_result(stored):
            return replace(
                stored,
                material=replace(material, condition=MaterialCondition.READY),
            )
        return stored

    def _get_stored_unlocked(
        self,
        material_id: MaterialId,
    ) -> _StoredMaterial | None:
        for record in self._records(self._load_manifest()):
            if record["material_id"] == str(material_id):
                return self._stored_from_record(record)
        return None

    def _require_stored_unlocked(self, material_id: MaterialId) -> _StoredMaterial:
        stored = self._get_stored_unlocked(material_id)
        if stored is None:
            raise MaterialNotFoundError(f"No Material with ID {material_id}")
        return stored

    @staticmethod
    def _require_material_id(material_id: MaterialId) -> MaterialId:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        return material_id

    def _load_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidManifestError(
                f"Cannot read Material Catalog manifest: {self.manifest_path}"
            ) from exc
        if not isinstance(manifest, dict):
            raise InvalidManifestError("Material Catalog manifest must be an object")
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise InvalidManifestError(
                "Unsupported Material Catalog manifest schema version: "
                f"{manifest.get('schema_version')!r}"
            )
        records = manifest.get("materials")
        if not isinstance(records, list):
            raise InvalidManifestError("Manifest materials must be an array")
        seen_names: set[tuple[str, str]] = set()
        seen_ids: set[str] = set()
        for record in records:
            self._validate_record(record)
            identity = (record["type"], record["name"])
            if identity in seen_names:
                raise InvalidManifestError(
                    f"Duplicate Material identity in manifest: {identity!r}"
                )
            material_id = record["material_id"]
            if material_id in seen_ids:
                raise InvalidManifestError(
                    f"Duplicate Material ID in manifest: {material_id!r}"
                )
            seen_names.add(identity)
            seen_ids.add(material_id)
        return manifest

    def _validate_record(self, value: object) -> None:
        if not isinstance(value, dict):
            raise InvalidManifestError("Each manifest Material must be an object")
        expected = {
            "material_id",
            "type",
            "name",
            "fingerprint",
            "source",
            "analysis",
        }
        if set(value) != expected:
            raise InvalidManifestError(
                f"Manifest Material fields must be exactly {sorted(expected)!r}"
            )
        try:
            MaterialType(value["type"])
            name = normalize_material_name(value["name"])
        except (TypeError, ValueError) as exc:
            raise InvalidManifestError("Manifest contains an invalid Material") from exc
        if name != value["name"]:
            raise InvalidManifestError("Manifest Material names are not normalized")
        material_id = value["material_id"]
        if not self._valid_material_id(material_id):
            raise InvalidManifestError("Manifest contains an invalid Material ID")
        fingerprint = value["fingerprint"]
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise InvalidManifestError(
                "Manifest contains an invalid SHA-256 fingerprint"
            )
        source_path = self._managed_path(value["source"])
        analysis_path = self._managed_path(value["analysis"])
        expected_directory = self.root / value["type"] / material_id
        if source_path.parent != expected_directory or not (
            source_path.name == "source" or source_path.name.startswith("source.")
        ):
            raise InvalidManifestError(
                "Manifest source path does not match its Material identity"
            )
        if analysis_path != source_path.parent / "analysis":
            raise InvalidManifestError(
                "Manifest analysis path does not match its Material identity"
            )

    @staticmethod
    def _valid_material_id(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            MaterialId.parse(value)
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _records(manifest: dict[str, Any]) -> list[dict[str, str]]:
        return manifest["materials"]

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{_MANIFEST_NAME}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self.manifest_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise


__all__ = [
    "InvalidAnalysisResultError",
    "InvalidManifestError",
    "InvalidMaterialNameError",
    "InvalidSubtitleSidecarError",
    "MANIFEST_SCHEMA_VERSION",
    "MaterialCatalog",
    "MaterialCatalogError",
    "MaterialInconsistentError",
    "MaterialLeaseRequiredError",
    "MaterialNameCollisionError",
    "MaterialNotFoundError",
    "MaterialReferencedError",
    "SubtitleSidecarConflictError",
    "fingerprint_file",
    "normalize_material_name",
]
