"""Internal ASTER retry state and decision rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cutmaster.workflow.contracts.checkpoints import PlannersReplanScope


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    """The next bounded retry step after one planning-stage failure."""

    aster_attempt: int
    local_attempt: int
    scope: PlannersReplanScope

    @property
    def is_local(self) -> bool:
        return self.scope is PlannersReplanScope.CANDIDATE_LOCAL


@dataclass(slots=True)
class ReplanState:
    """Mutable retry state owned only by one Planners invocation."""

    scope: PlannersReplanScope | None = None
    local_attempt: int = 0
    reuse: dict[str, Any] | None = None
    pending: bool = False

    def clear(self) -> None:
        self.scope = None
        self.local_attempt = 0
        self.reuse = None
        self.pending = False

    def apply(self, decision: ReplanDecision) -> None:
        self.scope = decision.scope
        self.local_attempt = decision.local_attempt
        self.pending = decision.is_local
        if not decision.is_local:
            self.reuse = None


def decide_replan(
    *,
    aster_attempt: int,
    local_attempt: int,
    max_local_attempts: int,
    candidate_failure: bool,
    has_failed_groups: bool,
) -> ReplanDecision:
    """Keep Candidate repair local while its independent budget remains."""

    if (
        candidate_failure
        and has_failed_groups
        and local_attempt < max_local_attempts
    ):
        return ReplanDecision(
            aster_attempt=aster_attempt,
            local_attempt=local_attempt + 1,
            scope=PlannersReplanScope.CANDIDATE_LOCAL,
        )
    return ReplanDecision(
        aster_attempt=aster_attempt + 1,
        local_attempt=0,
        scope=PlannersReplanScope.GLOBAL,
    )


__all__ = ["ReplanDecision", "ReplanState", "decide_replan"]
