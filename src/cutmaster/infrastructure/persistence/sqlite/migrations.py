"""Ordered, transactional SQLite schema migrations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                brief_intent TEXT,
                brief_target_duration REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (brief_intent IS NULL AND brief_target_duration IS NULL)
                    OR
                    (length(trim(brief_intent)) > 0 AND brief_target_duration > 0)
                )
            ) STRICT
            """,
            """
            CREATE TABLE project_materials (
                project_id TEXT NOT NULL
                    REFERENCES projects(project_id) ON DELETE CASCADE,
                material_type TEXT NOT NULL
                    CHECK (material_type IN ('video', 'music')),
                position INTEGER NOT NULL CHECK (position >= 0),
                material_id TEXT NOT NULL,
                PRIMARY KEY (project_id, material_type, position),
                UNIQUE (project_id, material_type, material_id)
            ) STRICT
            """,
            """
            CREATE INDEX project_materials_material_idx
            ON project_materials(material_id)
            """,
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL
                    REFERENCES projects(project_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                status TEXT NOT NULL CHECK (
                    status IN ('queued', 'planners', 'complete', 'failed', 'interrupted')
                ),
                editing_intent TEXT NOT NULL CHECK (length(trim(editing_intent)) > 0),
                target_duration_sec REAL NOT NULL CHECK (target_duration_sec > 0),
                configuration_json TEXT NOT NULL CHECK (json_valid(configuration_json)),
                failure_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (project_id, sequence)
            ) STRICT
            """,
            """
            CREATE INDEX runs_project_created_idx
            ON runs(project_id, created_at DESC)
            """,
            """
            CREATE TABLE run_materials (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                material_type TEXT NOT NULL
                    CHECK (material_type IN ('video', 'music')),
                position INTEGER NOT NULL CHECK (position >= 0),
                material_id TEXT NOT NULL,
                PRIMARY KEY (run_id, material_type, position),
                UNIQUE (run_id, material_type, material_id)
            ) STRICT
            """,
            """
            CREATE INDEX run_materials_material_idx
            ON run_materials(material_id)
            """,
            """
            CREATE TABLE frozen_edits (
                edit_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                origin TEXT NOT NULL CHECK (origin IN ('initial', 'guided_revision')),
                parent_edit_id TEXT REFERENCES frozen_edits(edit_id),
                plan_relative_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, sequence),
                CHECK (
                    (origin = 'initial' AND parent_edit_id IS NULL)
                    OR
                    (origin = 'guided_revision' AND parent_edit_id IS NOT NULL)
                )
            ) STRICT
            """,
            """
            CREATE UNIQUE INDEX frozen_edits_one_initial_idx
            ON frozen_edits(run_id) WHERE origin = 'initial'
            """,
            """
            CREATE TABLE render_variants (
                render_variant_id TEXT PRIMARY KEY,
                edit_id TEXT NOT NULL
                    REFERENCES frozen_edits(edit_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'rendering', 'ready', 'failed',
                        'interrupted', 'unavailable'
                    )
                ),
                specification_json TEXT NOT NULL CHECK (json_valid(specification_json)),
                specification_digest TEXT NOT NULL,
                master_relative_path TEXT,
                master_size_bytes INTEGER CHECK (master_size_bytes >= 0),
                master_sha256 TEXT,
                frame_count INTEGER CHECK (frame_count >= 0),
                duration_sec REAL CHECK (duration_sec >= 0),
                failure_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (edit_id, specification_digest),
                CHECK (
                    status != 'ready'
                    OR (
                        master_relative_path IS NOT NULL
                        AND master_size_bytes IS NOT NULL
                        AND length(master_sha256) = 64
                        AND frame_count IS NOT NULL
                        AND duration_sec IS NOT NULL
                    )
                )
            ) STRICT
            """,
            """
            CREATE INDEX render_variants_edit_created_idx
            ON render_variants(edit_id, created_at DESC)
            """,
            """
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL CHECK (
                    operation_type IN ('material_analysis', 'aster_planning', 'rendering')
                ),
                material_id TEXT,
                run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
                render_variant_id TEXT
                    REFERENCES render_variants(render_variant_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'running', 'retrying', 'stopping',
                        'interrupted', 'complete', 'failed'
                    )
                ),
                command_id TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                CHECK (
                    (material_id IS NOT NULL) +
                    (run_id IS NOT NULL) +
                    (render_variant_id IS NOT NULL) = 1
                )
            ) STRICT
            """,
            """
            CREATE UNIQUE INDEX attempts_material_sequence_idx
            ON attempts(material_id, sequence) WHERE material_id IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX attempts_run_sequence_idx
            ON attempts(run_id, sequence) WHERE run_id IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX attempts_render_sequence_idx
            ON attempts(render_variant_id, sequence)
            WHERE render_variant_id IS NOT NULL
            """,
            """
            CREATE INDEX attempts_status_created_idx
            ON attempts(status, created_at)
            """,
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE
                    REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'queued', 'running', 'retrying', 'stopping',
                        'interrupted', 'complete', 'failed'
                    )
                ),
                stop_requested INTEGER NOT NULL DEFAULT 0
                    CHECK (stop_requested IN (0, 1)),
                worker_id TEXT,
                process_id INTEGER CHECK (process_id > 0),
                heartbeat_at TEXT,
                progress_json TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(progress_json)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE INDEX jobs_queue_idx ON jobs(status, created_at)
            """,
            """
            CREATE TABLE events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                command_id TEXT,
                attempt_id TEXT,
                job_id TEXT,
                payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
                schema_version TEXT NOT NULL DEFAULT '1.0'
            ) STRICT
            """,
            """
            CREATE INDEX events_occurred_idx ON events(event_id)
            """,
            """
            CREATE TABLE idempotency_receipts (
                command_id TEXT PRIMARY KEY,
                command_kind TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                result_json TEXT NOT NULL CHECK (json_valid(result_json)),
                created_at TEXT NOT NULL
            ) STRICT
            """,
        ),
    ),
    Migration(
        version=2,
        statements=(
            """
            ALTER TABLE attempts
            ADD COLUMN model_usage_summary_json TEXT
                CHECK (
                    model_usage_summary_json IS NULL
                    OR json_valid(model_usage_summary_json)
                )
            """,
        ),
    ),
    Migration(
        version=3,
        statements=(
            """
            CREATE TABLE run_checkpoints (
                run_id TEXT PRIMARY KEY
                    REFERENCES runs(run_id) ON DELETE CASCADE,
                checkpoint_id TEXT NOT NULL UNIQUE,
                source_attempt_id TEXT NOT NULL
                    REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                completed_stage TEXT NOT NULL CHECK (
                    completed_stage IN (
                        'replan_pending', 'arrangement_architect', 'story_editor',
                        'timeline_scout', 'edit_composer', 'revision_editor'
                    )
                ),
                relative_path TEXT NOT NULL,
                content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
                identity_signature TEXT NOT NULL
                    CHECK (length(identity_signature) = 64),
                schema_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT
            """,
            """
            ALTER TABLE attempts
            ADD COLUMN resume_checkpoint_json TEXT
                CHECK (
                    resume_checkpoint_json IS NULL
                    OR json_valid(resume_checkpoint_json)
                )
            """,
        ),
    ),
    Migration(
        version=4,
        statements=(
            """
            ALTER TABLE runs
            ADD COLUMN planning_options_json TEXT NOT NULL
                DEFAULT '{"target_shot_length_sec":4.0,"prompt_type":"event","video_title":"","max_clip_duration_sec":null}'
                CHECK (json_valid(planning_options_json))
            """,
        ),
    ),
)


LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


__all__ = ["LATEST_SCHEMA_VERSION", "MIGRATIONS", "Migration"]
