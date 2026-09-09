"""Prompt contracts used by the ASTER team."""

from cutmaster.workflow.prompting.planners.tasks import (
    CandidateRetrievalDetails,
    CandidateVisualScoringDetails,
    DialogueAnchorSelectionDetails,
    PairwiseScoringDetails,
    SlotArrangementDetails,
    candidate_trajectory_contract,
)

__all__ = [
    "CandidateRetrievalDetails",
    "CandidateVisualScoringDetails",
    "DialogueAnchorSelectionDetails",
    "PairwiseScoringDetails",
    "SlotArrangementDetails",
    "candidate_trajectory_contract",
]
