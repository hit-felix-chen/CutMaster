from __future__ import annotations

import base64
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2

from cutmaster.analyser.asr import prepare_subtitles
from cutmaster.analyser.cache import (
    ANALYSIS_SCHEMA_VERSION,
    dialogue_checkpoint as _dialogue_checkpoint,
    material_directory as _material_directory,
    read_json_checkpoint as _read_json_checkpoint,
    valid_segment_checkpoint as _valid_segment_checkpoint,
    valid_shot_checkpoint as _valid_shot_checkpoint,
    write_json_checkpoint as _write_json_checkpoint,
)
from cutmaster.analyser.contracts import MaterialAnalysisResult
from cutmaster.runtime.shot_detection import detect_source_cuts
from cutmaster.analyser.dialogue import postprocess_dialogues
from cutmaster.configuration.schema import (
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.runtime.observability import log_event
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.analyser import (
    DialogueSegmentationDetails,
    ShotAnnotationDetails,
    VideoSummaryDetails,
)
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.runtime.progress import progress_bar
from cutmaster.runtime.media_probe import probe_media
from cutmaster.timecode import format_range, parse_time
from cutmaster.contracts.video import (
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
        expected_slice = expected_ids[len(assigned) : len(assigned) + len(dialogue_ids)]
        if dialogue_ids != expected_slice:
            next_expected = (
                expected_ids[len(assigned)]
                if len(assigned) < len(expected_ids)
                else None
            )
            if next_expected is not None and first_id < next_expected:
                raise ValueError("Dialogue Segments contain overlapping dialogue IDs")
            if next_expected is not None and first_id > next_expected:
                raise ValueError("Dialogue Segments omit one or more dialogue IDs")
            raise ValueError("Dialogue Segments must preserve dialogue source order")
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
    if assigned != expected_ids:
        raise ValueError("Dialogue Segments omit one or more dialogue IDs")

    merged: list[dict[str, Any]] = []
    for segment in normalized:
        if (
            merged
            and int(segment["first_shot_index"])
            <= int(merged[-1]["last_shot_index"])
        ):
            previous = merged[-1]
            participants = list(
                dict.fromkeys(
                    [
                        *previous["participants"],
                        *segment["participants"],
                    ]
                )
            )
            previous["last_dialogue_id"] = segment["last_dialogue_id"]
            previous["dialogue_ids"].extend(segment["dialogue_ids"])
            previous["speech_mode"] = (
                SpeechMode.DIALOGUE
                if (
                    len(participants) > 1
                    or previous["speech_mode"] == SpeechMode.DIALOGUE
                    or segment["speech_mode"] == SpeechMode.DIALOGUE
                )
                else SpeechMode.MONOLOGUE
            )
            previous["participants"] = participants
            previous["topic"] = " / ".join(
                dict.fromkeys([previous["topic"], segment["topic"]])
            )
            previous["summary"] = " ".join(
                dict.fromkeys([previous["summary"], segment["summary"]])
            )
            previous["grouping_reason"] = (
                "Merged adjacent LLM dialogue groups because their dialogue "
                "ranges share a PySceneDetect Shot"
            )
            previous["last_shot_index"] = max(
                int(previous["last_shot_index"]),
                int(segment["last_shot_index"]),
            )
        else:
            merged.append(dict(segment))

    for index, segment in enumerate(merged, 1):
        segment["dialogue_group_id"] = f"dialogue_group_{index:04d}"
    return merged


def _group_dialogue(
    context: WorkflowContext,
    config: LLMConfig,
    dialogue: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not dialogue:
        return []

    def validate(parsed: dict[str, Any]) -> list[dict[str, Any]]:
        return _validate_dialogue_segments(parsed, dialogue, shots)

    package = prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.DIALOGUE_SEGMENTATION,
        DialogueSegmentationDetails(
            dialogue_ids=[int(item["dialogue_id"]) for item in dialogue],
        ),
    )
    return context.call_prompt(
        package=package,
        config=config,
        validate_business=validate,
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
                log_event(
                    "DEBUG",
                    "analyser",
                    "cache.hit",
                    "Reusable Segment clip found",
                    artifact="segment_clip",
                    segment_id=segment["segment_id"],
                    path=output,
                )
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
                if not images:
                    raise RuntimeError(
                        f"Could not sample any frame from {clip_path}"
                    )
                repeated_frames = count - len(images)
                log_event(
                    "WARNING",
                    "analyser",
                    "fallback.apply",
                    "Last decodable Shot frame repeated",
                    clip_path=clip_path,
                    failed_sample_time_sec=local_time,
                    sampled_frames=len(images),
                    repeated_frames=repeated_frames,
                )
                images.extend([images[-1]] * repeated_frames)
                global_times.extend([global_times[-1]] * repeated_frames)
                break
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
        "shot_id": shot_id,
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


def _annotate_segments(
    segments: list[dict[str, Any]],
    context: WorkflowContext,
    config: VLMConfig,
    sample_frames: int,
    annotation_directory: Path,
) -> list[SegmentDescription]:
    annotation_directory.mkdir(parents=True, exist_ok=True)
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
            sampled_times = [
                round(
                    global_start
                    + (global_end - global_start) * (index + 0.5) / sample_frames,
                    6,
                )
                for index in range(sample_frames)
            ]
            package = prompt_registry.build(
                PromptStage.ANALYSER,
                PromptTask.SHOT_ANNOTATION,
                ShotAnnotationDetails(
                    segment=segment,
                    shot=shot,
                    sampled_frame_times_sec=sampled_times,
                ),
            )

            def validate_annotation(parsed: dict[str, Any]) -> dict[str, Any]:
                return _validate_shot_annotation(
                    parsed,
                    shot["shot_id"],
                    bool(shot["dialogue"]),
                )

            checkpoint_path = annotation_directory / f"{shot['shot_id']}.json"
            checkpoint = _read_json_checkpoint(checkpoint_path)
            annotation = None
            if (
                isinstance(checkpoint, dict)
                and checkpoint.get("prompt_fingerprint") == package.fingerprint
                and checkpoint.get("contract_fingerprint")
                == package.response_contract.fingerprint
                and isinstance(checkpoint.get("annotation"), dict)
            ):
                try:
                    structured = package.response_contract.validate_structure(
                        checkpoint["annotation"]
                    )
                    annotation = validate_annotation(structured)
                except ValueError:
                    annotation = None
            if annotation is None:
                images, sampled_times = _sample_shot_frames(
                    clip_path,
                    local_start,
                    local_end,
                    global_start,
                    sample_frames,
                )
                annotation = context.call_prompt(
                    package=package,
                    config=config,
                    validate_business=validate_annotation,
                    image_data_urls=images,
                    image_labels=[
                        f"{shot['shot_id']} frame {index}/5 at {time_sec:.3f}s"
                        for index, time_sec in enumerate(sampled_times, 1)
                    ],
                )
                _write_json_checkpoint(
                    checkpoint_path,
                    {
                        "schema_version": "1.0",
                        "shot_id": shot["shot_id"],
                        "prompt_id": package.prompt_id,
                        "prompt_version": package.prompt_version,
                        "prompt_fingerprint": package.fingerprint,
                        "contract_fingerprint": (
                            package.response_contract.fingerprint
                        ),
                        "annotation": annotation,
                    },
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
                        if key
                        not in {
                            "shot_id",
                            "scene",
                            "characters",
                        }
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
    log_event(
        "INFO",
        "analyser",
        "stage.progress",
        "Segment annotation concurrency configured",
        segments=len(segments),
        workers=worker_count,
        shots_within_segment="serial",
    )
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="segment-vlm",
        ) as executor:
            return list(executor.map(annotate_segment, segments))
    finally:
        annotation_progress.close()


def _valid_video_summary(
    value: Any,
    segment_ids: list[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not segment_ids:
        return None
    contract = prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.VIDEO_SUMMARY,
        VideoSummaryDetails(segment_ids=segment_ids),
    ).response_contract
    try:
        return contract.validate_structure(value)
    except ValueError:
        return None


def _cache_result(material_directory: Path) -> MaterialAnalysisResult | None:
    manifest_path = material_directory / "analysis_manifest.json"
    description_path = material_directory / "video_description.json"
    summary_path = material_directory / "video_summary.json"
    if (
        not manifest_path.is_file()
        or not description_path.is_file()
        or not summary_path.is_file()
    ):
        return None
    description = json.loads(description_path.read_text(encoding="utf-8"))
    summary = _valid_video_summary(
        _read_json_checkpoint(summary_path),
        [
            str(segment["segment_id"])
            for segment in description.get("segments") or []
        ],
    )
    if summary is None:
        return None
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
    log_event(
        "INFO",
        "analyser",
        "cache.hit",
        "Complete video material analysis found",
        artifact="video_material_analysis",
        material_directory=material_directory,
    )
    return MaterialAnalysisResult(
        material_directory=material_directory,
        source_srt=material_directory / "source.srt",
        processed_subtitle=material_directory / "dialogue_merged.srt",
        dialogues_json=material_directory / "dialogues.json",
        video_description_path=description_path,
        video_summary_path=summary_path,
        analysis_history_path=material_directory / "analysis_history.json",
        video_description=description,
        video_summary=summary,
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
    vlm_config: VLMConfig,
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
        vlm_config,
    )
    cached = _cache_result(material_directory)
    if cached is not None:
        return cached
    log_event(
        "INFO",
        "analyser",
        "cache.miss",
        "Complete video material analysis was not found",
        artifact="video_material_analysis",
        material_directory=material_directory,
    )
    material_directory.mkdir(parents=True, exist_ok=True)
    history_path = material_directory / "analysis_history.json"
    context = WorkflowContext(history_path)

    media = probe_media(video_path)
    duration_sec = float(media["duration"])
    shots_path = material_directory / "shots.json"
    shots = _valid_shot_checkpoint(
        _read_json_checkpoint(shots_path),
        duration_sec,
    )
    fps = float(media["fps"])
    if fps <= 0:
        raise ValueError("Could not determine source-video frame rate")
    if shots is None:
        stage_started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Full-video Shot detection started",
            stage="shot_detection",
            stage_index=1,
            stage_count=6,
        )
        shots, fps = _detect_full_video_shots(
            video_path,
            duration_sec,
            detection_config,
        )
        _write_json_checkpoint(shots_path, shots)
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Full-video Shot detection completed",
            stage="shot_detection",
            stage_index=1,
            stage_count=6,
            shots=len(shots),
            elapsed_sec=time.monotonic() - stage_started,
        )
    else:
        log_event(
            "INFO",
            "analyser",
            "checkpoint.resume",
            "Shot-boundary checkpoint resumed",
            stage="shot_detection",
            stage_index=1,
            stage_count=6,
            shots=len(shots),
        )
    context.set_artifact("shot_boundaries", shots)

    source_metadata = {
        "path": str(video_path.resolve()),
        "title": video_title or video_path.stem,
        "duration_sec": duration_sec,
        "fps": fps,
        "width": int(media["width"]),
        "height": int(media["height"]),
    }
    context.set_artifact("source_metadata", source_metadata)

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "analyser",
        "stage.start",
        "Subtitle and dialogue preparation started",
        stage="dialogue_preparation",
        stage_index=2,
        stage_count=6,
    )
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
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Subtitle and dialogue preparation completed",
            stage="dialogue_preparation",
            stage_index=2,
            stage_count=6,
            elapsed_sec=time.monotonic() - stage_started,
        )
    else:
        processed_subtitle, dialogues_json = dialogue_checkpoint
        log_event(
            "INFO",
            "analyser",
            "checkpoint.resume",
            "Dialogue checkpoint resumed",
            stage="dialogue_preparation",
            stage_index=2,
            stage_count=6,
        )
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
        stage_started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Dialogue grouping and Segment construction started",
            stage="segment_construction",
            stage_index=3,
            stage_count=6,
        )
        dialogue_groups = _group_dialogue(context, llm_config, dialogue, shots)
        segments = _raw_segments(shots, dialogue, dialogue_groups)
        _write_json_checkpoint(segment_boundaries_path, segments)
        context.set_artifact("segment_boundaries", segments)
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Dialogue grouping and Segment construction completed",
            stage="segment_construction",
            stage_index=3,
            stage_count=6,
            segments=len(segments),
            elapsed_sec=time.monotonic() - stage_started,
        )
    else:
        log_event(
            "INFO",
            "analyser",
            "checkpoint.resume",
            "Segment-boundary checkpoint resumed",
            stage="segment_construction",
            stage_index=3,
            stage_count=6,
            segments=len(segments),
        )
        if not segment_boundaries_path.is_file():
            _write_json_checkpoint(segment_boundaries_path, segments)
        context.set_artifact("segment_boundaries", segments)

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "analyser",
        "stage.start",
        "Reusable Segment clip preparation started",
        stage="segment_clip_preparation",
        stage_index=4,
        stage_count=6,
        segments=len(segments),
    )
    _split_segment_clips(video_path, segments, material_directory)
    log_event(
        "INFO",
        "analyser",
        "stage.complete",
        "Reusable Segment clip preparation completed",
        stage="segment_clip_preparation",
        stage_index=4,
        stage_count=6,
        segments=len(segments),
        elapsed_sec=time.monotonic() - stage_started,
    )

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "analyser",
        "stage.start",
        "Shot annotation started",
        stage="shot_annotation",
        stage_index=5,
        stage_count=6,
        segments=len(segments),
    )
    annotated_segments = _annotate_segments(
        segments,
        context,
        vlm_config,
        annotation_config.shot_sample_frames,
        material_directory / "shot_annotations",
    )
    log_event(
        "INFO",
        "analyser",
        "stage.complete",
        "Shot annotation completed",
        stage="shot_annotation",
        stage_index=5,
        stage_count=6,
        segments=len(annotated_segments),
        elapsed_sec=time.monotonic() - stage_started,
    )
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
        visual_description_model=vlm_config.model,
    )
    description_dict = video_description.to_dict()
    description_path = material_directory / "video_description.json"
    _write_json_checkpoint(description_path, description_dict)
    context.set_artifact("video_description", description_dict)
    summary_path = material_directory / "video_summary.json"
    video_summary = _valid_video_summary(
        _read_json_checkpoint(summary_path),
        [
            str(segment["segment_id"])
            for segment in description_dict["segments"]
        ],
    )
    if video_summary is None:
        stage_started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Full-video story summarization started",
            stage="video_summary",
            stage_index=6,
            stage_count=6,
            segments=len(annotated_segments),
        )
        package = prompt_registry.build(
            PromptStage.ANALYSER,
            PromptTask.VIDEO_SUMMARY,
            VideoSummaryDetails(
                segment_ids=[
                    str(segment["segment_id"])
                    for segment in description_dict["segments"]
                ],
            ),
        )
        video_summary = context.call_prompt(
            package=package,
            config=llm_config,
        )
        _write_json_checkpoint(summary_path, video_summary)
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Full-video story summarization completed",
            stage="video_summary",
            stage_index=6,
            stage_count=6,
            story_beats=len(
                video_summary["chronological_story_beats"]
            ),
            elapsed_sec=time.monotonic() - stage_started,
        )
    else:
        context.set_artifact("video_summary", video_summary)
        log_event(
            "INFO",
            "analyser",
            "checkpoint.resume",
            "Full-video story-summary checkpoint resumed",
            stage="video_summary",
            stage_index=6,
            stage_count=6,
        )
    _write_json_checkpoint(
        material_directory / "analysis_manifest.json",
        {
            **analysis_signature,
            "material_directory": str(material_directory.resolve()),
            "video_description": str(description_path.resolve()),
            "video_summary": str(summary_path.resolve()),
        },
    )
    return MaterialAnalysisResult(
        material_directory=material_directory,
        source_srt=source_srt,
        processed_subtitle=processed_subtitle,
        dialogues_json=dialogues_json,
        video_description_path=description_path,
        video_summary_path=summary_path,
        analysis_history_path=history_path,
        video_description=description_dict,
        video_summary=video_summary,
    )
