"""Frozen Edit Review and Guided Revision HTTP resources."""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from cutmaster.adapters.web.dependencies import ApplicationDependency, IdempotencyKey
from cutmaster.adapters.web.presenters import (
    frozen_edit_review_view,
    frozen_edit_view,
)
from cutmaster.application.runs import (
    CandidateReplacement,
    SaveGuidedRevisionCommand,
)
from cutmaster.domain.ids import FrozenEditId, RenderVariantId


router = APIRouter(prefix="/frozen-edits", tags=["review"])
render_media_router = APIRouter(prefix="/render-variants", tags=["review"])


class CandidateReplacementPayload(BaseModel):
    slot_id: str = Field(min_length=1, max_length=200)
    candidate_id: str = Field(min_length=1, max_length=300)


class SaveRevisionPayload(BaseModel):
    replacements: list[CandidateReplacementPayload] = Field(
        min_length=1,
        max_length=500,
    )


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
    return {
        "frozen_edit": frozen_edit_view(child),
        "review_url": (
            f"/projects/{run.project_id}/runs/{run.run_id}/review/{child.edit_id}"
        ),
    }


@render_media_router.get("/{render_variant_id}/media")
def get_render_variant_media(
    render_variant_id: str,
    application: ApplicationDependency,
) -> FileResponse:
    identifier = RenderVariantId.parse(render_variant_id)
    path = application.renders.media_path(identifier)
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(path.name)[0] or "video/mp4",
        filename=f"cutmaster-{identifier}{path.suffix}",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


__all__ = ["render_media_router", "router"]
