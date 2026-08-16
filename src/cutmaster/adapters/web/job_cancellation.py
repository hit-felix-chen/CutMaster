"""Database-backed cooperative cancellation for managed Web workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cutmaster.application.jobs import JobView
from cutmaster.domain.ids import JobId
from cutmaster.workflow.ports import WorkflowCancelledError


class _JobReader(Protocol):
    def get_job(self, job_id: JobId) -> JobView: ...


@dataclass(frozen=True, slots=True)
class DatabaseJobCancellationToken:
    """Poll the durable Stop flag only at explicit Workflow safe points."""

    jobs: _JobReader
    job_id: JobId

    def raise_if_cancelled(self) -> None:
        if self.jobs.get_job(self.job_id).stop_requested:
            raise WorkflowCancelledError(
                f"Managed Job {self.job_id} was stopped at a safe boundary"
            )


__all__ = ["DatabaseJobCancellationToken"]
