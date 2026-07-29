"""Prompt contracts used by the ASTER planning agents."""

from cutmaster.prompting.planners.tasks import (
    CandidateRetrievalDetails,
    CandidateVisualScoringDetails,
    DialogueAnchorSelectionDetails,
    PairwiseScoringDetails,
    ScriptReviewDetails,
    SlotPlanningDetails,
)

__all__ = [
    "CandidateRetrievalDetails",
    "CandidateVisualScoringDetails",
    "DialogueAnchorSelectionDetails",
    "PairwiseScoringDetails",
    "ScriptReviewDetails",
    "SlotPlanningDetails",
]
