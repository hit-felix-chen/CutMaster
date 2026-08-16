"""Managed, Run-owned persistence for resumable ASTER stage checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from cutmaster.application.ports.material_catalog import MaterialBinding
from cutmaster.application.runs.views import RunView
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.domain.ids import AttemptId, RunId
from cutmaster.infrastructure.persistence.sqlite import (
    ManagedStateConflict,
    SQLiteApplicationStore,
)
from cutmaster.workflow.contracts.checkpoints import (
    PLANNERS_CHECKPOINT_SCHEMA_VERSION,
    PlannersCheckpoint,
    PlannersCheckpointStore,
)


RUN_CHECKPOINT_ENVELOPE_SCHEMA_VERSION = "1.0"
_MAX_CHECKPOINT_BYTES = 128 * 1024 * 1024
_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "checkpoint_id",
        "identity",
        "identity_signature",
        "checkpoint",
    }
)
_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "project_id",
        "configuration_sha256",
        "video_material_id",
        "video_fingerprint",
        "music_material_id",
        "music_fingerprint",
        "brief_sha256",
        "target_duration_sec",
        "options_sha256",
    }
)
_FORBIDDEN_STATE_KEYS = frozenset(
    {
        "api_key",
        "api_key_env",
        "audio_path",
        "clip_path",
        "instruction",
        "memory_root",
        "path",
        "prompt",
        "provider_payload",
        "response",
        "response_id",
        "responses",
        "source_path",
        "system_prompt",
        "user_prompt",
        "video_path",
    }
)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("ASTER checkpoint must be finite JSON data") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value).rstrip(b"\n")).hexdigest()


def _reject_private_payloads(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("ASTER checkpoint state keys must be strings")
            if key.lower() in _FORBIDDEN_STATE_KEYS:
                raise ValueError(
                    f"ASTER checkpoint state cannot persist private field {key!r}"
                )
            _reject_private_payloads(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_private_payloads(item)


@dataclass(frozen=True)
class RunCheckpointIdentity:
    """Secret- and prompt-free identity bound to one immutable Run snapshot."""

    run_id: str
    project_id: str
    configuration_sha256: str
    video_material_id: str
    video_fingerprint: str
    music_material_id: str
    music_fingerprint: str
    brief_sha256: str
    target_duration_sec: float
    options_sha256: str

    @classmethod
    def create(
        cls,
        run: RunView,
        video: MaterialBinding,
        music: MaterialBinding,
        *,
        target_shot_length_sec: float,
        prompt_type: str,
        max_clip_duration_sec: float | None,
    ) -> "RunCheckpointIdentity":
        if video.material.material_id != run.video_material_ids[0]:
            raise ValueError("Video binding does not belong to the Run snapshot")
        if music.material.material_id != run.music_material_ids[0]:
            raise ValueError("Music binding does not belong to the Run snapshot")
        configuration = dict(run.configuration)
        brief = {
            "editing_intent": run.creative_brief.editing_intent,
            "target_duration_sec": run.creative_brief.target_duration_sec,
        }
        options = {
            "target_shot_length_sec": target_shot_length_sec,
            "prompt_type": prompt_type,
            "video_title_sha256": _canonical_hash(video.material.name),
            "max_clip_duration_sec": max_clip_duration_sec,
        }
        return cls(
            run_id=str(run.run_id),
            project_id=str(run.project_id),
            configuration_sha256=_canonical_hash(configuration),
            video_material_id=str(video.material.material_id),
            video_fingerprint=str(video.material.fingerprint),
            music_material_id=str(music.material.material_id),
            music_fingerprint=str(music.material.fingerprint),
            brief_sha256=_canonical_hash(brief),
            target_duration_sec=float(run.creative_brief.target_duration_sec),
            options_sha256=_canonical_hash(options),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUN_CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "configuration_sha256": self.configuration_sha256,
            "video_material_id": self.video_material_id,
            "video_fingerprint": self.video_fingerprint,
            "music_material_id": self.music_material_id,
            "music_fingerprint": self.music_fingerprint,
            "brief_sha256": self.brief_sha256,
            "target_duration_sec": self.target_duration_sec,
            "options_sha256": self.options_sha256,
        }

    @property
    def signature(self) -> str:
        return _canonical_hash(self.to_dict())


class ManagedRunCheckpointSession(PlannersCheckpointStore):
    """One Attempt's exact checkpoint pin plus managed publication boundary."""

    __slots__ = (
        "_data_root",
        "_identity",
        "_source_attempt_id",
        "_store",
        "_pinned",
    )

    def __init__(
        self,
        store: SQLiteApplicationStore,
        data_root: Path,
        identity: RunCheckpointIdentity,
        source_attempt_id: AttemptId,
    ) -> None:
        self._store = store
        self._data_root = data_root.resolve()
        self._identity = identity
        self._source_attempt_id = source_attempt_id
        self._pinned = store.get_attempt_resume_checkpoint(source_attempt_id)

    @property
    def resumes_from_checkpoint(self) -> bool:
        return self._pinned is not None

    def load(self) -> PlannersCheckpoint | None:
        if self._pinned is None:
            return None
        return self._load_receipt(self._pinned)

    def save(self, checkpoint: PlannersCheckpoint) -> None:
        if not isinstance(checkpoint, PlannersCheckpoint):
            raise TypeError("checkpoint must be a PlannersCheckpoint")
        checkpoint_value = checkpoint.to_dict()
        _reject_private_payloads(checkpoint_value)
        checkpoint_id = str(uuid4())
        envelope = {
            "schema_version": RUN_CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "identity": self._identity.to_dict(),
            "identity_signature": self._identity.signature,
            "checkpoint": checkpoint_value,
        }
        content = _canonical_bytes(envelope)
        content_sha256 = _sha256(content)
        relative_path = self._checkpoint_relative_path(
            checkpoint.completed_stage.value,
            checkpoint_id,
        )
        target = self._resolve_owned_path(relative_path, create_parent=True)
        self._publish_file(target, content)
        try:
            result = self._store.publish_run_checkpoint(
                run_id=RunId.parse(self._identity.run_id),
                source_attempt_id=self._source_attempt_id,
                checkpoint_id=checkpoint_id,
                completed_stage=checkpoint.completed_stage.value,
                relative_path=relative_path,
                content_sha256=content_sha256,
                identity_signature=self._identity.signature,
                schema_version=RUN_CHECKPOINT_ENVELOPE_SCHEMA_VERSION,
            )
        except Exception:
            self._unlink_owned(target)
            raise
        previous = result.get("replaced_checkpoint")
        if isinstance(previous, Mapping):
            previous_path = previous.get("relative_path")
            if isinstance(previous_path, str) and previous_path != relative_path:
                self._unlink_owned(self._resolve_owned_path(previous_path))

    def clear(self) -> None:
        value = self._store.clear_run_checkpoint(
            RunId.parse(self._identity.run_id),
        )
        if value is None:
            return
        relative_path = value.get("relative_path")
        if isinstance(relative_path, str):
            self._unlink_owned(self._resolve_owned_path(relative_path))

    def _load_receipt(self, receipt: Mapping[str, Any]) -> PlannersCheckpoint:
        if receipt.get("run_id") != self._identity.run_id:
            raise ManagedStateConflict(
                "run_checkpoint_invalid",
                "The pinned ASTER checkpoint belongs to another Run",
            )
        if receipt.get("identity_signature") != self._identity.signature:
            raise ManagedStateConflict(
                "run_checkpoint_invalid",
                "The ASTER checkpoint identity no longer matches the Run snapshot",
            )
        relative_path = receipt.get("relative_path")
        if not isinstance(relative_path, str):
            raise self._invalid("The ASTER checkpoint path receipt is invalid")
        path = self._resolve_owned_path(relative_path)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise self._invalid("The pinned ASTER checkpoint file is missing") from exc
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise self._invalid("The pinned ASTER checkpoint is not a regular file")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_CHECKPOINT_BYTES:
            raise self._invalid("The pinned ASTER checkpoint size is invalid")
        content = path.read_bytes()
        if _sha256(content) != receipt.get("content_sha256"):
            raise self._invalid("The pinned ASTER checkpoint digest does not match")
        try:
            envelope = json.loads(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise self._invalid("The pinned ASTER checkpoint is not valid JSON") from exc
        if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_KEYS:
            raise self._invalid("The ASTER checkpoint envelope is invalid")
        if envelope.get("schema_version") != RUN_CHECKPOINT_ENVELOPE_SCHEMA_VERSION:
            raise self._invalid("The ASTER checkpoint envelope schema is unsupported")
        if envelope.get("checkpoint_id") != receipt.get("checkpoint_id"):
            raise self._invalid("The ASTER checkpoint ID does not match its receipt")
        identity = envelope.get("identity")
        if not isinstance(identity, dict) or set(identity) != _IDENTITY_KEYS:
            raise self._invalid("The ASTER checkpoint identity fields are invalid")
        if identity != self._identity.to_dict():
            raise self._invalid("The ASTER checkpoint identity was changed")
        if envelope.get("identity_signature") != self._identity.signature:
            raise self._invalid("The ASTER checkpoint identity signature is invalid")
        checkpoint_value = envelope.get("checkpoint")
        if not isinstance(checkpoint_value, dict):
            raise self._invalid("The ASTER checkpoint state is invalid")
        _reject_private_payloads(checkpoint_value)
        try:
            checkpoint = PlannersCheckpoint.from_dict(checkpoint_value)
        except (TypeError, ValueError) as exc:
            raise self._invalid("The ASTER checkpoint state is invalid") from exc
        if checkpoint.completed_stage.value != receipt.get("completed_stage"):
            raise self._invalid("The ASTER checkpoint stage does not match its receipt")
        return checkpoint

    def _checkpoint_relative_path(self, stage: str, checkpoint_id: str) -> str:
        return (
            f"projects/{self._identity.project_id}/runs/{self._identity.run_id}/"
            f"checkpoints/{stage}-{checkpoint_id}.json"
        )

    def _resolve_owned_path(
        self,
        relative_path: str,
        *,
        create_parent: bool = False,
    ) -> Path:
        normalized = validate_portable_relative_file_path(relative_path)
        expected_prefix = PurePosixPath(
            "projects",
            self._identity.project_id,
            "runs",
            self._identity.run_id,
            "checkpoints",
        )
        relative = PurePosixPath(normalized)
        if relative.parent != expected_prefix:
            raise self._invalid("ASTER checkpoint escapes its Run owner namespace")
        parent = self._data_root.joinpath(*expected_prefix.parts)
        self._ensure_safe_directory(parent, create=create_parent)
        target = parent / relative.name
        if target.parent.resolve() != parent.resolve():
            raise self._invalid("ASTER checkpoint path resolution is unsafe")
        return target

    def _ensure_safe_directory(self, directory: Path, *, create: bool) -> None:
        try:
            relative = directory.relative_to(self._data_root)
        except ValueError as exc:
            raise self._invalid("ASTER checkpoint owner is outside the Data Root") from exc
        current = self._data_root
        if current.is_symlink() or not current.is_dir():
            raise self._invalid("Application Data Root is not a safe directory")
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if not create:
                    raise self._invalid("ASTER checkpoint owner directory is missing")
                current.mkdir()
                metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise self._invalid("ASTER checkpoint owner directory is unsafe")

    @staticmethod
    def _publish_file(path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _unlink_owned(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        except OSError:
            # A stale immutable checkpoint is harmless after the SQLite
            # receipt has switched.  Exact owner cleanup can retry later.
            return

    @staticmethod
    def _invalid(detail: str) -> ManagedStateConflict:
        return ManagedStateConflict("run_checkpoint_invalid", detail)


class ManagedRunCheckpointRepository:
    """Application-facing checkpoint preflight and Attempt session factory."""

    __slots__ = ("_data_root", "_store")

    def __init__(self, store: SQLiteApplicationStore, data_root: Path) -> None:
        self._store = store
        self._data_root = data_root.resolve()

    def validate_current(self, identity: RunCheckpointIdentity) -> str:
        value = self._store.get_run_checkpoint(RunId.parse(identity.run_id))
        if value is None:
            raise ManagedStateConflict(
                "run_checkpoint_unavailable",
                "Resume requires a completed ASTER stage checkpoint; use Retry",
            )
        # Use a temporary session-like reader without changing the Attempt pin.
        reader = object.__new__(ManagedRunCheckpointSession)
        reader._store = self._store
        reader._data_root = self._data_root
        reader._identity = identity
        reader._source_attempt_id = AttemptId.parse(str(value["source_attempt_id"]))
        reader._pinned = value
        reader._load_receipt(value)
        return str(value["checkpoint_id"])

    def session(
        self,
        identity: RunCheckpointIdentity,
        attempt_id: AttemptId,
    ) -> ManagedRunCheckpointSession:
        return ManagedRunCheckpointSession(
            self._store,
            self._data_root,
            identity,
            attempt_id,
        )

    def discard_path(self, relative_path: str | None) -> None:
        if relative_path is None:
            return
        # Constructing an identity is unnecessary for exact post-commit Retry
        # cleanup: validate the portable path and require the checkpoints shape.
        normalized = validate_portable_relative_file_path(relative_path)
        parts = PurePosixPath(normalized).parts
        if (
            len(parts) != 6
            or parts[0] != "projects"
            or parts[2] != "runs"
            or parts[4] != "checkpoints"
        ):
            return
        target = self._data_root.joinpath(*parts)
        try:
            resolved_parent = target.parent.resolve(strict=True)
            resolved_parent.relative_to(self._data_root)
        except (FileNotFoundError, ValueError):
            return
        if target.parent.is_symlink() or resolved_parent != target.parent:
            return
        ManagedRunCheckpointSession._unlink_owned(target)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ManagedRunCheckpointRepository",
    "ManagedRunCheckpointSession",
    "RUN_CHECKPOINT_ENVELOPE_SCHEMA_VERSION",
    "RunCheckpointIdentity",
]
