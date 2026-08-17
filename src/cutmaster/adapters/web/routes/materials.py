"""Material Library queries and durable Web analysis commands."""

from __future__ import annotations

import mimetypes
import os
import re
import stat
import tempfile
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
import anyio
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import Receive, Scope, Send

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
    MaterialDispatcherDependency,
)
from cutmaster.adapters.web.presenters import (
    attempt_view,
    job_view,
    material_detail_view,
    material_memory_view,
    material_reference_views,
)
from cutmaster.adapters.web.routes._execution import latest_material_execution
from cutmaster.adapters.web.schemas.materials import MaterialPreflightBody
from cutmaster.application.errors import MaterialPreviewUnavailableError
from cutmaster.application.jobs import (
    EnqueueMaterialAnalysisCommand,
    ResumeMaterialAnalysisCommand,
    RetryMaterialAnalysisCommand,
)
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.infrastructure.persistence.sqlite import (
    ActiveAttemptBlocker,
    IdempotencyConflict,
    ManagedStateConflict,
)
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialNameCollisionError,
    MaterialReferencedError,
    SubtitleSidecarConflictError,
    normalize_material_name,
)

router = APIRouter(prefix="/materials", tags=["materials"])


class _PinnedFileResponse(FileResponse):
    """Stream an already-open Material source descriptor without a lease."""

    def __init__(self, *args, stream, stat_result, **kwargs) -> None:
        kwargs["stat_result"] = stat_result
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            stream.close()
            raise
        self._stream = anyio.wrap_file(stream)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._stream.aclose()

    async def _handle_simple(
        self,
        send: Send,
        send_header_only: bool,
        _send_pathsend: bool,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        await self._stream.seek(0)
        while True:
            chunk = await self._stream.read(self.chunk_size)
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": len(chunk) == self.chunk_size,
                }
            )
            if len(chunk) != self.chunk_size:
                return

    async def _handle_single_range(
        self,
        send: Send,
        start: int,
        end: int,
        file_size: int,
        send_header_only: bool,
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        await self._stream.seek(start)
        while start < end:
            chunk = await self._stream.read(min(self.chunk_size, end - start))
            start += len(chunk)
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": start < end,
                }
            )

    async def _handle_multiple_ranges(
        self,
        send: Send,
        ranges: list[tuple[int, int]],
        file_size: int,
        send_header_only: bool,
    ) -> None:
        boundary = os.urandom(13).hex()
        content_length, header = self.generate_multipart(
            ranges,
            boundary,
            file_size,
            self.headers["content-type"],
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        for start, end in ranges:
            await send(
                {
                    "type": "http.response.body",
                    "body": header(start, end),
                    "more_body": True,
                }
            )
            await self._stream.seek(start)
            while start < end:
                chunk = await self._stream.read(min(self.chunk_size, end - start))
                start += len(chunk)
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"\r\n",
                    "more_body": True,
                }
            )
        await send(
            {
                "type": "http.response.body",
                "body": f"--{boundary}--".encode("latin-1"),
            }
        )


class MaterialSort(StrEnum):
    NAME_ASC = "name_asc"
    NAME_DESC = "name_desc"
    TYPE_NAME = "type_name"


