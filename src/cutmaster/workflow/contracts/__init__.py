"""Pure, handle-only contracts for the CutMaster Workflow."""

from cutmaster.workflow.contracts.analysis import (
    AnalyseMusicRequest,
    AnalyseVideoRequest,
    AnalysisWorkspace,
    MusicAnalysisResult,
    MusicAnalysisOptions,
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
    "VideoAnalysisOptions",
    "VideoAnalysisResult",
]
