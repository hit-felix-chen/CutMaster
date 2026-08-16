"""FastAPI dependencies shared by Web routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from cutmaster.application import CutMasterApplication
from cutmaster.adapters.web.run_supervisor import RunDispatcher
from cutmaster.adapters.web.material_supervisor import MaterialDispatcher
from cutmaster.adapters.web.render_supervisor import RenderDispatcher


def application(request: Request) -> CutMasterApplication:
    value = request.app.state.cutmaster_application
    if not isinstance(value, CutMasterApplication):
        raise RuntimeError("CutMaster Application is not configured")
    return value


def run_dispatcher(request: Request) -> RunDispatcher:
    return request.app.state.cutmaster_run_dispatcher


def material_dispatcher(request: Request) -> MaterialDispatcher:
    return request.app.state.cutmaster_material_dispatcher


def render_dispatcher(request: Request) -> RenderDispatcher:
    return request.app.state.cutmaster_render_dispatcher


ApplicationDependency = Annotated[CutMasterApplication, Depends(application)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
RunDispatcherDependency = Annotated[RunDispatcher, Depends(run_dispatcher)]
MaterialDispatcherDependency = Annotated[
    MaterialDispatcher,
    Depends(material_dispatcher),
]
RenderDispatcherDependency = Annotated[
    RenderDispatcher,
    Depends(render_dispatcher),
]


__all__ = [
    "ApplicationDependency",
    "IdempotencyKey",
    "MaterialDispatcherDependency",
    "RenderDispatcherDependency",
    "RunDispatcherDependency",
    "application",
    "material_dispatcher",
    "render_dispatcher",
    "run_dispatcher",
]
