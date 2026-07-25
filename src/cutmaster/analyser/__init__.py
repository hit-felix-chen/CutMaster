"""Source-video analysis public API."""

from cutmaster.analyser.contracts import MaterialAnalysisResult
from cutmaster.analyser.service import analyse_video_material

__all__ = [
    "MaterialAnalysisResult",
    "analyse_video_material",
]
