"""Transport-safe JSON projections for Application read models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from cutmaster.application.jobs import (
    AttemptLogEntryView,
    AttemptLogPageView,
    AttemptView,
    EventView,
    JobView,
)
from cutmaster.application.jobs.usage import compact_usage_summary
from cutmaster.application.materials import (
    MaterialDetailView,
    MaterialMemoryView,
    MaterialView,
)
from cutmaster.application.projects import ProjectView
from cutmaster.application.renders import RenderVariantView
from cutmaster.application.runs import (
    FrozenEditReviewView,
    FrozenEditView,
    RunUsageView,
    RunView,
)
from cutmaster.application.settings import (
    DataRootMigrationPreflightView,
    DataRootMigrationView,
    SettingsView,
    StorageReportView,
)
from cutmaster.domain.ids import EntityId, ProjectId, RunId
from cutmaster.domain.projects import CreativeBrief
from cutmaster.infrastructure.persistence.sqlite import ManagedStateNotFound


def json_value(value: Any) -> Any:
    if isinstance(value, EntityId):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def material_view(value: MaterialView) -> dict[str, Any]:
    return {
        "material_id": str(value.material_id),
        "material_type": value.material_type.value,
        "name": value.name,
        "condition": value.condition.value,
        "reused": value.reused,
    }


def material_detail_view(
    value: MaterialDetailView,
    *,
    references: Sequence[Mapping[str, Any]],
    attempts: tuple[AttemptView, ...] | None = None,
) -> dict[str, Any]:
    material_id = str(value.material.material_id)
    preview_base = f"/api/materials/{material_id}"
    has_video_preview = bool(
        value.preview_available and value.material.material_type.value == "video"
    )
    has_music_preview = bool(
        value.preview_available and value.material.material_type.value == "music"
    )
    result = {
        **material_view(value.material),
        "duration_sec": value.duration_sec,
        "analysis_available": value.analysis_available,
        "reference_count": value.reference_count,
        "references": [json_value(item) for item in references],
        "source": json_value(value.source),
        "memory_summary": json_value(value.memory_summary),
        "analysis_cost_yuan": value.analysis_cost_yuan,
        "thumbnail_url": f"{preview_base}/thumbnail" if has_video_preview else None,
        "waveform_url": f"{preview_base}/waveform" if has_music_preview else None,
    }
    if attempts is not None:
        result["attempts"] = [attempt_view(item) for item in attempts]
    return result


def material_reference_views(
    application: Any,
    references: Sequence[str],
) -> list[dict[str, Any]]:
    """Resolve opaque deletion references into safe, linkable Web metadata.

    The persistence/deletion boundary deliberately keeps compact raw strings.
    They are parsed only here: the Web transport receives human labels and
    canonical route identity, never the raw reference token itself.
    """

    return [_material_reference_view(application, value) for value in references]


def _material_reference_view(application: Any, value: str) -> dict[str, Any]:
    parts = value.split(":")
    if len(parts) != 4 or parts[3] not in {"video", "music"}:
        return {"kind": "unknown", "navigation": None}
    owner_kind, owner_id, relation, _material_type = parts
    try:
        if owner_kind == "project" and relation == "current":
            project = application.projects.get(ProjectId.parse(owner_id))
            return {
                "kind": "project_current",
                "project_name": project.name,
                "navigation": {
                    "kind": "project",
                    "project_id": str(project.project_id),
                },
            }
        if owner_kind == "run" and relation == "snapshot":
            run = application.runs.get(RunId.parse(owner_id))
            project = application.projects.get(run.project_id)
            return {
                "kind": "run_snapshot",
                "project_name": project.name,
                "run_sequence": run.sequence,
                "navigation": {
                    "kind": "run",
                    "project_id": str(project.project_id),
                    "run_id": str(run.run_id),
                },
            }
    except (ManagedStateNotFound, ValueError):
        # A stale/deleted or malformed historical relation remains a blocker,
        # but no opaque identity is exposed to the browser.
        pass
    return {"kind": "unknown", "navigation": None}


def material_memory_view(value: MaterialMemoryView) -> dict[str, Any]:
    return {
        "material_id": str(value.material_id),
        "material_type": value.material_type.value,
        "tab": value.tab,
        "limit": value.limit,
        "offset": value.offset,
        "payload": json_value(value.payload),
    }


def creative_brief_view(value: CreativeBrief | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "editing_intent": value.editing_intent,
        "target_duration_sec": value.target_duration_sec,
    }


def project_view(value: ProjectView) -> dict[str, Any]:
    return {
        "project_id": str(value.project_id),
        "name": value.name,
        "video_material_ids": [str(item) for item in value.video_material_ids],
        "music_material_ids": [str(item) for item in value.music_material_ids],
        "creative_brief": creative_brief_view(value.creative_brief),
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def run_view(value: RunView) -> dict[str, Any]:
    return {
        "run_id": str(value.run_id),
        "project_id": str(value.project_id),
        "sequence": value.sequence,
        "status": value.status.value,
        "creative_brief": creative_brief_view(value.creative_brief),
        "video_material_ids": [str(item) for item in value.video_material_ids],
        "music_material_ids": [str(item) for item in value.music_material_ids],
        "failure_message": value.failure_message,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def run_usage_view(value: RunUsageView) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "attempt_usage": [
            {
                "attempt_id": str(item.attempt_id),
                "sequence": item.sequence,
                "status": item.status.value,
                "model_usage_summary": (
                    None
                    if item.model_usage_summary is None
                    else json_value(item.model_usage_summary)
                ),
            }
            for item in value.attempt_usage
        ],
        "run_total": json_value(value.run_total),
    }


def run_usage_total_view(value: RunUsageView) -> dict[str, Any]:
    return compact_usage_summary(value.run_total)


def frozen_edit_view(value: FrozenEditView) -> dict[str, Any]:
    return {
        "edit_id": str(value.edit_id),
        "run_id": str(value.run_id),
        "sequence": value.sequence,
        "origin": value.origin.value,
        "parent_edit_id": (
            None if value.parent_edit_id is None else str(value.parent_edit_id)
        ),
        "created_at": value.created_at.isoformat(),
    }


def frozen_edit_review_view(value: FrozenEditReviewView) -> dict[str, Any]:
    video_source_url = f"/api/materials/{value.video_material_id}/source"
    candidates = {
        group_id: [
            {
                **json_value(trajectory),
                "items": [
                    {
                        **json_value(candidate),
                        "media_url": video_source_url,
                    }
                    for candidate in trajectory["items"]
                ],
            }
            for trajectory in values
        ]
        for group_id, values in value.candidates.items()
    }
    return {
        "edit": frozen_edit_view(value.edit),
        "run": run_view(value.run),
        "versions": [frozen_edit_view(item) for item in value.versions],
        "plan": json_value(value.plan),
        "media": {
            "video": {
                "material_id": str(value.video_material_id),
                "source_url": video_source_url,
            },
            "music": {
                "material_id": str(value.music_material_id),
                "source_url": f"/api/materials/{value.music_material_id}/source",
            },
        },
        "slots": [json_value(item) for item in value.slots],
        "candidates": candidates,
        "variants": [
            render_variant_view(item, edit=value.edit, run=value.run)
            for item in value.variants
        ],
        "timeline": {
            "dialogue_cues": [json_value(item) for item in value.dialogue_cues],
            "music_beats_sec": list(value.music_beats_sec),
            "music_beats_available": value.music_beats_available,
        },
    }


def render_variant_view(
    value: RenderVariantView,
    *,
    edit: FrozenEditView,
    run: RunView,
) -> dict[str, Any]:
    ready = value.status.value == "ready" and value.master is not None
    return {
        "render_variant_id": str(value.render_variant_id),
        "project_id": str(run.project_id),
        "run_id": str(run.run_id),
        "run_sequence": run.sequence,
        "edit_id": str(edit.edit_id),
        "edit_sequence": edit.sequence,
        "edit_origin": edit.origin.value,
        "status": value.status.value,
        "specification": json_value(value.specification),
        "frame_count": value.frame_count,
        "duration_sec": value.duration_sec,
        "size_bytes": value.master_size_bytes,
        "failure_message": value.failure_message,
        "media_url": (
            f"/api/render-variants/{value.render_variant_id}/media" if ready else None
        ),
        "download_url": (
            f"/api/render-variants/{value.render_variant_id}/download"
            if ready
            else None
        ),
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def attempt_view(value: AttemptView) -> dict[str, Any]:
    return {
        "attempt_id": str(value.attempt_id),
        "operation_type": value.operation_type,
        "owner_type": value.owner_type,
        "owner_id": value.owner_id,
        "sequence": value.sequence,
        "status": value.status.value,
        "command_id": value.command_id,
        "error_message": value.error_message,
        "created_at": value.created_at.isoformat(),
        "started_at": (
            None if value.started_at is None else value.started_at.isoformat()
        ),
        "finished_at": (
            None if value.finished_at is None else value.finished_at.isoformat()
        ),
        "updated_at": value.updated_at.isoformat(),
    }


def job_view(value: JobView) -> dict[str, Any]:
    return {
        "job_id": str(value.job_id),
        "attempt_id": str(value.attempt_id),
        "status": value.status.value,
        "stop_requested": value.stop_requested,
        "worker_id": value.worker_id,
        "process_id": value.process_id,
        "heartbeat_at": (
            None if value.heartbeat_at is None else value.heartbeat_at.isoformat()
        ),
        "progress": json_value(value.progress),
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def execution_view(
    attempt: AttemptView,
    job: JobView,
) -> dict[str, object]:
    return {
        "attempt": attempt_view(attempt),
        "job": job_view(job),
    }


def attempt_log_entry_view(value: AttemptLogEntryView) -> dict[str, object]:
    return {
        "cursor": value.cursor,
        "timestamp": value.timestamp,
        "level": value.level,
        "component": value.component,
        "event": value.event,
        "fields": value.fields,
        "message": value.message,
    }


def attempt_log_page_view(value: AttemptLogPageView) -> dict[str, object]:
    return {
        "log": {"path": value.path, "exists": value.exists},
        "entries": [attempt_log_entry_view(item) for item in value.entries],
        "start_cursor": value.start_cursor,
        "end_cursor": value.end_cursor,
        "has_more_before": value.has_more_before,
    }


def event_view(value: EventView) -> dict[str, Any]:
    return {
        "event_id": value.event_id,
        "event_type": value.event_type,
        "occurred_at": value.occurred_at.isoformat(),
        "object_type": value.object_type,
        "object_id": value.object_id,
        "command_id": value.command_id,
        "attempt_id": None if value.attempt_id is None else str(value.attempt_id),
        "job_id": None if value.job_id is None else str(value.job_id),
        "payload": json_value(value.payload),
        "schema_version": value.schema_version,
    }


def settings_view(value: SettingsView) -> dict[str, Any]:
    return {
        "values": json_value(value.values),
        "base_path": str(value.base_path),
        "data_root": str(value.data_root),
        "secrets": {
            "llm_configured": value.secrets.llm_configured,
            "vlm_configured": value.secrets.vlm_configured,
            "asr_configured": value.secrets.asr_configured,
        },
        "connections": {
            "profile": value.connections.profile,
            "providers": json_value(value.connections.providers),
            "presets": json_value(value.connections.presets),
            "credentials": {
                capability: {
                    "configured": credential.configured,
                    "suffix": credential.suffix,
                    "source": credential.source,
                    "writable": credential.writable,
                }
                for capability, credential in value.connections.credentials.items()
            },
        },
    }


def storage_report_view(value: StorageReportView) -> dict[str, Any]:
    return {
        "data_root": str(value.data_root),
        "categories": [
            {
                "name": item.name,
                "file_count": item.file_count,
                "size_bytes": item.size_bytes,
            }
            for item in value.categories
        ],
        "total_size_bytes": value.total_size_bytes,
        "reveal_supported": value.reveal_supported,
    }


_MIGRATION_BLOCKER_METADATA_KEYS: dict[str, frozenset[str]] = {
    "active_attempt": frozenset({"attempt_id", "status", "owner_type", "owner_id"}),
    "cleanup_incomplete": frozenset({"remaining_entries"}),
    "historical_file_not_empty": frozenset({"relative_path"}),
    "invalid_root_owner_marker": frozenset({"relative_path"}),
    "unmanifested_destination_entry": frozenset({"relative_path"}),
    "unknown_namespace": frozenset({"relative_path"}),
    "unsafe_filesystem_entry": frozenset({"relative_path"}),
}


def _safe_migration_metadata(kind: str, metadata: object) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    allowed = _MIGRATION_BLOCKER_METADATA_KEYS.get(kind, frozenset())
    result: dict[str, Any] = {}
    for key in allowed:
        value = metadata.get(key)
        if key == "relative_path":
            if (
                not isinstance(value, str)
                or Path(value).is_absolute()
                or ".." in Path(value).parts
            ):
                continue
            result[key] = value
        elif key == "remaining_entries":
            if isinstance(value, (tuple, list)):
                result[key] = [
                    item
                    for item in value
                    if isinstance(item, str)
                    and not Path(item).is_absolute()
                    and ".." not in Path(item).parts
                ]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
    return result


def data_root_migration_blockers(value: object) -> list[dict[str, Any]]:
    blockers = getattr(value, "blockers")
    return [
        {
            "kind": blocker.kind,
            "metadata": _safe_migration_metadata(blocker.kind, blocker.metadata),
        }
        for blocker in blockers
    ]


def data_root_migration_preflight_view(
    value: DataRootMigrationPreflightView,
) -> dict[str, Any]:
    return {
        "source_root": str(value.source_root),
        "destination_root": str(value.destination_root),
        "eligible": value.eligible,
        "estimated_file_count": value.estimated_file_count,
        "estimated_size_bytes": value.estimated_size_bytes,
        "blockers": data_root_migration_blockers(value),
    }


def data_root_migration_view(value: DataRootMigrationView) -> dict[str, Any]:
    return {
        "migration_id": value.migration_id,
        "source_root": str(value.source_root),
        "destination_root": str(value.destination_root),
        "status": value.status.value,
        "progress": {
            "phase": value.phase,
            "files_completed": value.files_completed,
            "files_total": value.files_total,
            "bytes_completed": value.bytes_completed,
            "bytes_total": value.bytes_total,
        },
        "cancel_requested": value.cancel_requested,
        "blockers": data_root_migration_blockers(value),
        "failure": (
            None
            if value.failure_code is None
            else {
                "code": value.failure_code,
            }
        ),
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
        "started_at": (
            None if value.started_at is None else value.started_at.isoformat()
        ),
        "finished_at": (
            None if value.finished_at is None else value.finished_at.isoformat()
        ),
    }


__all__ = [
    "attempt_view",
    "data_root_migration_preflight_view",
    "data_root_migration_blockers",
    "data_root_migration_view",
    "event_view",
    "execution_view",
    "frozen_edit_review_view",
    "frozen_edit_view",
    "job_view",
    "json_value",
    "material_detail_view",
    "material_memory_view",
    "material_reference_views",
    "material_view",
    "project_view",
    "render_variant_view",
    "run_usage_total_view",
    "run_usage_view",
    "run_view",
    "settings_view",
    "storage_report_view",
]
