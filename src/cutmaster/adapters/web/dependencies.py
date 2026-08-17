"""FastAPI dependencies shared by Web routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from cutmaster.application import CutMasterApplication
from cutmaster.application.ports import JobDispatcher


def application(request: Request) -> CutMasterApplication:
    value = request.app.state.cutmaster_application
    if not isinstance(value, CutMasterApplication):
        raise RuntimeError("CutMaster Application is not configured")
    return value


def job_dispatcher(request: Request) -> JobDispatcher:
    return request.app.state.cutmaster_job_dispatcher


ApplicationDependency = Annotated[CutMasterApplication, Depends(application)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
JobDispatcherDependency = Annotated[JobDispatcher, Depends(job_dispatcher)]


__all__ = [
    "ApplicationDependency",
    "IdempotencyKey",
    "JobDispatcherDependency",
    "application",
    "job_dispatcher",
]
