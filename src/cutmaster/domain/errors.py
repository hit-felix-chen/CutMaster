"""Transport-neutral domain errors."""


class DomainError(Exception):
    """Base class for violated CutMaster domain rules."""


class InvalidStateTransition(DomainError):
    """Raised when an entity cannot make the requested state transition."""


__all__ = ["DomainError", "InvalidStateTransition"]
