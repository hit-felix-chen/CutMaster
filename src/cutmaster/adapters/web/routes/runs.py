"""Web commands and projections for immutable ASTER Runs."""

from __future__ import annotations

from fastapi import APIRouter, status

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
    RunDispatcherDependency,
)
from cutmaster.adapters.web.presenters import (
    attempt_view,
    frozen_edit_view,
    job_view,
    run_view,
)
from cutmaster.adapters.web.routes._execution import latest_run_execution
from cutmaster.application.runs import CreateRunCommand
from cutmaster.domain.ids import ProjectId, RunId


router = APIRouter(tags=["runs"])


@router.post(
    "/projects/{project_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
)
def start_run(
    project_id: str,
    application: ApplicationDependency,
    dispatcher: RunDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    """Snapshot a saved Project and dispatch its real ASTER planning Job."""

    submission = application.runs.create(
        CreateRunCommand(command_id, ProjectId.parse(project_id))
    )
    dispatcher.dispatch(submission)
    return {
        "run": run_view(submission.run),
        "attempt": attempt_view(submission.attempt),
        "job": job_view(submission.job),
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    identifier = RunId.parse(run_id)
    run = application.runs.get(identifier)
    edits = application.runs.list_frozen_edits(identifier)
    return {
        "run": run_view(run),
        "frozen_edits": [frozen_edit_view(item) for item in edits],
        "execution": latest_run_execution(application, identifier),
    }


__all__ = ["router"]
