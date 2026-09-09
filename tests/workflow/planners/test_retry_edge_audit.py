"""Deterministic invocation-level audit of retry and resume boundaries.

Only editorial dependencies are replaced. Every case enters the real public
Planners.plan coordinator and round-trips the real checkpoint contract.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

import cutmaster.workflow.planners.planners as planners_module
from cutmaster.workflow.contracts.checkpoints import (
    PlannersCheckpoint,
    PlannersCheckpointStage,
)
from cutmaster.workflow.planners import Planners
from cutmaster.workflow.planners.tools.errors import (
    GroupNoCandidateError,
    NoFeasiblePathError,
)
from cutmaster.workflow.ports import WorkflowCancelledError

from test_checkpoint_resume import (
    _BoundaryToken,
    _config,
    _install_fake_workflow,
    _request,
)


class _InterruptEveryBoundary:
    def __init__(self, token: _BoundaryToken) -> None:
        self.token = token
        self.document: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []

    def load(self) -> PlannersCheckpoint | None:
        if self.document is None:
            return None
        return PlannersCheckpoint.from_dict(self.document)

    def save(self, checkpoint: PlannersCheckpoint) -> None:
        self.document = json.loads(json.dumps(checkpoint.to_dict()))
        self.history.append(self.document)
        self.token.cancelled = True


@pytest.mark.parametrize(
    ("failures", "terminal_error", "expected"),
    [
        pytest.param(
            {"fail_candidate_attempts": 99},
            GroupNoCandidateError,
            (3, 2, 5, 0),
            id="candidate-empty-all-rounds",
        ),
        pytest.param(
            {"fail_candidate_attempts": 99, "fail_local_repair_attempts": 99},
            GroupNoCandidateError,
            (3, 2, 3, 0),
            id="local-arrangement-impossible",
        ),
        pytest.param(
            {
                "fail_candidate_attempts": 99,
                "fail_local_story_refresh_attempts": 99,
            },
            GroupNoCandidateError,
            (3, 2, 3, 0),
            id="preserved-story-impossible",
        ),
        pytest.param(
            {
                "candidate_failure_groups": (
                    ("group_001",),
                    (),
                    ("group_001",),
                    ("group_001",),
                    ("group_001",),
                ),
                "fail_composition_attempts": 99,
            },
            GroupNoCandidateError,
            (3, 2, 5, 1),
            id="successful-local-then-global-and-candidate-failures",
        ),
        pytest.param(
            {"fail_composition_attempts": 99},
            NoFeasiblePathError,
            (3, 0, 3, 3),
            id="composition-exhaustion-never-local",
        ),
        pytest.param(
            {
                "candidate_failure_groups": (
                    (),
                    (),
                    ("group_001",),
                    ("group_001",),
                    ("group_001",),
                ),
                "fail_composition_attempts": 99,
            },
            GroupNoCandidateError,
            (3, 2, 5, 2),
            id="both-locals-still-available-in-last-global-round",
        ),
    ],
)
def test_repeated_resume_at_every_boundary_preserves_invocation_budgets(
    tmp_path,
    monkeypatch,
    failures,
    terminal_error,
    expected,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(monkeypatch, calls, **failures)
    token = _BoundaryToken()
    store = _InterruptEveryBoundary(token)

    for _resume in range(30):
        token.cancelled = False
        try:
            Planners(_config(tmp_path)).plan(
                request,
                cancellation_token=token,
                checkpoint_store=store,
            )
        except WorkflowCancelledError:
            continue
        except terminal_error:
            break
        else:
            pytest.fail("An exhausted fault sequence must not produce a RenderPlan")
    else:
        pytest.fail("Repeated checkpoint resume restored a consumed retry budget")

    counts = Counter(calls)
    assert (
        counts["arrangement_architect"],
        counts["repair_groups"],
        counts["timeline_scout"],
        counts["edit_composer"],
    ) == expected
    assert counts["profile_music"] == 1
    assert counts["compile_render_plan"] == 0
    assert max(item["aster_attempt"] for item in store.history) == 3
    budgets = [item["local_replan_attempt"] for item in store.history]
    assert budgets == sorted(budgets)
    assert max(budgets) <= 2
    for item in store.history:
        if item["completed_stage"] in {
            PlannersCheckpointStage.ARRANGEMENT.value,
            PlannersCheckpointStage.TIMELINE.value,
            PlannersCheckpointStage.EDIT.value,
        }:
            assert item["replan_scope"] is None
            assert item["replan_reuse"] is None


@pytest.mark.parametrize(
    "stage",
    [
        "arrange",
        "anchor_story",
        "scout",
        "validate_composition",
        "compose",
        "repair_groups",
        "refresh_story_groups",
        "scout_with_reuse",
        "build_script",
    ],
)
@pytest.mark.parametrize("error_type", [ConnectionError, OSError, RuntimeError])
def test_execution_failure_is_not_promoted_to_semantic_retry(
    tmp_path,
    monkeypatch,
    stage,
    error_type,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    local_stage = stage in {
        "repair_groups",
        "refresh_story_groups",
        "scout_with_reuse",
    }
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_candidate_attempts=int(local_stage),
    )
    failure = error_type(f"{stage} execution unavailable")
    injection_count = 0

    def fail_execution(self, *_args, **_kwargs):
        nonlocal injection_count
        injection_count += 1
        raise failure

    monkeypatch.setattr(planners_module.ASTERTeam, stage, fail_execution)

    with pytest.raises(error_type) as captured:
        Planners(_config(tmp_path)).plan(request)

    assert captured.value is failure
    assert injection_count == 1
    assert calls.count("record_failure") == int(local_stage)
    assert calls.count("arrangement_architect") <= 1
    assert calls.count("repair_groups") <= int(local_stage)
    assert calls.count("compile_render_plan") == 0
