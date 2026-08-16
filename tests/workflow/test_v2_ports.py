from __future__ import annotations

from collections.abc import Mapping

import pytest

from cutmaster.workflow.ports import (
    CancellationToken,
    ProgressReporter,
    ProgressUpdate,
    WorkflowEventSink,
)


class _Cancellation:
    def raise_if_cancelled(self) -> None:
        return None


class _Events:
    def emit(
        self,
        *,
        component: str,
        name: str,
        message: str,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        return None


class _Progress:
    def report(self, update: ProgressUpdate) -> None:
        return None


def test_workflow_ports_are_structural_protocols() -> None:
    assert isinstance(_Cancellation(), CancellationToken)
    assert isinstance(_Events(), WorkflowEventSink)
    assert isinstance(_Progress(), ProgressReporter)


def test_progress_update_validates_only_in_memory_counts() -> None:
    assert ProgressUpdate(3, 10, "Analysing shots", "shot").completed == 3
    with pytest.raises(ValueError, match="must not exceed"):
        ProgressUpdate(11, 10, "Analysing shots", "shot")
    with pytest.raises(ValueError, match="must not be empty"):
        ProgressUpdate(0, None, "", "shot")
