"""Cross-stage data contracts."""

from cutmaster.contracts.material import MaterialAnalysisResult
from cutmaster.contracts.workflow import OrchestrationResult, RunRequest

__all__ = ["MaterialAnalysisResult", "OrchestrationResult", "RunRequest"]
