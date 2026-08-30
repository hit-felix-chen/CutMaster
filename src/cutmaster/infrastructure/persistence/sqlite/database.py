"""Transactional SQLite store for CutMaster's managed local backend."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES, AttemptStatus
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.domain.edits import FrozenEditOrigin
from cutmaster.domain.ids import (
    AttemptId,
    FrozenEditId,
    JobId,
    MaterialId,
    ProjectId,
    RenderVariantId,
    RunId,
)
from cutmaster.domain.materials import MaterialType
from cutmaster.domain.renders import RenderVariantStatus
from cutmaster.domain.runs import RunStatus
from cutmaster.infrastructure.persistence.sqlite.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
)

JsonObject = dict[str, Any]
_ACTIVE_ATTEMPT_STATUSES = (
    AttemptStatus.QUEUED.value,
    AttemptStatus.RUNNING.value,
    AttemptStatus.RETRYING.value,
    AttemptStatus.STOPPING.value,
)
_ASTER_CHECKPOINT_STAGES = frozenset(
    {
        "replan_pending",
        "arrangement_architect",
        "story_editor",
        "timeline_scout",
        "edit_composer",
        "revision_editor",
    }
)


class SQLiteStoreError(RuntimeError):
    """Base error for managed local persistence."""


class ManagedStateNotFound(SQLiteStoreError):
    """A requested managed entity does not exist."""

    def __init__(self, object_type: str, object_id: str) -> None:
        self.object_type = object_type
        self.object_id = object_id
        super().__init__(f"{object_type} {object_id!r} was not found")


class ManagedStateConflict(SQLiteStoreError):
    """A managed command violates an ownership or lifecycle rule."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


class ProjectNameConflict(ManagedStateConflict):
    """One normalized Project Name already identifies another Project."""

    def __init__(self, project_id: ProjectId | str, project_name: str) -> None:
        self.existing_project_id = (
            project_id
            if isinstance(project_id, ProjectId)
            else ProjectId.parse(str(project_id))
        )
        self.existing_project_name = project_name
        # Transport projections use the concise names while the explicit
        # aliases retain the exception's conflict semantics for callers.
        self.project_id = self.existing_project_id
        self.project_name = self.existing_project_name
        super().__init__(
            "project_name_conflict",
            f"A Project named {project_name!r} already exists",
        )


class IdempotencyConflict(ManagedStateConflict):
    """One command UUID was reused for a different request."""

    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__(
            "idempotency_conflict",
            f"Command ID {command_id!r} was already used for another request",
        )


class ActiveAttemptBlocker(ManagedStateConflict):
    """Deletion was blocked by one or more non-terminal Attempts."""

    def __init__(self, owner_type: str, owner_id: str, attempt_ids: Sequence[str]):
        self.owner_type = owner_type
        self.owner_id = owner_id
        self.attempt_ids = tuple(attempt_ids)
        super().__init__(
            "active_attempts",
            f"Cannot delete {owner_type} {owner_id!r}; active Attempts: "
            + ", ".join(self.attempt_ids),
        )


@dataclass(frozen=True)
class IdempotentResult:
    value: JsonObject
    replayed: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Managed payload must be JSON-compatible") from exc


def _request_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_json(value: str) -> Any:
    return json.loads(value)


