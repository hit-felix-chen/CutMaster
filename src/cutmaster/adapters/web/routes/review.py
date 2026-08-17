"""Frozen Edit Review, Guided Revision, and Render Variant HTTP resources."""

from __future__ import annotations

import logging
import mimetypes
import os
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import anyio
from fastapi import APIRouter, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.types import Receive, Scope, Send

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
    JobDispatcherDependency,
)
from cutmaster.adapters.web.presenters import (
    frozen_edit_review_view,
    frozen_edit_view,
    render_variant_view,
)
from cutmaster.adapters.web.routes._execution import latest_render_execution
from cutmaster.application.renders import (
    CreateRenderVariantCommand,
    DeleteRenderVariantCommand,
    RecoverRenderVariantCommand,
    RenderSubmissionView,
    VerifyRenderVariantCommand,
)
from cutmaster.application.errors import RenderDispatchFailedError
from cutmaster.application.runs import (
    CandidateReplacement,
    SaveGuidedRevisionCommand,
)
from cutmaster.domain.ids import FrozenEditId, ProjectId, RenderVariantId


LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/frozen-edits", tags=["review"])
render_router = APIRouter(prefix="/render-variants", tags=["renders"])
project_render_router = APIRouter(prefix="/projects", tags=["renders"])


class _LeasedFileResponse(FileResponse):
    """Stream one already-open master while holding deletion exclusion."""

    def __init__(self, *args, lease, stream, stat_result, **kwargs) -> None:
        self._lease = lease
        try:
            kwargs["stat_result"] = stat_result
            super().__init__(*args, **kwargs)
        except Exception:
            lease.release()
            raise
        self._stream = anyio.wrap_file(stream)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Pinning the descriptor before returning the Response closes the
            # small gap in which a cascading Run/Project deletion could unlink
            # the path before Starlette opened it. Closing and releasing here
            # also covers disconnects and transport failures.
            try:
                await self._stream.aclose()
            finally:
                self._lease.release()

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
        more_body = True
        while more_body:
            chunk = await self._stream.read(self.chunk_size)
            more_body = len(chunk) == self.chunk_size
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": more_body,
                }
            )

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
            {
                "type": "http.response.start",
                "status": 206,
                "headers": headers.raw,
            }
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        await self._stream.seek(start)
        more_body = True
        while more_body:
            chunk = await self._stream.read(min(self.chunk_size, end - start))
            start += len(chunk)
            more_body = len(chunk) == self.chunk_size and start < end
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": more_body,
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
            {
                "type": "http.response.start",
                "status": 206,
                "headers": headers.raw,
            }
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


class CandidateReplacementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    slot_id: str = Field(min_length=1, max_length=200)
    candidate_id: str = Field(min_length=1, max_length=300)


class SaveRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    replacements: list[CandidateReplacementPayload] = Field(
        min_length=1,
        max_length=500,
    )


class CreateRenderVariantPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    audio_mode: Literal["dialogue", "bgm_only"]


