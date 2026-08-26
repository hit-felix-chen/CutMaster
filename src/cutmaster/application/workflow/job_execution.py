"""Durable managed Job execution shared by every inbound adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cutmaster.domain.ids import JobId
from cutmaster.infrastructure.observability.logging import capture_log_file

if TYPE_CHECKING:
    from cutmaster.application.cutmaster import CutMasterApplication


class ManagedJobExecutor:
    """Execute already-submitted Jobs through Application-owned state machines."""

    def __init__(self, application: CutMasterApplication) -> None:
        if application is None:
            raise TypeError("application must not be None")
        self._application = application

    def execute_material_job(self, job_id: JobId, **kwargs: Any):
        from cutmaster.application.workflow.material_job import execute_material_job

        with self._capture(job_id):
            return execute_material_job(self._application, job_id, **kwargs)

    def execute_run_job(self, job_id: JobId, **kwargs: Any):
        from cutmaster.application.workflow.run_job import execute_run_job

        with self._capture(job_id):
            return execute_run_job(self._application, job_id, **kwargs)

    def execute_render_job(self, job_id: JobId, **kwargs: Any):
        from cutmaster.application.workflow.render_job import execute_render_job

        with self._capture(job_id):
            return execute_render_job(self._application, job_id, **kwargs)

    def _capture(self, job_id: JobId):
        if not isinstance(job_id, JobId):
            raise TypeError("job_id must be a JobId")
        data_root = self._application.settings.effective_configuration.data_root
        return capture_log_file(
            data_root / "logs" / "jobs" / f"{job_id}.log"
        )


def execute_material_job(
    application: CutMasterApplication,
    job_id: JobId,
    **kwargs: Any,
):
    from cutmaster.application.workflow.material_job import execute_material_job

    return execute_material_job(application, job_id, **kwargs)


def execute_run_job(
    application: CutMasterApplication,
    job_id: JobId,
    **kwargs: Any,
):
    from cutmaster.application.workflow.run_job import execute_run_job

    return execute_run_job(application, job_id, **kwargs)


def execute_render_job(
    application: CutMasterApplication,
    job_id: JobId,
    **kwargs: Any,
):
    from cutmaster.application.workflow.render_job import execute_render_job

    return execute_render_job(application, job_id, **kwargs)


__all__ = [
    "ManagedJobExecutor",
    "execute_material_job",
    "execute_render_job",
    "execute_run_job",
]
