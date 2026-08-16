"""Transport-safe JSON projections for Application read models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from cutmaster.application.jobs import AttemptView, EventView, JobView
from cutmaster.application.materials import (
    MaterialDetailView,
    MaterialMemoryView,
    MaterialView,
)
from cutmaster.application.projects import ProjectView
from cutmaster.application.runs import FrozenEditView, RunView
from cutmaster.application.settings import SettingsView, StorageReportView
from cutmaster.domain.ids import EntityId
from cutmaster.domain.projects import CreativeBrief


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
    attempts: tuple[AttemptView, ...] | None = None,
) -> dict[str, Any]:
    result = {
        **material_view(value.material),
        "duration_sec": value.duration_sec,
        "analysis_available": value.analysis_available,
        "reference_count": value.reference_count,
        "references": list(value.references),
        "source": json_value(value.source),
        "memory_summary": json_value(value.memory_summary),
    }
    if attempts is not None:
        result["attempts"] = [attempt_view(item) for item in attempts]
    return result


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
        "overlay_path": str(value.overlay_path),
        "data_root": str(value.data_root),
        "secrets": {
            "llm_configured": value.secrets.llm_configured,
            "vlm_configured": value.secrets.vlm_configured,
            "asr_configured": value.secrets.asr_configured,
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
        "direct_bundle_count": value.direct_bundle_count,
        "total_size_bytes": value.total_size_bytes,
    }


__all__ = [
    "attempt_view",
    "event_view",
    "execution_view",
    "frozen_edit_view",
    "job_view",
    "json_value",
    "material_detail_view",
    "material_memory_view",
    "material_view",
    "project_view",
    "run_view",
    "settings_view",
    "storage_report_view",
]
