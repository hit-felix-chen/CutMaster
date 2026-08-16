"""Local stable control database and cross-process Data Root lease."""

from __future__ import annotations

import fcntl
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from cutmaster.application.ports.data_root import (
    DataRootControlState,
    DataRootMaintenanceError,
    DataRootRestartRequiredError,
)
from cutmaster.configuration.effective import EffectiveConfiguration


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LocalDataRootCoordinator:
    """Coordinate every peer using one external root-location authority."""

    def __init__(self, configuration: EffectiveConfiguration) -> None:
        self.configuration = configuration
        self.control_root = (
            configuration.sources.data_root_pointer_path.parent / ".cutmaster-control"
        )
        self.lease_path = self.control_root / "root.lease"
        self.database_path = self.control_root / "migration-control.db"
        self._local = threading.local()
        self._initialise_lock = threading.RLock()
        self._control_initialised = False

    def connect_control(self) -> sqlite3.Connection:
        self.control_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        if not self._control_initialised:
            with self._initialise_lock:
                if not self._control_initialised:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS root_control_state (
                            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                            maintenance INTEGER NOT NULL DEFAULT 0
                                CHECK (maintenance IN (0, 1)),
                            restart_required INTEGER NOT NULL DEFAULT 0
                                CHECK (restart_required IN (0, 1)),
                            migration_id TEXT,
                            migration_status TEXT,
                            updated_at TEXT NOT NULL
                        ) STRICT;
                        INSERT OR IGNORE INTO root_control_state (
                            singleton, maintenance, restart_required, updated_at
                        ) VALUES (1, 0, 0, '1970-01-01T00:00:00+00:00');
                        """
                    )
                    connection.commit()
                    self._control_initialised = True
        return connection

    def state(self) -> DataRootControlState:
        with self.connect_control() as connection:
            row = connection.execute(
                "SELECT * FROM root_control_state WHERE singleton = 1"
            ).fetchone()
        assert row is not None
        return DataRootControlState(
            maintenance=bool(row["maintenance"]),
            restart_required=bool(row["restart_required"]),
            migration_id=row["migration_id"],
            migration_status=row["migration_status"],
        )

    def set_state(
        self,
        *,
        maintenance: bool,
        restart_required: bool,
        migration_id: str | None,
        migration_status: str | None,
    ) -> None:
        with self.connect_control() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE root_control_state
                SET maintenance = ?, restart_required = ?, migration_id = ?,
                    migration_status = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (
                    int(maintenance),
                    int(restart_required),
                    migration_id,
                    migration_status,
                    _utc_now(),
                ),
            )
            connection.commit()

    def _current_pointer_root(self) -> Path:
        pointer = self.configuration.sources.data_root_pointer_path
        try:
            raw = pointer.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return (pointer.parent / ".cutmaster").resolve()
        if not raw:
            raise DataRootRestartRequiredError(self.state().migration_id)
        return Path(raw).expanduser().resolve()

    @contextmanager
    def shared(
        self,
        *,
        allow_maintenance: bool = False,
        verify_generation: bool = True,
    ) -> Iterator[None]:
        depth = int(getattr(self._local, "shared_depth", 0))
        if depth:
            self._local.shared_depth = depth + 1
            try:
                yield
            finally:
                self._local.shared_depth -= 1
            return
        self.control_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lease_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            try:
                state = self.state()
                if state.restart_required:
                    raise DataRootRestartRequiredError(state.migration_id)
                if state.maintenance and not allow_maintenance:
                    raise DataRootMaintenanceError(
                        state.migration_id,
                        state.migration_status,
                    )
                if (
                    verify_generation
                    and self._current_pointer_root()
                    != self.configuration.data_root.resolve()
                ):
                    raise DataRootRestartRequiredError(state.migration_id)
                self._local.shared_depth = 1
                try:
                    yield
                finally:
                    self._local.shared_depth = 0
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def exclusive(self, *, nonblocking: bool = False) -> Iterator[None]:
        self.control_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lease_path.open("a+b") as stream:
            operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(stream.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


__all__ = ["LocalDataRootCoordinator"]
