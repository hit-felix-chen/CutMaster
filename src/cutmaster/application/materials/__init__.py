"""Managed Material Application use cases."""

from cutmaster.application.materials.execution import (
    ExecuteMaterialAnalysisCommand,
    ManagedMaterialAnalysisExecutor,
    MaterialAnalysisEngine,
    MaterialAnalysisEngineFactory,
    MaterialAnalysisResult,
)
from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.materials.views import (
    MaterialDetailView,
    MaterialMemoryView,
    MaterialPreviewView,
    MaterialView,
)

__all__ = [
    "ExecuteMaterialAnalysisCommand",
    "ManagedMaterialAnalysisExecutor",
    "MaterialAnalysisEngine",
    "MaterialAnalysisEngineFactory",
    "MaterialAnalysisResult",
    "MaterialDetailView",
    "MaterialMemoryView",
    "MaterialPreviewView",
    "MaterialView",
    "MaterialsService",
]
