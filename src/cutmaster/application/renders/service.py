"""Managed Render Variant creation, recovery, integrity, and cleanup use cases."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from threading import Lock, RLock
from typing import TYPE_CHECKING, Any, BinaryIO, NoReturn
from uuid import uuid4

from cutmaster.application.errors import (
    RenderFpsMismatchError,
    RenderIntegrityMismatchError,
    RenderMediaUnavailableError,
)
from cutmaster.application.jobs.views import attempt_view, job_view
from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.ports.data_root import (
    DataRootCoordinator,
    root_shared_operation,
)
from cutmaster.application.renders.commands import (
    CompleteRenderVariantCommand,
    CreateRenderVariantCommand,
    DeleteRenderVariantCommand,
    RecoverRenderVariantCommand,
    VerifyRenderVariantCommand,
)
from cutmaster.application.renders.specification import RenderSpecification
from cutmaster.application.renders.views import (
    CompletedRenderView,
    DeletedRenderVariantView,
    RenderSubmissionView,
    RenderVariantView,
    VerifiedRenderIntegrityView,
    render_submission_view,
    render_variant_view,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.configuration.runtime import resolve_runtime_config
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.domain.ids import (
    AttemptId,
    FrozenEditId,
    RenderVariantId,
    RunId,
)
from cutmaster.domain.renders import RenderVariantStatus
from cutmaster.infrastructure.persistence.sqlite import (
    ManagedStateConflict,
    SQLiteApplicationStore,
)

if TYPE_CHECKING:
    from cutmaster.application.renders.execution import (
        ExecuteManagedRenderCommand,
        RenderEngineFactory,
    )


class RendersService:
    """Own strict Render Specifications and managed Render media state."""

    __slots__ = (
        "_effective_configuration",
        "_data_root_coordinator",
        "_integrity_cache",
        "_integrity_lock",
        "_materials_instance",
        "_media_locks",
        "_media_locks_guard",
        "_store_instance",
    )

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        store: SQLiteApplicationStore | None = None,
        *,
        materials: MaterialsService | None = None,
        data_root_coordinator: DataRootCoordinator | None = None,
    ) -> None:
        self._effective_configuration = effective_configuration
        self._data_root_coordinator = data_root_coordinator
        self._store_instance = store
        self._materials_instance = materials
        self._integrity_cache: dict[
            RenderVariantId,
            tuple[str, int, int, int, int, int, str],
        ] = {}
        self._integrity_lock = RLock()
        self._media_locks: dict[RenderVariantId, object] = {}
        self._media_locks_guard = Lock()

    @property
    def _store(self) -> SQLiteApplicationStore:
        store = self._store_instance
        if store is None:
            store = SQLiteApplicationStore(self._effective_configuration.data_root)
            self._store_instance = store
        return store

    @root_shared_operation
    def create(self, command: CreateRenderVariantCommand) -> RenderSubmissionView:
        """Create using the currently effective renderer configuration."""

        return self._create(command, self._effective_configuration)

    @root_shared_operation
    def create_dialogue_preview(
        self,
        command_id: str,
        edit_id: FrozenEditId,
    ) -> RenderSubmissionView:
        """Get or create the default Preview from its immutable Run snapshot."""

        if not isinstance(edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        replay = self._store.replay_idempotent(
            command_id,
            "renders.create_variant",
            {"edit_id": str(edit_id), "audio_mode": "dialogue"},
        )
        if replay is not None:
            # Automatic callers use this flag only as a wake-up edge. A replay
            # must not dispatch the same durable Job twice.
            return replace(render_submission_view(replay.value), created=False)
        edit = self._store.get_frozen_edit(edit_id)
        run = self._store.get_run(RunId.parse(str(edit["run_id"])))
        configuration = run.get("configuration")
        if not isinstance(configuration, Mapping):
            raise TypeError("Frozen Edit Run has no valid configuration snapshot")
        snapshot = EffectiveConfiguration(
            sources=self._effective_configuration.sources,
            data_root=self._effective_configuration.data_root,
            secret_references=self._effective_configuration.secret_references,
            _values=configuration,
        )
        return self._create(
            CreateRenderVariantCommand(command_id, edit_id, "dialogue"),
            snapshot,
        )

    @root_shared_operation
    def get(self, render_id: RenderVariantId) -> RenderVariantView:
        _require_render_id(render_id)
        return self._quick_check(self._raw_get(render_id))

    @root_shared_operation
    def list(self, edit_id: FrozenEditId) -> tuple[RenderVariantView, ...]:
        if not isinstance(edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        return tuple(
            self._quick_check(render_variant_view(value))
            for value in self._store.list_render_variants(edit_id)
        )

    @root_shared_operation
    def media_path(self, render_id: RenderVariantId) -> Path:
        """Resolve one fully verified Ready master for streaming or download."""

        _require_render_id(render_id)
        variant = self._raw_get(render_id)
        if variant.status is not RenderVariantStatus.READY:
            raise RenderMediaUnavailableError(
                "Render Variant has no Ready managed master"
            )
        try:
            path, _size, _cached = self._full_integrity(variant)
        except RenderIntegrityMismatchError as exc:
            raise RenderMediaUnavailableError(str(exc)) from exc
        return path

    def _open_media(
        self,
        render_id: RenderVariantId,
    ) -> tuple[Path, BinaryIO, os.stat_result]:
        """Open and verify one descriptor before a cascading delete can unlink it."""

        variant = self._raw_get(render_id)
        if variant.status is not RenderVariantStatus.READY:
            raise RenderMediaUnavailableError(
                "Render Variant has no Ready managed master"
            )
        if (
            variant.master is None
            or variant.master_size_bytes is None
            or variant.master_sha256 is None
        ):
            self._mark_unavailable(
                str(uuid4()),
                variant,
                "Ready Render Variant has incomplete integrity metadata",
            )
            raise RenderMediaUnavailableError(
                "Ready Render Variant has incomplete integrity metadata"
            )
        stream: BinaryIO | None = None
        try:
            path = self._resolve_managed_file(variant.master.relative_path)
            stream = path.open("rb")
            observed = os.fstat(stream.fileno())
            if observed.st_size != variant.master_size_bytes:
                raise RenderIntegrityMismatchError(
                    "Managed master byte size changed"
                )
            key = _integrity_stat_key(path, observed)
            with self._integrity_lock:
                cached = self._integrity_cache.get(render_id)
            if cached is not None and cached[:-1] == key:
                digest = cached[-1]
            else:
                digest = _fingerprint_stream(stream)
                stream.seek(0)
                with self._integrity_lock:
                    self._integrity_cache[render_id] = (*key, digest)
            if digest != variant.master_sha256:
                raise RenderIntegrityMismatchError(
                    "Managed master SHA-256 changed"
                )
            return path, stream, observed
        except (FileNotFoundError, OSError, ValueError) as error:
            if stream is not None:
                stream.close()
            reason = f"Managed master is unavailable: {type(error).__name__}"
            self._mark_unavailable(str(uuid4()), variant, reason)
            raise RenderMediaUnavailableError(reason) from error
        except RenderIntegrityMismatchError as error:
            if stream is not None:
                stream.close()
            self._mark_unavailable(str(uuid4()), variant, str(error))
            raise RenderMediaUnavailableError(str(error)) from error

    @root_shared_operation
    def acquire_media(self, render_id: RenderVariantId) -> _ManagedMediaLease:
        """Hold deletion exclusion until an HTTP file response completes."""

        _require_render_id(render_id)
        return _ManagedMediaLease(self, render_id, self._media_lock(render_id))

    @root_shared_operation
    def complete(
        self,
        command: CompleteRenderVariantCommand,
    ) -> CompletedRenderView:
        _require_render_id(command.render_variant_id)
        if not isinstance(command.attempt_id, AttemptId):
            raise TypeError("attempt_id must be an AttemptId")
        relative_path = validate_portable_relative_file_path(
            command.master_relative_path
        )
        replay = self._store.replay_idempotent(
            command.command_id,
            "renders.complete",
            {
                "render_variant_id": str(command.render_variant_id),
                "attempt_id": str(command.attempt_id),
                "master_relative_path": relative_path,
                "frame_count": command.frame_count,
                "duration_sec": float(command.duration_sec),
            },
        )
        if replay is not None:
            return _completed_render_result(replay.value)
        master = self._resolve_managed_file(relative_path)
        metadata = _fingerprint_master(master)
        observed = master.stat()
        value = self._store.complete_render_variant(
            command.command_id,
            command.render_variant_id,
            command.attempt_id,
            master_relative_path=relative_path,
            master_size_bytes=metadata["size_bytes"],
            master_sha256=metadata["sha256"],
            frame_count=command.frame_count,
            duration_sec=command.duration_sec,
        ).value
        variant = value["render_variant"]
        attempt = value["attempt"]
        job = value["job"]
        if not all(isinstance(item, dict) for item in (variant, attempt, job)):
            raise TypeError("Invalid completed Render persistence result")
        with self._integrity_lock:
            self._integrity_cache[command.render_variant_id] = (
                str(master),
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
                str(metadata["sha256"]),
            )
        return CompletedRenderView(
            render_variant=render_variant_view(variant),
            attempt=attempt_view(attempt),
            job=job_view(job),
        )

    @root_shared_operation
    def retry(self, command: RecoverRenderVariantCommand) -> RenderSubmissionView:
        return self._recover(command, "retry")

    @root_shared_operation
    def resume(self, command: RecoverRenderVariantCommand) -> RenderSubmissionView:
        return self._recover(command, "resume")

    @root_shared_operation
    def render_again(
        self,
        command: RecoverRenderVariantCommand,
    ) -> RenderSubmissionView:
        return self._recover(command, "render_again")

    @root_shared_operation
    def verify(
        self,
        command: VerifyRenderVariantCommand,
    ) -> RenderVariantView:
        """Verify bytes and return the resulting Render Variant state."""

        try:
            return self.verify_integrity(command).render_variant
        except RenderIntegrityMismatchError:
            return self._raw_get(command.render_variant_id)

    @root_shared_operation
    def verify_integrity(
        self,
        command: VerifyRenderVariantCommand,
    ) -> VerifiedRenderIntegrityView:
        """Verify Ready bytes and persist Unavailable before reporting damage."""

        _require_render_id(command.render_variant_id)
        request = {"render_variant_id": str(command.render_variant_id)}
        replay = self._store.replay_idempotent(
            command.command_id,
            "renders.verify",
            request,
        )
        if replay is not None:
            return _verified_integrity_result(replay.value)
        variant = self._raw_get(command.render_variant_id)
        if variant.status is not RenderVariantStatus.READY:
            raise RenderMediaUnavailableError(
                "Only a Ready Render Variant has media to verify"
            )
        try:
            _path, size, cached = self._full_integrity(
                variant,
                persist_unavailable=False,
            )
        except RenderIntegrityMismatchError as error:
            with self._integrity_lock:
                self._integrity_cache.pop(command.render_variant_id, None)
            result = self._store.record_render_verification(
                command.command_id,
                command.render_variant_id,
                verified=False,
                size_bytes=variant.master_size_bytes or 0,
                cached=False,
                reason=str(error),
            ).value
            # Validate the stored result before returning the stable problem.
            _verified_integrity_result(result)
            raise AssertionError("Unreachable integrity result")
        result = self._store.record_render_verification(
            command.command_id,
            command.render_variant_id,
            verified=True,
            size_bytes=size,
            cached=cached,
        ).value
        return _verified_integrity_result(result)

    @root_shared_operation
    def delete(
        self,
        command: DeleteRenderVariantCommand,
    ) -> DeletedRenderVariantView:
        _require_render_id(command.render_variant_id)
        lock = self._media_lock(command.render_variant_id)
        with lock:
            value = self._store.delete_render_variant(
                command.command_id,
                command.render_variant_id,
            ).value
            self._delete_owned_render_directory(
                str(value["project_id"]),
                command.render_variant_id,
            )
            raw_job_ids = value.get("job_ids", [])
            if not isinstance(raw_job_ids, list):
                raise TypeError("Invalid Render deletion job list")
            for job_id in raw_job_ids:
                self._delete_exact_job_log(str(job_id))
            with self._integrity_lock:
                self._integrity_cache.pop(command.render_variant_id, None)
        return DeletedRenderVariantView(
            render_variant_id=RenderVariantId.parse(
                str(value["render_variant_id"])
            ),
            deleted=bool(value["deleted"]),
        )

    @root_shared_operation
    def specification(self, render_id: RenderVariantId) -> RenderSpecification:
        """Return and validate the immutable executable specification."""

        return RenderSpecification.from_dict(self._raw_get(render_id).specification)

    @root_shared_operation
    def execute_attempt(
        self,
        command: ExecuteManagedRenderCommand,
        *,
        should_stop: Callable[[], bool],
        report_progress: Callable[[Mapping[str, object]], None],
        renderer_factory: RenderEngineFactory | None = None,
    ) -> CompletedRenderView:
        """Execute through the Application-owned managed rendering boundary."""

        from cutmaster.application.renders.execution import (
            ManagedRenderExecutor,
        )

        materials = self._materials_instance
        if materials is None:
            raise RuntimeError("Managed rendering requires the Material service")
        executor = ManagedRenderExecutor(
            self._effective_configuration,
            materials,
            self,
        )
        if renderer_factory is None:
            return executor.execute(
                command,
                should_stop=should_stop,
                report_progress=report_progress,
            )
        return executor.execute(
            command,
            should_stop=should_stop,
            report_progress=report_progress,
            renderer_factory=renderer_factory,
        )

    def _create(
        self,
        command: CreateRenderVariantCommand,
        configuration: EffectiveConfiguration,
    ) -> RenderSubmissionView:
        if not isinstance(command.edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        replay = self._store.replay_idempotent(
            command.command_id,
            "renders.create_variant",
            {
                "edit_id": str(command.edit_id),
                "audio_mode": command.audio_mode,
            },
        )
        if replay is not None:
            return render_submission_view(replay.value)
        from cutmaster.application.runs.review import load_render_plan

        edit = self._store.get_frozen_edit(command.edit_id)
        plan, _ = load_render_plan(
            self._effective_configuration.data_root,
            str(edit["plan_relative_path"]),
        )
        renderer = resolve_runtime_config(configuration).renderer
        specification = RenderSpecification.create(command.audio_mode, renderer)
        if specification.renderer.fps != plan.fps:
            raise RenderFpsMismatchError(
                f"RenderPlan fps {plan.fps} does not match effective renderer fps "
                f"{specification.renderer.fps}"
            )
        return render_submission_view(
            self._store.create_render_variant(
                command.command_id,
                command.edit_id,
                specification.to_dict(),
            ).value
        )

    def _recover(
        self,
        command: RecoverRenderVariantCommand,
        mode: str,
    ) -> RenderSubmissionView:
        _require_render_id(command.render_variant_id)
        request = {
            "render_variant_id": str(command.render_variant_id),
            "mode": mode,
        }
        replay = self._store.replay_idempotent(
            command.command_id,
            f"renders.{mode}",
            request,
        )
        if replay is not None:
            return render_submission_view(replay.value)
        self.specification(command.render_variant_id)
        return render_submission_view(
            self._store.retry_render_variant(
                command.command_id,
                command.render_variant_id,
                mode=mode,
            ).value
        )

    def _raw_get(self, render_id: RenderVariantId) -> RenderVariantView:
        return render_variant_view(self._store.get_render_variant(render_id))

    def _quick_check(self, variant: RenderVariantView) -> RenderVariantView:
        if variant.status is not RenderVariantStatus.READY:
            return variant
        reason: str | None = None
        if (
            variant.master is None
            or variant.master_size_bytes is None
            or variant.master_sha256 is None
        ):
            reason = "Ready Render Variant has incomplete integrity metadata"
        else:
            try:
                path = self._resolve_managed_file(variant.master.relative_path)
                observed = path.stat()
            except (FileNotFoundError, OSError, ValueError) as exc:
                reason = f"Managed master is unavailable: {type(exc).__name__}"
            else:
                if observed.st_size != variant.master_size_bytes:
                    reason = "Managed master byte size changed"
        if reason is None:
            return variant
        return self._mark_unavailable(str(uuid4()), variant, reason)

    def _full_integrity(
        self,
        variant: RenderVariantView,
        *,
        command_id: str | None = None,
        persist_unavailable: bool = True,
    ) -> tuple[Path, int, bool]:
        def fail(reason: str) -> NoReturn:
            if persist_unavailable:
                self._mark_unavailable(command_id or str(uuid4()), variant, reason)
            raise RenderIntegrityMismatchError(reason)

        if (
            variant.master is None
            or variant.master_size_bytes is None
            or variant.master_sha256 is None
        ):
            reason = "Ready Render Variant has incomplete integrity metadata"
            fail(reason)
        try:
            path = self._resolve_managed_file(variant.master.relative_path)
            observed = path.stat()
        except (FileNotFoundError, OSError, ValueError) as exc:
            reason = f"Managed master is unavailable: {type(exc).__name__}"
            if persist_unavailable:
                self._mark_unavailable(command_id or str(uuid4()), variant, reason)
            raise RenderIntegrityMismatchError(reason) from exc
        if observed.st_size != variant.master_size_bytes:
            reason = "Managed master byte size changed"
            fail(reason)
        # Size alone is the cheap list check. Once a full digest has been
        # computed, bind its process-local cache entry to the complete stable
        # stat identity so replacement and timestamp-preserving writes cannot
        # accidentally reuse an old digest.
        key = _integrity_stat_key(path, observed)
        with self._integrity_lock:
            cached = self._integrity_cache.get(variant.render_variant_id)
        if cached is not None and cached[:-1] == key:
            digest = cached[-1]
            cache_hit = True
        else:
            digest = str(_fingerprint_master(path)["sha256"])
            cache_hit = False
            with self._integrity_lock:
                self._integrity_cache[variant.render_variant_id] = (*key, digest)
        if digest != variant.master_sha256:
            reason = "Managed master SHA-256 changed"
            fail(reason)
        return path, observed.st_size, cache_hit

    def _media_lock(self, render_id: RenderVariantId) -> Any:
        with self._media_locks_guard:
            lock = self._media_locks.get(render_id)
            if lock is None:
                lock = Lock()
                self._media_locks[render_id] = lock
            return lock

    def _mark_unavailable(
        self,
        command_id: str,
        variant: RenderVariantView,
        reason: str,
    ) -> RenderVariantView:
        try:
            value = self._store.mark_render_unavailable(
                command_id,
                variant.render_variant_id,
                reason,
            ).value
        except ManagedStateConflict:
            current = self._raw_get(variant.render_variant_id)
            if current.status is not RenderVariantStatus.UNAVAILABLE:
                raise
            return current
        with self._integrity_lock:
            self._integrity_cache.pop(variant.render_variant_id, None)
        return render_variant_view(value)

    def _resolve_managed_file(self, relative_path: str) -> Path:
        normalized = validate_portable_relative_file_path(relative_path)
        root = self._effective_configuration.data_root.resolve()
        components = normalized.split("/")
        current = root
        for index, component in enumerate(components):
            current = current / component
            metadata = current.lstat()
            is_last = index == len(components) - 1
            if is_last:
                if not stat.S_ISREG(metadata.st_mode):
                    raise FileNotFoundError(
                        f"Managed master is not a regular file: {current}"
                    )
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Managed master path contains an unsafe directory")
        # Every component was lstat'd and symlinks are neither regular files nor
        # directories, so resolving cannot cross an ownership boundary.
        if current.resolve(strict=True) != current:
            raise ValueError("Managed master escapes its canonical managed path")
        return current

    def _delete_owned_render_directory(
        self,
        project_id: str,
        render_id: RenderVariantId,
    ) -> None:
        root = self._effective_configuration.data_root.resolve()
        owner = root / "projects" / project_id / "renders" / str(render_id)
        try:
            metadata = owner.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            owner.unlink()
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Managed Render owner path is not a directory")
        if owner.resolve() != owner:
            raise ValueError("Managed Render owner directory is unsafe")
        for directory, names, files in os.walk(owner, topdown=False, followlinks=False):
            current = Path(directory)
            for name in files:
                (current / name).unlink()
            for name in names:
                child = current / name
                if child.is_symlink():
                    child.unlink()
                else:
                    child.rmdir()
        owner.rmdir()

    def _delete_exact_job_log(self, job_id: str) -> None:
        root = self._effective_configuration.data_root.resolve()
        parent = root
        for component in ("logs", "jobs"):
            parent = parent / component
            try:
                metadata = parent.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Managed Job log directory is unsafe")
        log = parent / f"{job_id}.log"
        try:
            metadata = log.lstat()
        except FileNotFoundError:
            return
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise ValueError("Managed Job log is not a file")
        log.unlink()


def _require_render_id(value: RenderVariantId) -> None:
    if not isinstance(value, RenderVariantId):
        raise TypeError("render_variant_id must be a RenderVariantId")


class _ManagedMediaLease:
    __slots__ = ("_lock", "_released", "_render_id", "_service", "_stream")

    def __init__(self, service: RendersService, render_id: RenderVariantId, lock: Any):
        self._service = service
        self._render_id = render_id
        self._lock = lock
        self._released = True
        self._stream: BinaryIO | None = None

    def acquire(self) -> tuple[Path, BinaryIO, os.stat_result]:
        self._lock.acquire()
        self._released = False
        try:
            path, stream, observed = self._service._open_media(self._render_id)
            self._stream = stream
            return path, stream, observed
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        if not self._released:
            self._released = True
            stream = self._stream
            self._stream = None
            if stream is not None and not stream.closed:
                stream.close()
            self._lock.release()


def _verified_integrity_result(value: Mapping[str, Any]) -> VerifiedRenderIntegrityView:
    raw_variant = value.get("render_variant")
    if not isinstance(raw_variant, Mapping):
        raise TypeError("Invalid Render verification result")
    verified = value.get("verified")
    if not isinstance(verified, bool):
        raise TypeError("Invalid Render verification decision")
    if not verified:
        raise RenderIntegrityMismatchError(
            str(value.get("reason") or "Managed master failed integrity validation")
        )
    size = value.get("size_bytes")
    cached = value.get("cached")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(cached, bool)
    ):
        raise TypeError("Invalid Render verification metadata")
    return VerifiedRenderIntegrityView(
        render_variant=render_variant_view(raw_variant),
        size_bytes=size,
        cached=cached,
    )


def _completed_render_result(value: Mapping[str, Any]) -> CompletedRenderView:
    variant = value.get("render_variant")
    attempt = value.get("attempt")
    job = value.get("job")
    if (
        not isinstance(variant, Mapping)
        or not isinstance(attempt, Mapping)
        or not isinstance(job, Mapping)
    ):
        raise TypeError("Invalid completed Render persistence result")
    return CompletedRenderView(
        render_variant=render_variant_view(variant),
        attempt=attempt_view(attempt),
        job=job_view(job),
    )


def _fingerprint_master(path: Path) -> dict[str, int | str]:
    with path.open("rb") as stream:
        digest = _fingerprint_stream(stream)
        size = stream.tell()
    return {"size_bytes": size, "sha256": digest}


def _fingerprint_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _integrity_stat_key(
    path: Path,
    observed: os.stat_result,
) -> tuple[str, int, int, int, int, int]:
    return (
        str(path),
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


__all__ = ["RendersService"]
