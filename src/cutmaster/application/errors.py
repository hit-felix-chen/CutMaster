"""Transport-neutral errors raised by the CutMaster Application Layer."""


class ApplicationError(Exception):
    """Base class for errors reported by Application use cases."""

    code = "application_error"


class MaterialMemoryUnavailableError(ApplicationError):
    """A Material has no complete, readable Memory projection yet."""

    code = "material_memory_unavailable"


class MaterialMemoryTabNotFoundError(ApplicationError):
    """A requested Memory tab is not defined for the Material Type."""

    code = "material_memory_tab_not_found"


class ReviewArtifactUnavailableError(ApplicationError):
    """A Frozen Edit references an absent or invalid managed artifact."""

    code = "review_artifact_unavailable"


class CandidateSpaceUnavailableError(ApplicationError):
    """Guided Revision cannot proceed without the Run's validated candidates."""

    code = "candidate_space_unavailable"


class AnchorLockedError(ApplicationError):
    """Guided Revision attempted to replace a Story Anchor Slot."""

    code = "anchor_locked"


class InvalidCandidateReplacementError(ApplicationError):
    """A Guided Revision replacement does not belong to its source Slot."""

    code = "invalid_candidate_replacement"


class RevisionInfeasibleError(ApplicationError):
    """A replacement set cannot form one chronological source sequence."""

    code = "revision_infeasible"


class RenderMediaUnavailableError(ApplicationError):
    """A Render Variant has no safe, ready managed master to stream."""

    code = "render_media_unavailable"


__all__ = [
    "AnchorLockedError",
    "ApplicationError",
    "CandidateSpaceUnavailableError",
    "InvalidCandidateReplacementError",
    "MaterialMemoryTabNotFoundError",
    "MaterialMemoryUnavailableError",
    "RenderMediaUnavailableError",
    "ReviewArtifactUnavailableError",
    "RevisionInfeasibleError",
]
