"""Analyser prompt task definitions."""

from cutmaster.workflow.prompting.analyser.tasks import (
    SCENE_BOUNDARY_PROMPT_VERSION,
    DialogueReconstructionDetails,
    SceneBoundaryDetectionDetails,
    SegmentSummaryDetails,
    SegmentShotAnnotationDetails,
    VideoSummaryDetails,
)

__all__ = [
    "SCENE_BOUNDARY_PROMPT_VERSION",
    "DialogueReconstructionDetails",
    "SceneBoundaryDetectionDetails",
    "SegmentSummaryDetails",
    "SegmentShotAnnotationDetails",
    "VideoSummaryDetails",
]
