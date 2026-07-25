from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
from loguru import logger

from cutmaster.asr import prepare_subtitles
from cutmaster.cuts import detect_source_cuts
from cutmaster.dialogue import postprocess_dialogues
from cutmaster.models import (
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
)
from cutmaster.planner_context import PlanningContext
from cutmaster.progress import progress_bar
from cutmaster.renderer import probe_media
from cutmaster.timecode import format_range, parse_time
from cutmaster.video_description import (
    BoundarySource,
    CameraAngle,
    CameraMovement,
    CharacterAppearance,
    DialogueContext,
    DialogueOccurrence,
    InteriorExterior,
    SceneDescription,
    SceneDetectionConfig,
    SegmentContentType,
    SegmentDescription,
    ShotDescription,
    ShotScale,
    SourceVideoMetadata,
    SpeechMode,
    TimeOfDay,
    TimelineRole,
    TimeRange,
    VideoDescription,
)


ANALYSIS_SCHEMA_VERSION = "1.0"
DIALOGUE_SEGMENTER_SYSTEM = (
    "You divide the complete source transcript into contiguous spoken-content Segments. "
    "Every dialogue line must be assigned exactly once and returned in source order. "
    "Segment boundaries must be compatible with the supplied Shot memberships. "
    "Return strict JSON only."
)
SHOT_ANNOTATOR_SYSTEM = (
    "You annotate exactly one source-video Shot from five uniformly sampled frames. "
    "The complete transcript is global narrative context, never visual evidence. "
    "Describe only people, actions, locations, lighting, colors, objects, and camera properties "
    "that are visible in the five supplied frames. Return strict JSON only."
)


@dataclass(frozen=True)
class MaterialAnalysisResult:
    material_directory: Path
    source_srt: Path
    processed_subtitle: Path
    dialogues_json: Path
    video_description_path: Path
    analysis_history_path: Path
    video_description: dict[str, Any]


def _file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _material_directory(
    video_path: Path,
    video_title: str,
    subtitle_path: Path | None,
    material_config: MaterialAnalysisConfig,
    detection_config: ShotDetectionConfig,
    asr_config: ASRConfig,
    annotation_config: ShotAnnotationConfig,
    llm_config: LLMConfig,
) -> tuple[Path, dict[str, Any]]:
    source_signature = _file_signature(video_path)
    asset_id = f"{video_path.stem}-{_digest(source_signature)}"
    analysis_signature = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "source": source_signature,
        "video_title": video_title or video_path.stem,
        "subtitle": (
            _file_signature(subtitle_path)
            if subtitle_path is not None
            else {
                "backend": asr_config.backend,
                "max_chars": asr_config.max_chars,
                "max_subtitle_duration_sec": asr_config.max_subtitle_duration_sec,
            }
        ),
        "model": llm_config.model,
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
        / f"analysis-{_digest(analysis_signature)}",
        analysis_signature,
    )


