"""Reusable material-analysis cache keys and checkpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import (
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.runtime.observability import log_event


ANALYSIS_SCHEMA_VERSION = "1.0"


def file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def material_directory(
    video_path: Path,
    video_title: str,
    subtitle_path: Path | None,
    material_config: MaterialAnalysisConfig,
    detection_config: ShotDetectionConfig,
    asr_config: ASRConfig,
    annotation_config: ShotAnnotationConfig,
    llm_config: LLMConfig,
    vlm_config: VLMConfig,
) -> tuple[Path, dict[str, Any]]:
    source_signature = file_signature(video_path)
    asset_id = f"{video_path.stem}-{digest(source_signature)}"
    analysis_signature = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "source": source_signature,
        "video_title": video_title or video_path.stem,
        "subtitle": (
            file_signature(subtitle_path)
            if subtitle_path is not None
            else {
                "backend": asr_config.backend,
                "max_chars": asr_config.max_chars,
                "max_subtitle_duration_sec": asr_config.max_subtitle_duration_sec,
            }
        ),
        "llm": {
            "model": llm_config.model,
            "base_url": llm_config.base_url,
            "enable_thinking": llm_config.enable_thinking,
            "temperature": llm_config.temperature,
            "max_tokens": llm_config.max_tokens,
        },
        "vlm": {
            "model": vlm_config.model,
            "base_url": vlm_config.base_url,
            "enable_thinking": vlm_config.enable_thinking,
            "temperature": vlm_config.temperature,
            "max_tokens": vlm_config.max_tokens,
        },
        "shot_sample_frames": annotation_config.shot_sample_frames,
        "scene_detection": {
            "adaptive_threshold": detection_config.adaptive_threshold,
            "adaptive_min_content_val": detection_config.adaptive_min_content_val,
            "adaptive_min_scene_len_sec": (
                detection_config.adaptive_min_scene_len_sec
            ),
            "duplicate_frame_threshold": (
                detection_config.duplicate_frame_threshold
            ),
        },
    }
    return (
        material_config.material_cache_dir
        / asset_id
        / f"analysis-{digest(analysis_signature)}",
        analysis_signature,
    )


def write_json_checkpoint(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
    log_event(
        "INFO",
        "analyser",
        "checkpoint.write",
        "Analysis checkpoint written",
        path=path,
    )


def read_json_checkpoint(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log_event(
            "WARNING",
            "analyser",
            "cache.invalid",
            "Invalid analysis checkpoint was ignored",
            path=path,
        )
        return None


def valid_shot_checkpoint(
    value: Any,
    duration_sec: float,
) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not value:
        return None
    previous_end = 0.0
    shot_ids: set[str] = set()
    for shot in value:
        if not isinstance(shot, dict):
            return None
        shot_id = str(shot.get("shot_id") or "")
        time_range = shot.get("time_range")
        if not shot_id or shot_id in shot_ids or not isinstance(time_range, dict):
            return None
        try:
            start = float(time_range["start_sec"])
            end = float(time_range["end_sec"])
        except (KeyError, TypeError, ValueError):
            return None
        if abs(start - previous_end) > 1e-3 or end <= start:
            return None
        shot_ids.add(shot_id)
        previous_end = end
    if abs(previous_end - duration_sec) > 1e-3:
        return None
    return value


def valid_segment_checkpoint(
    value: Any,
    shots: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not value:
        return None
    expected_shot_ids = [str(shot["shot_id"]) for shot in shots]
    actual_shot_ids: list[str] = []
    previous_end = 0.0
    for segment in value:
        if not isinstance(segment, dict):
            return None
        segment_shots = segment.get("shots")
        time_range = segment.get("time_range")
        if not isinstance(segment_shots, list) or not segment_shots:
            return None
        if not isinstance(time_range, dict):
            return None
        try:
            start = float(time_range["start_sec"])
            end = float(time_range["end_sec"])
        except (KeyError, TypeError, ValueError):
            return None
        if abs(start - previous_end) > 1e-3 or end <= start:
            return None
        actual_shot_ids.extend(
            str(shot.get("shot_id") or "") for shot in segment_shots
        )
        previous_end = end
    if actual_shot_ids != expected_shot_ids:
        return None
    return value


def dialogue_checkpoint(material_directory: Path) -> tuple[Path, Path] | None:
    processed_subtitle = material_directory / "dialogue_merged.srt"
    dialogues_json = material_directory / "dialogues.json"
    document = read_json_checkpoint(dialogues_json)
    if (
        not processed_subtitle.is_file()
        or processed_subtitle.stat().st_size <= 0
        or not isinstance(document, dict)
        or not isinstance(document.get("sentences"), list)
    ):
        return None
    return processed_subtitle, dialogues_json
