"""Stable Application-facing execution contracts."""

from cutmaster.contracts.managed_workflow import (
    ExecuteManagedWorkflowCommand,
    ManagedWorkflowResult,
)
from cutmaster.contracts.workflow import ExecuteWorkflowCommand, WorkflowResult

__all__ = [
    "ExecuteManagedWorkflowCommand",
    "ExecuteWorkflowCommand",
    "ManagedWorkflowResult",
    "WorkflowResult",
]
