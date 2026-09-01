"""Pure, handle-only contracts for the CutMaster Workflow."""

from cutmaster.workflow.contracts.analysis import (
    AnalyseMusicRequest,
    AnalyseVideoRequest,
    AnalysisWorkspace,
    MUSIC_ANALYSIS_NODE_IDS,
    MusicAnalysisResult,
    MusicAnalysisOptions,
    VIDEO_ANALYSIS_NODE_IDS,
    VideoAnalysisResult,
    VideoAnalysisOptions,
)
from cutmaster.workflow.contracts.material import (
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
    MaterialRuntimeHandle,
    RenderRuntimeBindings,
)
from cutmaster.workflow.contracts.planners import (
    PlannersBrief,
    PlannersOptions,
    PlannersRequest,
    PlannersResult,
    PlannersWorkspace,
)
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.contracts.rendering import (
    AudioMode,
    RenderOptions,
    RenderOutputTarget,
    RenderRequest,
    RenderResult,
)

__all__ = [
    "AnalyseMusicRequest",
    "AnalyseVideoRequest",
    "AnalysedMusicRuntimeHandle",
    "AnalysedVideoRuntimeHandle",
    "AnalysisWorkspace",
    "AudioMode",
    "MaterialRuntimeHandle",
    "MUSIC_ANALYSIS_NODE_IDS",
    "MusicAnalysisOptions",
    "MusicAnalysisResult",
    "PlannersBrief",
    "PlannersOptions",
    "PlannersRequest",
    "PlannersResult",
    "PlannersWorkspace",
    "RenderOptions",
    "RenderOutputTarget",
    "RenderPlan",
    "RenderRequest",
    "RenderResult",
    "RenderRuntimeBindings",
    "VIDEO_ANALYSIS_NODE_IDS",
    "VideoAnalysisOptions",
    "VideoAnalysisResult",
]
