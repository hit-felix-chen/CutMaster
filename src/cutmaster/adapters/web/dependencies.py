"""FastAPI dependencies shared by Web routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from cutmaster.application import CutMasterApplication
from cutmaster.adapters.web.run_supervisor import RunDispatcher


def application(request: Request) -> CutMasterApplication:
    value = request.app.state.cutmaster_application
    if not isinstance(value, CutMasterApplication):
        raise RuntimeError("CutMaster Application is not configured")
    return value


def run_dispatcher(request: Request) -> RunDispatcher:
    return request.app.state.cutmaster_run_dispatcher


ApplicationDependency = Annotated[CutMasterApplication, Depends(application)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
RunDispatcherDependency = Annotated[RunDispatcher, Depends(run_dispatcher)]


__all__ = [
    "ApplicationDependency",
    "IdempotencyKey",
    "RunDispatcherDependency",
    "application",
    "run_dispatcher",
]
