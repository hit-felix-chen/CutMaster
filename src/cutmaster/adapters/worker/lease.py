"""Validated hand-off from a local supervisor to one worker subprocess."""

from __future__ import annotations

import argparse
import os

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import AdoptSupervisedJobCommand, JobSubmissionView
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId


def add_supervisor_lease_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--attempt-id")
    parser.add_argument("--lease-worker-id")
    parser.add_argument("--lease-process-id", type=int)


def adopt_supervisor_lease(
    application: CutMasterApplication,
    *,
    job_id: JobId,
    attempt_id: str | None,
    lease_worker_id: str | None,
    lease_process_id: int | None,
    worker_kind: str,
) -> JobSubmissionView | None:
    supplied = (attempt_id, lease_worker_id, lease_process_id)
    if all(value is None for value in supplied):
        return None
    if any(value is None for value in supplied):
        raise ValueError("Supervisor lease arguments must be supplied together")
    assert attempt_id is not None
    assert lease_worker_id is not None
    assert lease_process_id is not None
    adopted = application.jobs.adopt_supervised(
        AdoptSupervisedJobCommand(
            job_id=job_id,
            attempt_id=AttemptId.parse(attempt_id),
            expected_worker_id=lease_worker_id,
            expected_process_id=lease_process_id,
            worker_id=f"worker-{worker_kind}-{os.getpid()}",
            process_id=os.getpid(),
        )
    )
    if (
        adopted.job.job_id != job_id
        or adopted.job.attempt_id != adopted.attempt.attempt_id
        or adopted.attempt.status is not AttemptStatus.RUNNING
        or adopted.job.status is not AttemptStatus.RUNNING
    ):
        raise RuntimeError("Adopted worker lease is not an active exact Job")
    return adopted


__all__ = ["add_supervisor_lease_arguments", "adopt_supervisor_lease"]
