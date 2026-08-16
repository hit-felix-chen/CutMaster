"""Verified, recoverable copy engine for Data Root Migration."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cutmaster.application.ports.data_root import DataRootCoordinator
from cutmaster.application.settings.data_root_migration import (
    DataRootMigrationBlocker,
    DataRootMigrationStatus,
    DataRootMigrationStore,
    DataRootMigrationView,
    TERMINAL_MIGRATION_STATUSES,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.infrastructure.persistence.sqlite.migrations import (
    LATEST_SCHEMA_VERSION,
)
from cutmaster.infrastructure.observability.logging import error_summary


_CANONICAL_DIRECTORIES = ("media", "projects", "direct", "logs")
_TRANSIENT_DIRECTORIES = frozenset({"staging"})
_HISTORICAL_DIRECTORIES = frozenset({"materials-backup"})
_HISTORICAL_EMPTY_FILES = frozenset({"application.sqlite3", "cutmaster.sqlite3"})
_DATABASE_FILES = frozenset({"cutmaster.db", "cutmaster.db-wal", "cutmaster.db-shm"})
_OWNER_MARKER = ".cutmaster-root-owner.json"
_CHUNK_SIZE = 4 * 1024 * 1024


class DataRootMigrationCancelled(RuntimeError):
    pass


class DataRootMigrationValidationError(RuntimeError):
    def __init__(self, blockers: tuple[DataRootMigrationBlocker, ...]) -> None:
        self.blockers = blockers
        super().__init__("Data Root Migration validation failed")


@dataclass(frozen=True)
class _FileEntry:
    relative_path: str
    size_bytes: int
    mode: int


class LocalDataRootMigrator:
    """Copy one immutable source generation and commit its external pointer."""

    def __init__(
        self,
        configuration: EffectiveConfiguration,
        coordinator: DataRootCoordinator,
        store: DataRootMigrationStore,
    ) -> None:
        self._configuration = configuration
        self._coordinator = coordinator
        self._store = store

    def execute(self, migration_id: str) -> DataRootMigrationView:
        migration = self._store.get(migration_id)
        switched = False
        destination_owned = False
        try:
            self._coordinator.set_state(
                maintenance=True,
                restart_required=False,
                migration_id=migration_id,
                migration_status=DataRootMigrationStatus.QUIESCING,
            )
            self._store.update(
                migration_id,
                status=DataRootMigrationStatus.QUIESCING,
                phase="waiting_for_root_lease",
            )
            with self._exclusive_cancelable(migration_id):
                migration = self._store.get(migration_id)
                if migration.status in TERMINAL_MIGRATION_STATUSES or migration.status in {
                    DataRootMigrationStatus.SWITCHING,
                    DataRootMigrationStatus.RESTART_REQUIRED,
                }:
                    return migration
                self._require_not_cancelled(migration_id)
                self._verify_source_generation(migration)
                self._require_no_active_attempts(migration.source_root)
                entries, directories = self._scan_source(
                    migration.source_root,
                    migration.migration_id,
                )
                destination_preexisted = migration.destination_root.exists()
                self._prepare_destination(migration)
                destination_owned = True
                manifest = self._load_or_create_manifest(
                    migration,
                    destination_preexisted=destination_preexisted,
                    directories=directories,
                )
                db_size = (
                    migration.source_root.joinpath("cutmaster.db").stat().st_size
                    if migration.source_root.joinpath("cutmaster.db").is_file()
                    else 0
                )
                total_bytes = db_size + sum(entry.size_bytes for entry in entries)
                total_files = (1 if db_size else 0) + len(entries)
                self._store.update(
                    migration_id,
                    status=DataRootMigrationStatus.COPYING,
                    phase="copying",
                    files_total=total_files,
                    bytes_total=total_bytes,
                )
                for relative in directories:
                    target = migration.destination_root.joinpath(*relative.split("/"))
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                files_completed = 0
                bytes_completed = 0
                source_queued = 0
                if db_size:
                    source_queued = self._copy_database(migration, manifest)
                    files_completed += 1
                    copied_db_size = migration.destination_root.joinpath(
                        "cutmaster.db"
                    ).stat().st_size
                    bytes_completed += copied_db_size
                    self._progress(
                        migration_id,
                        files_completed,
                        bytes_completed,
                    )
                for entry in entries:
                    self._require_not_cancelled(migration_id)
                    digest = self._copy_file(migration, entry)
                    manifest["files"][entry.relative_path] = {
                        "size_bytes": entry.size_bytes,
                        "sha256": digest,
                        "mode": entry.mode,
                    }
                    self._write_manifest(migration_id, manifest)
                    files_completed += 1
                    bytes_completed += entry.size_bytes
                    self._progress(migration_id, files_completed, bytes_completed)
                self._store.update(
                    migration_id,
                    status=DataRootMigrationStatus.VERIFYING,
                    phase="verifying",
                )
                self._verify_destination(
                    migration,
                    manifest,
                    source_queued=source_queued,
                )
                self._verify_no_unmanifested_entries(migration, manifest)
                self._require_not_cancelled(migration_id)
                self._store.update(
                    migration_id,
                    status=DataRootMigrationStatus.SWITCHING,
                    phase="switching_pointer",
                )
                self._coordinator.set_state(
                    maintenance=True,
                    restart_required=True,
                    migration_id=migration_id,
                    migration_status=DataRootMigrationStatus.SWITCHING,
                )
                self._switch_pointer(migration.destination_root)
                switched = True
                result = self._store.update(
                    migration_id,
                    status=DataRootMigrationStatus.RESTART_REQUIRED,
                    phase="restart_required",
                )
                self._coordinator.set_state(
                    maintenance=True,
                    restart_required=True,
                    migration_id=migration_id,
                    migration_status=DataRootMigrationStatus.RESTART_REQUIRED,
                )
                return result
        except DataRootMigrationCancelled:
            if not switched:
                cleanup_blockers = self._rollback(migration, destination_owned)
                result = self._store.update(
                    migration_id,
                    status=DataRootMigrationStatus.CANCELLED,
                    phase="cancelled",
                    blockers=cleanup_blockers,
                    terminal=True,
                )
                self._coordinator.set_state(
                    maintenance=False,
                    restart_required=False,
                    migration_id=migration_id,
                    migration_status=DataRootMigrationStatus.CANCELLED,
                )
                return result
            raise
        except Exception as error:
            if switched:
                # Pointer replacement is the commit point.  Recovery must move
                # forward and startup will finalise this verified destination.
                self._coordinator.set_state(
                    maintenance=True,
                    restart_required=True,
                    migration_id=migration_id,
                    migration_status=DataRootMigrationStatus.RESTART_REQUIRED,
                )
                return self._store.update(
                    migration_id,
                    status=DataRootMigrationStatus.RESTART_REQUIRED,
                    phase="restart_required",
                    failure_code="post_switch_recovery",
                    failure_message=error_summary(error) or type(error).__name__,
                )
            blockers = (
                error.blockers
                if isinstance(error, DataRootMigrationValidationError)
                else ()
            )
            cleanup_blockers = self._rollback(migration, destination_owned)
            blockers = (*blockers, *cleanup_blockers)
            result = self._store.update(
                migration_id,
                status=DataRootMigrationStatus.FAILED,
                phase="failed",
                blockers=blockers,
                failure_code=(
                    "migration_validation_failed"
                    if blockers
                    else "migration_execution_failed"
                ),
                failure_message=error_summary(error) or type(error).__name__,
                terminal=True,
            )
            self._coordinator.set_state(
                maintenance=False,
                restart_required=False,
                migration_id=migration_id,
                migration_status=DataRootMigrationStatus.FAILED,
            )
            return result

    def _exclusive_cancelable(self, migration_id: str):
        migrator = self

        class _Lease:
            lease: Any = None

            def __enter__(self) -> None:
                while True:
                    migrator._require_not_cancelled(migration_id)
                    try:
                        self.lease = migrator._coordinator.exclusive(nonblocking=True)
                        self.lease.__enter__()
                        return None
                    except BlockingIOError:
                        self.lease = None
                        migrator._store.heartbeat(migration_id)
                        time.sleep(0.1)

            def __exit__(self, *args: object) -> None:
                if self.lease is not None:
                    self.lease.__exit__(*args)

        return _Lease()

    def _verify_source_generation(self, migration: DataRootMigrationView) -> None:
        if migration.source_root.resolve() != self._configuration.data_root.resolve():
            raise RuntimeError("Migration source no longer matches this Application")

    def _require_no_active_attempts(self, source_root: Path) -> None:
        database = source_root / "cutmaster.db"
        if not database.is_file():
            return
        uri = f"file:{database.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                """
                SELECT attempt_id, status FROM attempts
                WHERE status IN ('running', 'retrying', 'stopping')
                ORDER BY attempt_id
                """
            ).fetchall()
        if rows:
            blockers = tuple(
                DataRootMigrationBlocker(
                    "active_attempt",
                    f"Attempt {attempt_id} is {status}",
                    {"attempt_id": attempt_id, "status": status},
                )
                for attempt_id, status in rows
            )
            raise DataRootMigrationValidationError(blockers)

    def _scan_source(
        self,
        source: Path,
        migration_id: str,
    ) -> tuple[list[_FileEntry], list[str]]:
        if not source.exists():
            return [], []
        blockers: list[DataRootMigrationBlocker] = []
        for child in source.iterdir():
            name = child.name
            metadata = child.lstat()
            if name in _CANONICAL_DIRECTORIES:
                if not stat.S_ISDIR(metadata.st_mode) or child.is_symlink():
                    blockers.append(_unsafe_entry(name))
            elif name in _TRANSIENT_DIRECTORIES | _HISTORICAL_DIRECTORIES:
                continue
            elif name in _DATABASE_FILES:
                if not stat.S_ISREG(metadata.st_mode) or child.is_symlink():
                    blockers.append(_unsafe_entry(name))
            elif name in _HISTORICAL_EMPTY_FILES:
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
                    blockers.append(
                        DataRootMigrationBlocker(
                            "historical_file_not_empty",
                            f"Historical file {name} is not an empty legacy marker",
                            {"relative_path": name},
                        )
                    )
            elif name == _OWNER_MARKER:
                if not self._valid_source_owner_marker(child):
                    blockers.append(
                        DataRootMigrationBlocker(
                            "invalid_root_owner_marker",
                            "Application Data Root ownership marker is invalid",
                            {"relative_path": name},
                        )
                    )
            else:
                blockers.append(
                    DataRootMigrationBlocker(
                        "unknown_namespace",
                        f"Unknown Data Root namespace: {name}",
                        {"relative_path": name},
                    )
                )
        entries: list[_FileEntry] = []
        directories: list[str] = []
        inspected = 0
        for namespace in _CANONICAL_DIRECTORIES:
            base = source / namespace
            try:
                base_metadata = base.lstat()
            except FileNotFoundError:
                continue
            if base.is_symlink() or not stat.S_ISDIR(base_metadata.st_mode):
                continue
            for root, names, files in os.walk(base, followlinks=False):
                root_path = Path(root)
                relative_root = root_path.relative_to(source).as_posix()
                directories.append(relative_root)
                for name in list(names):
                    path = root_path / name
                    metadata = path.lstat()
                    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                        blockers.append(_unsafe_entry(path.relative_to(source).as_posix()))
                        names.remove(name)
                for name in files:
                    path = root_path / name
                    metadata = path.lstat()
                    relative = path.relative_to(source).as_posix()
                    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                        blockers.append(_unsafe_entry(relative))
                        continue
                    entries.append(
                        _FileEntry(
                            relative,
                            metadata.st_size,
                            stat.S_IMODE(metadata.st_mode),
                        )
                    )
                    inspected += 1
                    if inspected % 256 == 0:
                        self._heartbeat_boundary(migration_id)
        if blockers:
            raise DataRootMigrationValidationError(tuple(blockers))
        entries.sort(key=lambda value: value.relative_path)
        directories.sort()
        return entries, directories

    def _prepare_destination(self, migration: DataRootMigrationView) -> None:
        destination = migration.destination_root
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise DataRootMigrationValidationError((_unsafe_entry(str(destination)),))
            existing = list(destination.iterdir())
            if existing:
                marker = destination / _OWNER_MARKER
                if not marker.is_file() or not self._marker_matches(marker, migration):
                    raise DataRootMigrationValidationError(
                        (
                            DataRootMigrationBlocker(
                                "destination_not_empty",
                                "Destination is not empty or owned by this migration",
                                {},
                            ),
                        )
                    )
        else:
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        marker = destination / _OWNER_MARKER
        if not marker.exists():
            self._atomic_json(
                marker,
                {
                    "schema_version": "1.0",
                    "migration_id": migration.migration_id,
                    "source_root": str(migration.source_root),
                },
                exclusive=True,
            )

    def _marker_matches(self, marker: Path, migration: DataRootMigrationView) -> bool:
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return value.get("migration_id") == migration.migration_id

    def _manifest_path(self, migration_id: str) -> Path:
        path = self._coordinator.control_root / "migrations" / migration_id
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path / "manifest.json"

    def _load_or_create_manifest(
        self,
        migration: DataRootMigrationView,
        *,
        destination_preexisted: bool,
        directories: list[str],
    ) -> dict[str, Any]:
        path = self._manifest_path(migration.migration_id)
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("migration_id") != migration.migration_id:
                raise RuntimeError("Migration manifest identity mismatch")
            return value
        value: dict[str, Any] = {
            "schema_version": "1.0",
            "migration_id": migration.migration_id,
            "source_root": str(migration.source_root),
            "destination_root": str(migration.destination_root),
            "destination_preexisted": destination_preexisted,
            "directories": directories,
            "current_partial": None,
            "database": None,
            "files": {},
        }
        self._write_manifest(migration.migration_id, value)
        return value

    def _write_manifest(self, migration_id: str, value: dict[str, Any]) -> None:
        self._atomic_json(self._manifest_path(migration_id), value)

    def _copy_database(
        self,
        migration: DataRootMigrationView,
        manifest: dict[str, Any],
    ) -> int:
        self._require_not_cancelled(migration.migration_id)
        source = migration.source_root / "cutmaster.db"
        destination = migration.destination_root / "cutmaster.db"
        partial = destination.with_name(".cutmaster.db.part")
        manifest["current_partial"] = ".cutmaster.db.part"
        self._write_manifest(migration.migration_id, manifest)
        partial.unlink(missing_ok=True)
        source_uri = f"file:{source.as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            queued = int(
                source_connection.execute(
                    "SELECT count(*) FROM jobs WHERE status = 'queued'"
                ).fetchone()[0]
            )
            with sqlite3.connect(partial) as destination_connection:
                source_connection.backup(
                    destination_connection,
                    pages=256,
                    progress=lambda _status, _remaining, _total: (
                        self._heartbeat_boundary(migration.migration_id)
                    ),
                )
        os.chmod(partial, 0o600)
        self._heartbeat_boundary(migration.migration_id)
        _fsync_file(partial)
        self._heartbeat_boundary(migration.migration_id)
        partial.replace(destination)
        _fsync_directory(destination.parent)
        digest = _sha256(
            destination,
            heartbeat=lambda: self._heartbeat_boundary(migration.migration_id),
        )
        manifest["database"] = {
            "relative_path": "cutmaster.db",
            "size_bytes": destination.stat().st_size,
            "sha256": digest,
            "queued_jobs": queued,
        }
        manifest["current_partial"] = None
        self._write_manifest(migration.migration_id, manifest)
        return queued

    def _copy_file(self, migration: DataRootMigrationView, entry: _FileEntry) -> str:
        source = migration.source_root.joinpath(*entry.relative_path.split("/"))
        destination = migration.destination_root.joinpath(*entry.relative_path.split("/"))
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.migration-part")
        manifest_path = self._manifest_path(migration.migration_id)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["current_partial"] = partial.relative_to(
            migration.destination_root
        ).as_posix()
        self._write_manifest(migration.migration_id, manifest)
        partial.unlink(missing_ok=True)
        digest = hashlib.sha256()
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(source_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != entry.size_bytes:
                raise RuntimeError(
                    f"Source type changed during migration: {entry.relative_path}"
                )
            with (
                os.fdopen(source_descriptor, "rb") as input_stream,
                partial.open("xb") as output_stream,
            ):
                source_descriptor = -1
                while True:
                    self._require_not_cancelled(migration.migration_id)
                    chunk = input_stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    output_stream.write(chunk)
                    digest.update(chunk)
                    self._store.heartbeat(migration.migration_id)
                after_descriptor = os.fstat(input_stream.fileno())
                output_stream.flush()
                os.fsync(output_stream.fileno())
        finally:
            if source_descriptor >= 0:
                os.close(source_descriptor)
        after = source.stat(follow_symlinks=False)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_dev != after_descriptor.st_dev
            or before.st_ino != after_descriptor.st_ino
            or before.st_size != after_descriptor.st_size
            or before.st_mtime_ns != after_descriptor.st_mtime_ns
            or after.st_size != entry.size_bytes
        ):
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"Source changed during migration: {entry.relative_path}")
        os.chmod(partial, entry.mode)
        partial.replace(destination)
        _fsync_directory(destination.parent)
        manifest["current_partial"] = None
        self._write_manifest(migration.migration_id, manifest)
        return digest.hexdigest()

    def _verify_destination(
        self,
        migration: DataRootMigrationView,
        manifest: dict[str, Any],
        *,
        source_queued: int,
    ) -> None:
        self._require_not_cancelled(migration.migration_id)
        for relative, expected in sorted(manifest["files"].items()):
            path = migration.destination_root.joinpath(*relative.split("/"))
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Copied file is unavailable: {relative}")
            if path.stat().st_size != int(expected["size_bytes"]):
                raise RuntimeError(f"Copied file size mismatch: {relative}")
            if _sha256(
                path,
                heartbeat=lambda: self._heartbeat_boundary(migration.migration_id),
            ) != expected["sha256"]:
                raise RuntimeError(f"Copied file digest mismatch: {relative}")
            self._store.heartbeat(migration.migration_id)
        database_record = manifest.get("database")
        if database_record is None:
            return
        database = migration.destination_root / "cutmaster.db"
        if _sha256(
            database,
            heartbeat=lambda: self._heartbeat_boundary(migration.migration_id),
        ) != database_record["sha256"]:
            raise RuntimeError("Copied database digest mismatch")
        # The backup is self-contained.  Verify it as an immutable read-only
        # image so SQLite does not create destination WAL/SHM sidecars that
        # were never part of the signed manifest.
        database_uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(database_uri, uri=True) as connection:
            connection.set_progress_handler(
                lambda: self._sqlite_heartbeat(migration.migration_id),
                10_000,
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Copied database integrity check failed: {integrity}")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise RuntimeError("Copied database foreign key check failed")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Copied database schema mismatch: {version} != {LATEST_SCHEMA_VERSION}"
                )
            queued = int(
                connection.execute(
                    "SELECT count(*) FROM jobs WHERE status = 'queued'"
                ).fetchone()[0]
            )
        self._require_not_cancelled(migration.migration_id)
        if queued != source_queued:
            raise RuntimeError("Queued Job count changed during migration")

    def _switch_pointer(self, destination: Path) -> None:
        pointer = self._configuration.sources.data_root_pointer_path
        pointer.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{pointer.name}.",
            dir=pointer.parent,
        )
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"{destination}\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(pointer)
            _fsync_directory(pointer.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _rollback(
        self,
        migration: DataRootMigrationView,
        owned: bool,
    ) -> tuple[DataRootMigrationBlocker, ...]:
        if not owned:
            return ()
        destination = migration.destination_root
        marker = destination / _OWNER_MARKER
        if not marker.is_file() or not self._marker_matches(marker, migration):
            return (
                DataRootMigrationBlocker(
                    "cleanup_ownership_lost",
                    "Partial destination ownership could not be proven",
                    {},
                ),
            )
        manifest_path = self._manifest_path(migration.migration_id)
        preexisting = False
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                preexisting = bool(manifest.get("destination_preexisted"))
            except (OSError, ValueError):
                pass
        relative_files = set(manifest.get("files", {}))
        if manifest.get("database") is not None:
            relative_files.add("cutmaster.db")
        partial = manifest.get("current_partial")
        if isinstance(partial, str) and partial:
            relative_files.add(partial)
        relative_files.add(".cutmaster.db.part")
        for relative in sorted(relative_files, reverse=True):
            path = destination.joinpath(*relative.split("/"))
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        directories = {
            destination.joinpath(*str(relative).split("/"))
            for relative in manifest.get("directories", ())
            if isinstance(relative, str)
        }
        directories.update(path.parent for path in destination.rglob("*") if path != destination)
        for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
            try:
                directory.rmdir()
            except (FileNotFoundError, OSError):
                pass
        try:
            destination.rmdir()
        except OSError:
            remaining = sorted(
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
            )
            return (
                DataRootMigrationBlocker(
                    "cleanup_incomplete",
                    "Partial destination contains entries not owned by this migration",
                    {"remaining_entries": remaining},
                ),
            )
        if preexisting:
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        return ()

    @staticmethod
    def _valid_source_owner_marker(path: Path) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            value.get("schema_version") == "1.0"
            and isinstance(value.get("migration_id"), str)
            and bool(value["migration_id"])
        )

    def _verify_no_unmanifested_entries(
        self,
        migration: DataRootMigrationView,
        manifest: dict[str, Any],
    ) -> None:
        expected_files = set(manifest.get("files", {}))
        if manifest.get("database") is not None:
            expected_files.add("cutmaster.db")
        expected_files.add(_OWNER_MARKER)
        expected_directories = set(manifest.get("directories", ()))
        for relative in tuple(expected_files):
            parent = Path(relative).parent
            while parent != Path("."):
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        extras: list[str] = []
        for index, path in enumerate(migration.destination_root.rglob("*"), start=1):
            relative = path.relative_to(migration.destination_root).as_posix()
            if path.is_symlink():
                extras.append(relative)
            elif path.is_dir():
                if relative not in expected_directories:
                    extras.append(relative)
            elif relative not in expected_files:
                extras.append(relative)
            if index % 256 == 0:
                self._heartbeat_boundary(migration.migration_id)
        if extras:
            raise DataRootMigrationValidationError(
                tuple(
                    DataRootMigrationBlocker(
                        "unmanifested_destination_entry",
                        f"Destination entry was not verified: {relative}",
                        {"relative_path": relative},
                    )
                    for relative in sorted(extras)
                )
            )

    def _require_not_cancelled(self, migration_id: str) -> None:
        if self._store.cancel_requested(migration_id):
            raise DataRootMigrationCancelled(migration_id)

    def _progress(self, migration_id: str, files: int, bytes_: int) -> None:
        self._store.update(
            migration_id,
            files_completed=files,
            bytes_completed=bytes_,
        )

    def _heartbeat_boundary(self, migration_id: str) -> None:
        self._require_not_cancelled(migration_id)
        self._store.heartbeat(migration_id)

    def _sqlite_heartbeat(self, migration_id: str) -> int:
        self._store.heartbeat(migration_id)
        return 0

    @staticmethod
    def _atomic_json(
        path: Path,
        value: object,
        *,
        exclusive: bool = False,
    ) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if exclusive:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(path.parent)
            return
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _unsafe_entry(relative: str) -> DataRootMigrationBlocker:
    return DataRootMigrationBlocker(
        "unsafe_filesystem_entry",
        f"Unsafe filesystem entry: {relative}",
        {"relative_path": relative},
    )


def _sha256(path: Path, *, heartbeat: Callable[[], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
            if heartbeat is not None:
                heartbeat()
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DataRootMigrationCancelled",
    "DataRootMigrationValidationError",
    "LocalDataRootMigrator",
]
