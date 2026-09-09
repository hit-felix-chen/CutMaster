"""Edit Project queries and synchronous commands."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from fastapi import APIRouter, Query, Response, status

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
)
from cutmaster.adapters.web.presenters import (
    material_view,
    project_view,
    run_view,
    run_usage_total_view,
)
from cutmaster.adapters.web.routes._execution import latest_run_execution
from cutmaster.adapters.web.schemas.projects import (
    CreateProjectBody,
    RenameProjectBody,
    SaveCreativeBriefBody,
    SaveProjectSetupBody,
    SetProjectMaterialsBody,
)
from cutmaster.application import CutMasterApplication
from cutmaster.application.errors import (
    MaterialPreviewUnavailableError,
    ProjectCoverUnavailableError,
    RenderMediaUnavailableError,
)
from cutmaster.application.materials import MaterialView
from cutmaster.application.projects import (
    CreateProjectCommand,
    DeleteProjectCommand,
    ProjectView,
    RenameProjectCommand,
    SaveCreativeBriefCommand,
    SaveProjectSetupCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.renders import RenderVariantView
from cutmaster.application.runs import RunView
from cutmaster.domain.ids import MaterialId, ProjectId
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.domain.renders import RenderVariantStatus

router = APIRouter(prefix="/projects", tags=["projects"])

_COVER_HEADERS = {
    "Accept-Ranges": "none",
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}


class ProjectSort(StrEnum):
    UPDATED_DESC = "updated_desc"
    UPDATED_ASC = "updated_asc"
    NAME_ASC = "name_asc"
    NAME_DESC = "name_desc"


@router.get("")
def list_projects(
    application: ApplicationDependency,
    search: str = Query(default="", max_length=200),
    sort: ProjectSort = ProjectSort.UPDATED_DESC,
) -> dict[str, object]:
    items = application.projects.list(search=search, sort=sort.value)
    return {
        "items": [_project_card_view(application, item) for item in items],
        "total": len(items),
        "search": search,
        "sort": sort.value,
    }


def _project_card_view(
    application: CutMasterApplication,
    project: ProjectView,
) -> dict[str, object]:
    payload = project_view(project)

    def selected(values: tuple[MaterialId, ...]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for material_id in values:
            material = application.materials.get(material_id)
            if material is not None:
                result.append(material_view(material))
        return result

    runs = application.runs.list(project.project_id)
    latest = max(runs, key=lambda item: item.sequence) if runs else None
    latest_state: str | None = None
    if latest is not None:
        execution = latest_run_execution(application, latest.run_id)
        attempt = execution.get("attempt") if execution is not None else None
        latest_state = (
            str(attempt["status"])
            if isinstance(attempt, dict) and isinstance(attempt.get("status"), str)
            else latest.status.value
        )
    selected_videos = selected(project.video_material_ids)
    selected_music = selected(project.music_material_ids)
    ready_video = next(
        (
            item
            for item in selected_videos
            if item.get("condition") == "ready"
        ),
        None,
    )
    payload["latest_run_state"] = latest_state
    payload["selected_materials"] = {
        "video": selected_videos,
        "music": selected_music,
    }
    latest_render = _latest_ready_render(application, runs)
    payload["preview_url"] = (
        f"/api/projects/{project.project_id}/cover"
        if latest_render is not None or ready_video is not None
        else None
    )
    return payload


def _latest_ready_render(
    application: CutMasterApplication,
    runs: Sequence[RunView],
) -> RenderVariantView | None:
    ready: list[RenderVariantView] = []
    for run in runs:
        for edit in application.runs.list_frozen_edits(run.run_id):
            ready.extend(
                variant
                for variant in application.renders.list(edit.edit_id)
                if variant.status is RenderVariantStatus.READY
            )
    return (
        max(
            ready,
            key=lambda variant: (
                variant.updated_at,
                variant.created_at,
                str(variant.render_variant_id),
            ),
        )
        if ready
        else None
    )


def _first_ready_video(
    application: CutMasterApplication,
    project: ProjectView,
) -> MaterialView | None:
    for material_id in project.video_material_ids:
        material = application.materials.get(material_id)
        if (
            material is not None
            and material.material_type is MaterialType.VIDEO
            and material.condition is MaterialCondition.READY
        ):
            return material
    return None


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    body: CreateProjectBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return project_view(
        application.projects.create(CreateProjectCommand(command_id, body.name))
    )


@router.get("/{project_id}")
def get_project(
    project_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    return project_view(application.projects.get(ProjectId.parse(project_id)))


@router.get("/{project_id}/cover", response_class=Response)
def get_project_cover(
    project_id: str,
    application: ApplicationDependency,
) -> Response:
    """Return the latest Ready master cover or the first Ready Video cover."""

    project = application.projects.get(ProjectId.parse(project_id))
    latest_render = _latest_ready_render(
        application,
        application.runs.list(project.project_id),
    )
    if latest_render is not None:
        try:
            content = application.renders.cover(latest_render.render_variant_id)
        except RenderMediaUnavailableError as error:
            raise ProjectCoverUnavailableError(
                f"Project {project.project_id} has no safe Render cover"
            ) from error
    else:
        video = _first_ready_video(application, project)
        if video is None:
            raise ProjectCoverUnavailableError(
                f"Project {project.project_id} has no Ready Video cover"
            )
        try:
            content = application.materials.preview(video.material_id).content
        except MaterialPreviewUnavailableError as error:
            raise ProjectCoverUnavailableError(
                f"Project {project.project_id} has no safe Video cover"
            ) from error
    return Response(content=content, media_type="image/jpeg", headers=_COVER_HEADERS)


@router.get("/{project_id}/workspace")
def get_project_workspace(
    project_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    identifier = ProjectId.parse(project_id)
    project = application.projects.get(identifier)

    def materials(values: tuple[MaterialId, ...]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for material_id in values:
            material = application.materials.get(material_id)
            if material is not None:
                result.append(material_view(material))
        return result

    runs = application.runs.list(identifier)
    return {
        "project": project_view(project),
        "materials": {
            "video": materials(project.video_material_ids),
            "music": materials(project.music_material_ids),
        },
        "runs": [run_view(item) for item in runs],
    }


@router.get("/{project_id}/runs")
def list_project_runs(
    project_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    """List Run lifecycle and execution state without acquiring Material locks."""

    identifier = ProjectId.parse(project_id)
    runs = application.runs.list(identifier)
    return {
        "items": [
            {
                "run": run_view(run),
                "execution": latest_run_execution(application, run.run_id),
                "model_usage_total": run_usage_total_view(
                    application.runs.usage(run.run_id)
                ),
            }
            for run in runs
        ]
    }


@router.post("/{project_id}/rename")
def rename_project(
    project_id: str,
    body: RenameProjectBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return project_view(
        application.projects.rename(
            RenameProjectCommand(
                command_id,
                ProjectId.parse(project_id),
                body.name,
            )
        )
    )


@router.put("/{project_id}/materials")
def set_project_materials(
    project_id: str,
    body: SetProjectMaterialsBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return project_view(
        application.projects.set_materials(
            SetProjectMaterialsCommand(
                command_id,
                ProjectId.parse(project_id),
                tuple(MaterialId.parse(item) for item in body.video_material_ids),
                tuple(MaterialId.parse(item) for item in body.music_material_ids),
            )
        )
    )


@router.put("/{project_id}/creative-brief")
def save_creative_brief(
    project_id: str,
    body: SaveCreativeBriefBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return project_view(
        application.projects.save_creative_brief(
            SaveCreativeBriefCommand(
                command_id,
                ProjectId.parse(project_id),
                body.editing_intent,
                body.target_duration_sec,
                body.anchor_enabled,
            )
        )
    )


@router.put("/{project_id}/setup")
def save_project_setup(
    project_id: str,
    body: SaveProjectSetupBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    return project_view(
        application.projects.save_setup(
            SaveProjectSetupCommand(
                command_id,
                ProjectId.parse(project_id),
                tuple(MaterialId.parse(item) for item in body.video_material_ids),
                tuple(MaterialId.parse(item) for item in body.music_material_ids),
                body.editing_intent,
                body.target_duration_sec,
                body.anchor_enabled,
            )
        )
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> Response:
    application.projects.delete(
        DeleteProjectCommand(command_id, ProjectId.parse(project_id))
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
