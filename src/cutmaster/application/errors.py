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


class MaterialPreviewUnavailableError(ApplicationError):
    """A Material has no safe, lightweight card preview projection."""

    code = "material_preview_unavailable"


class ReviewArtifactUnavailableError(ApplicationError):
    """A Frozen Edit references an absent or invalid managed artifact."""

    code = "review_artifact_unavailable"


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


class RenderFpsMismatchError(ApplicationError):
    """A concrete renderer snapshot cannot execute the Frozen Edit plan."""

    code = "render_fps_mismatch"


class RenderIntegrityMismatchError(ApplicationError):
    """A persisted Ready master no longer matches its private fingerprint."""

    code = "render_integrity_mismatch"


class RenderDispatchFailedError(ApplicationError):
    """The durable Render Attempt exists but its worker could not be launched."""

    code = "render_dispatch_failed"


class ProviderConnectionFailedError(ApplicationError):
    """A bounded provider authentication probe failed."""

    code = "provider_connection_failed"


class StorageRevealUnavailableError(ApplicationError):
    """The host has no supported file-manager reveal capability."""

    code = "storage_reveal_unavailable"


class StorageRevealFailedError(ApplicationError):
    """The host file manager failed to open the Application Data Root."""

    code = "storage_reveal_failed"


__all__ = [
    "AnchorLockedError",
    "ApplicationError",
    "InvalidCandidateReplacementError",
    "MaterialMemoryTabNotFoundError",
    "MaterialMemoryUnavailableError",
    "MaterialPreviewUnavailableError",
    "ProviderConnectionFailedError",
    "RenderDispatchFailedError",
    "RenderFpsMismatchError",
    "RenderIntegrityMismatchError",
    "RenderMediaUnavailableError",
    "ReviewArtifactUnavailableError",
    "RevisionInfeasibleError",
    "StorageRevealFailedError",
    "StorageRevealUnavailableError",
]
