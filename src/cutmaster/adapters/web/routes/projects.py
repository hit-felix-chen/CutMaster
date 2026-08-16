"""Edit Project queries and synchronous commands."""

from __future__ import annotations

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
from cutmaster.application.projects import (
    CreateProjectCommand,
    DeleteProjectCommand,
    ProjectView,
    RenameProjectCommand,
    SaveCreativeBriefCommand,
    SaveProjectSetupCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.domain.ids import MaterialId, ProjectId

router = APIRouter(prefix="/projects", tags=["projects"])


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
    payload["preview_url"] = (
        f"/api/materials/{ready_video['material_id']}/thumbnail"
        if ready_video is not None
        else None
    )
    return payload


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