def _write_json_checkpoint(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _read_json_checkpoint(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring invalid analysis checkpoint: {}", path)
        return None


def _valid_shot_checkpoint(
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


def _valid_segment_checkpoint(
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
        actual_shot_ids.extend(str(shot.get("shot_id") or "") for shot in segment_shots)
        previous_end = end
    if actual_shot_ids != expected_shot_ids:
        return None
    return value


def _dialogue_checkpoint(
    material_directory: Path,
) -> tuple[Path, Path] | None:
    processed_subtitle = material_directory / "dialogue_merged.srt"
    dialogues_json = material_directory / "dialogues.json"
    document = _read_json_checkpoint(dialogues_json)
    if (
        not processed_subtitle.is_file()
        or processed_subtitle.stat().st_size <= 0
        or not isinstance(document, dict)
        or not isinstance(document.get("sentences"), list)
    ):
        return None
    return processed_subtitle, dialogues_json


def _detect_full_video_shots(
    video_path: Path,
    duration_sec: float,
    detection_config: ShotDetectionConfig,
) -> tuple[list[dict[str, Any]], float]:
    cuts, fps = detect_source_cuts(
        video_path,
        0.0,
        duration_sec,
        adaptive_threshold=detection_config.adaptive_threshold,
        adaptive_min_content_val=detection_config.adaptive_min_content_val,
        adaptive_min_scene_len_sec=(
            detection_config.adaptive_min_scene_len_sec
        ),
        duplicate_frame_threshold=detection_config.duplicate_frame_threshold,
        progress_label="Full-video Shot detection",
    )
    boundaries = [
        0.0,
        *sorted(
            {
                round(float(cut), 6)
                for cut in cuts
                if 0.0 < float(cut) < duration_sec
            }
        ),
        duration_sec,
    ]
    shots = []
    for index, (start, end) in enumerate(
        zip(boundaries, boundaries[1:]),
        1,
    ):
        shots.append(
            {
                "shot_id": f"shot_{index:05d}",
                "time_range": {"start_sec": start, "end_sec": end},
                "timestamp": format_range(start, end),
                "start_boundary": (
                    BoundarySource.VIDEO_START
                    if index == 1
                    else BoundarySource.ADAPTIVE_CUT
                ),
                "end_boundary": (
                    BoundarySource.VIDEO_END
                    if index == len(boundaries) - 1
                    else BoundarySource.ADAPTIVE_CUT
                ),
            }
        )
    if not shots:
        raise ValueError("Full-video Shot detection returned no Shots")
    return shots, fps


def _compact_dialogue(document: dict[str, Any]) -> list[dict[str, Any]]:
    dialogue = []
    for sentence in document["sentences"]:
        speaker = str(sentence.get("speaker") or "Speaker").strip()
        dialogue.append(
            {
                "dialogue_id": int(sentence["sentence_id"]),
                "start_sec": parse_time(str(sentence["start"])),
                "end_sec": parse_time(str(sentence["end"])),
                "timestamp": format_range(
                    parse_time(str(sentence["start"])),
                    parse_time(str(sentence["end"])),
                ),
                "speaker": speaker,
                "text": str(sentence["text"]).strip(),
            }
        )
    return dialogue


def _covering_shot_indexes(
    shots: list[dict[str, Any]],
    start_sec: float,
    end_sec: float,
) -> list[int]:
    indexes = [
        index
        for index, shot in enumerate(shots)
        if start_sec < float(shot["time_range"]["end_sec"])
        and end_sec > float(shot["time_range"]["start_sec"])
    ]
    if not indexes:
        raise ValueError(
            f"Dialogue range {format_range(start_sec, end_sec)} overlaps no Shot"
        )
    return indexes


def _dialogue_with_shot_membership(
    dialogue: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **line,
            "covering_shot_ids": [
                shots[index]["shot_id"]
                for index in _covering_shot_indexes(
                    shots,
                    float(line["start_sec"]),
                    float(line["end_sec"]),
                )
            ],
        }
        for line in dialogue
    ]


def _validate_dialogue_segments(
    parsed: dict[str, Any],
    dialogue: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_segments = parsed.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("Dialogue segmentation must return a non-empty segments array")
    expected_ids = [int(line["dialogue_id"]) for line in dialogue]
    by_id = {int(line["dialogue_id"]): line for line in dialogue}
    assigned: list[int] = []
    normalized: list[dict[str, Any]] = []
    previous_shot_end = -1
    for index, raw in enumerate(raw_segments, 1):
        if not isinstance(raw, dict):
            raise ValueError("Every dialogue Segment must be an object")
        first_id = int(raw["first_dialogue_id"])
        last_id = int(raw["last_dialogue_id"])
        if first_id not in by_id or last_id not in by_id or last_id < first_id:
            raise ValueError("Dialogue Segment contains an invalid dialogue ID range")
        dialogue_ids = [
            dialogue_id
            for dialogue_id in expected_ids
            if first_id <= dialogue_id <= last_id
        ]
        if dialogue_ids != list(range(first_id, last_id + 1)):
            raise ValueError("Dialogue IDs must be contiguous integers")
        mode = SpeechMode(str(raw["speech_mode"]))
        if mode == SpeechMode.NONE:
            raise ValueError("Spoken-content Segment cannot use speech_mode=none")
        shot_indexes = sorted(
            {
                shot_index
                for dialogue_id in dialogue_ids
                for shot_index in _covering_shot_indexes(
                    shots,
                    float(by_id[dialogue_id]["start_sec"]),
                    float(by_id[dialogue_id]["end_sec"]),
                )
            }
        )
        if shot_indexes[0] <= previous_shot_end:
            raise ValueError(
                "Adjacent dialogue Segments map to overlapping Shots; they must be grouped"
            )
        participants = list(
            dict.fromkeys(
                str(by_id[dialogue_id]["speaker"])
                for dialogue_id in dialogue_ids
            )
        )
        topic = str(raw.get("topic") or "").strip()
        summary = str(raw.get("summary") or "").strip()
        if not participants or not topic or not summary:
            raise ValueError("Dialogue Segment semantic fields must not be empty")
        normalized.append(
            {
                "dialogue_group_id": f"dialogue_group_{index:04d}",
                "first_dialogue_id": first_id,
                "last_dialogue_id": last_id,
                "dialogue_ids": dialogue_ids,
                "speech_mode": mode,
                "participants": participants,
                "topic": topic,
                "summary": summary,
                "grouping_reason": (
                    f"LLM grouped contiguous dialogue IDs {first_id}-{last_id} "
                    "as one spoken passage"
                ),
                "first_shot_index": shot_indexes[0],
                "last_shot_index": shot_indexes[-1],
            }
        )
        assigned.extend(dialogue_ids)
        previous_shot_end = shot_indexes[-1]
    if assigned != expected_ids:
        raise ValueError("Dialogue Segments must cover every dialogue line exactly once")
    return normalized


def _group_dialogue(
    context: PlanningContext,
    config: LLMConfig,
    dialogue: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not dialogue:
        return []
    prompt = """Divide the complete transcript in the maintained context into contiguous
spoken-content Segments.

Rules:
1. Assign every dialogue_id exactly once and preserve source order.
2. A Segment contains one continuous conversation or one continuous monologue.
3. Return only inclusive first_dialogue_id/last_dialogue_id ranges.
4. Do not split adjacent lines when their covering_shot_ids overlap. A Segment boundary must
   fall between two PySceneDetect Shots.
5. Do not group unrelated dialogue across a narrative, speaker, topic, location, or large time
   break merely to reduce the number of Segments.
6. speech_mode must be dialogue or monologue. If a passage contains interaction between
   speakers, classify it as dialogue.
7. Topic and summary must be concise and grounded only in the supplied transcript. Participants
   are derived locally from the assigned dialogue lines and must not be returned.

Return:
{"segments":[{
  "first_dialogue_id":1,
  "last_dialogue_id":4,
  "speech_mode":"dialogue|monologue",
  "topic":"specific subject of the passage",
  "summary":"what is said or narratively established"
}]}"""
    return context.call_json(
        operation="Full-transcript dialogue segmentation",
        prompt=prompt,
        config=config,
        context_keys=["source_metadata", "shot_boundaries", "full_dialogue"],
        system_prompt=DIALOGUE_SEGMENTER_SYSTEM,
        enable_thinking=True,
        validate=lambda parsed: _validate_dialogue_segments(
            parsed,
            dialogue,
            shots,
        ),
        output_artifact="dialogue_segments",
    )


def _raw_segments(
    shots: list[dict[str, Any]],
    dialogue: list[dict[str, Any]],
    dialogue_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    group_by_dialogue_id = {
        dialogue_id: group
        for group in dialogue_groups
        for dialogue_id in group["dialogue_ids"]
    }
    ranges: list[tuple[int, int, dict[str, Any] | None]] = []
    cursor = 0
    for group in dialogue_groups:
        first = int(group["first_shot_index"])
        last = int(group["last_shot_index"])
        if cursor < first:
            ranges.append((cursor, first - 1, None))
        ranges.append((first, last, group))
        cursor = last + 1
    if cursor < len(shots):
        ranges.append((cursor, len(shots) - 1, None))
    if not ranges:
        ranges = [(0, len(shots) - 1, None)]

    segments: list[dict[str, Any]] = []
    for index, (first, last, group) in enumerate(ranges, 1):
        segment_shots = []
        for shot in shots[first : last + 1]:
            shot_start = float(shot["time_range"]["start_sec"])
            shot_end = float(shot["time_range"]["end_sec"])
            occurrences = []
            for line in dialogue:
                line_start = float(line["start_sec"])
                line_end = float(line["end_sec"])
                if line_start >= shot_end or line_end <= shot_start:
                    continue
                owning_group = group_by_dialogue_id[int(line["dialogue_id"])]
                if group is None or owning_group["dialogue_group_id"] != group["dialogue_group_id"]:
                    continue
                occurrences.append(
                    {
                        "dialogue_id": int(line["dialogue_id"]),
                        "time_range": {
                            "start_sec": line_start,
                            "end_sec": line_end,
                        },
                        "speaker": line["speaker"],
                        "text": line["text"],
                        "dialogue_group_id": group["dialogue_group_id"],
                        "speech_mode": group["speech_mode"],
                    }
                )
            segment_shots.append({**shot, "dialogue": occurrences})
        start = float(segment_shots[0]["time_range"]["start_sec"])
        end = float(segment_shots[-1]["time_range"]["end_sec"])
        dialogue_context = None
        if group is not None:
            dialogue_context = {
                "dialogue_group_id": group["dialogue_group_id"],
                "speech_mode": group["speech_mode"],
                "dialogue_ids": group["dialogue_ids"],
                "participants": group["participants"],
                "topic": group["topic"],
                "summary": group["summary"],
                "grouping_reason": group["grouping_reason"],
            }
        segments.append(
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {"start_sec": start, "end_sec": end},
                "has_dialogue": group is not None,
                "speech_mode": (
                    group["speech_mode"] if group is not None else SpeechMode.NONE
                ),
                "shots": segment_shots,
                "dialogue_context": dialogue_context,
            }
        )

    for index, segment in enumerate(segments):
        segment["timeline_role"] = (
            TimelineRole.BODY
            if len(segments) == 1
            else (
                TimelineRole.OPENING
                if index == 0
                else TimelineRole.ENDING
                if index == len(segments) - 1
                else TimelineRole.BODY
            )
        )
    return segments


def _split_segment_clips(
    video_path: Path,
    segments: list[dict[str, Any]],
    material_directory: Path,
) -> None:
    clips_dir = material_directory / "segments"
    clips_dir.mkdir(parents=True, exist_ok=True)
    with progress_bar(
        segments,
        total=len(segments),
        description="Segment clip preparation",
        unit="segment",
    ) as progress:
        for segment in progress:
            start = float(segment["time_range"]["start_sec"])
            end = float(segment["time_range"]["end_sec"])
            output = clips_dir / f"{segment['segment_id']}.mp4"
            expected_duration = end - start
            try:
                existing_duration = float(probe_media(output)["duration"])
            except Exception:
                existing_duration = -1.0
            if abs(existing_duration - expected_duration) <= 0.1:
                logger.info("Reusing Segment clip: {}", output)
                segment["clip_path"] = str(output.resolve())
                continue
            temporary_output = output.with_suffix(".partial.mp4")
            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.6f}",
                "-t",
                f"{end - start:.6f}",
                "-i",
                str(video_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ]
            subprocess.run(command, check=True)
            temporary_output.replace(output)
            segment["clip_path"] = str(output.resolve())


def _sample_shot_frames(
    clip_path: Path,
    local_start_sec: float,
    local_end_sec: float,
    global_start_sec: float,
    count: int,
) -> tuple[list[str], list[float]]:
    if count != 5:
        raise ValueError("Shot annotation requires exactly five sampled frames")
    capture = cv2.VideoCapture(str(clip_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open Segment clip: {clip_path}")
    try:
        duration = local_end_sec - local_start_sec
        local_times = [
            local_start_sec + duration * (index + 0.5) / count
            for index in range(count)
        ]
        images = []
        global_times = []
        for local_time in local_times:
            capture.set(cv2.CAP_PROP_POS_MSEC, local_time * 1000.0)
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(
                    f"Could not sample frame at {local_time:.3f}s from {clip_path}"
                )
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), 88],
            )
            if not ok:
                raise RuntimeError("Could not encode sampled Shot frame")
            images.append(
                "data:image/jpeg;base64,"
                + base64.b64encode(encoded.tobytes()).decode("ascii")
            )
            global_times.append(
                round(global_start_sec + local_time - local_start_sec, 6)
            )
        return images, global_times
    finally:
        capture.release()


def _validate_shot_annotation(
    parsed: dict[str, Any],
    shot_id: str,
    has_dialogue: bool,
) -> dict[str, Any]:
    if str(parsed.get("shot_id") or "") != shot_id:
        raise ValueError(f"Shot annotation must return shot_id={shot_id}")
    content_type = SegmentContentType(str(parsed["content_type"]))
    if has_dialogue and content_type != SegmentContentType.NARRATIVE:
        raise ValueError("A Shot containing spoken content must be narrative")
    if not has_dialogue and content_type == SegmentContentType.NARRATIVE:
        raise ValueError("A silent Shot must be landscape, emotional, or pantomime")
    scene = parsed.get("scene")
    if not isinstance(scene, dict):
        raise ValueError("Shot annotation must contain a scene object")
    normalized_scene = {
        "interior_exterior": InteriorExterior(str(scene["interior_exterior"])),
        "location": str(scene["location"]).strip(),
        "time_of_day": TimeOfDay(str(scene["time_of_day"])),
        "environment_lighting": [
            str(value).strip()
            for value in scene.get("environment_lighting") or []
            if str(value).strip()
        ],
        "color_palette": [
            str(value).strip()
            for value in scene.get("color_palette") or []
            if str(value).strip()
        ],
        "color_tone": str(scene["color_tone"]).strip(),
        "set_details": [
            str(value).strip()
            for value in scene.get("set_details") or []
            if str(value).strip()
        ],
        "weather": str(scene.get("weather") or "").strip(),
        "atmosphere": str(scene["atmosphere"]).strip(),
    }
    SceneDescription(**normalized_scene).validate()

    characters = []
    for index, raw in enumerate(parsed.get("characters") or [], 1):
        character = {
            "character_id": str(raw.get("character_id") or f"person_{index:02d}").strip(),
            "name": str(raw["name"]).strip(),
            "description": str(raw["description"]).strip(),
            "identity_likert": int(raw["identity_likert"]),
            "identity_evidence": str(raw["identity_evidence"]).strip(),
            "screen_presence": max(0.0, min(1.0, float(raw["screen_presence"]))),
        }
        CharacterAppearance(**character).validate()
        characters.append(character)
    normalized = {
        "visual_description": str(parsed["visual_description"]).strip(),
        "dominant_action": str(parsed["dominant_action"]).strip(),
        "content_type": content_type,
        "narrative_function": str(parsed["narrative_function"]).strip(),
        "emotional_tone": str(parsed["emotional_tone"]).strip(),
        "emotional_intensity": max(
            0.0,
            min(1.0, float(parsed["emotional_intensity"])),
        ),
        "scene": normalized_scene,
        "characters": characters,
        "shot_scale": ShotScale(str(parsed["shot_scale"])),
        "camera_angle": CameraAngle(str(parsed["camera_angle"])),
        "camera_movement": CameraMovement(str(parsed["camera_movement"])),
        "composition": str(parsed["composition"]).strip(),
        "visual_evidence": str(parsed["visual_evidence"]).strip(),
    }
    required_text = (
        "visual_description",
        "dominant_action",
        "narrative_function",
        "emotional_tone",
        "composition",
        "visual_evidence",
    )
    if any(not normalized[key] for key in required_text):
        raise ValueError("Shot annotation text fields must not be empty")
    return normalized


def _shot_prompt(
    segment: dict[str, Any],
    shot: dict[str, Any],
    sampled_times: list[float],
) -> str:
    return f"""Annotate exactly one Shot from the five attached frames, shown in chronological
order and sampled uniformly inside the Shot.

The maintained full transcript is global context for names and narrative position only. It is
not evidence that a person, action, object, location, or emotion is visible. Pixel evidence
always wins. If a visible person's identity cannot be established, assign a stable generic name
such as person_01 instead of guessing a cast identity.

This Shot has_dialogue={str(bool(shot["dialogue"])).lower()}. Therefore content_type must be:
- narrative when has_dialogue is true;
- landscape, emotional, or pantomime when has_dialogue is false.

Identity Likert:
1 = identity cannot be established from these frames;
2 = weak person-specific evidence;
3 = plausible identity with partial facial evidence;
4 = clear facial match in a meaningful portion;
5 = repeated, unmistakable facial match.

For interior_exterior choose interior or exterior. For time_of_day choose dawn, day, dusk, or
night. Choose the single dominant camera scale, angle, and movement; do not return mixed, other,
or unknown labels. Describe locations concretely from visible structure even when the proper
place name is unavailable.

<segment>
{json.dumps({
    "segment_id": segment["segment_id"],
    "has_dialogue": segment["has_dialogue"],
    "speech_mode": segment["speech_mode"],
    "dialogue_context": segment["dialogue_context"],
}, ensure_ascii=False)}
</segment>
<shot>
{json.dumps({
    "shot_id": shot["shot_id"],
    "timestamp": shot["timestamp"],
    "dialogue": shot["dialogue"],
    "sampled_frame_times_sec": sampled_times,
}, ensure_ascii=False)}
</shot>

Return:
{{
  "shot_id":"{shot['shot_id']}",
  "visual_description":"literal visible content across the five frames",
  "dominant_action":"single dominant visible action",
  "content_type":"narrative|landscape|emotional|pantomime",
  "narrative_function":"specific function this visible Shot serves",
  "emotional_tone":"specific visible emotional tone",
  "emotional_intensity":0.0,
  "scene":{{
    "interior_exterior":"interior|exterior",
    "location":"concrete visible place description",
    "time_of_day":"dawn|day|dusk|night",
    "environment_lighting":["visible light source or lighting condition"],
    "color_palette":["dominant visible color"],
    "color_tone":"specific color treatment",
    "set_details":["visible furnishing, landscape element, prop, or architecture"],
    "weather":"visible weather; empty string when weather is not in frame",
    "atmosphere":"specific visual atmosphere"
  }},
  "characters":[{{
    "character_id":"stable character or person identifier",
    "name":"verified name or stable generic person label",
    "description":"appearance, clothing, expression, pose, and action",
    "identity_likert":1,
    "identity_evidence":"pixel-grounded identity evidence or explicit lack of it",
    "screen_presence":0.0
  }}],
  "shot_scale":"extreme_wide|wide|medium|close_up|extreme_close_up",
  "camera_angle":"eye_level|high_angle|low_angle|overhead|dutch_angle",
  "camera_movement":"static|pan|tilt|tracking|handheld|zoom|crane",
  "composition":"subject placement, depth, balance, and screen direction",
  "visual_evidence":"brief summary of decisive evidence in the five frames"
}}"""


def _annotate_segments(
    segments: list[dict[str, Any]],
    context: PlanningContext,
    config: LLMConfig,
    sample_frames: int,
) -> list[SegmentDescription]:
    annotation_progress = progress_bar(
        total=sum(len(segment["shots"]) for segment in segments),
        description="Shot VLM annotation",
        unit="shot",
    )

    def annotate_segment(segment: dict[str, Any]) -> SegmentDescription:
        clip_path = Path(segment["clip_path"])
        segment_start = float(segment["time_range"]["start_sec"])
        annotated_shots: list[ShotDescription] = []
        for shot in segment["shots"]:
            global_start = float(shot["time_range"]["start_sec"])
            global_end = float(shot["time_range"]["end_sec"])
            local_start = global_start - segment_start
            local_end = global_end - segment_start
            operation = f"Shot visual annotation {shot['shot_id']}"
            cached_annotation = context.get_successful_call_result(operation)
            if cached_annotation is not None:
                annotation = _validate_shot_annotation(
                    cached_annotation,
                    shot["shot_id"],
                    bool(shot["dialogue"]),
                )
                sampled_times = [
                    round(
                        global_start
                        + (global_end - global_start) * (index + 0.5) / sample_frames,
                        6,
                    )
                    for index in range(sample_frames)
                ]
            else:
                images, sampled_times = _sample_shot_frames(
                    clip_path,
                    local_start,
                    local_end,
                    global_start,
                    sample_frames,
                )
                annotation = context.call_json(
                    operation=operation,
                    prompt=_shot_prompt(segment, shot, sampled_times),
                    config=config,
                    context_keys=["source_metadata", "full_dialogue"],
                    system_prompt=SHOT_ANNOTATOR_SYSTEM,
                    enable_thinking=True,
                    validate=lambda parsed, shot=shot: _validate_shot_annotation(
                        parsed,
                        shot["shot_id"],
                        bool(shot["dialogue"]),
                    ),
                    image_data_urls=images,
                    image_labels=[
                        f"{shot['shot_id']} frame {index}/5 at {time_sec:.3f}s"
                        for index, time_sec in enumerate(sampled_times, 1)
                    ],
                )
            dialogue_occurrences = [
                DialogueOccurrence(
                    dialogue_id=int(item["dialogue_id"]),
                    time_range=TimeRange(**item["time_range"]),
                    speaker=str(item["speaker"]),
                    text=str(item["text"]),
                    dialogue_group_id=str(item["dialogue_group_id"]),
                    speech_mode=SpeechMode(str(item["speech_mode"])),
                )
                for item in shot["dialogue"]
            ]
            annotated_shots.append(
                ShotDescription(
                    shot_id=shot["shot_id"],
                    time_range=TimeRange(**shot["time_range"]),
                    segment_time_range=TimeRange(
                        start_sec=local_start,
                        end_sec=local_end,
                    ),
                    start_boundary=BoundarySource(str(shot["start_boundary"])),
                    end_boundary=BoundarySource(str(shot["end_boundary"])),
                    dialogue=dialogue_occurrences,
                    sampled_frame_times_sec=sampled_times,
                    scene=SceneDescription(**annotation["scene"]),
                    characters=[
                        CharacterAppearance(**value)
                        for value in annotation["characters"]
                    ],
                    **{
                        key: value
                        for key, value in annotation.items()
                        if key not in {"scene", "characters"}
                    },
                )
            )
            annotation_progress.update()

        total_duration = sum(shot.time_range.duration_sec for shot in annotated_shots)
        if segment["has_dialogue"]:
            content_type = SegmentContentType.NARRATIVE
        else:
            durations: dict[SegmentContentType, float] = {
                SegmentContentType.LANDSCAPE: 0.0,
                SegmentContentType.EMOTIONAL: 0.0,
                SegmentContentType.PANTOMIME: 0.0,
            }
            for shot in annotated_shots:
                durations[shot.content_type] += shot.time_range.duration_sec
            content_type = max(durations, key=durations.get)
        representative = max(
            annotated_shots,
            key=lambda shot: shot.time_range.duration_sec,
        )
        appearing_characters = list(
            dict.fromkeys(
                character.name
                for shot in annotated_shots
                for character in shot.characters
            )
        )
        dialogue_context = (
            DialogueContext(
                dialogue_group_id=segment["dialogue_context"]["dialogue_group_id"],
                speech_mode=SpeechMode(segment["dialogue_context"]["speech_mode"]),
                dialogue_ids=segment["dialogue_context"]["dialogue_ids"],
                participants=segment["dialogue_context"]["participants"],
                topic=segment["dialogue_context"]["topic"],
                summary=segment["dialogue_context"]["summary"],
                grouping_reason=segment["dialogue_context"]["grouping_reason"],
            )
            if segment["dialogue_context"] is not None
            else None
        )
        description = SegmentDescription(
            segment_id=segment["segment_id"],
            time_range=TimeRange(**segment["time_range"]),
            clip_path=segment["clip_path"],
            has_dialogue=bool(segment["has_dialogue"]),
            speech_mode=SpeechMode(segment["speech_mode"]),
            content_type=content_type,
            timeline_role=TimelineRole(segment["timeline_role"]),
            shots=annotated_shots,
            dialogue_context=dialogue_context,
            segment_summary=" ".join(
                shot.visual_description for shot in annotated_shots
            ),
            narrative_function=(
                dialogue_context.summary
                if dialogue_context is not None
                else " ".join(
                    dict.fromkeys(shot.narrative_function for shot in annotated_shots)
                )
            ),
            emotional_tone=representative.emotional_tone,
            emotional_intensity=sum(
                shot.emotional_intensity * shot.time_range.duration_sec
                for shot in annotated_shots
            )
            / total_duration,
            appearing_characters=appearing_characters,
        )
        description.validate()
        return description

    worker_count = max(1, min(config.max_concurrency, len(segments)))
    logger.info(
        "Annotating {} Segments with {} workers; Shots remain serial within each Segment",
        len(segments),
        worker_count,
    )
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="segment-vlm",
        ) as executor:
            return list(executor.map(annotate_segment, segments))
    finally:
        annotation_progress.close()


def _cache_result(material_directory: Path) -> MaterialAnalysisResult | None:
    manifest_path = material_directory / "analysis_manifest.json"
    description_path = material_directory / "video_description.json"
    if not manifest_path.is_file() or not description_path.is_file():
        return None
    description = json.loads(description_path.read_text(encoding="utf-8"))
    required = [
        material_directory / "source.srt",
        material_directory / "dialogue_merged.srt",
        material_directory / "dialogues.json",
        material_directory / "analysis_history.json",
        *[
            Path(segment["clip_path"])
            for segment in description.get("segments") or []
        ],
    ]
    if not required or any(not path.is_file() for path in required):
        return None
    logger.info("Reusing video material analysis: {}", material_directory)
    return MaterialAnalysisResult(
        material_directory=material_directory,
        source_srt=material_directory / "source.srt",
        processed_subtitle=material_directory / "dialogue_merged.srt",
        dialogues_json=material_directory / "dialogues.json",
        video_description_path=description_path,
        analysis_history_path=material_directory / "analysis_history.json",
        video_description=description,
    )


def analyse_video_material(
    video_path: Path,
    video_title: str,
    provided_subtitle: Path | None,
    material_config: MaterialAnalysisConfig,
    detection_config: ShotDetectionConfig,
    asr_config: ASRConfig,
    annotation_config: ShotAnnotationConfig,
    llm_config: LLMConfig,
) -> MaterialAnalysisResult:
    if annotation_config.shot_sample_frames != 5:
        raise ValueError("shot_annotation.shot_sample_frames must be exactly 5")
    material_directory, analysis_signature = _material_directory(
        video_path,
        video_title,
        provided_subtitle,
        material_config,
        detection_config,
        asr_config,
        annotation_config,
        llm_config,
    )
    cached = _cache_result(material_directory)
    if cached is not None:
        return cached
    material_directory.mkdir(parents=True, exist_ok=True)
    history_path = material_directory / "analysis_history.json"
    context = PlanningContext(history_path)

    media = probe_media(video_path)
    duration_sec = float(media["duration"])
    shots_path = material_directory / "shots.json"
    shots = _valid_shot_checkpoint(
        _read_json_checkpoint(shots_path),
        duration_sec,
    )
    if shots is None:
        shots = _valid_shot_checkpoint(
            context.get_artifact("shot_boundaries"),
            duration_sec,
        )
    cached_source_metadata = context.get_artifact("source_metadata")
    if shots is None:
        logger.info(
            "Analysis stage 1/5: detecting full-video Shot boundaries with PySceneDetect"
        )
        shots, fps = _detect_full_video_shots(
            video_path,
            duration_sec,
            detection_config,
        )
        _write_json_checkpoint(shots_path, shots)
        context.set_artifact("shot_boundaries", shots)
        logger.info("Analysis stage 1/5 complete: {} Shots", len(shots))
    else:
        logger.info(
            "Reusing analysis stage 1/5 Shot checkpoint: {} Shots",
            len(shots),
        )
        if not shots_path.is_file():
            _write_json_checkpoint(shots_path, shots)
        if isinstance(cached_source_metadata, dict):
            fps = float(cached_source_metadata.get("fps") or 0.0)
        else:
            fps = 0.0
        if fps <= 0:
            capture = cv2.VideoCapture(str(video_path))
            try:
                fps = float(capture.get(cv2.CAP_PROP_FPS))
            finally:
                capture.release()
        if fps <= 0:
            raise ValueError("Could not determine source-video frame rate")

    source_metadata = {
        "path": str(video_path.resolve()),
        "title": video_title or video_path.stem,
        "duration_sec": duration_sec,
        "fps": fps,
        "width": int(media["width"]),
        "height": int(media["height"]),
    }
    if cached_source_metadata != source_metadata:
        context.set_artifact("source_metadata", source_metadata)

    logger.info("Analysis stage 2/5: preparing subtitles and dialogue")
    source_srt = prepare_subtitles(
        video_path,
        material_directory,
        asr_config,
        provided_subtitle,
    )
    dialogue_checkpoint = _dialogue_checkpoint(material_directory)
    if dialogue_checkpoint is None:
        processed_subtitle, dialogues_json = postprocess_dialogues(
            source_srt,
            material_directory,
            llm_config,
            context=context,
        )
        logger.info("Analysis stage 2/5 complete")
    else:
        processed_subtitle, dialogues_json = dialogue_checkpoint
        logger.info("Reusing analysis stage 2/5 dialogue checkpoint")
    dialogue_document = json.loads(dialogues_json.read_text(encoding="utf-8"))
    dialogue = _dialogue_with_shot_membership(
        _compact_dialogue(dialogue_document),
        shots,
    )
    if context.get_artifact("full_dialogue") != dialogue:
        context.set_artifact("full_dialogue", dialogue)

    segment_boundaries_path = material_directory / "segment_boundaries.json"
    segments = _valid_segment_checkpoint(
        _read_json_checkpoint(segment_boundaries_path),
        shots,
    )
    if segments is None:
        segments = _valid_segment_checkpoint(
            context.get_artifact("segment_boundaries"),
            shots,
        )
    if segments is None:
        logger.info("Analysis stage 3/5: grouping dialogue and building Segments")
        dialogue_groups = _group_dialogue(context, llm_config, dialogue, shots)
        segments = _raw_segments(shots, dialogue, dialogue_groups)
        _write_json_checkpoint(segment_boundaries_path, segments)
        context.set_artifact("segment_boundaries", segments)
        logger.info("Analysis stage 3/5 complete: {} Segments", len(segments))
    else:
        logger.info(
            "Reusing analysis stage 3/5 Segment checkpoint: {} Segments",
            len(segments),
        )
        if not segment_boundaries_path.is_file():
            _write_json_checkpoint(segment_boundaries_path, segments)

    logger.info("Analysis stage 4/5: preparing reusable Segment clips")
    _split_segment_clips(video_path, segments, material_directory)
    logger.info("Analysis stage 4/5 complete")

    logger.info("Analysis stage 5/5: annotating Shots")
    annotated_segments = _annotate_segments(
        segments,
        context,
        llm_config,
        annotation_config.shot_sample_frames,
    )
    logger.info("Analysis stage 5/5 complete")
    video_description = VideoDescription(
        schema_version=ANALYSIS_SCHEMA_VERSION,
        source=SourceVideoMetadata(**source_metadata),
        scene_detection=SceneDetectionConfig(
            adaptive_threshold=detection_config.adaptive_threshold,
            adaptive_min_content_val=detection_config.adaptive_min_content_val,
            adaptive_min_scene_len_sec=(
                detection_config.adaptive_min_scene_len_sec
            ),
            duplicate_frame_threshold=detection_config.duplicate_frame_threshold,
        ),
        segments=annotated_segments,
        asr_model=asr_config.backend,
        dialogue_grouping_model=llm_config.model,
        visual_description_model=llm_config.model,
    )
    description_dict = video_description.to_dict()
    description_path = material_directory / "video_description.json"
    _write_json_checkpoint(description_path, description_dict)
    context.set_artifact("video_description", description_dict)
    _write_json_checkpoint(
        material_directory / "analysis_manifest.json",
        {
            **analysis_signature,
            "material_directory": str(material_directory.resolve()),
            "video_description": str(description_path.resolve()),
        },
    )
    return MaterialAnalysisResult(
        material_directory=material_directory,
        source_srt=source_srt,
        processed_subtitle=processed_subtitle,
        dialogues_json=dialogues_json,
        video_description_path=description_path,
        analysis_history_path=history_path,
        video_description=description_dict,
    )
