"""Immutable ASTER Run and Guided Revision use cases."""

from __future__ import annotations

from cutmaster.application.jobs.views import attempt_view, job_view
from cutmaster.application.runs.commands import (
    CompleteRunCommand,
    CreateRevisionCommand,
    CreateRunCommand,
    DeleteRunCommand,
    RecoverRunCommand,
)
from cutmaster.application.runs.views import (
    CompletedRunView,
    DeletedRunView,
    FrozenEditView,
    RunSubmissionView,
    RunView,
    frozen_edit_view,
    run_submission_view,
    run_view,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.domain.ids import AttemptId, FrozenEditId, ProjectId, RunId
from cutmaster.infrastructure.persistence.sqlite import SQLiteApplicationStore


class RunsService:
    """Own ASTER Run and Guided Revision use cases."""

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

    def create(self, command: CreateRunCommand) -> RunSubmissionView:
        if not isinstance(command.project_id, ProjectId):
            raise TypeError("project_id must be a ProjectId")
        return run_submission_view(
            self._store.create_run(
                command.command_id,
                command.project_id,
                self._effective_configuration.to_dict(),
            ).value
        )

    def get(self, run_id: RunId) -> RunView:
        _require_run_id(run_id)
        return run_view(self._store.get_run(run_id))

    def list(self, project_id: ProjectId) -> tuple[RunView, ...]:
        if not isinstance(project_id, ProjectId):
            raise TypeError("project_id must be a ProjectId")
        return tuple(run_view(value) for value in self._store.list_runs(project_id))

    def complete(self, command: CompleteRunCommand) -> CompletedRunView:
        _require_run_id(command.run_id)
        if not isinstance(command.attempt_id, AttemptId):
            raise TypeError("attempt_id must be an AttemptId")
        plan_path = validate_portable_relative_file_path(command.plan_relative_path)
        value = self._store.complete_run(
            command.command_id,
            command.run_id,
            command.attempt_id,
            plan_path,
        ).value
        run = value["run"]
        edit = value["frozen_edit"]
        attempt = value["attempt"]
        job = value["job"]
        if not all(isinstance(item, dict) for item in (run, edit, attempt, job)):
            raise TypeError("Invalid completed Run persistence result")
        return CompletedRunView(
            run=run_view(run),
            frozen_edit=frozen_edit_view(edit),
            attempt=attempt_view(attempt),
            job=job_view(job),
        )

    def create_revision(self, command: CreateRevisionCommand) -> FrozenEditView:
        if not isinstance(command.source_edit_id, FrozenEditId):
            raise TypeError("source_edit_id must be a FrozenEditId")
        plan_path = validate_portable_relative_file_path(command.plan_relative_path)
        return frozen_edit_view(
            self._store.create_revision(
                command.command_id,
                command.source_edit_id,
                plan_path,
            ).value
        )

    def get_frozen_edit(self, edit_id: FrozenEditId) -> FrozenEditView:
        if not isinstance(edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        return frozen_edit_view(self._store.get_frozen_edit(edit_id))

    def list_frozen_edits(self, run_id: RunId) -> tuple[FrozenEditView, ...]:
        _require_run_id(run_id)
        return tuple(
            frozen_edit_view(value)
            for value in self._store.list_frozen_edits(run_id)
        )

    def retry(self, command: RecoverRunCommand) -> RunSubmissionView:
        _require_run_id(command.run_id)
        return run_submission_view(
            self._store.retry_run(
                command.command_id,
                command.run_id,
                resume=False,
            ).value
        )

    def resume(self, command: RecoverRunCommand) -> RunSubmissionView:
        _require_run_id(command.run_id)
        return run_submission_view(
            self._store.retry_run(
                command.command_id,
                command.run_id,
                resume=True,
            ).value
        )

    def delete(self, command: DeleteRunCommand) -> DeletedRunView:
        _require_run_id(command.run_id)
        value = self._store.delete_run(command.command_id, command.run_id).value
        return DeletedRunView(
            run_id=RunId.parse(str(value["run_id"])),
            deleted=bool(value["deleted"]),
        )


def _require_run_id(value: RunId) -> None:
    if not isinstance(value, RunId):
        raise TypeError("run_id must be a RunId")


__all__ = ["RunsService"]
