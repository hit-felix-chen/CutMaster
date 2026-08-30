"""Execution Attempt and durable job use cases."""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType

from cutmaster.application.jobs.commands import (
    AdoptSupervisedJobCommand,
    ClaimJobCommand,
    ClaimSupervisedJobCommand,
    DismissActivityAttemptsCommand,
    EnqueueMaterialAnalysisCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
    InterruptOrphansCommand,
    RecordAttemptUsageCommand,
    ResumeMaterialAnalysisCommand,
    RetryMaterialAnalysisCommand,
    StopAttemptCommand,
)
from cutmaster.application.jobs.views import (
    AttemptLogPageView,
    AttemptView,
    EventBoundsView,
    EventPageView,
    EventView,
    JobSubmissionView,
    JobView,
    attempt_view,
    job_view,
)
from cutmaster.application.jobs.usage import normalize_usage_summary
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.application.ports.data_root import (
    DataRootCoordinator,
    root_shared_operation,
)
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId, MaterialId
from cutmaster.infrastructure.persistence.sqlite import SQLiteApplicationStore


class JobsService:
    """Own Execution Attempt, activity, and job-control use cases."""

    __slots__ = (
        "_data_root_coordinator",
        "_effective_configuration",
        "_store_instance",
    )

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        store: SQLiteApplicationStore | None = None,
        data_root_coordinator: DataRootCoordinator | None = None,
    ) -> None:
        self._effective_configuration = effective_configuration
        self._data_root_coordinator = data_root_coordinator
        self._store_instance = store

    @property
    def _store(self) -> SQLiteApplicationStore:
        store = self._store_instance
        if store is None:
            store = SQLiteApplicationStore(self._effective_configuration.data_root)
            self._store_instance = store
        return store

    @root_shared_operation
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

    @root_shared_operation
    def retry_material_analysis(
        self,
        command: RetryMaterialAnalysisCommand,
    ) -> JobSubmissionView:
        if not isinstance(command.material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        return _submission(
            self._store.requeue_material_analysis(
                command.command_id,
                command.material_id,
                expected_status=AttemptStatus.FAILED,
            ).value
        )

    @root_shared_operation
    def resume_material_analysis(
        self,
        command: ResumeMaterialAnalysisCommand,
    ) -> JobSubmissionView:
        if not isinstance(command.material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        return _submission(
            self._store.requeue_material_analysis(
                command.command_id,
                command.material_id,
                expected_status=AttemptStatus.INTERRUPTED,
            ).value
        )

    @root_shared_operation
    def active_material_attempt_ids(
        self,
        material_id: MaterialId,
    ) -> tuple[AttemptId, ...]:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        return tuple(
            AttemptId.parse(value)
            for value in self._store.active_material_attempt_ids(material_id)
        )

    @root_shared_operation
    def purge_material_history(self, material_id: MaterialId) -> None:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        self._store.purge_material_attempts(material_id)

    @root_shared_operation
    def replay_material_deletion(
        self,
        command_id: str,
        material_id: MaterialId,
    ) -> bool:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        return self._store.replay_material_deletion(command_id, material_id)

    @root_shared_operation
    def record_material_deletion(
        self,
        command_id: str,
        material_id: MaterialId,
    ) -> None:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        self._store.record_material_deletion(command_id, material_id)

    @root_shared_operation
    def claim_next(self, command: ClaimJobCommand) -> JobSubmissionView | None:
        if command.job_id is not None and not isinstance(command.job_id, JobId):
            raise TypeError("job_id must be a JobId or None")
        value = self._store.claim_next_job(
            command.worker_id,
            command.process_id,
            job_id=command.job_id,
        )
        return None if value is None else _submission(value)

    @root_shared_operation
    def claim_next_supervised(
        self,
        command: ClaimSupervisedJobCommand,
    ) -> JobSubmissionView | None:
        value = self._store.claim_next_supervised_job(
            command.worker_id,
            command.process_id,
            max_active_jobs=command.max_active_jobs,
        )
        return None if value is None else _submission(value)

    @root_shared_operation
    def adopt_supervised(
        self,
        command: AdoptSupervisedJobCommand,
    ) -> JobSubmissionView:
        if not isinstance(command.job_id, JobId):
            raise TypeError("job_id must be a JobId")
        _require_attempt_id(command.attempt_id)
        return _submission(
            self._store.adopt_supervised_job(
                command.job_id,
                command.attempt_id,
                expected_worker_id=command.expected_worker_id,
                expected_process_id=command.expected_process_id,
                worker_id=command.worker_id,
                process_id=command.process_id,
            )
        )

    @root_shared_operation
    def heartbeat(self, command: HeartbeatJobCommand) -> JobView:
        if not isinstance(command.job_id, JobId):
            raise TypeError("job_id must be a JobId")
        return job_view(
            self._store.heartbeat_job(
                command.job_id,
                progress=command.progress,
            )
        )

    @root_shared_operation
    def stop(self, command: StopAttemptCommand) -> JobSubmissionView:
        _require_attempt_id(command.attempt_id)
        return _submission(
            self._store.request_stop(
                command.command_id,
                command.attempt_id,
            ).value
        )

    @root_shared_operation
    def dismiss_activity_attempts(
        self,
        command: DismissActivityAttemptsCommand,
    ) -> tuple[AttemptId, ...]:
        if not command.attempt_ids:
            raise ValueError("attempt_ids must not be empty")
        for attempt_id in command.attempt_ids:
            _require_attempt_id(attempt_id)
        value = self._store.dismiss_activity_attempts(
            command.command_id,
            command.attempt_ids,
        ).value
        identifiers = value.get("attempt_ids")
        if not isinstance(identifiers, list):
            raise TypeError("Invalid dismissed Activity result")
        return tuple(AttemptId.parse(str(item)) for item in identifiers)

    @root_shared_operation
    def mark_retrying(self, attempt_id: AttemptId) -> JobSubmissionView:
        _require_attempt_id(attempt_id)
        return _submission(self._store.mark_attempt_retrying(attempt_id))

    @root_shared_operation
    def mark_interrupted(self, attempt_id: AttemptId) -> JobSubmissionView:
        _require_attempt_id(attempt_id)
        return _submission(self._store.mark_attempt_interrupted(attempt_id))

    @root_shared_operation
    def mark_failed(self, command: FailAttemptCommand) -> JobSubmissionView:
        _require_attempt_id(command.attempt_id)
        return _submission(
            self._store.mark_attempt_failed(
                command.attempt_id,
                command.error_message,
            )
        )

    @root_shared_operation
    def record_attempt_usage(
        self,
        command: RecordAttemptUsageCommand,
    ) -> AttemptView:
        _require_attempt_id(command.attempt_id)
        normalized = normalize_usage_summary(command.model_usage_summary)
        return attempt_view(
            self._store.record_attempt_model_usage(
                command.attempt_id,
                normalized,
            )
        )

    @root_shared_operation
    def complete_material_analysis(self, attempt_id: AttemptId) -> JobSubmissionView:
        _require_attempt_id(attempt_id)
        return _submission(self._store.complete_material_attempt(attempt_id))

    @root_shared_operation
    def interrupt_orphans(
        self,
        command: InterruptOrphansCommand,
    ) -> tuple[AttemptId, ...]:
        if not isinstance(command.heartbeat_before, datetime):
            raise TypeError("heartbeat_before must be a datetime")
        return tuple(
            AttemptId.parse(value)
            for value in self._store.interrupt_orphaned_jobs(command.heartbeat_before)
        )

    @root_shared_operation
    def get_attempt(self, attempt_id: AttemptId) -> AttemptView:
        _require_attempt_id(attempt_id)
        return attempt_view(self._store.get_attempt(attempt_id))

    @root_shared_operation
    def get_job(self, job_id: JobId) -> JobView:
        if not isinstance(job_id, JobId):
            raise TypeError("job_id must be a JobId")
        return job_view(self._store.get_job(job_id))

    @root_shared_operation
    def get_job_for_attempt(self, attempt_id: AttemptId) -> JobView:
        _require_attempt_id(attempt_id)
        return job_view(self._store.get_job_for_attempt(attempt_id))

    @root_shared_operation
    def attempt_log(self, attempt_id: AttemptId, *, tail: int = 50) -> AttemptLogPageView:
        _require_attempt_id(attempt_id)
        job = self.get_job_for_attempt(attempt_id)
        return self._log_reader().tail(job.job_id, limit=tail)

    @root_shared_operation
    def full_attempt_log(self, attempt_id: AttemptId) -> AttemptLogPageView:
        _require_attempt_id(attempt_id)
        job = self.get_job_for_attempt(attempt_id)
        return self._log_reader().full(job.job_id)

    @root_shared_operation
    def attempt_log_after(
        self,
        attempt_id: AttemptId,
        *,
        cursor: int,
        limit: int = 200,
    ) -> AttemptLogPageView:
        _require_attempt_id(attempt_id)
        job = self.get_job_for_attempt(attempt_id)
        return self._log_reader().after(job.job_id, cursor=cursor, limit=limit)

    @root_shared_operation
    def attempt_log_info(self, attempt_id: AttemptId) -> tuple[str, bool]:
        _require_attempt_id(attempt_id)
        job = self.get_job_for_attempt(attempt_id)
        path, exists = self._log_reader().info(job.job_id)
        return str(path), exists

    @root_shared_operation
    def activity(
        self,
        *,
        owner_type: str | None = None,
        owner_id: str | None = None,
        statuses: tuple[AttemptStatus, ...] = (),
        include_dismissed: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[AttemptView, ...]:
        return tuple(
            attempt_view(value)
            for value in self._store.list_attempts(
                owner_type=owner_type,
                owner_id=owner_id,
                statuses=statuses,
                include_dismissed=include_dismissed,
                limit=limit,
                offset=offset,
            )
        )

    def _log_reader(self):
        from cutmaster.infrastructure.observability.job_logs import JobLogReader

        return JobLogReader(self._effective_configuration.data_root)

    @root_shared_operation
    def events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> tuple[EventView, ...]:
        return self.event_page(after_event_id=after_event_id, limit=limit).items

    @root_shared_operation
    def event_bounds(self) -> EventBoundsView:
        first_event_id, last_event_id = self._store.event_bounds()
        return EventBoundsView(first_event_id, last_event_id)

    @root_shared_operation
    def event_page(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> EventPageView:
        value = self._store.read_event_page(after_event_id, limit)
        items = value["items"]
        if not isinstance(items, list):
            raise TypeError("Invalid durable event page")
        return EventPageView(
            items=tuple(_event(record) for record in items),
            bounds=EventBoundsView(
                _optional_event_id(value["first_event_id"]),
                _optional_event_id(value["last_event_id"]),
            ),
        )


def _require_attempt_id(value: AttemptId) -> None:
    if not isinstance(value, AttemptId):
        raise TypeError("attempt_id must be an AttemptId")


def _submission(value: dict[str, object]) -> JobSubmissionView:
    attempt = value["attempt"]
    job = value["job"]
    if not isinstance(attempt, dict) or not isinstance(job, dict):
        raise TypeError("Invalid job submission persistence result")
    return JobSubmissionView(attempt=attempt_view(attempt), job=job_view(job))


def _event(value: object) -> EventView:
    if not isinstance(value, dict):
        raise TypeError("Invalid durable event record")
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise TypeError("Invalid durable event payload")
    return EventView(
        event_id=int(value["event_id"]),
        event_type=str(value["event_type"]),
        occurred_at=datetime.fromisoformat(str(value["occurred_at"])),
        object_type=str(value["object_type"]),
        object_id=str(value["object_id"]),
        command_id=(None if value["command_id"] is None else str(value["command_id"])),
        attempt_id=(
            None
            if value["attempt_id"] is None
            else AttemptId.parse(str(value["attempt_id"]))
        ),
        job_id=(None if value["job_id"] is None else JobId.parse(str(value["job_id"]))),
        payload=MappingProxyType(dict(payload)),
        schema_version=str(value["schema_version"]),
    )


def _optional_event_id(value: object) -> int | None:
    if value is None:
        return None
    result = int(value)
    if result <= 0:
        raise TypeError("Durable event IDs must be positive")
    return result


__all__ = ["JobsService"]
