"""Reusable material-analysis cache keys and checkpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import (
    ASRConfig,
    LLMConfig,
    SceneSegmentationConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.infrastructure.observability.logging import log_event


ANALYSIS_SCHEMA_VERSION = "3.0"
_DIALOGUE_DOCUMENT_FIELDS = frozenset(
    {
        "schema_version",
        "postprocessor",
        "statistics",
        "sentences",
        "merge_operations",
    }
)


def file_signature(path: Path) -> dict[str, Any]:
    """Return a location-independent signature for one immutable sidecar."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest()}


def _source_signature(fingerprint: str) -> dict[str, str]:
    if not isinstance(fingerprint, str):
        raise TypeError("source_fingerprint must be a string")
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError("source_fingerprint must be a lowercase SHA-256")
    return {"sha256": fingerprint}


def analysis_signature(
    source_fingerprint: str,
    video_title: str,
    subtitle_path: Path | None,
    detection_config: ShotDetectionConfig,
    asr_config: ASRConfig,
    scene_config: SceneSegmentationConfig,
    annotation_config: ShotAnnotationConfig,
    llm_config: LLMConfig,
    vlm_config: VLMConfig,
) -> dict[str, Any]:
    if not isinstance(video_title, str) or not video_title.strip():
        raise ValueError("video_title must be non-empty")
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "source": _source_signature(source_fingerprint),
        "video_title": video_title.strip(),
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
        "scene_segmentation": {
            "context_shots": scene_config.context_shots,
            "focus_shots": scene_config.focus_shots,
            "frames_per_shot": scene_config.frames_per_shot,
        },
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
        or set(document) != _DIALOGUE_DOCUMENT_FIELDS
        or document.get("schema_version") != "2.0"
        or not isinstance(document.get("sentences"), list)
    ):
        return None
    return processed_subtitle, dialogues_json
