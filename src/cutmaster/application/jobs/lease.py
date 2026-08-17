"""Validation helpers for already-claimed durable Job submissions."""

from __future__ import annotations

from cutmaster.application.jobs.views import JobSubmissionView
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import JobId


def validate_claimed_submission(
    submission: JobSubmissionView,
    job_id: JobId,
) -> None:
    if not isinstance(submission, JobSubmissionView):
        raise TypeError("claimed_submission must be a JobSubmissionView")
    if (
        submission.job.job_id != job_id
        or submission.job.attempt_id != submission.attempt.attempt_id
        or submission.attempt.status is not AttemptStatus.RUNNING
        or submission.job.status is not AttemptStatus.RUNNING
    ):
        raise ValueError("claimed_submission does not own the exact active Job")


__all__ = ["validate_claimed_submission"]
