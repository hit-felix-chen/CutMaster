"""Managed Render Variant creation, recovery, and integrity use cases."""

from __future__ import annotations

import hashlib
from pathlib import Path

from cutmaster.application.errors import RenderMediaUnavailableError
from cutmaster.application.jobs.views import attempt_view, job_view
from cutmaster.application.renders.commands import (
    CompleteRenderVariantCommand,
    CreateRenderVariantCommand,
    DeleteRenderVariantCommand,
    RecoverRenderVariantCommand,
    VerifyRenderVariantCommand,
)
from cutmaster.application.renders.views import (
    CompletedRenderView,
    DeletedRenderVariantView,
    RenderSubmissionView,
    RenderVariantView,
    render_submission_view,
    render_variant_view,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.domain.ids import AttemptId, FrozenEditId, RenderVariantId
from cutmaster.domain.renders import RenderVariantStatus
from cutmaster.infrastructure.persistence.sqlite import SQLiteApplicationStore


class RendersService:
    """Own Render Variant creation and integrity use cases."""

    __slots__ = ("_effective_configuration", "_store_instance")

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        store: SQLiteApplicationStore | None = None,
    ) -> None:
        self._effective_configuration = effective_configuration
        self._store_instance = store

    @property
    def _store(self) -> SQLiteApplicationStore:
        store = self._store_instance
        if store is None:
            store = SQLiteApplicationStore(self._effective_configuration.data_root)
            self._store_instance = store
        return store

    def create(self, command: CreateRenderVariantCommand) -> RenderSubmissionView:
        if not isinstance(command.edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        return render_submission_view(
            self._store.create_render_variant(
                command.command_id,
                command.edit_id,
                command.specification,
            ).value
        )

    def get(self, render_id: RenderVariantId) -> RenderVariantView:
        _require_render_id(render_id)
        return render_variant_view(self._store.get_render_variant(render_id))

    def list(self, edit_id: FrozenEditId) -> tuple[RenderVariantView, ...]:
        if not isinstance(edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        return tuple(
            render_variant_view(value)
            for value in self._store.list_render_variants(edit_id)
        )

    def media_path(self, render_id: RenderVariantId) -> Path:
        """Resolve one verified Ready master for local HTTP streaming."""

        _require_render_id(render_id)
        variant = self.get(render_id)
        if (
            variant.status is not RenderVariantStatus.READY
            or variant.master is None
            or variant.master_size_bytes is None
            or variant.master_sha256 is None
        ):
            raise RenderMediaUnavailableError(
                "Render Variant has no Ready managed master"
            )
        try:
            path = self._resolve_managed_file(variant.master.relative_path)
            metadata = _fingerprint_master(path)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise RenderMediaUnavailableError(
                "Render Variant managed master is unavailable"
            ) from exc
        if (
            metadata["size_bytes"] != variant.master_size_bytes
            or metadata["sha256"] != variant.master_sha256
        ):
            raise RenderMediaUnavailableError(
                "Render Variant managed master failed integrity validation"
            )
        return path

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
        master = self._resolve_managed_file(relative_path)
        metadata = _fingerprint_master(master)
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
        return CompletedRenderView(
            render_variant=render_variant_view(variant),
            attempt=attempt_view(attempt),
            job=job_view(job),
        )

    def retry(self, command: RecoverRenderVariantCommand) -> RenderSubmissionView:
        return self._recover(command, "retry")

    def resume(self, command: RecoverRenderVariantCommand) -> RenderSubmissionView:
        return self._recover(command, "resume")

    def render_again(
        self,
        command: RecoverRenderVariantCommand,
    ) -> RenderSubmissionView:
        return self._recover(command, "render_again")

    def verify(self, command: VerifyRenderVariantCommand) -> RenderVariantView:
        """Recompute master integrity, persisting Unavailable on mismatch."""

        _require_render_id(command.render_variant_id)
        variant = self.get(command.render_variant_id)
        if variant.status is not RenderVariantStatus.READY:
            return variant
        if (
            variant.master is None
            or variant.master_size_bytes is None
            or variant.master_sha256 is None
        ):
            reason = "Ready Render Variant has incomplete integrity metadata"
        else:
            try:
                path = self._resolve_managed_file(variant.master.relative_path)
                metadata = _fingerprint_master(path)
            except (FileNotFoundError, OSError, ValueError) as exc:
                reason = f"Managed master is unavailable: {type(exc).__name__}"
            else:
                if metadata["size_bytes"] != variant.master_size_bytes:
                    reason = "Managed master byte size changed"
                elif metadata["sha256"] != variant.master_sha256:
                    reason = "Managed master SHA-256 changed"
                else:
                    return variant
        value = self._store.mark_render_unavailable(
            command.command_id,
            command.render_variant_id,
            reason,
        ).value
        return render_variant_view(value)

    def delete(
        self,
        command: DeleteRenderVariantCommand,
    ) -> DeletedRenderVariantView:
        _require_render_id(command.render_variant_id)
        value = self._store.delete_render_variant(
            command.command_id,
            command.render_variant_id,
        ).value
        relative_path = value.get("master_relative_path")
        if relative_path is not None:
            self._delete_managed_file(str(relative_path))
        return DeletedRenderVariantView(
            render_variant_id=RenderVariantId.parse(
                str(value["render_variant_id"])
            ),
            deleted=bool(value["deleted"]),
        )

    def _recover(
        self,
        command: RecoverRenderVariantCommand,
        mode: str,
    ) -> RenderSubmissionView:
        _require_render_id(command.render_variant_id)
        return render_submission_view(
            self._store.retry_render_variant(
                command.command_id,
                command.render_variant_id,
                mode=mode,
            ).value
        )

    def _resolve_managed_file(self, relative_path: str) -> Path:
        path = self._effective_configuration.data_root / relative_path
        resolved_parent = path.parent.resolve()
        try:
            resolved_parent.relative_to(self._effective_configuration.data_root)
        except ValueError as exc:
            raise ValueError("Managed master escapes the Application Data Root") from exc
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"Managed master is not a regular file: {path}")
        return path

    def _delete_managed_file(self, relative_path: str) -> None:
        normalized = validate_portable_relative_file_path(relative_path)
        path = self._effective_configuration.data_root / normalized
        resolved_parent = path.parent.resolve()
        try:
            resolved_parent.relative_to(self._effective_configuration.data_root)
        except ValueError as exc:
            raise ValueError("Managed master escapes the Application Data Root") from exc
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if not (path.is_file() or path.is_symlink()):
            raise ValueError("Managed master reference does not identify a file")
        path.unlink()


def _require_render_id(value: RenderVariantId) -> None:
    if not isinstance(value, RenderVariantId):
        raise TypeError("render_variant_id must be a RenderVariantId")


def _fingerprint_master(path: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


__all__ = ["RendersService"]