@router.post("/preflight")
def preflight_material_name(
    body: MaterialPreflightBody,
    application: ApplicationDependency,
) -> dict[str, object]:
    """Check one type-scoped public name without hashing or importing bytes."""

    normalized = normalize_material_name(body.name)
    existing = application.materials.find_by_name(body.material_type, normalized)
    payload: dict[str, object] = {
        "available": existing is None,
        "normalized_name": normalized,
        "existing_material": None,
    }
    if existing is not None:
        detail = application.materials.detail(existing.material_id)
        if detail is not None:
            payload["existing_material"] = _material_detail_payload(
                application,
                detail,
                include_attempts=False,
            )
    return payload


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def import_material(
    application: ApplicationDependency,
    dispatcher: MaterialDispatcherDependency,
    command_id: IdempotencyKey,
    material_type: MaterialType = Form(),
    name: str = Form(),
    source: UploadFile = File(),
    subtitle: UploadFile | None = File(default=None),
) -> JSONResponse:
    """Import immutable media, enqueue analysis, and dispatch a real worker."""

    _require_command_id(command_id)
    if subtitle is not None and material_type is not MaterialType.VIDEO:
        raise ValueError("Subtitle sidecars are only valid for video Materials")
    if subtitle is not None and Path(subtitle.filename or "").suffix.lower() != ".srt":
        raise ValueError("Subtitle upload must use the .srt suffix")

    upload_path = _stage_upload(application, source, label="source")
    subtitle_path = (
        None
        if subtitle is None
        else _stage_upload(application, subtitle, label="subtitle")
    )
    created_material_id: MaterialId | None = None
    submitted = False
    try:
        existing = application.materials.find_by_name(material_type, name)
        if existing is not None:
            matching_attempt = next(
                (
                    item
                    for item in application.jobs.activity(
                        owner_type="material",
                        owner_id=str(existing.material_id),
                        limit=500,
                    )
                    if item.command_id == command_id
                ),
                None,
            )
            if matching_attempt is None:
                raise MaterialNameCollisionError(
                    f"A {material_type.value} Material named {existing.name!r} "
                    "already exists"
                )
            try:
                material = application.materials.ensure(
                    upload_path,
                    material_type,
                    name,
                )
                if subtitle_path is not None:
                    with application.materials.lease(material.material_id) as binding:
                        application.materials.ensure_subtitle(binding, subtitle_path)
            except (
                MaterialNameCollisionError,
                SubtitleSidecarConflictError,
            ) as error:
                raise IdempotencyConflict(command_id) from error
            submission = application.jobs.enqueue_material_analysis(
                EnqueueMaterialAnalysisCommand(command_id, material.material_id)
            )
        else:
            material = application.materials.add(upload_path, material_type, name)
            created_material_id = material.material_id
            if subtitle_path is not None:
                with application.materials.lease(material.material_id) as binding:
                    application.materials.ensure_subtitle(binding, subtitle_path)
            submission = application.jobs.enqueue_material_analysis(
                EnqueueMaterialAnalysisCommand(command_id, material.material_id)
            )
        submitted = True
        _dispatch_submission(application, dispatcher, submission)
        detail = application.materials.detail(material.material_id)
        if detail is None:
            raise RuntimeError("Imported Material is unavailable")
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Location": f"/api/materials/{material.material_id}"},
            content={
                "material": _material_detail_payload(
                    application,
                    detail,
                    include_attempts=False,
                ),
                "attempt": attempt_view(submission.attempt),
                "job": job_view(submission.job),
            },
        )
    except Exception:
        # Before a durable Job exists, roll the fresh Catalog entry back.  Once
        # queued, the failed/queued Attempt remains the authoritative outcome.
        if created_material_id is not None and not submitted:
            try:
                application.materials.delete(created_material_id)
                application.jobs.purge_material_history(created_material_id)
            except Exception:
                pass
        raise
    finally:
        upload_path.unlink(missing_ok=True)
        if subtitle_path is not None:
            subtitle_path.unlink(missing_ok=True)


@router.get("")
def list_materials(
    application: ApplicationDependency,
    material_type: MaterialType | None = Query(default=None, alias="type"),
    search: str = Query(default="", max_length=200),
    sort: MaterialSort = MaterialSort.NAME_ASC,
) -> dict[str, object]:
    materials = application.materials.list(
        material_type,
        search=search,
        sort=sort.value,
    )
    items = []
    for material in materials:
        detail = application.materials.detail(material.material_id)
        if detail is not None:
            items.append(
                _material_detail_payload(
                    application,
                    detail,
                    include_attempts=False,
                )
            )
    return {
        "items": items,
        "total": len(items),
        "type": None if material_type is None else material_type.value,
        "search": search,
        "sort": sort.value,
    }


