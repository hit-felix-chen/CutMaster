"""Durable Data Root Migration use cases and read models.

Migration coordination deliberately lives outside the movable Application Data
Root.  Product jobs and migration progress therefore never compete with the
SQLite snapshot being copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from cutmaster.application.ports.data_root import DataRootCoordinator
from cutmaster.configuration.effective import EffectiveConfiguration


class DataRootMigrationStatus(StrEnum):
    REQUESTED = "requested"
    QUIESCING = "quiescing"
    COPYING = "copying"
    VERIFYING = "verifying"
    CANCELLING = "cancelling"
    ROLLING_BACK = "rolling_back"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SWITCHING = "switching"
    RESTART_REQUIRED = "restart_required"
    COMPLETE = "complete"


TERMINAL_MIGRATION_STATUSES = frozenset(
    {
        DataRootMigrationStatus.CANCELLED,
        DataRootMigrationStatus.FAILED,
        DataRootMigrationStatus.COMPLETE,
    }
)


@dataclass(frozen=True)
class DataRootMigrationBlocker:
    kind: str
    detail: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class DataRootMigrationPreflightView:
    source_root: Path
    destination_root: Path
    eligible: bool
    estimated_file_count: int
    estimated_size_bytes: int
    blockers: tuple[DataRootMigrationBlocker, ...]


@dataclass(frozen=True)
class DataRootMigrationView:
    migration_id: str
    source_root: Path
    destination_root: Path
    status: DataRootMigrationStatus
    phase: str
    files_completed: int
    files_total: int | None
    bytes_completed: int
    bytes_total: int | None
    cancel_requested: bool
    blockers: tuple[DataRootMigrationBlocker, ...]
    failure_code: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    worker_id: str | None
    process_id: int | None
    heartbeat_at: datetime | None


class DataRootMigrationError(RuntimeError):
    code = "data_root_migration_error"


class DataRootMigrationNotFoundError(DataRootMigrationError):
    code = "data_root_migration_not_found"


class DataRootMigrationConflictError(DataRootMigrationError):
    code = "data_root_migration_in_progress"


class DataRootMigrationIdempotencyError(DataRootMigrationError):
    code = "idempotency_conflict"


class DataRootMigrationBlockedError(DataRootMigrationError):
    code = "data_root_migration_blocked"

    def __init__(self, blockers: tuple[DataRootMigrationBlocker, ...]) -> None:
        self.blockers = blockers
        super().__init__("Data Root Migration is blocked")


class DataRootMigrationCancellationTooLateError(DataRootMigrationError):
    code = "data_root_migration_cancel_too_late"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalise_destination(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Data Root destination must be an absolute path")
    return candidate.resolve(strict=False)


def _process_is_alive(process_id: object) -> bool:
    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class DataRootMigrationStore:
    """Small external SQLite control plane for one root-location authority."""

    def __init__(self, coordinator: DataRootCoordinator) -> None:
        self._coordinator = coordinator
        self._initialise_lock = threading.RLock()
        self._initialised = False

    def _connect(self) -> sqlite3.Connection:
        self._coordinator.control_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self._coordinator.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        if not self._initialised:
            with self._initialise_lock:
                if not self._initialised:
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
                        CREATE TABLE IF NOT EXISTS data_root_migrations (
                            migration_id TEXT PRIMARY KEY,
                            source_root TEXT NOT NULL,
                            destination_root TEXT NOT NULL,
                            status TEXT NOT NULL,
                            phase TEXT NOT NULL,
                            files_completed INTEGER NOT NULL DEFAULT 0,
                            files_total INTEGER,
                            bytes_completed INTEGER NOT NULL DEFAULT 0,
                            bytes_total INTEGER,
                            cancel_requested INTEGER NOT NULL DEFAULT 0
                                CHECK (cancel_requested IN (0, 1)),
                            blockers_json TEXT NOT NULL DEFAULT '[]'
                                CHECK (json_valid(blockers_json)),
                            failure_code TEXT,
                            failure_message TEXT,
                            worker_id TEXT,
                            process_id INTEGER,
                            heartbeat_at TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            started_at TEXT,
                            finished_at TEXT
                        ) STRICT;
                        CREATE INDEX IF NOT EXISTS data_root_migrations_created_idx
                        ON data_root_migrations(created_at DESC);
                        CREATE TABLE IF NOT EXISTS data_root_migration_receipts (
                            command_id TEXT PRIMARY KEY,
                            command_kind TEXT NOT NULL,
                            request_digest TEXT NOT NULL,
                            migration_id TEXT NOT NULL
                                REFERENCES data_root_migrations(migration_id),
                            created_at TEXT NOT NULL
                        ) STRICT;
                        """
                    )
                    connection.commit()
                    self._initialised = True
        return connection

    def start(
        self,
        *,
        command_id: str,
        source_root: Path,
        destination_root: Path,
    ) -> DataRootMigrationView:
        request = {
            "source_root": str(source_root),
            "destination_root": str(destination_root),
        }
        request_digest = _digest(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT * FROM data_root_migration_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["command_kind"] != "settings.storage.migration.start"
                    or receipt["request_digest"] != request_digest
                ):
                    connection.rollback()
                    raise DataRootMigrationIdempotencyError(command_id)
                row = self._get_row(connection, receipt["migration_id"])
                connection.commit()
                return _migration_view(row)
            active = connection.execute(
                """
                SELECT migration_id FROM data_root_migrations
                WHERE status NOT IN ('cancelled', 'failed', 'complete')
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise DataRootMigrationConflictError(active["migration_id"])
            migration_id = f"drm_{uuid4()}"
            now = _now()
            connection.execute(
                """
                INSERT INTO data_root_migrations (
                    migration_id, source_root, destination_root, status, phase,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'requested', 'admission', ?, ?)
                """,
                (migration_id, str(source_root), str(destination_root), now, now),
            )
            connection.execute(
                """
                INSERT INTO data_root_migration_receipts (
                    command_id, command_kind, request_digest, migration_id,
                    created_at
                ) VALUES (?, 'settings.storage.migration.start', ?, ?, ?)
                """,
                (command_id, request_digest, migration_id, now),
            )
            connection.commit()
            row = self._get_row(connection, migration_id)
        self._coordinator.set_state(
            maintenance=True,
            restart_required=False,
            migration_id=migration_id,
            migration_status=DataRootMigrationStatus.REQUESTED,
        )
        return _migration_view(row)

    def replay_start(
        self,
        *,
        command_id: str,
        source_root: Path,
        destination_root: Path,
    ) -> DataRootMigrationView | None:
        """Replay an admitted command before mutable filesystem preflight.

        A successful migration makes its once-empty destination non-empty.  The
        receipt therefore has to be authoritative before current filesystem
        conditions are evaluated, otherwise an exact retry would incorrectly
        fail admission after the copy has begun or completed.
        """

        request_digest = _digest(
            {
                "source_root": str(source_root),
                "destination_root": str(destination_root),
            }
        )
        with self._connect() as connection:
            receipt = connection.execute(
                "SELECT * FROM data_root_migration_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if receipt is None:
                return None
            if (
                receipt["command_kind"] != "settings.storage.migration.start"
                or receipt["request_digest"] != request_digest
            ):
                raise DataRootMigrationIdempotencyError(command_id)
            return _migration_view(self._get_row(connection, receipt["migration_id"]))

    def current(self) -> DataRootMigrationView | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM data_root_migrations
                ORDER BY created_at DESC, migration_id DESC LIMIT 1
                """
            ).fetchone()
        return None if row is None else _migration_view(row)

    def get(self, migration_id: str) -> DataRootMigrationView:
        with self._connect() as connection:
            return _migration_view(self._get_row(connection, migration_id))

    def request_cancel(
        self,
        *,
        command_id: str,
        migration_id: str,
    ) -> DataRootMigrationView:
        request_digest = _digest({"migration_id": migration_id})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT * FROM data_root_migration_receipts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["command_kind"] != "settings.storage.migration.cancel"
                    or receipt["request_digest"] != request_digest
                ):
                    connection.rollback()
                    raise DataRootMigrationIdempotencyError(command_id)
                row = self._get_row(connection, receipt["migration_id"])
                connection.commit()
                return _migration_view(row)
            row = self._get_row(connection, migration_id)
            status = DataRootMigrationStatus(row["status"])
            if status in {
                DataRootMigrationStatus.SWITCHING,
                DataRootMigrationStatus.RESTART_REQUIRED,
                DataRootMigrationStatus.COMPLETE,
            }:
                connection.rollback()
                raise DataRootMigrationCancellationTooLateError(migration_id)
            now = _now()
            if status not in TERMINAL_MIGRATION_STATUSES:
                connection.execute(
                    """
                    UPDATE data_root_migrations
                    SET cancel_requested = 1, status = 'cancelling',
                        phase = 'cancelling', updated_at = ?
                    WHERE migration_id = ?
                    """,
                    (now, migration_id),
                )
            connection.execute(
                """
                INSERT INTO data_root_migration_receipts (
                    command_id, command_kind, request_digest, migration_id,
                    created_at
                ) VALUES (?, 'settings.storage.migration.cancel', ?, ?, ?)
                """,
                (command_id, request_digest, migration_id, now),
            )
            connection.commit()
            return _migration_view(self._get_row(connection, migration_id))

    def claim_worker(
        self,
        migration_id: str,
        *,
        worker_id: str,
        process_id: int,
        stale_after_sec: float = 45.0,
    ) -> bool:
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_sec)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, migration_id)
            status = DataRootMigrationStatus(row["status"])
            if status in TERMINAL_MIGRATION_STATUSES or status in {
                DataRootMigrationStatus.SWITCHING,
                DataRootMigrationStatus.RESTART_REQUIRED,
            }:
                connection.rollback()
                return False
            heartbeat = row["heartbeat_at"]
            if (
                row["worker_id"] is not None
                and (
                    row["worker_id"] != worker_id
                    or row["process_id"] != process_id
                )
                and (
                    (
                        heartbeat is not None
                        and datetime.fromisoformat(heartbeat) >= cutoff
                    )
                    or _process_is_alive(row["process_id"])
                )
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE data_root_migrations
                SET worker_id = ?, process_id = ?, heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE migration_id = ?
                """,
                (worker_id, process_id, now, now, now, migration_id),
            )
            connection.commit()
        return True

    def reserve_worker_launch(
        self,
        migration_id: str,
        *,
        worker_id: str,
        stale_after_sec: float = 45.0,
    ) -> bool:
        """Atomically reserve one subprocess launch across supervisor peers."""

        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_sec)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, migration_id)
            status = DataRootMigrationStatus(row["status"])
            if status in TERMINAL_MIGRATION_STATUSES or status in {
                DataRootMigrationStatus.SWITCHING,
                DataRootMigrationStatus.RESTART_REQUIRED,
            }:
                connection.rollback()
                return False
            heartbeat = row["heartbeat_at"]
            if row["worker_id"] is not None and (
                (
                    heartbeat is not None
                    and datetime.fromisoformat(heartbeat) >= cutoff
                )
                or _process_is_alive(row["process_id"])
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE data_root_migrations
                SET worker_id = ?, process_id = NULL, heartbeat_at = ?,
                    updated_at = ?
                WHERE migration_id = ?
                """,
                (worker_id, now, now, migration_id),
            )
            connection.commit()
        return True

    def adopt_reserved_worker(
        self,
        migration_id: str,
        *,
        worker_id: str,
        process_id: int,
    ) -> bool:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, migration_id)
            status = DataRootMigrationStatus(row["status"])
            if status in TERMINAL_MIGRATION_STATUSES or status in {
                DataRootMigrationStatus.SWITCHING,
                DataRootMigrationStatus.RESTART_REQUIRED,
            }:
                connection.rollback()
                return False
            if row["worker_id"] != worker_id or row["process_id"] not in {
                None,
                process_id,
            }:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE data_root_migrations
                SET process_id = ?, heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE migration_id = ?
                """,
                (process_id, now, now, now, migration_id),
            )
            connection.commit()
        return True

    def release_worker(
        self,
        migration_id: str,
        *,
        worker_id: str,
        process_id: int,
    ) -> bool:
        """Release a known-dead child without touching a replacement lease."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, migration_id)
            if row["worker_id"] != worker_id or row["process_id"] not in {
                None,
                process_id,
            }:
                connection.rollback()
                return False
            if DataRootMigrationStatus(row["status"]) in TERMINAL_MIGRATION_STATUSES | {
                DataRootMigrationStatus.SWITCHING,
                DataRootMigrationStatus.RESTART_REQUIRED,
            }:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE data_root_migrations
                SET worker_id = NULL, process_id = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE migration_id = ?
                """,
                (_now(), migration_id),
            )
            connection.commit()
        return True

    def recover_switch_before_commit(
        self,
        migration_id: str,
    ) -> DataRootMigrationView:
        """Requeue a verified copy when the process died before pointer commit."""

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_row(connection, migration_id)
            if DataRootMigrationStatus(row["status"]) is not DataRootMigrationStatus.SWITCHING:
                connection.rollback()
                return _migration_view(row)
            connection.execute(
                """
                UPDATE data_root_migrations
                SET status = 'verifying', phase = 'recovering_before_switch',
                    worker_id = NULL, process_id = NULL, heartbeat_at = NULL,
                    updated_at = ?
                WHERE migration_id = ?
                """,
                (now, migration_id),
            )
            connection.commit()
            return _migration_view(self._get_row(connection, migration_id))

    def update(
        self,
        migration_id: str,
        *,
        status: DataRootMigrationStatus | None = None,
        phase: str | None = None,
        files_completed: int | None = None,
        files_total: int | None = None,
        bytes_completed: int | None = None,
        bytes_total: int | None = None,
        blockers: tuple[DataRootMigrationBlocker, ...] | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        terminal: bool = False,
    ) -> DataRootMigrationView:
        assignments = ["heartbeat_at = ?", "updated_at = ?"]
        now = _now()
        values: list[object] = [now, now]
        for column, value in (
            ("status", None if status is None else status.value),
            ("phase", phase),
            ("files_completed", files_completed),
            ("files_total", files_total),
            ("bytes_completed", bytes_completed),
            ("bytes_total", bytes_total),
            ("failure_code", failure_code),
            ("failure_message", failure_message),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if blockers is not None:
            assignments.append("blockers_json = ?")
            values.append(_blockers_json(blockers))
        if terminal:
            assignments.append("finished_at = ?")
            values.append(now)
        values.append(migration_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._get_row(connection, migration_id)
            connection.execute(
                f"UPDATE data_root_migrations SET {', '.join(assignments)} "
                "WHERE migration_id = ?",
                values,
            )
            connection.commit()
            return _migration_view(self._get_row(connection, migration_id))

    def heartbeat(self, migration_id: str) -> None:
        self.update(migration_id)

    def cancel_requested(self, migration_id: str) -> bool:
        with self._connect() as connection:
            row = self._get_row(connection, migration_id)
            return bool(row["cancel_requested"])

    @staticmethod
    def _get_row(connection: sqlite3.Connection, migration_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM data_root_migrations WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        if row is None:
            raise DataRootMigrationNotFoundError(migration_id)
        return row


class DataRootMigrationService:
    """Transport-neutral admission and durable control operations."""

    def __init__(
        self,
        configuration: EffectiveConfiguration,
        coordinator: DataRootCoordinator,
    ) -> None:
        self._configuration = configuration
        self._coordinator = coordinator
        self._store = DataRootMigrationStore(coordinator)

    @property
    def store(self) -> DataRootMigrationStore:
        return self._store

    def preflight(self, destination_root: Path | str) -> DataRootMigrationPreflightView:
        source = self._configuration.data_root.resolve()
        destination = _normalise_destination(destination_root)
        blockers = _lightweight_blockers(
            source,
            destination,
            self._configuration.sources.data_root_pointer_path,
        )
        blockers = (*blockers, *_active_attempt_blockers(source))
        file_count, size_bytes = _estimated_usage(source)
        return DataRootMigrationPreflightView(
            source_root=source,
            destination_root=destination,
            eligible=not blockers,
            estimated_file_count=file_count,
            estimated_size_bytes=size_bytes,
            blockers=blockers,
        )

    def start(
        self,
        *,
        command_id: str,
        destination_root: Path | str,
    ) -> DataRootMigrationView:
        source = self._configuration.data_root.resolve()
        destination = _normalise_destination(destination_root)
        replay = self._store.replay_start(
            command_id=command_id,
            source_root=source,
            destination_root=destination,
        )
        if replay is not None:
            return replay
        preflight = self.preflight(destination)
        if preflight.blockers:
            raise DataRootMigrationBlockedError(preflight.blockers)
        return self._store.start(
            command_id=command_id,
            source_root=preflight.source_root,
            destination_root=preflight.destination_root,
        )

    def current(self) -> DataRootMigrationView | None:
        return self._store.current()

    def get(self, migration_id: str) -> DataRootMigrationView:
        return self._store.get(migration_id)

    def cancel(self, *, command_id: str, migration_id: str) -> DataRootMigrationView:
        return self._store.request_cancel(
            command_id=command_id,
            migration_id=migration_id,
        )


def _lightweight_blockers(
    source: Path,
    destination: Path,
    pointer_path: Path,
) -> tuple[DataRootMigrationBlocker, ...]:
    blockers: list[DataRootMigrationBlocker] = []
    if destination == Path(destination.anchor):
        blockers.append(_blocker("filesystem_root", "Filesystem root is unsafe"))
    if destination == source:
        blockers.append(_blocker("same_root", "Destination is the active Data Root"))
    elif source in destination.parents or destination in source.parents:
        blockers.append(
            _blocker("nested_root", "Source and destination cannot contain each other")
        )
    control_root = pointer_path.parent / ".cutmaster-control"
    if (
        destination == control_root
        or destination in control_root.parents
        or control_root in destination.parents
    ):
        blockers.append(
            _blocker("control_root_overlap", "Destination overlaps stable control data")
        )
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if destination.is_symlink() or not destination.is_dir():
            blockers.append(
                _blocker("unsafe_destination_type", "Destination is not a regular directory")
            )
        else:
            try:
                occupied = next(destination.iterdir(), None)
            except OSError as error:
                blockers.append(_blocker("destination_unreadable", str(error)))
            else:
                if occupied is not None:
                    blockers.append(
                        _blocker("destination_not_empty", "Destination must be empty")
                    )
    return tuple(blockers)


def _active_attempt_blockers(source: Path) -> tuple[DataRootMigrationBlocker, ...]:
    database = source / "cutmaster.db"
    if not database.is_file():
        return ()
    try:
        uri = f"file:{database.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                """
                SELECT attempt_id, status,
                       CASE
                         WHEN material_id IS NOT NULL THEN 'material'
                         WHEN run_id IS NOT NULL THEN 'run'
                         ELSE 'render_variant'
                       END,
                       COALESCE(material_id, run_id, render_variant_id)
                FROM attempts
                WHERE status IN ('running', 'retrying', 'stopping')
                ORDER BY attempt_id
                """
            ).fetchall()
    except sqlite3.Error as error:
        return (
            _blocker(
                "database_unreadable",
                f"Cannot inspect active Attempts: {error}",
            ),
        )
    return tuple(
        _blocker(
            "active_attempt",
            f"Attempt {attempt_id} is {status}",
            attempt_id=attempt_id,
            status=status,
            owner_type=owner_type,
            owner_id=owner_id,
        )
        for attempt_id, status, owner_type, owner_id in rows
    )


def _estimated_usage(source: Path) -> tuple[int, int]:
    if not source.exists():
        return 0, 0
    count = 0
    size = 0
    for root, directories, files in os.walk(source, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if name not in {"materials-backup", "staging"}
        ]
        for name in files:
            path = Path(root) / name
            try:
                metadata = path.lstat()
            except OSError:
                continue
            count += 1
            size += metadata.st_size
    return count, size


def _blocker(kind: str, detail: str, **metadata: Any) -> DataRootMigrationBlocker:
    return DataRootMigrationBlocker(kind, detail, metadata)


def _blockers_json(blockers: tuple[DataRootMigrationBlocker, ...]) -> str:
    return json.dumps(
        [
            {
                "kind": blocker.kind,
                "detail": blocker.detail,
                "metadata": dict(blocker.metadata),
            }
            for blocker in blockers
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _migration_view(row: sqlite3.Row) -> DataRootMigrationView:
    raw_blockers = json.loads(row["blockers_json"])
    blockers = tuple(
        DataRootMigrationBlocker(
            str(value["kind"]),
            str(value["detail"]),
            dict(value.get("metadata", {})),
        )
        for value in raw_blockers
    )
    return DataRootMigrationView(
        migration_id=row["migration_id"],
        source_root=Path(row["source_root"]),
        destination_root=Path(row["destination_root"]),
        status=DataRootMigrationStatus(row["status"]),
        phase=row["phase"],
        files_completed=int(row["files_completed"]),
        files_total=None if row["files_total"] is None else int(row["files_total"]),
        bytes_completed=int(row["bytes_completed"]),
        bytes_total=None if row["bytes_total"] is None else int(row["bytes_total"]),
        cancel_requested=bool(row["cancel_requested"]),
        blockers=blockers,
        failure_code=row["failure_code"],
        failure_message=row["failure_message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        started_at=(
            None if row["started_at"] is None else datetime.fromisoformat(row["started_at"])
        ),
        finished_at=(
            None if row["finished_at"] is None else datetime.fromisoformat(row["finished_at"])
        ),
        worker_id=row["worker_id"],
        process_id=row["process_id"],
        heartbeat_at=(
            None
            if row["heartbeat_at"] is None
            else datetime.fromisoformat(row["heartbeat_at"])
        ),
    )


__all__ = [
    "DataRootMigrationBlockedError",
    "DataRootMigrationBlocker",
    "DataRootMigrationCancellationTooLateError",
    "DataRootMigrationConflictError",
    "DataRootMigrationError",
    "DataRootMigrationIdempotencyError",
    "DataRootMigrationNotFoundError",
    "DataRootMigrationPreflightView",
    "DataRootMigrationService",
    "DataRootMigrationStatus",
    "DataRootMigrationStore",
    "DataRootMigrationView",
    "TERMINAL_MIGRATION_STATUSES",
]
