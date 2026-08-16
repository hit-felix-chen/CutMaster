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
    run_usage_view,
)
from cutmaster.adapters.web.routes._execution import latest_run_execution
from cutmaster.application.runs import (
    CreateRunCommand,
    DeleteRunCommand,
    RecoverRunCommand,
    RunAgainCommand,
    RunSubmissionView,
)
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
    return _dispatch_submission(submission, dispatcher)


@router.post(
    "/runs/{run_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_run(
    run_id: str,
    application: ApplicationDependency,
    dispatcher: RunDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    """Create and dispatch another Attempt for one failed Run snapshot."""

    submission = application.runs.retry(
        RecoverRunCommand(command_id, RunId.parse(run_id))
    )
    return _dispatch_submission(submission, dispatcher)


@router.post(
    "/runs/{run_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_run(
    run_id: str,
    application: ApplicationDependency,
    dispatcher: RunDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    """Create and dispatch another Attempt for one interrupted Run snapshot."""

    submission = application.runs.resume(
        RecoverRunCommand(command_id, RunId.parse(run_id))
    )
    return _dispatch_submission(submission, dispatcher)


@router.post(
    "/runs/{run_id}/run-again",
    status_code=status.HTTP_202_ACCEPTED,
)
def run_again(
    run_id: str,
    application: ApplicationDependency,
    dispatcher: RunDispatcherDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    """Create a new Run from one completed immutable historical snapshot."""

    submission = application.runs.run_again(
        RunAgainCommand(command_id, RunId.parse(run_id))
    )
    return _dispatch_submission(submission, dispatcher)


@router.delete("/runs/{run_id}")
def delete_run(
    run_id: str,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    """Permanently delete one terminal Run and its exactly owned artifacts."""

    result = application.runs.delete(DeleteRunCommand(command_id, RunId.parse(run_id)))
    return {"run_id": str(result.run_id), "deleted": result.deleted}


def _dispatch_submission(
    submission: RunSubmissionView,
    dispatcher: RunDispatcherDependency,
) -> dict[str, object]:
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
        "model_usage": run_usage_view(application.runs.usage(identifier)),
    }


__all__ = ["router"]
