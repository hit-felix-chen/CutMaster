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


__all__ = [
    "ApplicationError",
    "MaterialMemoryTabNotFoundError",
    "MaterialMemoryUnavailableError",
]
