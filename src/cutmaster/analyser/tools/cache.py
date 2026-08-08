"""Reusable material-analysis cache keys and checkpoints."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import (
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    SceneSegmentationConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.runtime.observability import log_event


ANALYSIS_SCHEMA_VERSION = "2.0"


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
    scene_config: SceneSegmentationConfig,
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


def reuse_compatible_stage_checkpoints(
    material_directory: Path,
    analysis_signature: dict[str, Any],
    duration_sec: float,
) -> None:
    """Reuse deterministic upstream stages from an older analysis schema."""
    if not material_directory.parent.is_dir():
        return
    candidates = sorted(
        (
            path.parent
            for path in material_directory.parent.glob(
                "analysis-*/analysis_manifest.json"
            )
            if path.parent != material_directory
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    shots_path = material_directory / "shots.json"
    needs_shots = valid_shot_checkpoint(
        read_json_checkpoint(shots_path),
        duration_sec,
    ) is None
    source_subtitle_path = material_directory / "source.srt"
    needs_source_subtitle = not (
        source_subtitle_path.is_file()
        and source_subtitle_path.stat().st_size > 0
    )
    needs_dialogue = dialogue_checkpoint(material_directory) is None
    for candidate in candidates:
        manifest = read_json_checkpoint(candidate / "analysis_manifest.json")
        if not isinstance(manifest, dict):
            continue
        if manifest.get("source") != analysis_signature.get("source"):
            continue
        if (
            needs_shots
            and manifest.get("scene_detection")
            == analysis_signature.get("scene_detection")
        ):
            candidate_shots = valid_shot_checkpoint(
                read_json_checkpoint(candidate / "shots.json"),
                duration_sec,
            )
            if candidate_shots is not None:
                write_json_checkpoint(shots_path, candidate_shots)
                needs_shots = False
                log_event(
                    "INFO",
                    "analyser",
                    "cache.hit",
                    "Compatible Shot detection checkpoint reused",
                    source_directory=candidate,
                    target_directory=material_directory,
                    shots=len(candidate_shots),
                )
        subtitle_matches = (
            manifest.get("subtitle") == analysis_signature.get("subtitle")
        )
        candidate_source_subtitle = candidate / "source.srt"
        if (
            needs_source_subtitle
            and subtitle_matches
            and candidate_source_subtitle.is_file()
            and candidate_source_subtitle.stat().st_size > 0
        ):
            shutil.copy2(candidate_source_subtitle, source_subtitle_path)
            needs_source_subtitle = False
            log_event(
                "INFO",
                "analyser",
                "cache.hit",
                "Compatible ASR subtitle checkpoint reused",
                source_directory=candidate,
                target_directory=material_directory,
            )
        if (
            needs_dialogue
            and subtitle_matches
            and manifest.get("llm") == analysis_signature.get("llm")
            and dialogue_checkpoint(candidate) is not None
            and candidate_source_subtitle.is_file()
        ):
            for name in ("source.srt", "dialogue_merged.srt", "dialogues.json"):
                shutil.copy2(candidate / name, material_directory / name)
            needs_source_subtitle = False
            needs_dialogue = False
            log_event(
                "INFO",
                "analyser",
                "cache.hit",
                "Compatible dialogue checkpoints reused",
                source_directory=candidate,
                target_directory=material_directory,
            )
        if not needs_shots and not needs_source_subtitle and not needs_dialogue:
            return


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
