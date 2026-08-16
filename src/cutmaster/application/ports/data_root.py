"""Application ports for locating and coordinating the managed data root."""

from __future__ import annotations

import functools
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable


class DataRootUnavailableError(RuntimeError):
    code = "data_root_unavailable"


class DataRootMaintenanceError(DataRootUnavailableError):
    code = "data_root_maintenance"

    def __init__(self, migration_id: str | None, status: str | None) -> None:
        self.migration_id = migration_id
        self.status = status
        super().__init__("The Application Data Root is in read-only maintenance mode")


class DataRootRestartRequiredError(DataRootUnavailableError):
    code = "data_root_restart_required"

    def __init__(self, migration_id: str | None = None) -> None:
        self.migration_id = migration_id
        super().__init__("CutMaster must restart before using the migrated Data Root")


@dataclass(frozen=True)
class DataRootControlState:
    maintenance: bool
    restart_required: bool
    migration_id: str | None
    migration_status: str | None


@runtime_checkable
class DataRootCoordinator(Protocol):
    control_root: Path
    database_path: Path

    def state(self) -> DataRootControlState: ...

    def set_state(
        self,
        *,
        maintenance: bool,
        restart_required: bool,
        migration_id: str | None,
        migration_status: str | None,
    ) -> None: ...

    def shared(
        self,
        *,
        allow_maintenance: bool = False,
        verify_generation: bool = True,
    ) -> AbstractContextManager[None]: ...

    def exclusive(
        self,
        *,
        nonblocking: bool = False,
    ) -> AbstractContextManager[None]: ...


_T = TypeVar("_T")


def root_shared_operation(method: Callable[..., _T]) -> Callable[..., _T]:
    """Lease a mutation on services composed with a coordinator."""

    @functools.wraps(method)
    def coordinated(self: Any, *args: Any, **kwargs: Any) -> _T:
        coordinator = getattr(self, "_data_root_coordinator", None)
        if coordinator is None:
            return method(self, *args, **kwargs)
        if not isinstance(coordinator, DataRootCoordinator):
            raise TypeError("Invalid Data Root coordinator")
        with coordinator.shared():
            return method(self, *args, **kwargs)

    return coordinated


def root_shared_context(method: Callable[..., AbstractContextManager[_T]]):
    """Hold the root lease for the lifetime of a returned context manager."""

    @functools.wraps(method)
    @contextmanager
    def coordinated(self: Any, *args: Any, **kwargs: Any):
        coordinator = getattr(self, "_data_root_coordinator", None)
        if coordinator is None:
            with method(self, *args, **kwargs) as value:
                yield value
            return
        if not isinstance(coordinator, DataRootCoordinator):
            raise TypeError("Invalid Data Root coordinator")
        with coordinator.shared(), method(self, *args, **kwargs) as value:
            yield value

    return coordinated


__all__ = [
    "DataRootControlState",
    "DataRootCoordinator",
    "DataRootMaintenanceError",
    "DataRootRestartRequiredError",
    "DataRootUnavailableError",
    "root_shared_operation",
    "root_shared_context",
]