def _validate_command_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("command_id must be a string UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("command_id must be a canonical lowercase UUID") from exc
    if str(parsed) != value or parsed.variant != "specified in RFC 4122":
        raise ValueError("command_id must be a canonical lowercase UUID")
    return value


def _normalise_name(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label} must not contain control characters")
    return normalized


def _checkpoint_digest(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _checkpoint_stage(value: str) -> str:
    if not isinstance(value, str) or value not in _ASTER_CHECKPOINT_STAGES:
        raise ValueError(f"Unsupported ASTER checkpoint stage: {value!r}")
    return value


def _expect_row(
    row: sqlite3.Row | None,
    object_type: str,
    object_id: str,
) -> sqlite3.Row:
    if row is None:
        raise ManagedStateNotFound(object_type, object_id)
    return row


class SQLiteApplicationStore:
    """Own the canonical SQLite database beneath one Application Data Root.

    A store opens short-lived connections for operations.  This keeps service
    instances safe across request threads and lets SQLite serialize writers.
    Schema upgrades and every managed command are transactional.
    """

    def __init__(
        self,
        data_root: Path | str,
        *,
        clock: Callable[[], datetime] = _utc_now,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        root = Path(data_root).expanduser().resolve()
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise ValueError("Application Data Root must be a regular directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data_root = root
        self.database_path = root / "cutmaster.db"
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("BEGIN EXCLUSIVE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > LATEST_SCHEMA_VERSION:
                    raise SQLiteStoreError(
                        "Database schema is newer than this CutMaster build: "
                        f"{version} > {LATEST_SCHEMA_VERSION}"
                    )
                for migration in MIGRATIONS:
                    if migration.version <= version:
                        continue
                    if migration.version == 5:
                        self._require_unique_project_names_for_migration(connection)
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {migration.version}")
                    version = migration.version
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _require_unique_project_names_for_migration(
        connection: sqlite3.Connection,
    ) -> None:
        duplicate = connection.execute(
            """
            SELECT name, count(*) AS duplicate_count,
                   group_concat(project_id, ', ') AS project_ids
            FROM projects
            GROUP BY name
            HAVING count(*) > 1
            ORDER BY name
            LIMIT 1
            """
        ).fetchone()
        if duplicate is None:
            return
        raise SQLiteStoreError(
            "Cannot migrate to schema version 5 because Project Name "
            f"{duplicate['name']!r} is used by {duplicate['duplicate_count']} "
            f"Projects ({duplicate['project_ids']}). Rename or remove the "
            "duplicates before restarting CutMaster; no data was changed."
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _now(self) -> str:
        return _iso(self._clock())

    def _idempotent(
        self,
        command_id: str,
        command_kind: str,
        request: object,
        action: Callable[[sqlite3.Connection, str], JsonObject],
    ) -> IdempotentResult:
        command_id = _validate_command_id(command_id)
        request_digest = _request_digest(request)
        with self._transaction() as connection:
            receipt = connection.execute(
                """
                SELECT command_kind, request_digest, result_json
                FROM idempotency_receipts WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["command_kind"] != command_kind
                    or receipt["request_digest"] != request_digest
                ):
                    raise IdempotencyConflict(command_id)
                result = _parse_json(receipt["result_json"])
                if not isinstance(result, dict):
                    raise SQLiteStoreError("Invalid idempotency receipt result")
                return IdempotentResult(result, replayed=True)

            now = self._now()
            result = action(connection, now)
            result_json = _canonical_json(result)
            connection.execute(
                """
                INSERT INTO idempotency_receipts (
                    command_id, command_kind, request_digest, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (command_id, command_kind, request_digest, result_json, now),
            )
            return IdempotentResult(result, replayed=False)

    def replay_idempotent(
        self,
        command_id: str,
        command_kind: str,
        request: object,
    ) -> IdempotentResult | None:
        """Return a matching durable receipt without executing a command.

        Application services use this read-only preflight before filesystem
        validation.  A network replay must return its original result even if
        the referenced files or mutable Project setup changed afterwards.
        """

        command_id = _validate_command_id(command_id)
        request_digest = _request_digest(request)
        with self._read() as connection:
            receipt = connection.execute(
                """
                SELECT command_kind, request_digest, result_json
                FROM idempotency_receipts WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
        if receipt is None:
            return None
        if (
            receipt["command_kind"] != command_kind
            or receipt["request_digest"] != request_digest
        ):
            raise IdempotencyConflict(command_id)
        result = _parse_json(receipt["result_json"])
        if not isinstance(result, dict):
            raise SQLiteStoreError("Invalid idempotency receipt result")
        return IdempotentResult(result, replayed=True)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        occurred_at: str,
        object_type: str,
        object_id: str,
        command_id: str | None = None,
        attempt_id: str | None = None,
        job_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                event_type, occurred_at, object_type, object_id, command_id,
                attempt_id, job_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                occurred_at,
                object_type,
                object_id,
                command_id,
                attempt_id,
                job_id,
                _canonical_json(dict(payload or {})),
            ),
        )

    # ------------------------------------------------------------------
    # Project persistence

    def create_project(self, command_id: str, name: str) -> IdempotentResult:
        normalized_name = _normalise_name(name, "Project Name")
        request = {"name": normalized_name}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            existing = connection.execute(
                "SELECT project_id, name FROM projects WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if existing is not None:
                raise ProjectNameConflict(
                    existing["project_id"],
                    existing["name"],
                )
            project_id = str(ProjectId.new())
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, name, brief_intent, brief_target_duration,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, NULL, ?, ?)
                """,
                (project_id, normalized_name, now, now),
            )
            self._event(
                connection,
                event_type="project.created",
                occurred_at=now,
                object_type="project",
                object_id=project_id,
                command_id=command_id,
            )
            return self._project_record(connection, project_id)

        return self._idempotent(command_id, "projects.create", request, action)

    def ensure_project_by_name(
        self,
        command_id: str,
        name: str,
    ) -> IdempotentResult:
        """Atomically resolve or create one Project by normalized exact name."""

        normalized_name = _normalise_name(name, "Project Name")
        request = {"name": normalized_name}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            existing = connection.execute(
                "SELECT project_id FROM projects WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if existing is not None:
                return self._project_record(connection, existing["project_id"])
            project_id = str(ProjectId.new())
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, name, brief_intent, brief_target_duration,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, NULL, ?, ?)
                """,
                (project_id, normalized_name, now, now),
            )
            self._event(
                connection,
                event_type="project.created",
                occurred_at=now,
                object_type="project",
                object_id=project_id,
                command_id=command_id,
            )
            return self._project_record(connection, project_id)

        return self._idempotent(
            command_id,
            "projects.ensure_by_name",
            request,
            action,
        )

    def rename_project(
        self,
        command_id: str,
        project_id: ProjectId,
        name: str,
    ) -> IdempotentResult:
        normalized_name = _normalise_name(name, "Project Name")
        request = {"project_id": str(project_id), "name": normalized_name}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            self._require_project(connection, str(project_id))
            existing = connection.execute(
                """
                SELECT project_id, name FROM projects
                WHERE name = ? AND project_id != ?
                """,
                (normalized_name, str(project_id)),
            ).fetchone()
            if existing is not None:
                raise ProjectNameConflict(
                    existing["project_id"],
                    existing["name"],
                )
            cursor = connection.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE project_id = ?",
                (normalized_name, now, str(project_id)),
            )
            if cursor.rowcount != 1:
                raise ManagedStateNotFound("project", str(project_id))
            self._event(
                connection,
                event_type="project.renamed",
                occurred_at=now,
                object_type="project",
                object_id=str(project_id),
                command_id=command_id,
                payload={"name": normalized_name},
            )
            return self._project_record(connection, str(project_id))

        return self._idempotent(command_id, "projects.rename", request, action)

    def set_project_materials(
        self,
        command_id: str,
        project_id: ProjectId,
        videos: Sequence[MaterialId],
        music: Sequence[MaterialId],
    ) -> IdempotentResult:
        video_values = [str(item) for item in videos]
        music_values = [str(item) for item in music]
        if len(set(video_values)) != len(video_values):
            raise ValueError("Project video Materials must be unique")
        if len(set(music_values)) != len(music_values):
            raise ValueError("Project music Materials must be unique")
        request = {
            "project_id": str(project_id),
            "videos": video_values,
            "music": music_values,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            self._require_project(connection, str(project_id))
            connection.execute(
                "DELETE FROM project_materials WHERE project_id = ?",
                (str(project_id),),
            )
            for material_type, identifiers in (
                (MaterialType.VIDEO.value, video_values),
                (MaterialType.MUSIC.value, music_values),
            ):
                connection.executemany(
                    """
                    INSERT INTO project_materials (
                        project_id, material_type, position, material_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (str(project_id), material_type, position, material_id)
                        for position, material_id in enumerate(identifiers)
                    ],
                )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                (now, str(project_id)),
            )
            self._event(
                connection,
                event_type="project.materials_changed",
                occurred_at=now,
                object_type="project",
                object_id=str(project_id),
                command_id=command_id,
                payload={"video_count": len(videos), "music_count": len(music)},
            )
            return self._project_record(connection, str(project_id))

        return self._idempotent(
            command_id,
            "projects.set_materials",
            request,
            action,
        )

    def save_creative_brief(
        self,
        command_id: str,
        project_id: ProjectId,
        editing_intent: str,
        target_duration_sec: float,
    ) -> IdempotentResult:
        intent = _normalise_name(editing_intent, "Editing Intent")
        if isinstance(target_duration_sec, bool) or not isinstance(
            target_duration_sec,
            (int, float),
        ):
            raise TypeError("Target Duration must be a number")
        duration = float(target_duration_sec)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Target Duration must be finite and positive")
        request = {
            "project_id": str(project_id),
            "editing_intent": intent,
            "target_duration_sec": duration,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            cursor = connection.execute(
                """
                UPDATE projects
                SET brief_intent = ?, brief_target_duration = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (intent, duration, now, str(project_id)),
            )
            if cursor.rowcount != 1:
                raise ManagedStateNotFound("project", str(project_id))
            self._event(
                connection,
                event_type="project.creative_brief_saved",
                occurred_at=now,
                object_type="project",
                object_id=str(project_id),
                command_id=command_id,
            )
            return self._project_record(connection, str(project_id))

        return self._idempotent(
            command_id,
            "projects.save_creative_brief",
            request,
            action,
        )

    def save_project_setup(
        self,
        command_id: str,
        project_id: ProjectId,
        videos: Sequence[MaterialId],
        music: Sequence[MaterialId],
        editing_intent: str,
        target_duration_sec: float,
    ) -> IdempotentResult:
        """Atomically persist the complete mutable Project setup."""

        video_values = [str(item) for item in videos]
        music_values = [str(item) for item in music]
        if len(set(video_values)) != len(video_values):
            raise ValueError("Project video Materials must be unique")
        if len(set(music_values)) != len(music_values):
            raise ValueError("Project music Materials must be unique")
        intent = _normalise_name(editing_intent, "Editing Intent")
        if isinstance(target_duration_sec, bool) or not isinstance(
            target_duration_sec,
            (int, float),
        ):
            raise TypeError("Target Duration must be a number")
        duration = float(target_duration_sec)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Target Duration must be finite and positive")
        request = {
            "project_id": str(project_id),
            "videos": video_values,
            "music": music_values,
            "editing_intent": intent,
            "target_duration_sec": duration,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            self._require_project(connection, str(project_id))
            connection.execute(
                "DELETE FROM project_materials WHERE project_id = ?",
                (str(project_id),),
            )
            for material_type, identifiers in (
                (MaterialType.VIDEO.value, video_values),
                (MaterialType.MUSIC.value, music_values),
            ):
                connection.executemany(
                    """
                    INSERT INTO project_materials (
                        project_id, material_type, position, material_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (str(project_id), material_type, position, material_id)
                        for position, material_id in enumerate(identifiers)
                    ],
                )
            connection.execute(
                """
                UPDATE projects
                SET brief_intent = ?, brief_target_duration = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (intent, duration, now, str(project_id)),
            )
            self._event(
                connection,
                event_type="project.setup_saved",
                occurred_at=now,
                object_type="project",
                object_id=str(project_id),
                command_id=command_id,
                payload={
                    "video_count": len(video_values),
                    "music_count": len(music_values),
                },
            )
            return self._project_record(connection, str(project_id))

        return self._idempotent(command_id, "projects.save_setup", request, action)

    def get_project(self, project_id: ProjectId) -> JsonObject:
        with self._read() as connection:
            return self._project_record(connection, str(project_id))

    def list_projects(self) -> list[JsonObject]:
        with self._read() as connection:
            ids = connection.execute(
                "SELECT project_id FROM projects ORDER BY updated_at DESC, project_id"
            ).fetchall()
            return [self._project_record(connection, row[0]) for row in ids]

    def delete_project(
        self,
        command_id: str,
        project_id: ProjectId,
    ) -> IdempotentResult:
        request = {"project_id": str(project_id)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            self._require_project(connection, str(project_id))
            blockers = self._active_attempts_for_project(connection, str(project_id))
            if blockers:
                raise ActiveAttemptBlocker("project", str(project_id), blockers)
            job_rows = connection.execute(
                """
                SELECT DISTINCT jobs.job_id
                FROM jobs
                JOIN attempts ON attempts.attempt_id = jobs.attempt_id
                LEFT JOIN runs direct_runs ON direct_runs.run_id = attempts.run_id
                LEFT JOIN render_variants
                  ON render_variants.render_variant_id = attempts.render_variant_id
                LEFT JOIN frozen_edits
                  ON frozen_edits.edit_id = render_variants.edit_id
                LEFT JOIN runs render_runs
                  ON render_runs.run_id = frozen_edits.run_id
                WHERE direct_runs.project_id = ? OR render_runs.project_id = ?
                ORDER BY jobs.job_id
                """,
                (str(project_id), str(project_id)),
            ).fetchall()
            connection.execute(
                "DELETE FROM projects WHERE project_id = ?",
                (str(project_id),),
            )
            self._event(
                connection,
                event_type="project.deleted",
                occurred_at=now,
                object_type="project",
                object_id=str(project_id),
                command_id=command_id,
            )
            return {
                "project_id": str(project_id),
                "artifact_owner_relative_path": f"projects/{project_id}",
                "job_ids": [row["job_id"] for row in job_rows],
                "deleted": True,
            }

        return self._idempotent(command_id, "projects.delete", request, action)

    def references(self, material_id: MaterialId) -> Sequence[str]:
        """Implement MaterialReferenceChecker for current and frozen relations."""

        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT 'project:' || project_id || ':current:' || material_type AS ref
                FROM project_materials WHERE material_id = ?
                UNION ALL
                SELECT 'run:' || run_id || ':snapshot:' || material_type AS ref
                FROM run_materials WHERE material_id = ?
                ORDER BY ref
                """,
                (str(material_id), str(material_id)),
            ).fetchall()
            return tuple(row["ref"] for row in rows)

    @staticmethod
    def _require_project(connection: sqlite3.Connection, project_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        _expect_row(row, "project", project_id)

    def _project_record(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> JsonObject:
        row = _expect_row(
            connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone(),
            "project",
            project_id,
        )
        materials = connection.execute(
            """
            SELECT material_type, material_id
            FROM project_materials
            WHERE project_id = ?
            ORDER BY material_type, position
            """,
            (project_id,),
        ).fetchall()
        return {
            "project_id": row["project_id"],
            "name": row["name"],
            "video_material_ids": [
                item["material_id"]
                for item in materials
                if item["material_type"] == MaterialType.VIDEO.value
            ],
            "music_material_ids": [
                item["material_id"]
                for item in materials
                if item["material_type"] == MaterialType.MUSIC.value
            ],
            "creative_brief": (
                None
                if row["brief_intent"] is None
                else {
                    "editing_intent": row["brief_intent"],
                    "target_duration_sec": row["brief_target_duration"],
                }
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _active_attempts_for_project(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> list[str]:
        placeholders = ",".join("?" for _ in _ACTIVE_ATTEMPT_STATUSES)
        rows = connection.execute(
            f"""
            SELECT DISTINCT attempts.attempt_id
            FROM attempts
            LEFT JOIN render_variants
                ON render_variants.render_variant_id = attempts.render_variant_id
            LEFT JOIN frozen_edits ON frozen_edits.edit_id = render_variants.edit_id
            WHERE attempts.status IN ({placeholders})
              AND (
                  attempts.run_id IN (
                      SELECT run_id FROM runs WHERE project_id = ?
                  )
                  OR frozen_edits.run_id IN (
                      SELECT run_id FROM runs WHERE project_id = ?
                  )
              )
            ORDER BY attempts.attempt_id
            """,
            (*_ACTIVE_ATTEMPT_STATUSES, project_id, project_id),
        ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # ASTER Runs and Frozen Edits

    def create_run(
        self,
        command_id: str,
        project_id: ProjectId,
        configuration_snapshot: Mapping[str, Any],
        planning_options: Mapping[str, Any],
        *,
        validated_video_material_id: MaterialId,
        validated_music_material_id: MaterialId,
        music_duration_sec: float,
    ) -> IdempotentResult:
        snapshot_json = _canonical_json(dict(configuration_snapshot))
        planning_options_json = _canonical_json(dict(planning_options))
        request = {
            "project_id": str(project_id),
            "configuration": _parse_json(snapshot_json),
            "planning_options": _parse_json(planning_options_json),
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            project = self._project_record(connection, str(project_id))
            if project["creative_brief"] is None:
                raise ManagedStateConflict(
                    "creative_brief_missing",
                    "An ASTER Run requires a saved Creative Brief",
                )
            videos = project["video_material_ids"]
            music = project["music_material_ids"]
            if len(videos) != 1 or len(music) != 1:
                raise ManagedStateConflict(
                    "project_materials_incomplete",
                    "The first release requires exactly one video and one music Material",
                )
            if videos != [str(validated_video_material_id)] or music != [
                str(validated_music_material_id)
            ]:
                raise ManagedStateConflict(
                    "project_setup_changed",
                    "Project Materials changed while the ASTER Run was starting",
                )
            brief = project["creative_brief"]
            if float(brief["target_duration_sec"]) > music_duration_sec + 1e-6:
                raise ManagedStateConflict(
                    "target_duration_exceeds_music_duration",
                    "Target Duration must not exceed the selected Music Material duration",
                )
            sequence = int(
                connection.execute(
                    "SELECT coalesce(max(sequence), 0) + 1 FROM runs WHERE project_id = ?",
                    (str(project_id),),
                ).fetchone()[0]
            )
            run_id = str(RunId.new())
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, sequence, status, editing_intent,
                    target_duration_sec, configuration_json, failure_message,
                    created_at, updated_at, planning_options_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    str(project_id),
                    sequence,
                    RunStatus.QUEUED.value,
                    brief["editing_intent"],
                    brief["target_duration_sec"],
                    snapshot_json,
                    now,
                    now,
                    planning_options_json,
                ),
            )
            for material_type, identifiers in (
                (MaterialType.VIDEO.value, videos),
                (MaterialType.MUSIC.value, music),
            ):
                connection.executemany(
                    """
                    INSERT INTO run_materials (
                        run_id, material_type, position, material_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (run_id, material_type, position, material_id)
                        for position, material_id in enumerate(identifiers)
                    ],
                )
            attempt, job = self._insert_attempt(
                connection,
                operation_type="aster_planning",
                owner_type="run",
                owner_id=run_id,
                command_id=command_id,
                now=now,
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                (now, str(project_id)),
            )
            self._event(
                connection,
                event_type="run.queued",
                occurred_at=now,
                object_type="run",
                object_id=run_id,
                command_id=command_id,
                attempt_id=attempt["attempt_id"],
                job_id=job["job_id"],
            )
            return {
                "run": self._run_record(connection, run_id),
                "attempt": attempt,
                "job": job,
            }

        return self._idempotent(command_id, "runs.create", request, action)

    def create_run_again(
        self,
        command_id: str,
        source_run_id: RunId,
        *,
        validated_video_material_id: MaterialId,
        validated_music_material_id: MaterialId,
        music_duration_sec: float,
    ) -> IdempotentResult:
        """Create a new Run by copying one completed immutable Run snapshot."""

        request = {"source_run_id": str(source_run_id)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            source = self._require_run_row(connection, str(source_run_id))
            if source["status"] != RunStatus.COMPLETE.value:
                raise ManagedStateConflict(
                    "run_again_not_allowed",
                    f"Run {source_run_id} must be complete before it can run again",
                )
            source_materials = connection.execute(
                """
                SELECT material_type, position, material_id FROM run_materials
                WHERE run_id = ? ORDER BY material_type, position
                """,
                (str(source_run_id),),
            ).fetchall()
            videos = [
                row["material_id"]
                for row in source_materials
                if row["material_type"] == MaterialType.VIDEO.value
            ]
            music = [
                row["material_id"]
                for row in source_materials
                if row["material_type"] == MaterialType.MUSIC.value
            ]
            if videos != [str(validated_video_material_id)] or music != [
                str(validated_music_material_id)
            ]:
                raise ManagedStateConflict(
                    "run_snapshot_changed",
                    "The source Run Material snapshot changed unexpectedly",
                )
            if float(source["target_duration_sec"]) > music_duration_sec + 1e-6:
                raise ManagedStateConflict(
                    "target_duration_exceeds_music_duration",
                    "Target Duration must not exceed the snapshotted Music Material duration",
                )
            project_id = str(source["project_id"])
            sequence = int(
                connection.execute(
                    "SELECT coalesce(max(sequence), 0) + 1 FROM runs WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            )
            run_id = str(RunId.new())
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, sequence, status, editing_intent,
                    target_duration_sec, configuration_json, failure_message,
                    created_at, updated_at, planning_options_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    sequence,
                    RunStatus.QUEUED.value,
                    source["editing_intent"],
                    source["target_duration_sec"],
                    source["configuration_json"],
                    now,
                    now,
                    source["planning_options_json"],
                ),
            )
            connection.executemany(
                """
                INSERT INTO run_materials (
                    run_id, material_type, position, material_id
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row["material_type"],
                        row["position"],
                        row["material_id"],
                    )
                    for row in source_materials
                ],
            )
            attempt, job = self._insert_attempt(
                connection,
                operation_type="aster_planning",
                owner_type="run",
                owner_id=run_id,
                command_id=command_id,
                now=now,
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                (now, project_id),
            )
            self._event(
                connection,
                event_type="run.queued",
                occurred_at=now,
                object_type="run",
                object_id=run_id,
                command_id=command_id,
                attempt_id=attempt["attempt_id"],
                job_id=job["job_id"],
                payload={"source_run_id": str(source_run_id)},
            )
            return {
                "run": self._run_record(connection, run_id),
                "attempt": attempt,
                "job": job,
            }

        return self._idempotent(command_id, "runs.run_again", request, action)

    def get_run(self, run_id: RunId) -> JsonObject:
        with self._read() as connection:
            return self._run_record(connection, str(run_id))

    def list_runs(self, project_id: ProjectId) -> list[JsonObject]:
        with self._read() as connection:
            self._require_project(connection, str(project_id))
            rows = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE project_id = ? ORDER BY sequence DESC
                """,
                (str(project_id),),
            ).fetchall()
            return [self._run_record(connection, row[0]) for row in rows]

    def complete_run(
        self,
        command_id: str,
        run_id: RunId,
        attempt_id: AttemptId,
        plan_relative_path: str,
        model_usage_summary: Mapping[str, Any] | None = None,
    ) -> IdempotentResult:
        request = {
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "plan_relative_path": plan_relative_path,
            "model_usage_summary": (
                None if model_usage_summary is None else dict(model_usage_summary)
            ),
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            row = self._require_run_row(connection, str(run_id))
            if row["status"] != RunStatus.PLANNERS.value:
                raise ManagedStateConflict(
                    "run_not_planning",
                    f"Run {run_id} is {row['status']}, not in Planners",
                )
            attempt_row = self._require_attempt_owner(
                connection,
                str(attempt_id),
                "run",
                str(run_id),
            )
            if attempt_row["status"] not in {
                AttemptStatus.RUNNING.value,
                AttemptStatus.RETRYING.value,
            }:
                raise ManagedStateConflict(
                    "attempt_not_running",
                    f"Attempt {attempt_id} cannot complete from {attempt_row['status']}",
                )
            if model_usage_summary is not None:
                self._write_attempt_model_usage(
                    connection,
                    str(attempt_id),
                    model_usage_summary,
                )
            edit_id = str(FrozenEditId.new())
            connection.execute(
                """
                INSERT INTO frozen_edits (
                    edit_id, run_id, sequence, origin, parent_edit_id,
                    plan_relative_path, created_at
                ) VALUES (?, ?, 1, ?, NULL, ?, ?)
                """,
                (
                    edit_id,
                    str(run_id),
                    FrozenEditOrigin.INITIAL.value,
                    plan_relative_path,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, failure_message = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (RunStatus.COMPLETE.value, now, str(run_id)),
            )
            self._finish_attempt(
                connection,
                attempt_id=str(attempt_id),
                status=AttemptStatus.COMPLETE,
                now=now,
            )
            job = self._job_for_attempt(connection, str(attempt_id))
            self._event(
                connection,
                event_type="run.completed",
                occurred_at=now,
                object_type="run",
                object_id=str(run_id),
                command_id=command_id,
                attempt_id=str(attempt_id),
                job_id=job["job_id"],
                payload={"edit_id": edit_id},
            )
            return {
                "run": self._run_record(connection, str(run_id)),
                "frozen_edit": self._edit_record(connection, edit_id),
                "attempt": self._attempt_record(connection, str(attempt_id)),
                "job": self._job_record(connection, job["job_id"]),
            }

        return self._idempotent(command_id, "runs.complete", request, action)

    def create_revision(
        self,
        command_id: str,
        source_edit_id: FrozenEditId,
        plan_relative_path: str,
    ) -> IdempotentResult:
        request = {
            "source_edit_id": str(source_edit_id),
            "plan_relative_path": plan_relative_path,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            source = self._require_edit_row(connection, str(source_edit_id))
            run = self._require_run_row(connection, source["run_id"])
            if run["status"] != RunStatus.COMPLETE.value:
                raise ManagedStateConflict(
                    "run_not_complete",
                    "Guided Revision requires a completed ASTER Run",
                )
            sequence = int(
                connection.execute(
                    """
                    SELECT coalesce(max(sequence), 0) + 1
                    FROM frozen_edits WHERE run_id = ?
                    """,
                    (source["run_id"],),
                ).fetchone()[0]
            )
            edit_id = str(FrozenEditId.new())
            connection.execute(
                """
                INSERT INTO frozen_edits (
                    edit_id, run_id, sequence, origin, parent_edit_id,
                    plan_relative_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edit_id,
                    source["run_id"],
                    sequence,
                    FrozenEditOrigin.GUIDED_REVISION.value,
                    str(source_edit_id),
                    plan_relative_path,
                    now,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                (now, run["project_id"]),
            )
            self._event(
                connection,
                event_type="frozen_edit.created",
                occurred_at=now,
                object_type="frozen_edit",
                object_id=edit_id,
                command_id=command_id,
                payload={"parent_edit_id": str(source_edit_id)},
            )
            return self._edit_record(connection, edit_id)

        return self._idempotent(
            command_id,
            "runs.create_revision",
            request,
            action,
        )

    def get_frozen_edit(self, edit_id: FrozenEditId) -> JsonObject:
        with self._read() as connection:
            return self._edit_record(connection, str(edit_id))

    def list_frozen_edits(self, run_id: RunId) -> list[JsonObject]:
        with self._read() as connection:
            self._require_run_row(connection, str(run_id))
            rows = connection.execute(
                """
                SELECT edit_id FROM frozen_edits
                WHERE run_id = ? ORDER BY sequence
                """,
                (str(run_id),),
            ).fetchall()
            return [self._edit_record(connection, row[0]) for row in rows]

    def retry_run(
        self,
        command_id: str,
        run_id: RunId,
        *,
        resume: bool,
        resume_checkpoint_id: str | None = None,
    ) -> IdempotentResult:
        request = {"run_id": str(run_id), "resume": resume}
        command_kind = "runs.resume" if resume else "runs.retry"
        if resume:
            if resume_checkpoint_id is None:
                raise ValueError("resume_checkpoint_id is required for Resume")
            _validate_command_id(resume_checkpoint_id)
        elif resume_checkpoint_id is not None:
            raise ValueError("Retry cannot pin a Resume checkpoint")

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            run = self._require_run_row(connection, str(run_id))
            expected = RunStatus.INTERRUPTED.value if resume else RunStatus.FAILED.value
            if run["status"] != expected:
                raise ManagedStateConflict(
                    "run_recovery_not_allowed",
                    f"Run {run_id} must be {expected} for this recovery action",
                )
            checkpoint_row = connection.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            checkpoint_pin: JsonObject | None = None
            discarded_checkpoint_relative_path: str | None = None
            if resume:
                if checkpoint_row is None:
                    raise ManagedStateConflict(
                        "run_checkpoint_unavailable",
                        "Resume requires a completed ASTER stage checkpoint; use Retry",
                    )
                if checkpoint_row["checkpoint_id"] != resume_checkpoint_id:
                    raise ManagedStateConflict(
                        "run_checkpoint_changed",
                        "The ASTER checkpoint changed before Resume was queued",
                    )
                checkpoint_pin = self._run_checkpoint_record(checkpoint_row)
            elif checkpoint_row is not None:
                discarded_checkpoint_relative_path = str(
                    checkpoint_row["relative_path"]
                )
                connection.execute(
                    "DELETE FROM run_checkpoints WHERE run_id = ?",
                    (str(run_id),),
                )
            attempt, job = self._insert_attempt(
                connection,
                operation_type="aster_planning",
                owner_type="run",
                owner_id=str(run_id),
                command_id=command_id,
                now=now,
            )
            if checkpoint_pin is not None:
                connection.execute(
                    """
                    UPDATE attempts SET resume_checkpoint_json = ?
                    WHERE attempt_id = ?
                    """,
                    (_canonical_json(checkpoint_pin), attempt["attempt_id"]),
                )
                attempt = self._attempt_record(connection, attempt["attempt_id"])
            connection.execute(
                """
                UPDATE runs SET status = ?, failure_message = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (RunStatus.QUEUED.value, now, str(run_id)),
            )
            self._event(
                connection,
                event_type="run.resumed" if resume else "run.retried",
                occurred_at=now,
                object_type="run",
                object_id=str(run_id),
                command_id=command_id,
                attempt_id=attempt["attempt_id"],
                job_id=job["job_id"],
            )
            return {
                "run": self._run_record(connection, str(run_id)),
                "attempt": attempt,
                "job": job,
                "discarded_checkpoint_relative_path": (
                    discarded_checkpoint_relative_path
                ),
            }

        return self._idempotent(command_id, command_kind, request, action)

    def get_run_checkpoint(self, run_id: RunId) -> JsonObject | None:
        with self._read() as connection:
            self._require_run_row(connection, str(run_id))
            row = connection.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            return None if row is None else self._run_checkpoint_record(row)

    def get_attempt_resume_checkpoint(
        self,
        attempt_id: AttemptId,
    ) -> JsonObject | None:
        with self._read() as connection:
            row = self._require_attempt_row(connection, str(attempt_id))
            raw = row["resume_checkpoint_json"]
            if raw is None:
                return None
            value = _parse_json(raw)
            if not isinstance(value, dict):
                raise SQLiteStoreError("Invalid pinned ASTER checkpoint receipt")
            self._validate_checkpoint_record(value)
            return value

    def publish_run_checkpoint(
        self,
        *,
        run_id: RunId,
        source_attempt_id: AttemptId,
        checkpoint_id: str,
        completed_stage: str,
        relative_path: str,
        content_sha256: str,
        identity_signature: str,
        schema_version: str,
    ) -> JsonObject:
        _validate_command_id(checkpoint_id)
        _checkpoint_stage(completed_stage)
        normalized_path = validate_portable_relative_file_path(relative_path)
        _checkpoint_digest(content_sha256, "content_sha256")
        _checkpoint_digest(identity_signature, "identity_signature")
        if schema_version != "1.0":
            raise ValueError(f"Unsupported ASTER checkpoint schema: {schema_version!r}")
        with self._transaction() as connection:
            self._require_run_row(connection, str(run_id))
            attempt = self._require_attempt_owner(
                connection,
                str(source_attempt_id),
                "run",
                str(run_id),
            )
            if attempt["status"] not in {
                AttemptStatus.RUNNING.value,
                AttemptStatus.RETRYING.value,
                AttemptStatus.STOPPING.value,
            }:
                raise ManagedStateConflict(
                    "run_checkpoint_attempt_inactive",
                    "ASTER checkpoints can only be published by the active Attempt",
                )
            existing = connection.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            previous = (
                None if existing is None else self._run_checkpoint_record(existing)
            )
            now = self._now()
            created_at = now if existing is None else str(existing["created_at"])
            connection.execute(
                """
                INSERT INTO run_checkpoints (
                    run_id, checkpoint_id, source_attempt_id, completed_stage,
                    relative_path, content_sha256, identity_signature,
                    schema_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    checkpoint_id = excluded.checkpoint_id,
                    source_attempt_id = excluded.source_attempt_id,
                    completed_stage = excluded.completed_stage,
                    relative_path = excluded.relative_path,
                    content_sha256 = excluded.content_sha256,
                    identity_signature = excluded.identity_signature,
                    schema_version = excluded.schema_version,
                    updated_at = excluded.updated_at
                """,
                (
                    str(run_id),
                    checkpoint_id,
                    str(source_attempt_id),
                    completed_stage,
                    normalized_path,
                    content_sha256,
                    identity_signature,
                    schema_version,
                    created_at,
                    now,
                ),
            )
            current = connection.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            assert current is not None
            return {
                "checkpoint": self._run_checkpoint_record(current),
                "replaced_checkpoint": previous,
            }

    def clear_run_checkpoint(
        self,
        run_id: RunId,
        *,
        expected_checkpoint_id: str | None = None,
    ) -> JsonObject | None:
        if expected_checkpoint_id is not None:
            _validate_command_id(expected_checkpoint_id)
        with self._transaction() as connection:
            self._require_run_row(connection, str(run_id))
            row = connection.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                return None
            if (
                expected_checkpoint_id is not None
                and row["checkpoint_id"] != expected_checkpoint_id
            ):
                return None
            value = self._run_checkpoint_record(row)
            connection.execute(
                "DELETE FROM run_checkpoints WHERE run_id = ?",
                (str(run_id),),
            )
            return value

    def delete_run(
        self,
        command_id: str,
        run_id: RunId,
    ) -> IdempotentResult:
        request = {"run_id": str(run_id)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            run = self._require_run_row(connection, str(run_id))
            blockers = self._active_attempts_for_run(connection, str(run_id))
            if blockers:
                raise ActiveAttemptBlocker("run", str(run_id), blockers)
            render_rows = connection.execute(
                """
                SELECT render_variants.render_variant_id
                FROM render_variants
                JOIN frozen_edits
                  ON frozen_edits.edit_id = render_variants.edit_id
                WHERE frozen_edits.run_id = ?
                ORDER BY render_variants.render_variant_id
                """,
                (str(run_id),),
            ).fetchall()
            job_rows = connection.execute(
                """
                SELECT DISTINCT jobs.job_id
                FROM jobs
                JOIN attempts ON attempts.attempt_id = jobs.attempt_id
                LEFT JOIN render_variants
                  ON render_variants.render_variant_id = attempts.render_variant_id
                LEFT JOIN frozen_edits
                  ON frozen_edits.edit_id = render_variants.edit_id
                WHERE attempts.run_id = ? OR frozen_edits.run_id = ?
                ORDER BY jobs.job_id
                """,
                (str(run_id), str(run_id)),
            ).fetchall()
            connection.execute("DELETE FROM runs WHERE run_id = ?", (str(run_id),))
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                (now, run["project_id"]),
            )
            self._event(
                connection,
                event_type="run.deleted",
                occurred_at=now,
                object_type="run",
                object_id=str(run_id),
                command_id=command_id,
            )
            return {
                "run_id": str(run_id),
                "project_id": str(run["project_id"]),
                "render_variant_ids": [row["render_variant_id"] for row in render_rows],
                "job_ids": [row["job_id"] for row in job_rows],
                "deleted": True,
            }

        return self._idempotent(command_id, "runs.delete", request, action)

    @staticmethod
    def _require_run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        return _expect_row(
            connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone(),
            "run",
            run_id,
        )

    @staticmethod
    def _require_edit_row(connection: sqlite3.Connection, edit_id: str) -> sqlite3.Row:
        return _expect_row(
            connection.execute(
                "SELECT * FROM frozen_edits WHERE edit_id = ?",
                (edit_id,),
            ).fetchone(),
            "frozen_edit",
            edit_id,
        )

    def _run_record(self, connection: sqlite3.Connection, run_id: str) -> JsonObject:
        row = self._require_run_row(connection, run_id)
        materials = connection.execute(
            """
            SELECT material_type, material_id FROM run_materials
            WHERE run_id = ? ORDER BY material_type, position
            """,
            (run_id,),
        ).fetchall()
        return {
            "run_id": row["run_id"],
            "project_id": row["project_id"],
            "sequence": row["sequence"],
            "status": row["status"],
            "editing_intent": row["editing_intent"],
            "target_duration_sec": row["target_duration_sec"],
            "configuration": _parse_json(row["configuration_json"]),
            "planning_options": _parse_json(row["planning_options_json"]),
            "video_material_ids": [
                item["material_id"]
                for item in materials
                if item["material_type"] == MaterialType.VIDEO.value
            ],
            "music_material_ids": [
                item["material_id"]
                for item in materials
                if item["material_type"] == MaterialType.MUSIC.value
            ],
            "failure_message": row["failure_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _edit_record(self, connection: sqlite3.Connection, edit_id: str) -> JsonObject:
        row = self._require_edit_row(connection, edit_id)
        return {
            "edit_id": row["edit_id"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "origin": row["origin"],
            "parent_edit_id": row["parent_edit_id"],
            "plan_relative_path": row["plan_relative_path"],
            "created_at": row["created_at"],
        }

    def _active_attempts_for_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> list[str]:
        placeholders = ",".join("?" for _ in _ACTIVE_ATTEMPT_STATUSES)
        rows = connection.execute(
            f"""
            SELECT DISTINCT attempts.attempt_id
            FROM attempts
            LEFT JOIN render_variants
                ON render_variants.render_variant_id = attempts.render_variant_id
            LEFT JOIN frozen_edits ON frozen_edits.edit_id = render_variants.edit_id
            WHERE attempts.status IN ({placeholders})
              AND (attempts.run_id = ? OR frozen_edits.run_id = ?)
            ORDER BY attempts.attempt_id
            """,
            (*_ACTIVE_ATTEMPT_STATUSES, run_id, run_id),
        ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Render Variants

    def create_render_variant(
        self,
        command_id: str,
        edit_id: FrozenEditId,
        specification: Mapping[str, Any],
    ) -> IdempotentResult:
        specification_json = _canonical_json(dict(specification))
        specification_value = _parse_json(specification_json)
        if not isinstance(specification_value, dict) or not specification_value:
            raise ValueError("Render Specification must be a non-empty object")
        specification_digest = hashlib.sha256(
            specification_json.encode("utf-8")
        ).hexdigest()
        audio_mode = specification_value.get("audio_mode")
        if audio_mode not in {"dialogue", "bgm_only"}:
            raise ValueError("Render Specification has no supported audio_mode")
        request = {
            "edit_id": str(edit_id),
            "audio_mode": audio_mode,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            edit = self._require_edit_row(connection, str(edit_id))
            existing = connection.execute(
                """
                SELECT render_variant_id FROM render_variants
                WHERE edit_id = ? AND specification_digest = ?
                """,
                (str(edit_id), specification_digest),
            ).fetchone()
            if existing is not None:
                return {
                    "render_variant": self._render_record(connection, existing[0]),
                    "attempt": None,
                    "job": None,
                    "created": False,
                }

            render_id = str(RenderVariantId.new())
            connection.execute(
                """
                INSERT INTO render_variants (
                    render_variant_id, edit_id, status, specification_json,
                    specification_digest, master_relative_path,
                    master_size_bytes, master_sha256, frame_count, duration_sec,
                    failure_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    render_id,
                    str(edit_id),
                    RenderVariantStatus.QUEUED.value,
                    specification_json,
                    specification_digest,
                    now,
                    now,
                ),
            )
            attempt, job = self._insert_attempt(
                connection,
                operation_type="rendering",
                owner_type="render_variant",
                owner_id=render_id,
                command_id=command_id,
                now=now,
            )
            run = self._require_run_row(connection, edit["run_id"])
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE project_id = ?",
                (now, run["project_id"]),
            )
            self._event(
                connection,
                event_type="render_variant.queued",
                occurred_at=now,
                object_type="render_variant",
                object_id=render_id,
                command_id=command_id,
                attempt_id=attempt["attempt_id"],
                job_id=job["job_id"],
            )
            return {
                "render_variant": self._render_record(connection, render_id),
                "attempt": attempt,
                "job": job,
                "created": True,
            }

        return self._idempotent(
            command_id,
            "renders.create_variant",
            request,
            action,
        )

    def get_render_variant(self, render_id: RenderVariantId) -> JsonObject:
        with self._read() as connection:
            return self._render_record(connection, str(render_id))

    def list_render_variants(self, edit_id: FrozenEditId) -> list[JsonObject]:
        with self._read() as connection:
            self._require_edit_row(connection, str(edit_id))
            rows = connection.execute(
                """
                SELECT render_variant_id FROM render_variants
                WHERE edit_id = ? ORDER BY created_at, render_variant_id
                """,
                (str(edit_id),),
            ).fetchall()
            return [self._render_record(connection, row[0]) for row in rows]

    def complete_render_variant(
        self,
        command_id: str,
        render_id: RenderVariantId,
        attempt_id: AttemptId,
        *,
        master_relative_path: str,
        master_size_bytes: int,
        master_sha256: str,
        frame_count: int,
        duration_sec: float,
    ) -> IdempotentResult:
        if (
            not isinstance(master_size_bytes, int)
            or isinstance(master_size_bytes, bool)
            or master_size_bytes < 0
        ):
            raise ValueError("master_size_bytes must be a non-negative integer")
        if (
            not isinstance(master_sha256, str)
            or len(master_sha256) != 64
            or any(character not in "0123456789abcdef" for character in master_sha256)
        ):
            raise ValueError("master_sha256 must be a lowercase SHA-256")
        if (
            not isinstance(frame_count, int)
            or isinstance(frame_count, bool)
            or frame_count < 0
        ):
            raise ValueError("frame_count must be a non-negative integer")
        if isinstance(duration_sec, bool) or not isinstance(duration_sec, (int, float)):
            raise TypeError("duration_sec must be a number")
        duration = float(duration_sec)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration_sec must be finite and non-negative")
        request = {
            "render_variant_id": str(render_id),
            "attempt_id": str(attempt_id),
            "master_relative_path": master_relative_path,
            "frame_count": frame_count,
            "duration_sec": duration,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            variant = self._require_render_row(connection, str(render_id))
            if variant["status"] != RenderVariantStatus.RENDERING.value:
                raise ManagedStateConflict(
                    "render_not_running",
                    f"Render Variant {render_id} is {variant['status']}",
                )
            attempt = self._require_attempt_owner(
                connection,
                str(attempt_id),
                "render_variant",
                str(render_id),
            )
            if attempt["status"] not in {
                AttemptStatus.RUNNING.value,
                AttemptStatus.RETRYING.value,
                AttemptStatus.STOPPING.value,
            }:
                raise ManagedStateConflict(
                    "attempt_not_running",
                    f"Attempt {attempt_id} cannot complete from {attempt['status']}",
                )
            connection.execute(
                """
                UPDATE render_variants
                SET status = ?, master_relative_path = ?, master_size_bytes = ?,
                    master_sha256 = ?, frame_count = ?, duration_sec = ?,
                    failure_message = NULL, updated_at = ?
                WHERE render_variant_id = ?
                """,
                (
                    RenderVariantStatus.READY.value,
                    master_relative_path,
                    master_size_bytes,
                    master_sha256,
                    frame_count,
                    duration,
                    now,
                    str(render_id),
                ),
            )
            self._finish_attempt(
                connection,
                attempt_id=str(attempt_id),
                status=AttemptStatus.COMPLETE,
                now=now,
            )
            job = self._job_for_attempt(connection, str(attempt_id))
            self._event(
                connection,
                event_type="render_variant.ready",
                occurred_at=now,
                object_type="render_variant",
                object_id=str(render_id),
                command_id=command_id,
                attempt_id=str(attempt_id),
                job_id=job["job_id"],
            )
            return {
                "render_variant": self._render_record(connection, str(render_id)),
                "attempt": self._attempt_record(connection, str(attempt_id)),
                "job": self._job_record(connection, job["job_id"]),
            }

        return self._idempotent(
            command_id,
            "renders.complete",
            request,
            action,
        )

    def retry_render_variant(
        self,
        command_id: str,
        render_id: RenderVariantId,
        *,
        mode: str,
    ) -> IdempotentResult:
        expected_by_mode = {
            "retry": RenderVariantStatus.FAILED.value,
            "resume": RenderVariantStatus.INTERRUPTED.value,
            "render_again": RenderVariantStatus.UNAVAILABLE.value,
        }
        try:
            expected = expected_by_mode[mode]
        except KeyError as exc:
            raise ValueError(f"Unsupported render recovery mode: {mode!r}") from exc
        request = {"render_variant_id": str(render_id), "mode": mode}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            variant = self._require_render_row(connection, str(render_id))
            if variant["status"] != expected:
                raise ManagedStateConflict(
                    "render_recovery_not_allowed",
                    f"Render Variant {render_id} must be {expected} for {mode}",
                )
            attempt, job = self._insert_attempt(
                connection,
                operation_type="rendering",
                owner_type="render_variant",
                owner_id=str(render_id),
                command_id=command_id,
                now=now,
            )
            connection.execute(
                """
                UPDATE render_variants
                SET status = ?, failure_message = NULL, updated_at = ?
                WHERE render_variant_id = ?
                """,
                (RenderVariantStatus.QUEUED.value, now, str(render_id)),
            )
            self._event(
                connection,
                event_type=f"render_variant.{mode}",
                occurred_at=now,
                object_type="render_variant",
                object_id=str(render_id),
                command_id=command_id,
                attempt_id=attempt["attempt_id"],
                job_id=job["job_id"],
            )
            return {
                "render_variant": self._render_record(connection, str(render_id)),
                "attempt": attempt,
                "job": job,
                "created": False,
            }

        return self._idempotent(
            command_id,
            f"renders.{mode}",
            request,
            action,
        )

    def mark_render_unavailable(
        self,
        command_id: str,
        render_id: RenderVariantId,
        reason: str,
    ) -> IdempotentResult:
        normalized_reason = _normalise_name(reason, "Unavailable reason")
        request = {
            "render_variant_id": str(render_id),
            "reason": normalized_reason,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            variant = self._require_render_row(connection, str(render_id))
            if variant["status"] not in {
                RenderVariantStatus.READY.value,
                RenderVariantStatus.UNAVAILABLE.value,
            }:
                raise ManagedStateConflict(
                    "render_unavailable_not_allowed",
                    "Only a Ready Render Variant can become Unavailable",
                )
            if variant["status"] == RenderVariantStatus.UNAVAILABLE.value:
                # Concurrent lightweight reads can observe the same stale Ready
                # row. The first request owns the transition; followers return
                # the durable state without rewriting timestamps or events.
                return self._render_record(connection, str(render_id))
            connection.execute(
                """
                UPDATE render_variants
                SET status = ?, failure_message = ?, updated_at = ?
                WHERE render_variant_id = ?
                """,
                (
                    RenderVariantStatus.UNAVAILABLE.value,
                    normalized_reason,
                    now,
                    str(render_id),
                ),
            )
            self._event(
                connection,
                event_type="render_variant.unavailable",
                occurred_at=now,
                object_type="render_variant",
                object_id=str(render_id),
                command_id=command_id,
                payload={"reason": normalized_reason},
            )
            return self._render_record(connection, str(render_id))

        return self._idempotent(
            command_id,
            "renders.mark_unavailable",
            request,
            action,
        )

    def record_render_verification(
        self,
        command_id: str,
        render_id: RenderVariantId,
        *,
        verified: bool,
        size_bytes: int,
        cached: bool,
        reason: str | None = None,
    ) -> IdempotentResult:
        """Persist one idempotent integrity decision without exposing its digest."""

        if not isinstance(verified, bool) or not isinstance(cached, bool):
            raise TypeError("verified and cached must be booleans")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")
        normalized_reason = None
        if not verified:
            normalized_reason = _normalise_name(
                reason or "Managed master failed integrity validation",
                "Integrity failure reason",
            )
        request = {"render_variant_id": str(render_id)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            variant = self._require_render_row(connection, str(render_id))
            if verified:
                if variant["status"] != RenderVariantStatus.READY.value:
                    raise ManagedStateConflict(
                        "render_media_unavailable",
                        "Only a Ready Render Variant can be verified",
                    )
            else:
                if variant["status"] not in {
                    RenderVariantStatus.READY.value,
                    RenderVariantStatus.UNAVAILABLE.value,
                }:
                    raise ManagedStateConflict(
                        "render_media_unavailable",
                        "Render Variant has no Ready managed master",
                    )
                if variant["status"] == RenderVariantStatus.READY.value:
                    connection.execute(
                        """
                        UPDATE render_variants
                        SET status = ?, failure_message = ?, updated_at = ?
                        WHERE render_variant_id = ?
                        """,
                        (
                            RenderVariantStatus.UNAVAILABLE.value,
                            normalized_reason,
                            now,
                            str(render_id),
                        ),
                    )
                    self._event(
                        connection,
                        event_type="render_variant.unavailable",
                        occurred_at=now,
                        object_type="render_variant",
                        object_id=str(render_id),
                        command_id=command_id,
                        payload={"reason": normalized_reason},
                    )
            return {
                "render_variant": self._render_record(connection, str(render_id)),
                "verified": verified,
                "size_bytes": size_bytes,
                "cached": cached,
                "reason": normalized_reason,
            }

        return self._idempotent(
            command_id,
            "renders.verify",
            request,
            action,
        )

    def delete_render_variant(
        self,
        command_id: str,
        render_id: RenderVariantId,
    ) -> IdempotentResult:
        request = {"render_variant_id": str(render_id)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            variant = self._require_render_row(connection, str(render_id))
            blockers = self._active_attempts_for_render(connection, str(render_id))
            if blockers:
                raise ActiveAttemptBlocker(
                    "render_variant",
                    str(render_id),
                    blockers,
                )
            edit = self._require_edit_row(connection, variant["edit_id"])
            run = self._require_run_row(connection, edit["run_id"])
            jobs = connection.execute(
                """
                SELECT jobs.job_id FROM jobs
                JOIN attempts USING (attempt_id)
                WHERE attempts.render_variant_id = ?
                ORDER BY jobs.job_id
                """,
                (str(render_id),),
            ).fetchall()
            connection.execute(
                "DELETE FROM render_variants WHERE render_variant_id = ?",
                (str(render_id),),
            )
            self._event(
                connection,
                event_type="render_variant.deleted",
                occurred_at=now,
                object_type="render_variant",
                object_id=str(render_id),
                command_id=command_id,
            )
            return {
                "render_variant_id": str(render_id),
                "deleted": True,
                "master_relative_path": variant["master_relative_path"],
                "project_id": run["project_id"],
                "job_ids": [row["job_id"] for row in jobs],
            }

        return self._idempotent(
            command_id,
            "renders.delete",
            request,
            action,
        )

    @staticmethod
    def _require_render_row(
        connection: sqlite3.Connection,
        render_id: str,
    ) -> sqlite3.Row:
        return _expect_row(
            connection.execute(
                """
                SELECT * FROM render_variants WHERE render_variant_id = ?
                """,
                (render_id,),
            ).fetchone(),
            "render_variant",
            render_id,
        )

    def _render_record(
        self,
        connection: sqlite3.Connection,
        render_id: str,
    ) -> JsonObject:
        row = self._require_render_row(connection, render_id)
        return {
            "render_variant_id": row["render_variant_id"],
            "edit_id": row["edit_id"],
            "status": row["status"],
            "specification": _parse_json(row["specification_json"]),
            "specification_digest": row["specification_digest"],
            "master_relative_path": row["master_relative_path"],
            "master_size_bytes": row["master_size_bytes"],
            "master_sha256": row["master_sha256"],
            "frame_count": row["frame_count"],
            "duration_sec": row["duration_sec"],
            "failure_message": row["failure_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _active_attempts_for_render(
        self,
        connection: sqlite3.Connection,
        render_id: str,
    ) -> list[str]:
        placeholders = ",".join("?" for _ in _ACTIVE_ATTEMPT_STATUSES)
        rows = connection.execute(
            f"""
            SELECT attempt_id FROM attempts
            WHERE render_variant_id = ? AND status IN ({placeholders})
            ORDER BY attempt_id
            """,
            (render_id, *_ACTIVE_ATTEMPT_STATUSES),
        ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Attempts, durable jobs, and event replay

    def enqueue_material_analysis(
        self,
        command_id: str,
        material_id: MaterialId,
    ) -> IdempotentResult:
        request = {"material_id": str(material_id)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            active = connection.execute(
                """
                SELECT attempts.attempt_id, jobs.job_id
                FROM attempts JOIN jobs USING (attempt_id)
                WHERE material_id = ?
                  AND attempts.status IN ('queued', 'running', 'retrying', 'stopping')
                ORDER BY attempts.sequence DESC LIMIT 1
                """,
                (str(material_id),),
            ).fetchone()
            if active is not None:
                raise ManagedStateConflict(
                    "material_analysis_active",
                    f"Material {material_id} already has active Attempt "
                    f"{active['attempt_id']}",
                )
            attempt, job = self._insert_attempt(
                connection,
                operation_type="material_analysis",
                owner_type="material",
                owner_id=str(material_id),
                command_id=command_id,
                now=now,
            )
            self._event(
                connection,
                event_type="material_analysis.queued",
                occurred_at=now,
                object_type="material",
                object_id=str(material_id),
                command_id=command_id,
                attempt_id=attempt["attempt_id"],
                job_id=job["job_id"],
            )
            return {"attempt": attempt, "job": job}

        return self._idempotent(
            command_id,
            "jobs.enqueue_material_analysis",
            request,
            action,
        )

    def requeue_material_analysis(
        self,
        command_id: str,
        material_id: MaterialId,
        *,
        expected_status: AttemptStatus,
    ) -> IdempotentResult:
        """Create a new Material Analysis Attempt from one terminal state.

        The status check and insertion share the idempotent transaction.  A
        replay of the same command therefore returns the already-created Job
        even though that Job is now the latest active Attempt.
        """

        if expected_status not in {AttemptStatus.FAILED, AttemptStatus.INTERRUPTED}:
            raise ValueError("Material Analysis can only retry or resume")
        request = {
            "material_id": str(material_id),
            "expected_status": expected_status.value,
        }

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            latest = connection.execute(
                """
                SELECT attempt_id, status FROM attempts
                WHERE material_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (str(material_id),),
            ).fetchone()
            if latest is None:
                raise ManagedStateConflict(
                    "material_analysis_history_missing",
                    f"Material {material_id} has no Analysis Attempt to "
                    f"{('retry' if expected_status is AttemptStatus.FAILED else 'resume')}",
                )
            current = AttemptStatus(latest["status"])
            if current is not expected_status:
                raise ManagedStateConflict(
                    "material_analysis_state_conflict",
                    f"Material {material_id} latest Attempt is {current.value}; "
                    f"expected {expected_status.value}",
                )
            attempt, job = self._insert_attempt(
                connection,
                operation_type="material_analysis",
                owner_type="material",
                owner_id=str(material_id),
                command_id=command_id,
                now=now,
            )
            action_name = (
                "retried" if expected_status is AttemptStatus.FAILED else "resumed"
            )
            self._event(
                connection,
                event_type=f"material_analysis.{action_name}",
                occurred_at=now,
                object_type="material",
                object_id=str(material_id),
                command_id=command_id,
                attempt_id=attempt["attempt_id"],
                job_id=job["job_id"],
                payload={"previous_attempt_id": latest["attempt_id"]},
            )
            return {"attempt": attempt, "job": job}

        return self._idempotent(
            command_id,
            f"jobs.{expected_status.value}_material_analysis",
            request,
            action,
        )

    def active_material_attempt_ids(self, material_id: MaterialId) -> tuple[str, ...]:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        placeholders = ",".join("?" for _ in _ACTIVE_ATTEMPT_STATUSES)
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT attempt_id FROM attempts
                WHERE material_id = ? AND status IN ({placeholders})
                ORDER BY sequence
                """,
                (str(material_id), *_ACTIVE_ATTEMPT_STATUSES),
            ).fetchall()
        return tuple(str(row["attempt_id"]) for row in rows)

    def purge_material_attempts(self, material_id: MaterialId) -> None:
        """Remove terminal execution rows after their Material is deleted."""

        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        with self._transaction() as connection:
            blockers = connection.execute(
                f"""
                SELECT attempt_id FROM attempts
                WHERE material_id = ? AND status IN (
                    {",".join("?" for _ in _ACTIVE_ATTEMPT_STATUSES)}
                ) ORDER BY sequence
                """,
                (str(material_id), *_ACTIVE_ATTEMPT_STATUSES),
            ).fetchall()
            if blockers:
                raise ActiveAttemptBlocker(
                    "material",
                    str(material_id),
                    [str(row["attempt_id"]) for row in blockers],
                )
            connection.execute(
                "DELETE FROM attempts WHERE material_id = ?",
                (str(material_id),),
            )

    def replay_material_deletion(
        self,
        command_id: str,
        material_id: MaterialId,
    ) -> bool:
        request = {"material_id": str(material_id)}
        return (
            self.replay_idempotent(
                command_id,
                "materials.delete",
                request,
            )
            is not None
        )

    def record_material_deletion(
        self,
        command_id: str,
        material_id: MaterialId,
    ) -> IdempotentResult:
        request = {"material_id": str(material_id)}

        def action(
            _connection: sqlite3.Connection,
            _now: str,
        ) -> JsonObject:
            return {"material_id": str(material_id), "deleted": True}

        return self._idempotent(
            command_id,
            "materials.delete",
            request,
            action,
        )

    def claim_next_job(
        self,
        worker_id: str,
        process_id: int,
        *,
        job_id: JobId | None = None,
    ) -> JsonObject | None:
        normalized_worker = _normalise_name(worker_id, "worker_id")
        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id <= 0
        ):
            raise ValueError("process_id must be a positive integer")
        if job_id is not None and not isinstance(job_id, JobId):
            raise TypeError("job_id must be a JobId or None")
        with self._transaction() as connection:
            if job_id is None:
                row = connection.execute(
                    """
                    SELECT jobs.job_id, jobs.attempt_id
                    FROM jobs JOIN attempts USING (attempt_id)
                    WHERE jobs.status = 'queued' AND attempts.status = 'queued'
                    ORDER BY jobs.created_at, jobs.job_id
                    LIMIT 1
                    """
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT jobs.job_id, jobs.attempt_id
                    FROM jobs JOIN attempts USING (attempt_id)
                    WHERE jobs.job_id = ?
                      AND jobs.status = 'queued'
                      AND attempts.status = 'queued'
                    """,
                    (str(job_id),),
                ).fetchone()
            if row is None:
                return None
            return self._claim_job_row(
                connection,
                row,
                worker_id=normalized_worker,
                process_id=process_id,
            )

    def claim_next_supervised_job(
        self,
        worker_id: str,
        process_id: int,
        *,
        max_active_jobs: int,
    ) -> JsonObject | None:
        """Atomically claim the oldest capacity- and owner-eligible Job."""

        normalized_worker = _normalise_name(worker_id, "worker_id")
        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id <= 0
        ):
            raise ValueError("process_id must be a positive integer")
        if (
            not isinstance(max_active_jobs, int)
            or isinstance(max_active_jobs, bool)
            or max_active_jobs <= 0
        ):
            raise ValueError("max_active_jobs must be a positive integer")
        with self._transaction() as connection:
            active = int(
                connection.execute(
                    """
                    SELECT count(*)
                    FROM jobs JOIN attempts USING (attempt_id)
                    WHERE jobs.status IN ('running', 'retrying', 'stopping')
                      AND attempts.status IN ('running', 'retrying', 'stopping')
                    """
                ).fetchone()[0]
            )
            if active >= max_active_jobs:
                return None
            row = connection.execute(
                """
                SELECT queued_jobs.job_id, queued_jobs.attempt_id
                FROM jobs AS queued_jobs
                JOIN attempts AS queued_attempts USING (attempt_id)
                WHERE queued_jobs.status = 'queued'
                  AND queued_attempts.status = 'queued'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM attempts AS active_attempts
                    WHERE active_attempts.status IN (
                        'running', 'retrying', 'stopping'
                    )
                      AND (
                        (
                          queued_attempts.material_id IS NOT NULL
                          AND active_attempts.material_id = queued_attempts.material_id
                        ) OR (
                          queued_attempts.run_id IS NOT NULL
                          AND active_attempts.run_id = queued_attempts.run_id
                        ) OR (
                          queued_attempts.render_variant_id IS NOT NULL
                          AND active_attempts.render_variant_id =
                              queued_attempts.render_variant_id
                        )
                      )
                  )
                ORDER BY queued_jobs.created_at, queued_jobs.job_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            return self._claim_job_row(
                connection,
                row,
                worker_id=normalized_worker,
                process_id=process_id,
            )

    def adopt_supervised_job(
        self,
        job_id: JobId,
        attempt_id: AttemptId,
        *,
        expected_worker_id: str,
        expected_process_id: int,
        worker_id: str,
        process_id: int,
    ) -> JsonObject:
        """Transfer one exact supervisor lease to its launched child process."""

        if not isinstance(job_id, JobId):
            raise TypeError("job_id must be a JobId")
        if not isinstance(attempt_id, AttemptId):
            raise TypeError("attempt_id must be an AttemptId")
        expected_worker = _normalise_name(expected_worker_id, "expected_worker_id")
        normalized_worker = _normalise_name(worker_id, "worker_id")
        for value, label in (
            (expected_process_id, "expected_process_id"),
            (process_id, "process_id"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT jobs.*, attempts.status AS attempt_status
                FROM jobs JOIN attempts USING (attempt_id)
                WHERE jobs.job_id = ? AND jobs.attempt_id = ?
                """,
                (str(job_id), str(attempt_id)),
            ).fetchone()
            if row is None:
                raise ManagedStateConflict(
                    "supervisor_lease_mismatch",
                    f"Job {job_id} is not bound to Attempt {attempt_id}",
                )
            if (
                row["status"] != AttemptStatus.RUNNING.value
                or row["attempt_status"] != AttemptStatus.RUNNING.value
                or row["worker_id"] != expected_worker
                or row["process_id"] != expected_process_id
            ):
                raise ManagedStateConflict(
                    "supervisor_lease_mismatch",
                    f"Job {job_id} no longer owns the expected supervisor lease",
                )
            now = self._now()
            connection.execute(
                """
                UPDATE jobs
                SET worker_id = ?, process_id = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (normalized_worker, process_id, now, now, str(job_id)),
            )
            return {
                "attempt": self._attempt_record(connection, str(attempt_id)),
                "job": self._job_record(connection, str(job_id)),
            }

    def _claim_job_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        worker_id: str,
        process_id: int,
    ) -> JsonObject:
        now = self._now()
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, worker_id = ?, process_id = ?, heartbeat_at = ?,
                updated_at = ?
            WHERE job_id = ? AND status = ?
            """,
            (
                AttemptStatus.RUNNING.value,
                worker_id,
                process_id,
                now,
                now,
                row["job_id"],
                AttemptStatus.QUEUED.value,
            ),
        )
        connection.execute(
            """
            UPDATE attempts
            SET status = ?, started_at = coalesce(started_at, ?), updated_at = ?
            WHERE attempt_id = ? AND status = ?
            """,
            (
                AttemptStatus.RUNNING.value,
                now,
                now,
                row["attempt_id"],
                AttemptStatus.QUEUED.value,
            ),
        )
        attempt = self._attempt_record(connection, row["attempt_id"])
        self._update_owner_for_claim(connection, attempt, now)
        self._event(
            connection,
            event_type="attempt.started",
            occurred_at=now,
            object_type=attempt["owner_type"],
            object_id=attempt["owner_id"],
            command_id=attempt["command_id"],
            attempt_id=attempt["attempt_id"],
            job_id=row["job_id"],
        )
        return {
            "attempt": self._attempt_record(connection, row["attempt_id"]),
            "job": self._job_record(connection, row["job_id"]),
        }

    def heartbeat_job(
        self,
        job_id: JobId,
        *,
        progress: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        progress_json = None if progress is None else _canonical_json(dict(progress))
        with self._transaction() as connection:
            job = self._require_job_row(connection, str(job_id))
            if job["status"] not in {
                AttemptStatus.RUNNING.value,
                AttemptStatus.RETRYING.value,
                AttemptStatus.STOPPING.value,
            }:
                raise ManagedStateConflict(
                    "job_not_active",
                    f"Job {job_id} is not active",
                )
            now = self._now()
            if progress_json is None:
                connection.execute(
                    """
                    UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ?
                    """,
                    (now, now, str(job_id)),
                )
            else:
                connection.execute(
                    """
                    UPDATE jobs
                    SET heartbeat_at = ?, progress_json = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, progress_json, now, str(job_id)),
                )
            return self._job_record(connection, str(job_id))

    def request_stop(
        self,
        command_id: str,
        attempt_id: AttemptId,
    ) -> IdempotentResult:
        request = {"attempt_id": str(attempt_id)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            attempt = self._require_attempt_row(connection, str(attempt_id))
            job = self._job_for_attempt(connection, str(attempt_id))
            status = AttemptStatus(attempt["status"])
            if status in TERMINAL_ATTEMPT_STATUSES:
                return {
                    "attempt": self._attempt_record(connection, str(attempt_id)),
                    "job": self._job_record(connection, job["job_id"]),
                }
            if status is AttemptStatus.QUEUED:
                self._finish_attempt(
                    connection,
                    attempt_id=str(attempt_id),
                    status=AttemptStatus.INTERRUPTED,
                    now=now,
                )
                self._update_owner_terminal(
                    connection,
                    self._attempt_record(connection, str(attempt_id)),
                    AttemptStatus.INTERRUPTED,
                    now,
                    None,
                )
            else:
                connection.execute(
                    """
                    UPDATE attempts SET status = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (AttemptStatus.STOPPING.value, now, str(attempt_id)),
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, stop_requested = 1, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (AttemptStatus.STOPPING.value, now, job["job_id"]),
                )
            current_attempt = self._attempt_record(connection, str(attempt_id))
            current_job = self._job_record(connection, job["job_id"])
            self._event(
                connection,
                event_type=(
                    "attempt.interrupted"
                    if current_attempt["status"] == AttemptStatus.INTERRUPTED.value
                    else "attempt.stop_requested"
                ),
                occurred_at=now,
                object_type=current_attempt["owner_type"],
                object_id=current_attempt["owner_id"],
                command_id=command_id,
                attempt_id=str(attempt_id),
                job_id=current_job["job_id"],
            )
            return {"attempt": current_attempt, "job": current_job}

        return self._idempotent(command_id, "jobs.request_stop", request, action)

    def mark_attempt_retrying(self, attempt_id: AttemptId) -> JsonObject:
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.RETRYING,
            allowed={AttemptStatus.RUNNING},
        )

    def mark_attempt_interrupted(self, attempt_id: AttemptId) -> JsonObject:
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.INTERRUPTED,
            allowed={
                AttemptStatus.RUNNING,
                AttemptStatus.RETRYING,
                AttemptStatus.STOPPING,
            },
        )

    def mark_attempt_failed(
        self,
        attempt_id: AttemptId,
        error_message: str,
    ) -> JsonObject:
        error = _normalise_name(error_message, "error_message")
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.FAILED,
            allowed={
                AttemptStatus.QUEUED,
                AttemptStatus.RUNNING,
                AttemptStatus.RETRYING,
                AttemptStatus.STOPPING,
            },
            error_message=error,
        )

    def complete_material_attempt(self, attempt_id: AttemptId) -> JsonObject:
        with self._read() as connection:
            attempt = self._require_attempt_row(connection, str(attempt_id))
            if attempt["operation_type"] != "material_analysis":
                raise ManagedStateConflict(
                    "completion_owned_by_service",
                    "Run and Render completion must commit through their owning service",
                )
        return self._transition_attempt(
            attempt_id,
            AttemptStatus.COMPLETE,
            # Material publication is atomic but cannot be rolled back once the
            # canonical Memory commit point is visible.  A late Stop request
            # therefore completes successfully instead of reporting an
            # interrupted Material that is already Ready.
            allowed={
                AttemptStatus.RUNNING,
                AttemptStatus.RETRYING,
                AttemptStatus.STOPPING,
            },
        )

    def _transition_attempt(
        self,
        attempt_id: AttemptId,
        target: AttemptStatus,
        *,
        allowed: set[AttemptStatus],
        error_message: str | None = None,
    ) -> JsonObject:
        with self._transaction() as connection:
            row = self._require_attempt_row(connection, str(attempt_id))
            current = AttemptStatus(row["status"])
            if current not in allowed:
                raise ManagedStateConflict(
                    "attempt_transition_not_allowed",
                    f"Attempt {attempt_id} cannot transition from {current} to {target}",
                )
            now = self._now()
            if target in TERMINAL_ATTEMPT_STATUSES:
                self._finish_attempt(
                    connection,
                    attempt_id=str(attempt_id),
                    status=target,
                    now=now,
                    error_message=error_message,
                )
                self._update_owner_terminal(
                    connection,
                    self._attempt_record(connection, str(attempt_id)),
                    target,
                    now,
                    error_message,
                )
            else:
                connection.execute(
                    """
                    UPDATE attempts SET status = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (target.value, now, str(attempt_id)),
                )
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (target.value, now, str(attempt_id)),
                )
            attempt = self._attempt_record(connection, str(attempt_id))
            job = self._job_for_attempt(connection, str(attempt_id))
            self._event(
                connection,
                event_type=f"attempt.{target.value}",
                occurred_at=now,
                object_type=attempt["owner_type"],
                object_id=attempt["owner_id"],
                command_id=attempt["command_id"],
                attempt_id=str(attempt_id),
                job_id=job["job_id"],
                payload={} if error_message is None else {"error": error_message},
            )
            return {
                "attempt": attempt,
                "job": self._job_record(connection, job["job_id"]),
            }

    def interrupt_orphaned_jobs(self, heartbeat_before: datetime) -> tuple[str, ...]:
        cutoff = _iso(heartbeat_before)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT jobs.attempt_id
                FROM jobs JOIN attempts USING (attempt_id)
                WHERE jobs.status IN ('running', 'retrying', 'stopping')
                  AND attempts.status IN ('running', 'retrying', 'stopping')
                  AND (jobs.heartbeat_at IS NULL OR jobs.heartbeat_at < ?)
                ORDER BY jobs.created_at
                """,
                (cutoff,),
            ).fetchall()
            interrupted: list[str] = []
            for row in rows:
                attempt_id = row["attempt_id"]
                now = self._now()
                self._finish_attempt(
                    connection,
                    attempt_id=attempt_id,
                    status=AttemptStatus.INTERRUPTED,
                    now=now,
                    error_message="Worker heartbeat expired",
                )
                attempt = self._attempt_record(connection, attempt_id)
                self._update_owner_terminal(
                    connection,
                    attempt,
                    AttemptStatus.INTERRUPTED,
                    now,
                    "Worker heartbeat expired",
                )
                job = self._job_for_attempt(connection, attempt_id)
                self._event(
                    connection,
                    event_type="attempt.orphaned",
                    occurred_at=now,
                    object_type=attempt["owner_type"],
                    object_id=attempt["owner_id"],
                    command_id=attempt["command_id"],
                    attempt_id=attempt_id,
                    job_id=job["job_id"],
                )
                interrupted.append(attempt_id)
            return tuple(interrupted)

    def get_attempt(self, attempt_id: AttemptId) -> JsonObject:
        with self._read() as connection:
            return self._attempt_record(connection, str(attempt_id))

    def record_attempt_model_usage(
        self,
        attempt_id: AttemptId,
        model_usage_summary: Mapping[str, Any],
    ) -> JsonObject:
        with self._transaction() as connection:
            self._write_attempt_model_usage(
                connection,
                str(attempt_id),
                model_usage_summary,
            )
            return self._attempt_record(connection, str(attempt_id))

    def get_job(self, job_id: JobId) -> JsonObject:
        with self._read() as connection:
            return self._job_record(connection, str(job_id))

    def get_job_for_attempt(self, attempt_id: AttemptId) -> JsonObject:
        with self._read() as connection:
            row = self._job_for_attempt(connection, str(attempt_id))
            return self._job_record(connection, row["job_id"])

    def list_attempts(
        self,
        *,
        owner_type: str | None = None,
        owner_id: str | None = None,
        statuses: Sequence[AttemptStatus] = (),
        include_dismissed: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JsonObject]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
        ):
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        conditions: list[str] = []
        parameters: list[Any] = []
        if owner_type is not None or owner_id is not None:
            if owner_type not in {"material", "run", "render_variant"} or not owner_id:
                raise ValueError("owner_type and owner_id must identify one owner")
            column = {
                "material": "material_id",
                "run": "run_id",
                "render_variant": "render_variant_id",
            }[owner_type]
            conditions.append(f"{column} = ?")
            parameters.append(owner_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            parameters.extend(status.value for status in statuses)
        if not isinstance(include_dismissed, bool):
            raise TypeError("include_dismissed must be a boolean")
        if not include_dismissed:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM activity_dismissals "
                "WHERE activity_dismissals.attempt_id = attempts.attempt_id)"
            )
        where = "" if not conditions else "WHERE " + " AND ".join(conditions)
        parameters.extend((limit, offset))
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT attempt_id FROM attempts {where}
                ORDER BY created_at DESC, attempt_id DESC LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
            return [self._attempt_record(connection, row[0]) for row in rows]

    def dismiss_activity_attempts(
        self,
        command_id: str,
        attempt_ids: Sequence[AttemptId],
    ) -> IdempotentResult:
        identifiers = tuple(str(value) for value in attempt_ids)
        if not identifiers:
            raise ValueError("attempt_ids must not be empty")
        if len(identifiers) > 500:
            raise ValueError("attempt_ids must contain at most 500 entries")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("attempt_ids must not contain duplicates")
        request = {"attempt_ids": list(identifiers)}

        def action(connection: sqlite3.Connection, now: str) -> JsonObject:
            placeholders = ",".join("?" for _ in identifiers)
            rows = connection.execute(
                f"SELECT attempt_id, status FROM attempts "
                f"WHERE attempt_id IN ({placeholders})",
                identifiers,
            ).fetchall()
            by_id = {str(row["attempt_id"]): str(row["status"]) for row in rows}
            for attempt_id in identifiers:
                if attempt_id not in by_id:
                    raise ManagedStateNotFound("attempt", attempt_id)
                if AttemptStatus(by_id[attempt_id]) not in TERMINAL_ATTEMPT_STATUSES:
                    raise ManagedStateConflict(
                        "activity_attempt_not_terminal",
                        f"Activity Attempt {attempt_id!r} is not terminal",
                    )
            connection.executemany(
                """
                INSERT INTO activity_dismissals (attempt_id, dismissed_at)
                VALUES (?, ?)
                ON CONFLICT(attempt_id) DO NOTHING
                """,
                ((attempt_id, now) for attempt_id in identifiers),
            )
            return {"attempt_ids": list(identifiers), "dismissed": len(identifiers)}

        return self._idempotent(
            command_id,
            "activity.dismiss_attempts",
            request,
            action,
        )

    def event_bounds(self) -> tuple[int | None, int | None]:
        """Return the currently replayable inclusive durable Event bounds."""

        with self._read() as connection:
            row = connection.execute(
                "SELECT min(event_id), max(event_id) FROM events"
            ).fetchone()
            if row is None:  # pragma: no cover - aggregate always returns one row
                return None, None
            return (
                None if row[0] is None else int(row[0]),
                None if row[1] is None else int(row[1]),
            )

    def read_event_page(
        self,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> JsonObject:
        """Read replay bounds and one ordered page from one SQLite snapshot."""

        if (
            not isinstance(after_event_id, int)
            or isinstance(after_event_id, bool)
            or after_event_id < 0
            or after_event_id > 9_223_372_036_854_775_807
        ):
            raise ValueError("after_event_id must be a non-negative SQLite integer")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        with self._read() as connection:
            rows = connection.execute(
                """
                WITH bounds AS (
                    SELECT min(event_id) AS first_event_id,
                           max(event_id) AS last_event_id
                    FROM events
                ), page AS (
                    SELECT * FROM events
                    WHERE event_id > ?
                    ORDER BY event_id
                    LIMIT ?
                )
                SELECT bounds.first_event_id AS available_first_event_id,
                       bounds.last_event_id AS available_last_event_id,
                       page.*
                FROM bounds LEFT JOIN page ON 1 = 1
                ORDER BY page.event_id
                """,
                (after_event_id, limit),
            ).fetchall()
            first_event_id = rows[0]["available_first_event_id"]
            last_event_id = rows[0]["available_last_event_id"]
            items = [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "occurred_at": row["occurred_at"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "command_id": row["command_id"],
                    "attempt_id": row["attempt_id"],
                    "job_id": row["job_id"],
                    "payload": _parse_json(row["payload_json"]),
                    "schema_version": row["schema_version"],
                }
                for row in rows
                if row["event_id"] is not None
            ]
            return {
                "first_event_id": first_event_id,
                "last_event_id": last_event_id,
                "items": items,
            }

    def list_events(
        self, after_event_id: int = 0, limit: int = 200
    ) -> list[JsonObject]:
        items = self.read_event_page(after_event_id, limit)["items"]
        if not isinstance(items, list):  # pragma: no cover - internal invariant
            raise SQLiteStoreError("Invalid durable event page")
        return items

    def _insert_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        operation_type: str,
        owner_type: str,
        owner_id: str,
        command_id: str,
        now: str,
    ) -> tuple[JsonObject, JsonObject]:
        owner_columns = {
            "material": ("material_id", owner_id),
            "run": ("run_id", owner_id),
            "render_variant": ("render_variant_id", owner_id),
        }
        try:
            owner_column, _ = owner_columns[owner_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported Attempt owner: {owner_type!r}") from exc
        sequence = int(
            connection.execute(
                f"""
                SELECT coalesce(max(sequence), 0) + 1
                FROM attempts WHERE {owner_column} = ?
                """,
                (owner_id,),
            ).fetchone()[0]
        )
        attempt_id = str(AttemptId.new())
        job_id = str(JobId.new())
        owner_values = {
            "material_id": owner_id if owner_type == "material" else None,
            "run_id": owner_id if owner_type == "run" else None,
            "render_variant_id": owner_id if owner_type == "render_variant" else None,
        }
        connection.execute(
            """
            INSERT INTO attempts (
                attempt_id, operation_type, material_id, run_id,
                render_variant_id, sequence, status, command_id, error_message,
                created_at, started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?)
            """,
            (
                attempt_id,
                operation_type,
                owner_values["material_id"],
                owner_values["run_id"],
                owner_values["render_variant_id"],
                sequence,
                AttemptStatus.QUEUED.value,
                command_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, attempt_id, status, stop_requested, worker_id,
                process_id, heartbeat_at, progress_json, created_at, updated_at
            ) VALUES (?, ?, ?, 0, NULL, NULL, NULL, '{}', ?, ?)
            """,
            (job_id, attempt_id, AttemptStatus.QUEUED.value, now, now),
        )
        return (
            self._attempt_record(connection, attempt_id),
            self._job_record(connection, job_id),
        )

    @staticmethod
    def _require_attempt_row(
        connection: sqlite3.Connection,
        attempt_id: str,
    ) -> sqlite3.Row:
        return _expect_row(
            connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone(),
            "attempt",
            attempt_id,
        )

    def _require_attempt_owner(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        owner_type: str,
        owner_id: str,
    ) -> sqlite3.Row:
        row = self._require_attempt_row(connection, attempt_id)
        column = {
            "material": "material_id",
            "run": "run_id",
            "render_variant": "render_variant_id",
        }[owner_type]
        if row[column] != owner_id:
            raise ManagedStateConflict(
                "attempt_owner_mismatch",
                f"Attempt {attempt_id} does not belong to {owner_type} {owner_id}",
            )
        return row

    @staticmethod
    def _require_job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        return _expect_row(
            connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone(),
            "job",
            job_id,
        )

    def _job_for_attempt(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
    ) -> sqlite3.Row:
        return _expect_row(
            connection.execute(
                "SELECT * FROM jobs WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone(),
            "job_for_attempt",
            attempt_id,
        )

    def _attempt_record(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
    ) -> JsonObject:
        row = self._require_attempt_row(connection, attempt_id)
        if row["material_id"] is not None:
            owner_type, owner_id = "material", row["material_id"]
        elif row["run_id"] is not None:
            owner_type, owner_id = "run", row["run_id"]
        else:
            owner_type, owner_id = "render_variant", row["render_variant_id"]
        raw_usage = row["model_usage_summary_json"]
        usage = None if raw_usage is None else _parse_json(raw_usage)
        if usage is not None and not isinstance(usage, dict):
            raise SQLiteStoreError("Invalid Attempt model usage summary")
        return {
            "attempt_id": row["attempt_id"],
            "operation_type": row["operation_type"],
            "owner_type": owner_type,
            "owner_id": owner_id,
            "sequence": row["sequence"],
            "status": row["status"],
            "command_id": row["command_id"],
            "error_message": row["error_message"],
            "model_usage_summary": usage,
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _validate_checkpoint_record(value: Mapping[str, Any]) -> None:
        expected = {
            "run_id",
            "checkpoint_id",
            "source_attempt_id",
            "completed_stage",
            "relative_path",
            "content_sha256",
            "identity_signature",
            "schema_version",
            "created_at",
            "updated_at",
        }
        if set(value) != expected:
            raise SQLiteStoreError("Invalid ASTER checkpoint receipt fields")
        RunId.parse(str(value["run_id"]))
        AttemptId.parse(str(value["source_attempt_id"]))
        _validate_command_id(str(value["checkpoint_id"]))
        _checkpoint_stage(str(value["completed_stage"]))
        validate_portable_relative_file_path(str(value["relative_path"]))
        _checkpoint_digest(str(value["content_sha256"]), "content_sha256")
        _checkpoint_digest(
            str(value["identity_signature"]),
            "identity_signature",
        )
        if value["schema_version"] != "1.0":
            raise SQLiteStoreError("Unsupported ASTER checkpoint receipt schema")
        for field_name in ("created_at", "updated_at"):
            raw = value[field_name]
            if not isinstance(raw, str):
                raise SQLiteStoreError(
                    f"Invalid ASTER checkpoint {field_name}"
                )

    def _run_checkpoint_record(self, row: Mapping[str, Any]) -> JsonObject:
        value = {
            "run_id": row["run_id"],
            "checkpoint_id": row["checkpoint_id"],
            "source_attempt_id": row["source_attempt_id"],
            "completed_stage": row["completed_stage"],
            "relative_path": row["relative_path"],
            "content_sha256": row["content_sha256"],
            "identity_signature": row["identity_signature"],
            "schema_version": row["schema_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        self._validate_checkpoint_record(value)
        return value

    def _write_attempt_model_usage(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        model_usage_summary: Mapping[str, Any],
    ) -> None:
        if not isinstance(model_usage_summary, Mapping):
            raise TypeError("model_usage_summary must be a mapping")
        row = self._require_attempt_row(connection, attempt_id)
        if row["operation_type"] != "aster_planning":
            raise ManagedStateConflict(
                "attempt_usage_not_supported",
                "Model usage is only persisted for ASTER planning Attempts",
            )
        encoded = _canonical_json(dict(model_usage_summary))
        existing = row["model_usage_summary_json"]
        if existing is not None:
            if existing != encoded:
                raise ManagedStateConflict(
                    "attempt_usage_conflict",
                    f"Attempt {attempt_id} already has a different usage summary",
                )
            return
        connection.execute(
            """
            UPDATE attempts SET model_usage_summary_json = ?
            WHERE attempt_id = ?
            """,
            (encoded, attempt_id),
        )

    def _job_record(self, connection: sqlite3.Connection, job_id: str) -> JsonObject:
        row = self._require_job_row(connection, job_id)
        return {
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "status": row["status"],
            "stop_requested": bool(row["stop_requested"]),
            "worker_id": row["worker_id"],
            "process_id": row["process_id"],
            "heartbeat_at": row["heartbeat_at"],
            "progress": _parse_json(row["progress_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _finish_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        attempt_id: str,
        status: AttemptStatus,
        now: str,
        error_message: str | None = None,
    ) -> None:
        connection.execute(
            """
            UPDATE attempts
            SET status = ?, error_message = ?, finished_at = ?, updated_at = ?
            WHERE attempt_id = ?
            """,
            (status.value, error_message, now, now, attempt_id),
        )
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, stop_requested = 0, heartbeat_at = ?, updated_at = ?
            WHERE attempt_id = ?
            """,
            (status.value, now, now, attempt_id),
        )

    @staticmethod
    def _update_owner_for_claim(
        connection: sqlite3.Connection,
        attempt: Mapping[str, Any],
        now: str,
    ) -> None:
        if attempt["owner_type"] == "run":
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (RunStatus.PLANNERS.value, now, attempt["owner_id"]),
            )
        elif attempt["owner_type"] == "render_variant":
            connection.execute(
                """
                UPDATE render_variants SET status = ?, updated_at = ?
                WHERE render_variant_id = ?
                """,
                (
                    RenderVariantStatus.RENDERING.value,
                    now,
                    attempt["owner_id"],
                ),
            )

    @staticmethod
    def _update_owner_terminal(
        connection: sqlite3.Connection,
        attempt: Mapping[str, Any],
        status: AttemptStatus,
        now: str,
        error_message: str | None,
    ) -> None:
        if status is AttemptStatus.COMPLETE:
            return
        if attempt["owner_type"] == "run":
            run_status = (
                RunStatus.INTERRUPTED
                if status is AttemptStatus.INTERRUPTED
                else RunStatus.FAILED
            )
            connection.execute(
                """
                UPDATE runs SET status = ?, failure_message = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (run_status.value, error_message, now, attempt["owner_id"]),
            )
        elif attempt["owner_type"] == "render_variant":
            render_status = (
                RenderVariantStatus.INTERRUPTED
                if status is AttemptStatus.INTERRUPTED
                else RenderVariantStatus.FAILED
            )
            connection.execute(
                """
                UPDATE render_variants
                SET status = ?, failure_message = ?, updated_at = ?
                WHERE render_variant_id = ?
                """,
                (render_status.value, error_message, now, attempt["owner_id"]),
            )

    # ------------------------------------------------------------------
    # Diagnostics

    def integrity_check(self) -> str:
        with self._read() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    @property
    def schema_version(self) -> int:
        with self._read() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def database_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.database_path}{suffix}")
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def execute_external_command(
        self,
        command_id: str,
        command_kind: str,
        request: Mapping[str, Any],
        action: Callable[[], Mapping[str, Any]],
    ) -> IdempotentResult:
        """Protect one short external atomic write with a durable receipt.

        The callback runs while the SQLite write transaction is held, so local
        adapters cannot interleave the same managed command.  Callers remain
        responsible for making their external write atomic and restoring its
        prior bytes if this method raises after invoking the callback.
        """

        def execute(_connection: sqlite3.Connection, _now: str) -> JsonObject:
            value = dict(action())
            _canonical_json(value)
            return value

        return self._idempotent(command_id, command_kind, dict(request), execute)

    def command_receipt_exists(
        self,
        command_id: str,
        command_kind: str,
        request: Mapping[str, Any],
    ) -> bool:
        """Validate and report whether an exact durable receipt exists."""

        command_id = _validate_command_id(command_id)
        digest = _request_digest(dict(request))
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT command_kind, request_digest
                FROM idempotency_receipts WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
        if row is None:
            return False
        if row["command_kind"] != command_kind or row["request_digest"] != digest:
            raise IdempotencyConflict(command_id)
        return True


__all__ = [
    "ActiveAttemptBlocker",
    "IdempotencyConflict",
    "IdempotentResult",
    "ManagedStateConflict",
    "ManagedStateNotFound",
    "SQLiteApplicationStore",
    "SQLiteStoreError",
]
