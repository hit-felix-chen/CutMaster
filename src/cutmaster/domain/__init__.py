"""Pure CutMaster domain types."""

from cutmaster.domain.artifacts import ManagedArtifactReference
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.events import DomainEvent
from cutmaster.domain.errors import DomainError, InvalidStateTransition
from cutmaster.domain.ids import (
    AttemptId,
    FrozenEditId,
    JobId,
    MaterialId,
    NotificationId,
    ProjectId,
    RenderVariantId,
    RunId,
)
from cutmaster.domain.materials import (
    Material,
    MaterialCondition,
    MaterialFingerprint,
    MaterialType,
)
from cutmaster.domain.projects import CreativeBrief, EditProject

__all__ = [
    "AttemptId",
    "AttemptStatus",
    "CreativeBrief",
    "DomainEvent",
    "DomainError",
    "EditProject",
    "FrozenEditId",
    "InvalidStateTransition",
    "JobId",
    "ManagedArtifactReference",
    "Material",
    "MaterialCondition",
    "MaterialFingerprint",
    "MaterialId",
    "MaterialType",
    "NotificationId",
    "ProjectId",
    "RenderVariantId",
    "RunId",
]
