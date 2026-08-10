"""A small, persistent library for immutable source media.

Material names are the public identities in this module. A SHA-256 fingerprint
checks integrity and recognizes repeated ingestion only within one candidate
name family: equal bytes submitted under different candidate names create
different Materials.
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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator


MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_MANIFEST_LOCK_NAME = ".manifest.lock"
_WHITESPACE = re.compile(r"\s+")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class MaterialLibraryError(RuntimeError):
    """Base error raised by the Material Library."""


class InvalidMaterialNameError(MaterialLibraryError, ValueError):
    """Raised when a candidate Material Name cannot be normalized."""


class InvalidManifestError(MaterialLibraryError):
    """Raised when the on-disk manifest is malformed or unsafe."""


class MaterialNotFoundError(MaterialLibraryError):
    """Raised when a type/name pair does not identify a Material."""


class MaterialInconsistentError(MaterialLibraryError):
    """Raised when managed source bytes no longer match their fingerprint."""


class MaterialReferencedError(MaterialLibraryError):
    """Raised when deletion is attempted for a referenced Material."""


class MaterialType(StrEnum):
    """The source-media categories supported by the first Material Library."""

    VIDEO = "video"
    MUSIC = "music"

    @classmethod
    def parse(cls, value: MaterialType | str) -> MaterialType:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(
                f"Unsupported material type {value!r}; expected one of: {choices}"
            ) from exc


@dataclass(frozen=True)
class Material:
    """One immutable, managed source asset.

    ``candidate_name`` records the normalized name requested at ingestion.  It
    remains the family key after ``name`` receives an automatic numeric suffix.
    ``reused`` describes the result of the current ``add`` call and is therefore
    never persisted in the manifest.
    """

    material_type: MaterialType
    name: str
    candidate_name: str
    fingerprint: str
    source_path: Path
    analysis_dir: Path
    reused: bool = False


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
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize registry and per-Material mutations across processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class MaterialLibrary:
    """Persistent Material registry backed by managed source-file copies."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.manifest_path = self.root / _MANIFEST_NAME
        self._manifest_lock_path = self.root / _MANIFEST_LOCK_NAME
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self._manifest_lock_path):
            if self.manifest_path.exists():
                self._load_manifest()
            else:
                self._write_manifest(self._empty_manifest())

    def add(
        self,
        source: Path | str,
        material_type: MaterialType | str,
        name: str | None = None,
    ) -> Material:
        """Copy ``source`` into the library or reuse its matching name-family member.

        A repeated fingerprint is reused only within the same normalized
        candidate-name family.  Otherwise a collision receives `` (2)``,
        `` (3)``, and so on.
        """

        kind = MaterialType.parse(material_type)
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Material source is not a file: {source_path}")
        candidate_name = normalize_material_name(
            source_path.stem if name is None else name
        )
        fingerprint = fingerprint_file(source_path)
        with _exclusive_file_lock(self._manifest_lock_path):
            return self._add_locked(
                source_path,
                kind,
                candidate_name,
                fingerprint,
            )

    def _add_locked(
        self,
        source_path: Path,
        kind: MaterialType,
        candidate_name: str,
        fingerprint: str,
    ) -> Material:
        manifest = self._load_manifest()
        records = self._records(manifest)

        # A managed source fed back through the CLI must never be treated as a
        # new upload after tampering.  Its recorded identity wins and the
        # inconsistency is surfaced immediately.
        for record in records:
            material = self._material_from_record(record)
            if (
                material.source_path == source_path
                and material.fingerprint != fingerprint
            ):
                raise MaterialInconsistentError(
                    f"Material {material.material_type.value}/{material.name!r} "
                    "is inconsistent"
                )

        # An exact public-name match is the least surprising result when a
        # caller feeds a previously allocated name (for example ``Film (2)``)
        # back into the CLI.  Candidate-family matching then handles repeated
        # uploads that both started as ``Film``.
        for record in records:
            if (
                record["type"] == kind.value
                and record["name"] == candidate_name
                and record["fingerprint"] == fingerprint
            ):
                material = self._material_from_record(record)
                if (
                    source_path != material.source_path
                    and not self._verify_material(material)
                ):
                    raise MaterialInconsistentError(
                        f"Material {kind.value}/{material.name!r} is inconsistent"
                    )
                return replace(material, reused=True)

        for record in records:
            if (
                record["type"] == kind.value
                and record["candidate_name"] == candidate_name
                and record["fingerprint"] == fingerprint
            ):
                material = self._material_from_record(record)
                if (
                    source_path != material.source_path
                    and not self._verify_material(material)
                ):
                    raise MaterialInconsistentError(
                        f"Material {kind.value}/{material.name!r} is inconsistent"
                    )
                return replace(material, reused=True)

        occupied_names = {
            record["name"] for record in records if record["type"] == kind.value
        }
        material_name = self._allocate_name(candidate_name, occupied_names)
        material_directory = self.root / kind.value / self._storage_key(material_name)
        suffix = source_path.suffix
        managed_source = material_directory / f"source{suffix}"
        analysis_directory = material_directory / "analysis"

        material_directory_created = False
        try:
            material_directory.mkdir(parents=True, exist_ok=False)
            material_directory_created = True
            self._copy_exclusive(source_path, managed_source)
            if fingerprint_file(managed_source) != fingerprint:
                raise MaterialLibraryError(
                    f"Source changed while Material {material_name!r} was being added"
                )
            managed_source.chmod(
                stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            )
            analysis_directory.mkdir()
            record = {
                "type": kind.value,
                "name": material_name,
                "candidate_name": candidate_name,
                "fingerprint": fingerprint,
                "source": self._relative_to_root(managed_source),
                "analysis": self._relative_to_root(analysis_directory),
            }
            records.append(record)
            self._write_manifest(manifest)
        except Exception:
            if material_directory_created and material_directory.exists():
                shutil.rmtree(material_directory)
            raise

        return self._material_from_record(record)

    @contextmanager
    def analysis_lock(self, material: Material) -> Iterator[None]:
        """Serialize cache-check, analysis, and result writes for a Material."""

        material_directory = material.source_path.parent.resolve()
        try:
            material_directory.relative_to(self.root)
        except ValueError as exc:
            raise MaterialLibraryError(
                "Cannot lock a Material outside this library"
            ) from exc
        with _exclusive_file_lock(material_directory / ".analysis.lock"):
            yield

    def resolve(
        self,
        material_type: MaterialType | str,
        name: str,
    ) -> Material:
        """Resolve one Material by its type and exact normalized public name."""

        kind = MaterialType.parse(material_type)
        normalized_name = normalize_material_name(name)
        with _exclusive_file_lock(self._manifest_lock_path):
            return self._resolve_unlocked(kind, normalized_name)

    def _resolve_unlocked(
        self,
        kind: MaterialType,
        normalized_name: str,
    ) -> Material:
        for record in self._records(self._load_manifest()):
            if record["type"] == kind.value and record["name"] == normalized_name:
                return self._material_from_record(record)
        raise MaterialNotFoundError(
            f"No {kind.value} Material named {normalized_name!r}"
        )

    def list(
        self,
        material_type: MaterialType | str | None = None,
    ) -> list[Material]:
        """List Materials, optionally restricting the result to one type."""

        kind = MaterialType.parse(material_type) if material_type is not None else None
        with _exclusive_file_lock(self._manifest_lock_path):
            return [
                self._material_from_record(record)
                for record in self._records(self._load_manifest())
                if kind is None or record["type"] == kind.value
            ]

    def verify(self, material_type: MaterialType | str, name: str) -> bool:
        """Check that a Material's managed source is present and unchanged."""

        kind = MaterialType.parse(material_type)
        normalized_name = normalize_material_name(name)
        with _exclusive_file_lock(self._manifest_lock_path):
            material = self._resolve_unlocked(kind, normalized_name)
            return self._verify_material(material)

    @staticmethod
    def _verify_material(material: Material) -> bool:
        if not material.source_path.is_file() or material.source_path.is_symlink():
            return False
        try:
            return fingerprint_file(material.source_path) == material.fingerprint
        except OSError:
            return False

    def require_consistent(
        self,
        material_type: MaterialType | str,
        name: str,
    ) -> Material:
        """Resolve a Material, raising when its managed bytes are inconsistent."""

        kind = MaterialType.parse(material_type)
        normalized_name = normalize_material_name(name)
        with _exclusive_file_lock(self._manifest_lock_path):
            material = self._resolve_unlocked(kind, normalized_name)
            if not self._verify_material(material):
                raise MaterialInconsistentError(
                    f"Material {material.material_type.value}/{material.name!r} "
                    "is inconsistent"
                )
            return material

    def delete(
        self,
        material_type: MaterialType | str,
        name: str,
        *,
        referenced: bool,
    ) -> None:
        """Delete an unreferenced Material and its managed data.

        Reference persistence belongs to the project layer.  Until it is wired
        in, callers pass their reference check through ``referenced``.
        """

        with _exclusive_file_lock(self._manifest_lock_path):
            kind = MaterialType.parse(material_type)
            normalized_name = normalize_material_name(name)
            material = self._resolve_unlocked(kind, normalized_name)
            if referenced:
                raise MaterialReferencedError(
                    f"Material {material.material_type.value}/{material.name!r} "
                    "is still referenced"
                )
            with self.analysis_lock(material):
                manifest = self._load_manifest()
                records = self._records(manifest)
                manifest["materials"] = [
                    record
                    for record in records
                    if not (
                        record["type"] == material.material_type.value
                        and record["name"] == material.name
                    )
                ]
                material_directory = material.source_path.parent
                deleting_directory = material_directory.with_name(
                    f".{material_directory.name}.deleting"
                )
                if deleting_directory.exists():
                    raise MaterialLibraryError(
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

    @staticmethod
    def _empty_manifest() -> dict[str, Any]:
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "materials": []}

    @staticmethod
    def _allocate_name(candidate_name: str, occupied_names: set[str]) -> str:
        if candidate_name not in occupied_names:
            return candidate_name
        suffix = 2
        while f"{candidate_name} ({suffix})" in occupied_names:
            suffix += 1
        return f"{candidate_name} ({suffix})"

    @staticmethod
    def _storage_key(name: str) -> str:
        # Hex is an injective filesystem-safe encoding of the normalized name,
        # not a content fingerprint or identity exposed to users.
        return f"name-{name.encode('utf-8').hex()}"

    @staticmethod
    def _copy_exclusive(source: Path, destination: Path) -> None:
        with source.open("rb") as source_stream, destination.open("xb") as target:
            shutil.copyfileobj(source_stream, target, length=1024 * 1024)

    def _relative_to_root(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _managed_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise InvalidManifestError("Managed paths must be non-empty strings")
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise InvalidManifestError(
                f"Managed path escapes the Material Library: {relative!r}"
            ) from exc
        return path

    def _material_from_record(self, record: dict[str, str]) -> Material:
        return Material(
            material_type=MaterialType(record["type"]),
            name=record["name"],
            candidate_name=record["candidate_name"],
            fingerprint=record["fingerprint"],
            source_path=self._managed_path(record["source"]),
            analysis_dir=self._managed_path(record["analysis"]),
        )

    def _load_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidManifestError(
                f"Cannot read Material Library manifest: {self.manifest_path}"
            ) from exc
        if not isinstance(manifest, dict):
            raise InvalidManifestError("Material Library manifest must be an object")
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise InvalidManifestError(
                "Unsupported Material Library manifest schema version: "
                f"{manifest.get('schema_version')!r}"
            )
        records = manifest.get("materials")
        if not isinstance(records, list):
            raise InvalidManifestError("Manifest materials must be an array")
        seen: set[tuple[str, str]] = set()
        for record in records:
            self._validate_record(record)
            identity = (record["type"], record["name"])
            if identity in seen:
                raise InvalidManifestError(
                    f"Duplicate Material identity in manifest: {identity!r}"
                )
            seen.add(identity)
        return manifest

    def _validate_record(self, value: object) -> None:
        if not isinstance(value, dict):
            raise InvalidManifestError("Each manifest Material must be an object")
        expected = {
            "type",
            "name",
            "candidate_name",
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
            candidate_name = normalize_material_name(value["candidate_name"])
        except (TypeError, ValueError) as exc:
            raise InvalidManifestError("Manifest contains an invalid Material") from exc
        if name != value["name"] or candidate_name != value["candidate_name"]:
            raise InvalidManifestError("Manifest Material names are not normalized")
        fingerprint = value["fingerprint"]
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise InvalidManifestError(
                "Manifest contains an invalid SHA-256 fingerprint"
            )
        source_path = self._managed_path(value["source"])
        analysis_path = self._managed_path(value["analysis"])
        expected_directory = (
            self.root / value["type"] / self._storage_key(value["name"])
        )
        if source_path.parent != expected_directory or not (
            source_path.name == "source" or source_path.name.startswith("source.")
        ):
            raise InvalidManifestError(
                "Manifest source path does not match its Material identity"
            )
        if analysis_path != expected_directory / "analysis":
            raise InvalidManifestError(
                "Manifest analysis path does not match its Material identity"
            )

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
    "InvalidManifestError",
    "InvalidMaterialNameError",
    "MANIFEST_SCHEMA_VERSION",
    "Material",
    "MaterialInconsistentError",
    "MaterialLibrary",
    "MaterialLibraryError",
    "MaterialNotFoundError",
    "MaterialReferencedError",
    "MaterialType",
    "fingerprint_file",
    "normalize_material_name",
]
