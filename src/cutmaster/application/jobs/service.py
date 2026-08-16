"""Execution Attempt and durable job use cases."""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType

from cutmaster.application.jobs.commands import (
    ClaimJobCommand,
    EnqueueMaterialAnalysisCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
    InterruptOrphansCommand,
    StopAttemptCommand,
)
from cutmaster.application.jobs.views import (
    AttemptView,
    EventView,
    JobSubmissionView,
    JobView,
    attempt_view,
    job_view,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId, MaterialId
from cutmaster.infrastructure.persistence.sqlite import SQLiteApplicationStore


class JobsService:
    """Own Execution Attempt, activity, and job-control use cases."""

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

    def enqueue_material_analysis(
        self,
        command: EnqueueMaterialAnalysisCommand,
    ) -> JobSubmissionView:
        if not isinstance(command.material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        value = self._store.enqueue_material_analysis(
            command.command_id,
            command.material_id,
        ).value
        return _submission(value)

    def claim_next(self, command: ClaimJobCommand) -> JobSubmissionView | None:
        if command.job_id is not None and not isinstance(command.job_id, JobId):
            raise TypeError("job_id must be a JobId or None")
        value = self._store.claim_next_job(
            command.worker_id,
            command.process_id,
            job_id=command.job_id,
        )
        return None if value is None else _submission(value)

    def heartbeat(self, command: HeartbeatJobCommand) -> JobView:
        if not isinstance(command.job_id, JobId):
            raise TypeError("job_id must be a JobId")
        return job_view(
            self._store.heartbeat_job(
                command.job_id,
                progress=command.progress,
            )
        )

    def stop(self, command: StopAttemptCommand) -> JobSubmissionView:
        _require_attempt_id(command.attempt_id)
        return _submission(
            self._store.request_stop(
                command.command_id,
                command.attempt_id,
            ).value
        )

    def mark_retrying(self, attempt_id: AttemptId) -> JobSubmissionView:
        _require_attempt_id(attempt_id)
        return _submission(self._store.mark_attempt_retrying(attempt_id))

    def mark_interrupted(self, attempt_id: AttemptId) -> JobSubmissionView:
        _require_attempt_id(attempt_id)
        return _submission(self._store.mark_attempt_interrupted(attempt_id))

    def mark_failed(self, command: FailAttemptCommand) -> JobSubmissionView:
        _require_attempt_id(command.attempt_id)
        return _submission(
            self._store.mark_attempt_failed(
                command.attempt_id,
                command.error_message,
            )
        )

    def complete_material_analysis(self, attempt_id: AttemptId) -> JobSubmissionView:
        _require_attempt_id(attempt_id)
        return _submission(self._store.complete_material_attempt(attempt_id))

    def interrupt_orphans(
        self,
        command: InterruptOrphansCommand,
    ) -> tuple[AttemptId, ...]:
        if not isinstance(command.heartbeat_before, datetime):
            raise TypeError("heartbeat_before must be a datetime")
        return tuple(
            AttemptId.parse(value)
            for value in self._store.interrupt_orphaned_jobs(
                command.heartbeat_before
            )
        )

    def get_attempt(self, attempt_id: AttemptId) -> AttemptView:
        _require_attempt_id(attempt_id)
        return attempt_view(self._store.get_attempt(attempt_id))

    def get_job(self, job_id: JobId) -> JobView:
        if not isinstance(job_id, JobId):
            raise TypeError("job_id must be a JobId")
        return job_view(self._store.get_job(job_id))

    def get_job_for_attempt(self, attempt_id: AttemptId) -> JobView:
        _require_attempt_id(attempt_id)
        return job_view(self._store.get_job_for_attempt(attempt_id))

    def activity(
        self,
        *,
        owner_type: str | None = None,
        owner_id: str | None = None,
        statuses: tuple[AttemptStatus, ...] = (),
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[AttemptView, ...]:
        return tuple(
            attempt_view(value)
            for value in self._store.list_attempts(
                owner_type=owner_type,
                owner_id=owner_id,
                statuses=statuses,
                limit=limit,
                offset=offset,
            )
        )

    def events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> tuple[EventView, ...]:
        result: list[EventView] = []
        for value in self._store.list_events(after_event_id, limit):
            payload = value["payload"]
            if not isinstance(payload, dict):
                raise TypeError("Invalid durable event payload")
            result.append(
                EventView(
                    event_id=int(value["event_id"]),
                    event_type=str(value["event_type"]),
                    occurred_at=datetime.fromisoformat(str(value["occurred_at"])),
                    object_type=str(value["object_type"]),
                    object_id=str(value["object_id"]),
                    command_id=(
                        None
                        if value["command_id"] is None
                        else str(value["command_id"])
                    ),
                    attempt_id=(
                        None
                        if value["attempt_id"] is None
                        else AttemptId.parse(str(value["attempt_id"]))
                    ),
                    job_id=(
                        None
                        if value["job_id"] is None
                        else JobId.parse(str(value["job_id"]))
                    ),
                    payload=MappingProxyType(payload),
                    schema_version=str(value["schema_version"]),
                )
            )
        return tuple(result)


def _require_attempt_id(value: AttemptId) -> None:
    if not isinstance(value, AttemptId):
        raise TypeError("attempt_id must be an AttemptId")


def _submission(value: dict[str, object]) -> JobSubmissionView:
    attempt = value["attempt"]
    job = value["job"]
    if not isinstance(attempt, dict) or not isinstance(job, dict):
        raise TypeError("Invalid job submission persistence result")
    return JobSubmissionView(attempt=attempt_view(attempt), job=job_view(job))


__all__ = ["JobsService"]