@router.get("/{material_id}")
def get_material(
    material_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    identifier = MaterialId.parse(material_id)
    value = application.materials.detail(identifier)
    if value is None:
        raise HTTPException(status_code=404, detail="Material not found")
    return _material_detail_payload(
        application,
        value,
        include_attempts=True,
    )


@router.post(
    "/{material_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_material_analysis(
    material_id: str,
    application: ApplicationDependency,
    dispatcher: MaterialDispatcherDependency,
    command_id: IdempotencyKey,
) -> JSONResponse:
    identifier = MaterialId.parse(material_id)
    material = application.materials.get(identifier)
    if material is None:
        raise HTTPException(status_code=404, detail="Material not found")
    replay_candidate = _material_has_command(application, identifier, command_id)
    if not replay_candidate and material.condition is MaterialCondition.READY:
        raise ManagedStateConflict(
            "material_already_ready",
            f"Material {identifier} is already Ready",
        )
    submission = application.jobs.retry_material_analysis(
        RetryMaterialAnalysisCommand(command_id, identifier)
    )
    _dispatch_submission(application, dispatcher, submission)
    return _submission_response(application, identifier, submission)


@router.post(
    "/{material_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_material_analysis(
    material_id: str,
    application: ApplicationDependency,
    dispatcher: MaterialDispatcherDependency,
    command_id: IdempotencyKey,
) -> JSONResponse:
    identifier = MaterialId.parse(material_id)
    material = application.materials.get(identifier)
    if material is None:
        raise HTTPException(status_code=404, detail="Material not found")
    replay_candidate = _material_has_command(application, identifier, command_id)
    if not replay_candidate and material.condition is MaterialCondition.READY:
        raise ManagedStateConflict(
            "material_already_ready",
            f"Material {identifier} is already Ready",
        )
    submission = application.jobs.resume_material_analysis(
        ResumeMaterialAnalysisCommand(command_id, identifier)
    )
    _dispatch_submission(application, dispatcher, submission)
    return _submission_response(application, identifier, submission)


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material(
    material_id: str,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> Response:
    identifier = MaterialId.parse(material_id)
    replayed = application.jobs.replay_material_deletion(command_id, identifier)
    material = application.materials.get(identifier)
    if material is None and not replayed:
        raise HTTPException(status_code=404, detail="Material not found")
    references = application.materials.references(identifier)
    if references:
        raise MaterialReferencedError(identifier, references)
    active = application.jobs.active_material_attempt_ids(identifier)
    if active:
        raise ActiveAttemptBlocker(
            "material",
            str(identifier),
            [str(item) for item in active],
        )
    if not replayed:
        # Persist the command intent before crossing the SQLite/Catalog
        # boundary.  A process crash after the filesystem commit can then
        # safely replay cleanup instead of turning into an ambiguous 404.
        application.jobs.record_material_deletion(command_id, identifier)
    if material is not None:
        application.materials.delete(identifier)
    application.jobs.purge_material_history(identifier)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{material_id}/memory/{tab}")
def get_material_memory(
    material_id: str,
    tab: str,
    application: ApplicationDependency,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    identifier = MaterialId.parse(material_id)
    if application.materials.get(identifier) is None:
        raise HTTPException(status_code=404, detail="Material not found")
    return material_memory_view(
        application.materials.memory(
            identifier,
            tab,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/{material_id}/source")
def get_material_source(
    material_id: str,
    application: ApplicationDependency,
) -> FileResponse:
    """Pin and stream source bytes without holding a Material workflow lock."""

    identifier = MaterialId.parse(material_id)
    material = application.materials.get(identifier)
    if material is None:
        raise HTTPException(status_code=404, detail="Material not found")
    try:
        with application.materials.read_lease(identifier) as binding:
            path = binding.source_path
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        stream = os.fdopen(descriptor, "rb")
        metadata = os.fstat(stream.fileno())
        current = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or current.st_ino != metadata.st_ino
            or current.st_dev != metadata.st_dev
        ):
            stream.close()
            raise FileNotFoundError(path)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = _inline_filename(material.name, path.suffix)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail="Material source unavailable") from exc

    return _PinnedFileResponse(
        path,
        stream=stream,
        stat_result=metadata,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


_PREVIEW_HEADERS = {
    "Accept-Ranges": "none",
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}


def _material_preview_response(
    material_id: str,
    expected_type: MaterialType,
    application,
) -> Response:
    identifier = MaterialId.parse(material_id)
    material = application.materials.get(identifier)
    if material is None:
        raise HTTPException(status_code=404, detail="Material not found")
    if material.material_type is not expected_type:
        raise MaterialPreviewUnavailableError(
            f"Material {identifier} has no {expected_type.value} preview"
        )
    preview = application.materials.preview(identifier)
    return Response(
        content=preview.content,
        media_type=preview.media_type,
        headers=_PREVIEW_HEADERS,
    )


@router.get("/{material_id}/thumbnail", response_class=Response)
def get_material_thumbnail(
    material_id: str,
    application: ApplicationDependency,
) -> Response:
    """Return one bounded published frame without opening source video."""

    return _material_preview_response(material_id, MaterialType.VIDEO, application)


@router.get("/{material_id}/waveform", response_class=Response)
def get_material_waveform(
    material_id: str,
    application: ApplicationDependency,
) -> Response:
    """Render a bounded SVG from the published analyser energy curve."""

    return _material_preview_response(material_id, MaterialType.MUSIC, application)


_UNSAFE_FILENAME = re.compile(r"[^\w .()\-]+", flags=re.UNICODE)
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,12}$")


def _material_detail_payload(
    application,
    detail,
    *,
    include_attempts: bool,
) -> dict[str, object]:
    attempts = application.jobs.activity(
        owner_type="material",
        owner_id=str(detail.material.material_id),
        limit=500,
    )
    latest = latest_material_execution(application, detail.material.material_id)
    payload = material_detail_view(
        detail,
        references=material_reference_views(application, detail.references),
        attempts=attempts if include_attempts else None,
    )
    condition = _project_material_condition(detail.material.condition, latest)
    payload["condition"] = condition.value
    payload["analysis_available"] = condition is MaterialCondition.READY
    payload["latest_execution"] = latest
    return payload


def _project_material_condition(
    persisted: MaterialCondition,
    latest: dict[str, object] | None,
) -> MaterialCondition:
    if persisted is MaterialCondition.READY:
        return MaterialCondition.READY
    if latest is None:
        return persisted
    attempt = latest.get("attempt")
    job = latest.get("job")
    if isinstance(job, dict):
        progress = job.get("progress")
        if isinstance(progress, dict) and progress.get("state") == "inconsistent":
            return MaterialCondition.INCONSISTENT
    status_value = attempt.get("status") if isinstance(attempt, dict) else None
    if status_value == AttemptStatus.QUEUED.value:
        return MaterialCondition.QUEUED
    if status_value in {
        AttemptStatus.RUNNING.value,
        AttemptStatus.RETRYING.value,
        AttemptStatus.STOPPING.value,
    }:
        return MaterialCondition.ANALYSING
    if status_value in {
        AttemptStatus.FAILED.value,
        AttemptStatus.INTERRUPTED.value,
    }:
        return MaterialCondition.FAILED
    # A completed Attempt without a valid canonical result is inconsistent.
    if status_value == AttemptStatus.COMPLETE.value:
        return MaterialCondition.INCONSISTENT
    return persisted


def _submission_response(application, material_id, submission) -> JSONResponse:
    detail = application.materials.detail(material_id)
    if detail is None:
        raise RuntimeError("Material is unavailable")
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Location": f"/api/materials/{material_id}"},
        content={
            "material": _material_detail_payload(
                application,
                detail,
                include_attempts=False,
            ),
            "attempt": attempt_view(submission.attempt),
            "job": job_view(submission.job),
        },
    )


def _material_has_command(
    application, material_id: MaterialId, command_id: str
) -> bool:
    return any(
        attempt.command_id == command_id
        for attempt in application.jobs.activity(
            owner_type="material",
            owner_id=str(material_id),
            limit=500,
        )
    )


def _dispatch_submission(application, dispatcher, submission) -> None:
    current_job = application.jobs.get_job(submission.job.job_id)
    if current_job.status is AttemptStatus.QUEUED:
        dispatcher.dispatch(submission)


def _stage_upload(
    application,
    upload: UploadFile,
    *,
    label: str,
) -> Path:
    suffix = Path(upload.filename or "").suffix
    safe_suffix = suffix if _SAFE_SUFFIX.fullmatch(suffix) else ""
    root = (
        application.settings.effective_configuration.data_root / "staging" / "uploads"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        dir=root,
        prefix=f".{label}-",
        suffix=safe_suffix,
    )
    path = Path(raw_path)
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
                size += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if size == 0:
            raise ValueError(f"{label.capitalize()} upload cannot be empty")
        path.chmod(0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _require_command_id(value: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Idempotency-Key must be a canonical lowercase UUID"
        ) from error
    if str(parsed) != value or parsed.variant != "specified in RFC 4122":
        raise ValueError("Idempotency-Key must be a canonical lowercase UUID")


def _inline_filename(name: str, suffix: str) -> str:
    safe_name = _UNSAFE_FILENAME.sub("_", name).strip(" .")[:120]
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:16]
    if not safe_suffix.startswith("."):
        safe_suffix = ""
    return f"{safe_name or 'material'}{safe_suffix}"


__all__ = ["router"]
