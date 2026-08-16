"""Lazy public API for the complete ASTER Planners stage."""

from __future__ import annotations

from typing import Any

from cutmaster.workflow.contracts.planners import PlannersRequest, PlannersResult


def __getattr__(name: str) -> Any:
    if name == "Planners":
        # Keep Application.open and managed state inspection independent from
        # provider SDKs; the workflow is loaded only by a real planning call.
        from cutmaster.workflow.planners.planners import Planners

        return Planners
    raise AttributeError(name)


__all__ = ["Planners", "PlannersRequest", "PlannersResult"]
