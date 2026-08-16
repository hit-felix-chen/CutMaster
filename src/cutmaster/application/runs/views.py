"""Read models for immutable ASTER Run history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from cutmaster.application.jobs.views import (
    AttemptView,
    JobView,
    attempt_view,
    job_view,
)
from cutmaster.application.renders.views import RenderVariantView
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.artifacts import ManagedArtifactReference
from cutmaster.domain.edits import FrozenEditOrigin
from cutmaster.domain.ids import AttemptId, FrozenEditId, MaterialId, ProjectId, RunId
from cutmaster.domain.projects import CreativeBrief
from cutmaster.domain.runs import RunStatus


@dataclass(frozen=True)
class RunView:
    run_id: RunId
    project_id: ProjectId
    sequence: int
    status: RunStatus
    creative_brief: CreativeBrief
    video_material_ids: tuple[MaterialId, ...]
    music_material_ids: tuple[MaterialId, ...]
    configuration: Mapping[str, Any]
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FrozenEditView:
    edit_id: FrozenEditId
    run_id: RunId
    sequence: int
    origin: FrozenEditOrigin
    parent_edit_id: FrozenEditId | None
    plan: ManagedArtifactReference
    created_at: datetime


@dataclass(frozen=True)
class RunSubmissionView:
    run: RunView
    attempt: AttemptView
    job: JobView


@dataclass(frozen=True)
class CompletedRunView:
    run: RunView
    frozen_edit: FrozenEditView
    attempt: AttemptView
    job: JobView


@dataclass(frozen=True)
class DeletedRunView:
    run_id: RunId
    deleted: bool


@dataclass(frozen=True)
class AttemptUsageView:
    attempt_id: AttemptId
    sequence: int
    status: AttemptStatus
    model_usage_summary: Mapping[str, Any] | None


@dataclass(frozen=True)
class RunUsageView:
    attempt_usage: tuple[AttemptUsageView, ...]
    run_total: Mapping[str, Any]


@dataclass(frozen=True)
class FrozenEditReviewView:
    """Transport-neutral projection of one immutable Frozen Edit."""

    edit: FrozenEditView
    run: RunView
    versions: tuple[FrozenEditView, ...]
    plan: Mapping[str, Any]
    video_material_id: MaterialId
    music_material_id: MaterialId
    slots: tuple[Mapping[str, Any], ...]
    candidates: Mapping[str, tuple[Mapping[str, Any], ...]]
    dialogue_cues: tuple[Mapping[str, Any], ...]
    music_beats_sec: tuple[float, ...]
    music_beats_available: bool
    variants: tuple[RenderVariantView, ...]


def run_view(value: Mapping[str, Any]) -> RunView:
    configuration = value["configuration"]
    if not isinstance(configuration, Mapping):
        raise TypeError("Invalid Run configuration persistence result")
    return RunView(
        run_id=RunId.parse(str(value["run_id"])),
        project_id=ProjectId.parse(str(value["project_id"])),
        sequence=int(value["sequence"]),
        status=RunStatus(str(value["status"])),
        creative_brief=CreativeBrief(
            editing_intent=str(value["editing_intent"]),
            target_duration_sec=float(value["target_duration_sec"]),
        ),
        video_material_ids=tuple(
            MaterialId.parse(str(item)) for item in value["video_material_ids"]
        ),
        music_material_ids=tuple(
            MaterialId.parse(str(item)) for item in value["music_material_ids"]
        ),
        configuration=MappingProxyType(dict(configuration)),
        failure_message=(
            None
            if value.get("failure_message") is None
            else str(value["failure_message"])
        ),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
    )


def frozen_edit_view(value: Mapping[str, Any]) -> FrozenEditView:
    edit_id = FrozenEditId.parse(str(value["edit_id"]))
    return FrozenEditView(
        edit_id=edit_id,
        run_id=RunId.parse(str(value["run_id"])),
        sequence=int(value["sequence"]),
        origin=FrozenEditOrigin(str(value["origin"])),
        parent_edit_id=(
            None
            if value.get("parent_edit_id") is None
            else FrozenEditId.parse(str(value["parent_edit_id"]))
        ),
        plan=ManagedArtifactReference(
            owner_id=str(edit_id),
            relative_path=str(value["plan_relative_path"]),
        ),
        created_at=datetime.fromisoformat(str(value["created_at"])),
    )


def run_submission_view(value: Mapping[str, Any]) -> RunSubmissionView:
    run = value["run"]
    attempt = value["attempt"]
    job = value["job"]
    if not all(isinstance(item, Mapping) for item in (run, attempt, job)):
        raise TypeError("Invalid Run submission persistence result")
    return RunSubmissionView(
        run=run_view(run),
        attempt=attempt_view(attempt),
        job=job_view(job),
    )


__all__ = [
    "AttemptUsageView",
    "CompletedRunView",
    "DeletedRunView",
    "FrozenEditView",
    "FrozenEditReviewView",
    "RunSubmissionView",
    "RunUsageView",
    "RunView",
    "frozen_edit_view",
    "run_submission_view",
    "run_view",
]
