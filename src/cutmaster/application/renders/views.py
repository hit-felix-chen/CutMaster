"""Read models for managed Render Variants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from cutmaster.application.jobs.views import AttemptView, JobView, attempt_view, job_view
from cutmaster.domain.artifacts import ManagedArtifactReference
from cutmaster.domain.ids import FrozenEditId, RenderVariantId
from cutmaster.domain.renders import RenderVariantStatus


@dataclass(frozen=True)
class RenderVariantView:
    render_variant_id: RenderVariantId
    edit_id: FrozenEditId
    status: RenderVariantStatus
    specification: Mapping[str, Any]
    specification_digest: str
    master: ManagedArtifactReference | None
    master_size_bytes: int | None
    master_sha256: str | None
    frame_count: int | None
    duration_sec: float | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RenderSubmissionView:
    render_variant: RenderVariantView
    attempt: AttemptView | None
    job: JobView | None
    created: bool


@dataclass(frozen=True)
class CompletedRenderView:
    render_variant: RenderVariantView
    attempt: AttemptView
    job: JobView


@dataclass(frozen=True)
class DeletedRenderVariantView:
    render_variant_id: RenderVariantId
    deleted: bool


def render_variant_view(value: Mapping[str, Any]) -> RenderVariantView:
    render_id = RenderVariantId.parse(str(value["render_variant_id"]))
    specification = value["specification"]
    if not isinstance(specification, Mapping):
        raise TypeError("Invalid Render Specification persistence result")
    path = value.get("master_relative_path")
    return RenderVariantView(
        render_variant_id=render_id,
        edit_id=FrozenEditId.parse(str(value["edit_id"])),
        status=RenderVariantStatus(str(value["status"])),
        specification=MappingProxyType(dict(specification)),
        specification_digest=str(value["specification_digest"]),
        master=(
            None
            if path is None
            else ManagedArtifactReference(
                owner_id=str(render_id),
                relative_path=str(path),
            )
        ),
        master_size_bytes=(
            None
            if value.get("master_size_bytes") is None
            else int(value["master_size_bytes"])
        ),
        master_sha256=(
            None if value.get("master_sha256") is None else str(value["master_sha256"])
        ),
        frame_count=(
            None if value.get("frame_count") is None else int(value["frame_count"])
        ),
        duration_sec=(
            None if value.get("duration_sec") is None else float(value["duration_sec"])
        ),
        failure_message=(
            None if value.get("failure_message") is None else str(value["failure_message"])
        ),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
    )


def render_submission_view(value: Mapping[str, Any]) -> RenderSubmissionView:
    variant = value["render_variant"]
    attempt = value.get("attempt")
    job = value.get("job")
    if not isinstance(variant, Mapping):
        raise TypeError("Invalid Render Variant persistence result")
    if (attempt is None) != (job is None):
        raise TypeError("Attempt and job must be returned together")
    return RenderSubmissionView(
        render_variant=render_variant_view(variant),
        attempt=None if attempt is None else attempt_view(attempt),
        job=None if job is None else job_view(job),
        created=bool(value["created"]),
    )


__all__ = [
    "CompletedRenderView",
    "DeletedRenderVariantView",
    "RenderSubmissionView",
    "RenderVariantView",
    "render_submission_view",
    "render_variant_view",
]