@router.get("/{edit_id}/review")
def get_frozen_edit_review(
    edit_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    return frozen_edit_review_view(
        application.runs.review(FrozenEditId.parse(edit_id))
    )


@router.post(
    "/{edit_id}/revisions",
    status_code=status.HTTP_201_CREATED,
)
def save_guided_revision(
    edit_id: str,
    payload: SaveRevisionPayload,
    application: ApplicationDependency,
    dispatcher: JobDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    source_id = FrozenEditId.parse(edit_id)
    child = application.runs.save_guided_revision(
        SaveGuidedRevisionCommand(
            command_id=command_id,
            source_edit_id=source_id,
            replacements=tuple(
                CandidateReplacement(item.slot_id, item.candidate_id)
                for item in payload.replacements
            ),
        )
    )
    run = application.runs.get(child.run_id)
    _create_and_dispatch_preview(application, dispatcher, child.edit_id)
    return {
        "frozen_edit": frozen_edit_view(child),
        "review_url": (
            f"/projects/{run.project_id}/runs/{run.run_id}/review/{child.edit_id}"
        ),
    }


@router.post("/{edit_id}/render-variants")
def create_render_variant(
    edit_id: str,
    body: CreateRenderVariantPayload,
    response: Response,
    application: ApplicationDependency,
    dispatcher: JobDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    submission = application.renders.create(
        CreateRenderVariantCommand(
            command_id,
            FrozenEditId.parse(edit_id),
            body.audio_mode,
        )
    )
    if submission.created:
        _dispatch(dispatcher, submission)
        response.status_code = status.HTTP_202_ACCEPTED
    else:
        response.status_code = status.HTTP_200_OK
    return _submission_payload(application, submission)


@router.get("/{edit_id}/render-variants")
def list_frozen_edit_render_variants(
    edit_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    identifier = FrozenEditId.parse(edit_id)
    edit = application.runs.get_frozen_edit(identifier)
    run = application.runs.get(edit.run_id)
    items = application.renders.list(identifier)
    return {
        "items": [
            {
                "render_variant": render_variant_view(item, edit=edit, run=run),
                "execution": latest_render_execution(
                    application,
                    item.render_variant_id,
                ),
            }
            for item in items
        ],
        "count": len(items),
    }


@project_render_router.get("/{project_id}/render-variants")
def list_project_render_variants(
    project_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    identifier = ProjectId.parse(project_id)
    application.projects.get(identifier)
    items: list[dict[str, object]] = []
    for run in application.runs.list(identifier):
        for edit in application.runs.list_frozen_edits(run.run_id):
            for variant in application.renders.list(edit.edit_id):
                items.append(
                    {
                        "render_variant": render_variant_view(
                            variant,
                            edit=edit,
                            run=run,
                        ),
                        "execution": latest_render_execution(
                            application,
                            variant.render_variant_id,
                        ),
                    }
                )
    items.sort(
        key=lambda item: (
            item["render_variant"]["run_sequence"],
            item["render_variant"]["edit_sequence"],
            item["render_variant"]["created_at"],
        )
    )
    return {"items": items, "count": len(items)}


@render_router.get("/{render_variant_id}")
def get_render_variant(
    render_variant_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    identifier = RenderVariantId.parse(render_variant_id)
    variant = application.renders.get(identifier)
    edit = application.runs.get_frozen_edit(variant.edit_id)
    run = application.runs.get(edit.run_id)
    return {
        "render_variant": render_variant_view(variant, edit=edit, run=run),
        "execution": latest_render_execution(application, identifier),
    }


@render_router.post(
    "/{render_variant_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_render_variant(
    render_variant_id: str,
    application: ApplicationDependency,
    dispatcher: JobDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return _recover(
        "retry",
        render_variant_id,
        application,
        dispatcher,
        command_id,
    )


@render_router.post(
    "/{render_variant_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_render_variant(
    render_variant_id: str,
    application: ApplicationDependency,
    dispatcher: JobDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return _recover(
        "resume",
        render_variant_id,
        application,
        dispatcher,
        command_id,
    )


@render_router.post(
    "/{render_variant_id}/render-again",
    status_code=status.HTTP_202_ACCEPTED,
)
def render_again(
    render_variant_id: str,
    application: ApplicationDependency,
    dispatcher: JobDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return _recover(
        "render_again",
        render_variant_id,
        application,
        dispatcher,
        command_id,
    )


@render_router.post("/{render_variant_id}/verify")
def verify_render_variant(
    render_variant_id: str,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    verified = application.renders.verify_integrity(
        VerifyRenderVariantCommand(
            command_id,
            RenderVariantId.parse(render_variant_id),
        )
    )
    edit = application.runs.get_frozen_edit(verified.render_variant.edit_id)
    run = application.runs.get(edit.run_id)
    return {
        "render_variant": render_variant_view(
            verified.render_variant,
            edit=edit,
            run=run,
        ),
        "integrity": {
            "state": "verified",
            "cached": verified.cached,
            "size_bytes": verified.size_bytes,
        },
    }


@render_router.delete("/{render_variant_id}")
def delete_render_variant(
    render_variant_id: str,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    deleted = application.renders.delete(
        DeleteRenderVariantCommand(
            command_id,
            RenderVariantId.parse(render_variant_id),
        )
    )
    return {
        "render_variant_id": str(deleted.render_variant_id),
        "deleted": deleted.deleted,
    }


@render_router.get("/{render_variant_id}/media")
def get_render_variant_media(
    render_variant_id: str,
    application: ApplicationDependency,
) -> FileResponse:
    identifier = RenderVariantId.parse(render_variant_id)
    lease = application.renders.acquire_media(identifier)
    path, stream, observed = lease.acquire()
    return _LeasedFileResponse(
        path,
        lease=lease,
        stream=stream,
        stat_result=observed,
        media_type=mimetypes.guess_type(path.name)[0] or "video/mp4",
        filename=f"cutmaster-{identifier}{path.suffix}",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


@render_router.get("/{render_variant_id}/download")
def download_render_variant(
    render_variant_id: str,
    application: ApplicationDependency,
) -> FileResponse:
    identifier = RenderVariantId.parse(render_variant_id)
    lease = application.renders.acquire_media(identifier)
    path, stream, observed = lease.acquire()
    return _LeasedFileResponse(
        path,
        lease=lease,
        stream=stream,
        stat_result=observed,
        media_type="video/mp4",
        filename=f"cutmaster-{identifier}.mp4",
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


def _recover(
    mode: str,
    render_variant_id: str,
    application,
    dispatcher,
    command_id: str,
) -> dict[str, object]:
    command = RecoverRenderVariantCommand(
        command_id,
        RenderVariantId.parse(render_variant_id),
    )
    if mode == "retry":
        submission = application.renders.retry(command)
    elif mode == "resume":
        submission = application.renders.resume(command)
    elif mode == "render_again":
        submission = application.renders.render_again(command)
    else:  # pragma: no cover - private total branch
        raise ValueError(f"Unsupported Render recovery mode: {mode}")
    _dispatch(dispatcher, submission)
    return _submission_payload(application, submission)


def _submission_payload(
    application,
    submission: RenderSubmissionView,
) -> dict[str, object]:
    variant = submission.render_variant
    edit = application.runs.get_frozen_edit(variant.edit_id)
    run = application.runs.get(edit.run_id)
    return {
        "render_variant": render_variant_view(variant, edit=edit, run=run),
        "execution": latest_render_execution(
            application,
            variant.render_variant_id,
        ),
        "created": submission.created,
    }


def _create_and_dispatch_preview(
    application,
    dispatcher,
    edit_id: FrozenEditId,
) -> None:
    try:
        command_id = str(
            uuid5(NAMESPACE_URL, f"cutmaster:dialogue-preview:{edit_id}")
        )
        submission = application.renders.create_dialogue_preview(command_id, edit_id)
        if submission.created:
            dispatcher.dispatch(submission)
    except Exception:
        # The immutable Edit is already committed. Preview failure remains an
        # independent Render lifecycle and must never roll back that commit.
        LOGGER.exception("Unable to create or dispatch the default dialogue Preview")


def _dispatch(dispatcher, submission: RenderSubmissionView) -> None:
    try:
        dispatcher.dispatch(submission)
    except RuntimeError as error:
        raise RenderDispatchFailedError(str(error)) from error


__all__ = ["project_render_router", "render_router", "router"]
