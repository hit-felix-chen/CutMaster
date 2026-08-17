"""Inward Application protocols implemented by outer adapters."""

from cutmaster.application.ports.job_dispatcher import (
    JobDispatcher,
    LifecycleJobSupervisor,
)

__all__ = ["JobDispatcher", "LifecycleJobSupervisor"]
