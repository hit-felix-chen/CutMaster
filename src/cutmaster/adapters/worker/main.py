"""Process entry point for one durable managed CutMaster Job."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from cutmaster.adapters.worker.lease import (
    add_supervisor_lease_arguments,
    adopt_supervisor_lease,
)
from cutmaster.application import CutMasterApplication
from cutmaster.domain.ids import JobId
from cutmaster.infrastructure.observability.logging import configure_console_logging


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one managed CutMaster Job")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    add_supervisor_lease_arguments(parser)
    args = parser.parse_args(argv)

    # The supervisor owns the per-Job log file and redirects this process's
    # stderr into it.  Configure only the structured console sink here so
    # Application and Workflow events are parseable without being duplicated.
    configure_console_logging(console_color=False)
    application = CutMasterApplication.open(args.config)
    job_id = JobId.parse(args.job_id)
    job = application.jobs.get_job(job_id)
    attempt = application.jobs.get_attempt(job.attempt_id)
    exact_supervisor_grant = all(
        value is not None
        for value in (
            args.attempt_id,
            args.lease_worker_id,
            args.lease_process_id,
        )
    )
    with application.data_root_coordinator.shared(
        allow_maintenance=exact_supervisor_grant
    ):
        claimed = adopt_supervisor_lease(
            application,
            job_id=job_id,
            attempt_id=args.attempt_id,
            lease_worker_id=args.lease_worker_id,
            lease_process_id=args.lease_process_id,
            worker_kind=attempt.operation_type,
        )
        if attempt.operation_type == "material_analysis":
            from cutmaster.application.workflow.material_job import (
                execute_material_job,
            )

            result = execute_material_job(
                application,
                job_id,
                claimed_submission=claimed,
            )
        elif attempt.operation_type == "aster_planning":
            from cutmaster.application.workflow.run_job import execute_run_job

            result = execute_run_job(
                application,
                job_id,
                # Preview creation is durable; the supervisor owns processes.
                preview_dispatcher=lambda _submission: None,
                claimed_submission=claimed,
            )
        elif attempt.operation_type == "rendering":
            from cutmaster.application.workflow.render_job import execute_render_job

            result = execute_render_job(
                application,
                job_id,
                claimed_submission=claimed,
            )
        else:
            raise ValueError(
                f"Unsupported managed Job operation: {attempt.operation_type!r}"
            )
    status = None if result is None else getattr(result, "value", None)
    return 0 if status in {None, "complete", "interrupted"} else 1


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    raise SystemExit(main())


__all__ = ["main"]
