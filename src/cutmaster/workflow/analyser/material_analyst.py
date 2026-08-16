from __future__ import annotations

import base64
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from cutmaster.workflow.analyser.tools.asr import prepare_subtitles
from cutmaster.workflow.analyser.tools.cache import (
    ANALYSIS_SCHEMA_VERSION,
    analysis_signature as _analysis_signature,
    dialogue_checkpoint as _dialogue_checkpoint,
    read_json_checkpoint as _read_json_checkpoint,
    reuse_compatible_stage_checkpoints as _reuse_compatible_stage_checkpoints,
    valid_segment_checkpoint as _valid_segment_checkpoint,
    valid_shot_checkpoint as _valid_shot_checkpoint,
    write_json_checkpoint as _write_json_checkpoint,
)
from cutmaster.workflow.shared.shot_detection import detect_source_cuts
from cutmaster.workflow.analyser.tools.dialogue import postprocess_dialogues
from cutmaster.workflow.analyser.tools.music_analysis import analyze_music_memory
from cutmaster.workflow.analyser.tools.scene_segmenter import (
    SCENE_SEGMENTATION_VERSION,
    build_segments_from_scene_boundaries,
    detect_scene_boundaries,
)
from cutmaster.configuration.schema import (
    AppConfig,
    ASRConfig,
    LLMConfig,
    SceneSegmentationConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.infrastructure.models.openai_compatible import (
    empty_usage_summary,
    load_usage_summary,
)
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.workflow.prompting.analyser import (
    SegmentSummaryDetails,
    SegmentShotAnnotationDetails,
    VideoSummaryDetails,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.infrastructure.observability.progress import progress_bar
from cutmaster.infrastructure.media.ffprobe import probe_media
from cutmaster.workflow.shared.timecode import format_range, parse_time
from cutmaster.workflow.contracts.video import (
    BoundarySource,
    CameraAngle,
    CameraMovement,
    CharacterAppearance,
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
    VisualAnnotationStatus,
)


@dataclass(frozen=True)
class _MaterialAnalysisArtifacts:
    """Internal artifacts produced while building reusable Video Memory."""

    material_directory: Path
    source_srt: Path
    processed_subtitle: Path
    dialogues_json: Path
    video_description_path: Path
    video_summary_path: Path
    analysis_history_path: Path
    video_description: dict[str, Any]
    video_summary: dict[str, Any]
    model_usage_path: Path | None = None
    model_usage_summary: dict[str, Any] = field(default_factory=dict)
    model_usage_cumulative_summary: dict[str, Any] = field(default_factory=dict)
    analysis_reused: bool = False

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


def _shot_frame_contact_sheet(image_data_urls: list[str]) -> str:
    if len(image_data_urls) != 5:
        raise ValueError("Shot contact sheet requires exactly five frames")
    frames: list[np.ndarray] = []
    for index, data_url in enumerate(image_data_urls, 1):
        _, separator, payload = data_url.partition(",")
        if not separator:
            raise ValueError("Shot frame must be an encoded data URI")
        encoded = np.frombuffer(base64.b64decode(payload), dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("Could not decode sampled Shot frame")
        height, width = frame.shape[:2]
        scale = min(1.0, 640.0 / max(1, width))
        resized = cv2.resize(
            frame,
            (
                max(1, round(width * scale)),
                max(1, round(height * scale)),
            ),
            interpolation=cv2.INTER_AREA,
        )
        cv2.rectangle(resized, (0, 0), (72, 42), (0, 0, 0), -1)
        cv2.putText(
            resized,
            str(index),
            (18, 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        frames.append(resized)

    cell_width = max(frame.shape[1] for frame in frames)
    cell_height = max(frame.shape[0] for frame in frames)
    canvas = np.zeros((cell_height * 2, cell_width * 3, 3), dtype=np.uint8)
    for index, frame in enumerate(frames):
        row, column = divmod(index, 3)
        height, width = frame.shape[:2]
        canvas[
            row * cell_height : row * cell_height + height,
            column * cell_width : column * cell_width + width,
        ] = frame
    ok, encoded_sheet = cv2.imencode(
        ".jpg",
        canvas,
        [int(cv2.IMWRITE_JPEG_QUALITY), 88],
    )
    if not ok:
        raise RuntimeError("Could not encode Shot frame contact sheet")
    return (
        "data:image/jpeg;base64,"
        + base64.b64encode(encoded_sheet.tobytes()).decode("ascii")
    )


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
    max_images_per_request: int = 250,
    max_shots_per_request: int = 20,
) -> list[SegmentDescription]:
    if max_images_per_request <= 0:
        raise ValueError("max_images_per_request must be positive")
    if max_shots_per_request <= 0:
        raise ValueError("max_shots_per_request must be positive")
    annotation_directory.mkdir(parents=True, exist_ok=True)
    annotation_progress = progress_bar(
        total=len(segments),
        description="Segment Shot VLM annotation",
        unit="segment",
    )

    def unavailable_annotation(
        shot_id: str,
        _has_dialogue: bool,
    ) -> dict[str, Any]:
        return {
            "shot_id": shot_id,
            "visual_description": None,
            "dominant_action": None,
            "content_type": None,
            "narrative_function": None,
            "emotional_tone": None,
            "emotional_intensity": None,
            "scene": None,
            "characters": [],
            "shot_scale": None,
            "camera_angle": None,
            "camera_movement": None,
            "composition": None,
            "visual_evidence": None,
            "visual_annotation_status": VisualAnnotationStatus.PROVIDER_REJECTED,
            "visual_annotation_failure": "data_inspection_failed",
        }

    def annotate_segment(segment: dict[str, Any]) -> SegmentDescription:
        clip_path = Path(segment["clip_path"])
        segment_start = float(segment["time_range"]["start_sec"])
        source_shots = segment["shots"]
        shot_ids = [str(shot["shot_id"]) for shot in source_shots]
        whole_request_uses_contact_sheets = (
            len(source_shots) * sample_frames > max_images_per_request
        )

        def nominal_sample_times(
            shots: list[dict[str, Any]],
        ) -> dict[str, list[float]]:
            result: dict[str, list[float]] = {}
            for shot in shots:
                global_start = float(shot["time_range"]["start_sec"])
                global_end = float(shot["time_range"]["end_sec"])
                result[str(shot["shot_id"])] = [
                    round(
                        global_start
                        + (global_end - global_start)
                        * (index + 0.5)
                        / sample_frames,
                        6,
                    )
                    for index in range(sample_frames)
                ]
            return result

        def valid_sample_times(value: Any, expected_ids: list[str]) -> bool:
            return (
                isinstance(value, dict)
                and set(value) == set(expected_ids)
                and all(
                    isinstance(value[shot_id], list)
                    and len(value[shot_id]) == sample_frames
                    and all(
                        isinstance(time_sec, (int, float))
                        for time_sec in value[shot_id]
                    )
                    for shot_id in expected_ids
                )
            )

        def build_package(
            sampled_times_by_shot: dict[str, list[float]],
            request_segment: dict[str, Any],
            frame_delivery: str,
            batch_index: int = 1,
            batch_count: int = 1,
        ):
            return prompt_registry.build(
                PromptStage.ANALYSER,
                PromptTask.SHOT_ANNOTATION,
                SegmentShotAnnotationDetails(
                    segment=request_segment,
                    sampled_frame_times_by_shot=sampled_times_by_shot,
                    frame_delivery=frame_delivery,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    total_segment_shots=len(source_shots),
                ),
            )

        def validate_annotations(
            parsed: dict[str, Any],
            expected_shots: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            expected_ids = [str(shot["shot_id"]) for shot in expected_shots]
            raw_annotations = parsed.get("shots")
            if not isinstance(raw_annotations, list):
                raise ValueError("Segment Shot annotation must return a shots array")
            if len(raw_annotations) != len(expected_shots):
                raise ValueError(
                    "Segment Shot annotation returned "
                    f"{len(raw_annotations)} Shots; expected {len(expected_shots)}"
                )
            returned_ids = [
                str(annotation.get("shot_id"))
                if isinstance(annotation, dict)
                else ""
                for annotation in raw_annotations
            ]
            if returned_ids != expected_ids:
                raise ValueError(
                    "Segment Shot annotation must preserve exact source order: "
                    f"expected {expected_ids}, got {returned_ids}"
                )
            return [
                _validate_shot_annotation(
                    annotation,
                    shot_id,
                    bool(shot["dialogue"]),
                )
                for shot, shot_id, annotation in zip(
                    expected_shots,
                    expected_ids,
                    raw_annotations,
                    strict=True,
                )
            ]

        checkpoint_path = annotation_directory / f"{segment['segment_id']}.json"
        checkpoint = _read_json_checkpoint(checkpoint_path)
        checkpoint_sample_times = (
            checkpoint.get("sampled_frame_times_by_shot")
            if isinstance(checkpoint, dict)
            else None
        )
        sampled_times_by_shot = (
            checkpoint_sample_times
            if valid_sample_times(checkpoint_sample_times, shot_ids)
            else nominal_sample_times(source_shots)
        )
        package = build_package(
            sampled_times_by_shot,
            segment,
            (
                "contact_sheet"
                if whole_request_uses_contact_sheets
                else "individual"
            ),
        )
        checkpoint_matches = (
            isinstance(checkpoint, dict)
            and checkpoint.get("prompt_fingerprint") == package.fingerprint
            and checkpoint.get("contract_fingerprint")
            == package.response_contract.fingerprint
        )
        annotations: list[dict[str, Any]] | None = None
        if checkpoint_matches and isinstance(checkpoint.get("response"), dict):
            try:
                structured = package.response_contract.validate_structure(
                    checkpoint["response"]
                )
                annotations = validate_annotations(structured, source_shots)
            except ValueError:
                annotations = None
        elif (
            checkpoint_matches
            and checkpoint.get("visual_annotation_status")
            == VisualAnnotationStatus.PROVIDER_REJECTED
            and checkpoint.get("visual_annotation_failure")
            == "data_inspection_failed"
        ):
            annotations = [
                unavailable_annotation(shot_id, bool(shot["dialogue"]))
                for shot, shot_id in zip(source_shots, shot_ids, strict=True)
            ]

        if annotations is None:
            request_shot_limit = min(
                max_shots_per_request,
                max_images_per_request,
            )
            request_batches = [
                source_shots[index : index + request_shot_limit]
                for index in range(0, len(source_shots), request_shot_limit)
            ]
            batch_count = len(request_batches)
            use_request_batches = batch_count > 1
            if use_request_batches:
                log_event(
                    "INFO",
                    "analyser",
                    "stage.progress",
                    "Long Segment Shot annotation split into checkpointed requests",
                    segment_id=segment["segment_id"],
                    shots=len(source_shots),
                    request_batches=batch_count,
                    max_shots_per_request=max_shots_per_request,
                )
            batch_directory = annotation_directory / str(segment["segment_id"])
            if use_request_batches:
                batch_directory.mkdir(parents=True, exist_ok=True)

            merged_annotations: list[dict[str, Any]] = []
            sampled_times_by_shot = {}
            contains_provider_rejection = False
            for batch_index, batch_shots in enumerate(request_batches, 1):
                batch_ids = [str(shot["shot_id"]) for shot in batch_shots]
                batch_segment = {**segment, "shots": batch_shots}
                frame_delivery = (
                    "contact_sheet"
                    if (
                        use_request_batches
                        or len(batch_shots) * sample_frames
                        > max_images_per_request
                    )
                    else "individual"
                )
                batch_checkpoint_path = (
                    batch_directory / f"batch_{batch_index:03d}.json"
                    if use_request_batches
                    else checkpoint_path
                )
                batch_checkpoint = _read_json_checkpoint(batch_checkpoint_path)
                checkpoint_batch_times = (
                    batch_checkpoint.get("sampled_frame_times_by_shot")
                    if isinstance(batch_checkpoint, dict)
                    else None
                )
                batch_sample_times = (
                    checkpoint_batch_times
                    if valid_sample_times(checkpoint_batch_times, batch_ids)
                    else nominal_sample_times(batch_shots)
                )
                batch_package = build_package(
                    batch_sample_times,
                    batch_segment,
                    frame_delivery,
                    batch_index,
                    batch_count,
                )
                batch_checkpoint_matches = (
                    isinstance(batch_checkpoint, dict)
                    and batch_checkpoint.get("prompt_fingerprint")
                    == batch_package.fingerprint
                    and batch_checkpoint.get("contract_fingerprint")
                    == batch_package.response_contract.fingerprint
                )
                batch_annotations: list[dict[str, Any]] | None = None
                batch_rejected = False
                if (
                    batch_checkpoint_matches
                    and isinstance(batch_checkpoint.get("response"), dict)
                ):
                    try:
                        structured = (
                            batch_package.response_contract.validate_structure(
                                batch_checkpoint["response"]
                            )
                        )
                        batch_annotations = validate_annotations(
                            structured,
                            batch_shots,
                        )
                    except ValueError:
                        batch_annotations = None
                elif (
                    batch_checkpoint_matches
                    and batch_checkpoint.get("visual_annotation_status")
                    == VisualAnnotationStatus.PROVIDER_REJECTED
                    and batch_checkpoint.get("visual_annotation_failure")
                    == "data_inspection_failed"
                ):
                    batch_annotations = [
                        unavailable_annotation(
                            shot_id,
                            bool(shot["dialogue"]),
                        )
                        for shot, shot_id in zip(
                            batch_shots,
                            batch_ids,
                            strict=True,
                        )
                    ]
                    batch_rejected = True

                if batch_annotations is None:
                    image_data_urls: list[str] = []
                    image_labels: list[str] = []
                    batch_sample_times = {}
                    for shot, shot_id in zip(
                        batch_shots,
                        batch_ids,
                        strict=True,
                    ):
                        global_start = float(shot["time_range"]["start_sec"])
                        global_end = float(shot["time_range"]["end_sec"])
                        images, sampled_times = _sample_shot_frames(
                            clip_path,
                            global_start - segment_start,
                            global_end - segment_start,
                            global_start,
                            sample_frames,
                        )
                        batch_sample_times[shot_id] = sampled_times
                        if frame_delivery == "contact_sheet":
                            image_data_urls.append(
                                _shot_frame_contact_sheet(images)
                            )
                            image_labels.append(
                                f"{shot_id} contact sheet; "
                                f"frames 1-{sample_frames} at "
                                + ", ".join(
                                    f"{time_sec:.3f}s"
                                    for time_sec in sampled_times
                                )
                            )
                        else:
                            image_data_urls.extend(images)
                            image_labels.extend(
                                f"{shot_id} frame {index}/{sample_frames} "
                                f"at {time_sec:.3f}s"
                                for index, time_sec in enumerate(
                                    sampled_times,
                                    1,
                                )
                            )
                    batch_package = build_package(
                        batch_sample_times,
                        batch_segment,
                        frame_delivery,
                        batch_index,
                        batch_count,
                    )
                    if frame_delivery == "contact_sheet":
                        log_event(
                            "INFO",
                            "analyser",
                            "fallback.apply",
                            "Shot frames packed into per-Shot contact sheets",
                            segment_id=segment["segment_id"],
                            batch_index=batch_index,
                            batch_count=batch_count,
                            shots=len(batch_shots),
                            sampled_frames=len(batch_shots) * sample_frames,
                            request_images=len(image_data_urls),
                            max_images_per_request=max_images_per_request,
                        )
                    try:
                        batch_annotations = context.call_prompt(
                            package=batch_package,
                            config=config,
                            validate_business=lambda parsed, expected=batch_shots: (
                                validate_annotations(parsed, expected)
                            ),
                            image_data_urls=image_data_urls,
                            image_labels=image_labels,
                        )
                    except Exception as exc:
                        if "data_inspection_failed" not in str(exc).lower():
                            raise
                        batch_annotations = [
                            unavailable_annotation(
                                shot_id,
                                bool(shot["dialogue"]),
                            )
                            for shot, shot_id in zip(
                                batch_shots,
                                batch_ids,
                                strict=True,
                            )
                        ]
                        batch_rejected = True
                        failure = build_prompt_failure(
                            PromptFailureCode.PROVIDER_IMAGE_INSPECTION_FAILED,
                            operation="segment_shot_visual_annotation",
                            error_message=str(exc),
                        )
                        log_event(
                            "WARNING",
                            "analyser",
                            "fallback.apply",
                            "Segment Shot annotation batch skipped after provider content inspection",
                            segment_id=segment["segment_id"],
                            batch_index=batch_index,
                            batch_count=batch_count,
                            shots=len(batch_shots),
                            visual_annotation_status=(
                                VisualAnnotationStatus.PROVIDER_REJECTED
                            ),
                            **failure,
                        )

                    batch_checkpoint_payload: dict[str, Any] = {
                        "schema_version": "1.0",
                        "segment_id": segment["segment_id"],
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "shot_ids": batch_ids,
                        "prompt_id": batch_package.prompt_id,
                        "prompt_version": batch_package.prompt_version,
                        "prompt_fingerprint": batch_package.fingerprint,
                        "contract_fingerprint": (
                            batch_package.response_contract.fingerprint
                        ),
                        "sampled_frame_times_by_shot": batch_sample_times,
                    }
                    if batch_rejected:
                        batch_checkpoint_payload.update(
                            {
                                "visual_annotation_status": (
                                    VisualAnnotationStatus.PROVIDER_REJECTED
                                ),
                                "visual_annotation_failure": (
                                    "data_inspection_failed"
                                ),
                            }
                        )
                    else:
                        batch_checkpoint_payload["response"] = {
                            "shots": batch_annotations
                        }
                    _write_json_checkpoint(
                        batch_checkpoint_path,
                        batch_checkpoint_payload,
                    )

                sampled_times_by_shot.update(batch_sample_times)
                merged_annotations.extend(batch_annotations)
                contains_provider_rejection = (
                    contains_provider_rejection or batch_rejected
                )
            annotations = merged_annotations
            package = build_package(
                sampled_times_by_shot,
                segment,
                (
                    "contact_sheet"
                    if whole_request_uses_contact_sheets
                    else "individual"
                ),
            )
            if not contains_provider_rejection:
                _write_json_checkpoint(
                    checkpoint_path,
                    {
                        "schema_version": "1.0",
                        "segment_id": segment["segment_id"],
                        "shot_ids": shot_ids,
                        "prompt_id": package.prompt_id,
                        "prompt_version": package.prompt_version,
                        "prompt_fingerprint": package.fingerprint,
                        "contract_fingerprint": (
                            package.response_contract.fingerprint
                        ),
                        "sampled_frame_times_by_shot": sampled_times_by_shot,
                        "response": {"shots": annotations},
                    },
                )

        annotated_shots: list[ShotDescription] = []
        for shot, shot_id, annotation in zip(
            source_shots,
            shot_ids,
            annotations,
            strict=True,
        ):
            global_start = float(shot["time_range"]["start_sec"])
            global_end = float(shot["time_range"]["end_sec"])
            local_start = global_start - segment_start
            local_end = global_end - segment_start
            dialogue_occurrences = [
                DialogueOccurrence(
                    dialogue_id=int(item["dialogue_id"]),
                    time_range=TimeRange(**item["time_range"]),
                    speaker=str(item["speaker"]),
                    text=str(item["text"]),
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
                    sampled_frame_times_sec=sampled_times_by_shot[shot_id],
                    scene=(
                        SceneDescription(**annotation["scene"])
                        if annotation["scene"] is not None
                        else None
                    ),
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

        visually_annotated_shots = [
            shot
            for shot in annotated_shots
            if shot.visual_annotation_status == VisualAnnotationStatus.COMPLETE
        ]
        total_duration = sum(
            shot.time_range.duration_sec for shot in visually_annotated_shots
        )
        if segment["has_dialogue"]:
            content_type = SegmentContentType.NARRATIVE
        else:
            durations: dict[SegmentContentType, float] = {
                SegmentContentType.LANDSCAPE: 0.0,
                SegmentContentType.EMOTIONAL: 0.0,
                SegmentContentType.PANTOMIME: 0.0,
            }
            for shot in visually_annotated_shots:
                durations[shot.content_type] += shot.time_range.duration_sec
            content_type = (
                max(durations, key=durations.get)
                if visually_annotated_shots
                else None
            )
        representative = (
            max(
                visually_annotated_shots,
                key=lambda shot: shot.time_range.duration_sec,
            )
            if visually_annotated_shots
            else None
        )
        appearing_characters = list(
            dict.fromkeys(
                character.name
                for shot in visually_annotated_shots
                for character in shot.characters
            )
        )
        segment_dialogue = [
            DialogueOccurrence(
                dialogue_id=int(item["dialogue_id"]),
                time_range=TimeRange(**item["time_range"]),
                speaker=str(item["speaker"]),
                text=str(item["text"]),
            )
            for item in segment["dialogue_items"]
        ]
        description = SegmentDescription(
            segment_id=segment["segment_id"],
            time_range=TimeRange(**segment["time_range"]),
            clip_path=segment["clip_path"],
            has_dialogue=bool(segment["has_dialogue"]),
            speech_mode=SpeechMode(segment["speech_mode"]),
            content_type=content_type,
            timeline_role=TimelineRole(segment["timeline_role"]),
            shots=annotated_shots,
            dialogue_items=segment_dialogue,
            segment_summary=None,
            narrative_function=(
                " ".join(
                    dict.fromkeys(
                        shot.narrative_function
                        for shot in visually_annotated_shots
                        if shot.narrative_function is not None
                    )
                )
                or None
            ),
            emotional_tone=(
                representative.emotional_tone
                if representative is not None
                else None
            ),
            emotional_intensity=(
                sum(
                    shot.emotional_intensity * shot.time_range.duration_sec
                    for shot in visually_annotated_shots
                )
                / total_duration
                if total_duration > 0
                else None
            ),
            appearing_characters=appearing_characters,
        )
        description.validate()
        annotation_progress.update()
        return description

    worker_count = max(1, min(config.max_concurrency, len(segments)))
    log_event(
        "INFO",
        "analyser",
        "stage.progress",
        "Segment Shot annotation concurrency configured",
        segments=len(segments),
        workers=worker_count,
        request_policy="bounded_shot_batches_serial_per_segment",
        max_shots_per_request=max_shots_per_request,
    )
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="segment-vlm",
        ) as executor:
            return list(executor.map(annotate_segment, segments))
    finally:
        annotation_progress.close()


def _segment_summary_input(
    segment: SegmentDescription,
) -> dict[str, Any]:
    dialogues: dict[int, dict[str, Any]] = {}
    shots: list[dict[str, Any]] = []
    for shot in segment.shots:
        for dialogue in shot.dialogue:
            dialogues.setdefault(
                dialogue.dialogue_id,
                {
                    "dialogue_id": dialogue.dialogue_id,
                    "time_range": asdict(dialogue.time_range),
                    "speaker": dialogue.speaker,
                    "text": dialogue.text,
                },
            )
        shots.append(
            {
                "shot_id": shot.shot_id,
                "time_range": asdict(shot.time_range),
                "visual_annotation_status": shot.visual_annotation_status,
                "visual_description": shot.visual_description,
                "dominant_action": shot.dominant_action,
                "narrative_function": shot.narrative_function,
                "emotional_tone": shot.emotional_tone,
                "characters": [
                    {
                        "name": character.name,
                        "description": character.description,
                        "identity_likert": character.identity_likert,
                    }
                    for character in shot.characters
                ],
            }
        )
    return {
        "segment_id": segment.segment_id,
        "time_range": asdict(segment.time_range),
        "has_dialogue": segment.has_dialogue,
        "speech_mode": segment.speech_mode,
        "dialogue_items": sorted(
            dialogues.values(),
            key=lambda item: (
                float(item["time_range"]["start_sec"]),
                int(item["dialogue_id"]),
            ),
        ),
        "shots": shots,
    }


def _summarize_segments(
    segments: list[SegmentDescription],
    context: WorkflowContext,
    config: LLMConfig,
    summary_directory: Path,
) -> list[SegmentDescription]:
    summary_directory.mkdir(parents=True, exist_ok=True)
    summary_progress = progress_bar(
        total=len(segments),
        description="Segment LLM summarization",
        unit="segment",
    )

    def summarize(segment: SegmentDescription) -> SegmentDescription:
        if (
            not segment.has_dialogue
            and not any(
                shot.visual_annotation_status
                == VisualAnnotationStatus.COMPLETE
                for shot in segment.shots
            )
        ):
            summary_progress.update()
            return segment
        package = prompt_registry.build(
            PromptStage.ANALYSER,
            PromptTask.SEGMENT_SUMMARY,
            SegmentSummaryDetails(
                segment=_segment_summary_input(segment),
            ),
        )
        checkpoint_path = summary_directory / f"{segment.segment_id}.json"
        checkpoint = _read_json_checkpoint(checkpoint_path)
        summary: str | None = None
        timeline_role: TimelineRole | None = None
        if (
            isinstance(checkpoint, dict)
            and checkpoint.get("prompt_fingerprint") == package.fingerprint
            and checkpoint.get("contract_fingerprint")
            == package.response_contract.fingerprint
            and isinstance(checkpoint.get("summary"), dict)
        ):
            try:
                structured = package.response_contract.validate_structure(
                    checkpoint["summary"]
                )
                summary = str(structured["segment_summary"]).strip()
                timeline_role = TimelineRole(str(structured["timeline_role"]))
            except ValueError:
                summary = None
                timeline_role = None
        if summary is None or timeline_role is None:
            result = context.call_prompt(
                package=package,
                config=config,
            )
            summary = str(result["segment_summary"]).strip()
            timeline_role = TimelineRole(str(result["timeline_role"]))
            _write_json_checkpoint(
                checkpoint_path,
                {
                    "schema_version": "1.0",
                    "segment_id": segment.segment_id,
                    "prompt_id": package.prompt_id,
                    "prompt_version": package.prompt_version,
                    "prompt_fingerprint": package.fingerprint,
                    "contract_fingerprint": (
                        package.response_contract.fingerprint
                    ),
                    "summary": result,
                },
            )
        summarized = replace(
            segment,
            segment_summary=summary,
            timeline_role=timeline_role,
        )
        summarized.validate()
        summary_progress.update()
        return summarized

    worker_count = max(1, min(config.max_concurrency, len(segments)))
    log_event(
        "INFO",
        "analyser",
        "stage.progress",
        "Segment summarization concurrency configured",
        segments=len(segments),
        workers=worker_count,
    )
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="segment-llm",
        ) as executor:
            return list(executor.map(summarize, segments))
    finally:
        summary_progress.close()


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


def _video_summary_context(
    video_description: dict[str, Any],
    full_dialogue: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "segments": [
            {
                key: value
                for key, value in segment.items()
                if key != "shots"
            }
            for segment in video_description["segments"]
        ],
        "full_dialogue": full_dialogue,
    }


def _cache_result(
    material_directory: Path,
    expected_signature: dict[str, Any],
) -> _MaterialAnalysisArtifacts | None:
    manifest_path = material_directory / "analysis_manifest.json"
    description_path = material_directory / "video_description.json"
    summary_path = material_directory / "video_summary.json"
    manifest = _read_json_checkpoint(manifest_path)
    if (
        not isinstance(manifest, dict)
        or any(
            manifest.get(key) != value
            for key, value in expected_signature.items()
        )
        or manifest.get("scene_segmentation_version")
        != SCENE_SEGMENTATION_VERSION
        or manifest.get("segment_summary_prompt_version") != "2.0"
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
    return _MaterialAnalysisArtifacts(
        material_directory=material_directory,
        source_srt=material_directory / "source.srt",
        processed_subtitle=material_directory / "dialogue_merged.srt",
        dialogues_json=material_directory / "dialogues.json",
        video_description_path=description_path,
        video_summary_path=summary_path,
        analysis_history_path=material_directory / "analysis_history.json",
        video_description=description,
        video_summary=summary,
        model_usage_path=(
            material_directory / "model_usage.json"
            if (material_directory / "model_usage.json").is_file()
            else None
        ),
        model_usage_summary=empty_usage_summary(),
        model_usage_cumulative_summary=load_usage_summary(
            material_directory / "model_usage.json",
            cumulative=True,
        ),
        analysis_reused=True,
    )


def _require_compatible_incomplete_analysis(
    material_directory: Path,
    analysis_signature: dict[str, Any],
) -> None:
    """Prevent checkpoints from different analysis inputs being mixed."""

    spec_path = material_directory / "analysis_spec.json"
    existing = _read_json_checkpoint(spec_path)
    if existing is not None:
        if existing != analysis_signature:
            raise ValueError(
                "Incomplete Material analysis was created with a different "
                "subtitle or analysis configuration; resume with the original "
                "inputs or add the source under a different Material Name"
            )
        return
    if spec_path.exists():
        raise ValueError(f"Material analysis specification is invalid: {spec_path}")
    stale_entries = (
        [
            path
            for path in material_directory.iterdir()
            if path.name != ".analysis.lock"
        ]
        if material_directory.is_dir()
        else []
    )
    if stale_entries:
        raise ValueError(
            "Incomplete Material analysis has checkpoints without a recorded "
            "analysis specification; add the source under a different Material "
            "Name before retrying"
        )
    material_directory.mkdir(parents=True, exist_ok=True)
    _write_json_checkpoint(spec_path, analysis_signature)


def _analyse_video_material(
    video_path: Path,
    video_title: str,
    provided_subtitle: Path | None,
    detection_config: ShotDetectionConfig,
    asr_config: ASRConfig,
    scene_config: SceneSegmentationConfig,
    annotation_config: ShotAnnotationConfig,
    llm_config: LLMConfig,
    vlm_config: VLMConfig,
    material_directory: Path,
) -> _MaterialAnalysisArtifacts:
    if annotation_config.shot_sample_frames != 5:
        raise ValueError("shot_annotation.shot_sample_frames must be exactly 5")
    analysis_signature = _analysis_signature(
        video_path,
        video_title,
        provided_subtitle,
        detection_config,
        asr_config,
        scene_config,
        annotation_config,
        llm_config,
        vlm_config,
    )
    material_directory = material_directory.resolve()
    cached = _cache_result(material_directory, analysis_signature)
    if cached is not None:
        return cached
    _require_compatible_incomplete_analysis(
        material_directory,
        analysis_signature,
    )
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
    model_usage_path = material_directory / "model_usage.json"
    context = WorkflowContext(
        history_path,
        model_usage_path=model_usage_path,
        stage_name="analyser",
    )

    media = probe_media(video_path)
    duration_sec = float(media["duration"])
    _reuse_compatible_stage_checkpoints(
        material_directory,
        analysis_signature,
        duration_sec,
    )
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
            stage_count=7,
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
            stage_count=7,
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
            stage_count=7,
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
        stage_count=7,
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
            stage_count=7,
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
            stage_count=7,
        )
    dialogue_document = json.loads(dialogues_json.read_text(encoding="utf-8"))
    dialogue = _dialogue_with_shot_membership(
        _compact_dialogue(dialogue_document),
        shots,
    )
    if context.get_artifact("full_dialogue") != dialogue:
        context.set_artifact("full_dialogue", dialogue)

    segments_path = material_directory / "segments.json"
    scene_manifest_path = material_directory / "scene_segmentation_manifest.json"
    scene_manifest = _read_json_checkpoint(scene_manifest_path)
    segments = (
        _valid_segment_checkpoint(
            _read_json_checkpoint(segments_path),
            shots,
        )
        if isinstance(scene_manifest, dict)
        and scene_manifest.get("method") == "scene_vlm"
        and scene_manifest.get("version") == SCENE_SEGMENTATION_VERSION
        else None
    )
    if segments is None:
        stage_started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Scene-VLM segmentation started",
            stage="scene_segmentation",
            stage_index=3,
            stage_count=7,
        )
        scene_boundaries = detect_scene_boundaries(
            video_path,
            shots,
            dialogue,
            context,
            vlm_config,
            scene_config,
            material_directory / "scene_frames",
            material_directory / "scene_boundary_windows",
        )
        _write_json_checkpoint(
            material_directory / "scene_boundaries.json",
            scene_boundaries,
        )
        segments = build_segments_from_scene_boundaries(
            shots,
            dialogue,
            scene_boundaries,
        )
        _write_json_checkpoint(segments_path, segments)
        _write_json_checkpoint(
            scene_manifest_path,
            {
                "method": "scene_vlm",
                "version": SCENE_SEGMENTATION_VERSION,
                "context_shots": scene_config.context_shots,
                "focus_shots": scene_config.focus_shots,
                "frames_per_shot": scene_config.frames_per_shot,
                "model": vlm_config.model,
            },
        )
        context.set_artifact("segments", segments)
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Scene-VLM segmentation completed",
            stage="scene_segmentation",
            stage_index=3,
            stage_count=7,
            segments=len(segments),
            elapsed_sec=time.monotonic() - stage_started,
        )
    else:
        log_event(
            "INFO",
            "analyser",
            "checkpoint.resume",
            "Scene-VLM Segment checkpoint resumed",
            stage="scene_segmentation",
            stage_index=3,
            stage_count=7,
            segments=len(segments),
        )
        if not segments_path.is_file():
            _write_json_checkpoint(segments_path, segments)
        context.set_artifact("segments", segments)

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "analyser",
        "stage.start",
        "Reusable Segment clip preparation started",
        stage="segment_clip_preparation",
        stage_index=4,
        stage_count=7,
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
        stage_count=7,
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
        stage_count=7,
        segments=len(segments),
    )
    annotated_segments = _annotate_segments(
        segments,
        context,
        vlm_config,
        annotation_config.shot_sample_frames,
        material_directory / "shot_annotations",
        annotation_config.max_images_per_request,
        annotation_config.max_shots_per_request,
    )
    log_event(
        "INFO",
        "analyser",
        "stage.complete",
        "Shot annotation completed",
        stage="shot_annotation",
        stage_index=5,
        stage_count=7,
        segments=len(annotated_segments),
        elapsed_sec=time.monotonic() - stage_started,
    )
    stage_started = time.monotonic()
    log_event(
        "INFO",
        "analyser",
        "stage.start",
        "Segment summarization started",
        stage="segment_summarization",
        stage_index=6,
        stage_count=7,
        segments=len(annotated_segments),
    )
    annotated_segments = _summarize_segments(
        annotated_segments,
        context,
        llm_config,
        material_directory / "segment_summaries",
    )
    log_event(
        "INFO",
        "analyser",
        "stage.complete",
        "Segment summarization completed",
        stage="segment_summarization",
        stage_index=6,
        stage_count=7,
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
        scene_boundary_model=vlm_config.model,
        visual_description_model=vlm_config.model,
    )
    description_dict = video_description.to_dict()
    description_path = material_directory / "video_description.json"
    _write_json_checkpoint(description_path, description_dict)
    context.set_artifact("video_description", description_dict)
    context.set_artifact(
        "video_summary_context",
        _video_summary_context(description_dict, dialogue),
    )
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
            stage_index=7,
            stage_count=7,
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
            stage_index=7,
            stage_count=7,
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
            stage_index=7,
            stage_count=7,
        )
    _write_json_checkpoint(
        material_directory / "analysis_manifest.json",
        {
            **analysis_signature,
            "scene_segmentation_method": "scene_vlm",
            "scene_segmentation_version": SCENE_SEGMENTATION_VERSION,
            "scene_boundaries": str(
                (material_directory / "scene_boundaries.json").resolve()
            ),
            "segment_summary_prompt_version": "2.0",
            "material_directory": str(material_directory.resolve()),
            "video_description": str(description_path.resolve()),
            "video_summary": str(summary_path.resolve()),
        },
    )
    context.save_model_usage()
    return _MaterialAnalysisArtifacts(
        material_directory=material_directory,
        source_srt=source_srt,
        processed_subtitle=processed_subtitle,
        dialogues_json=dialogues_json,
        video_description_path=description_path,
        video_summary_path=summary_path,
        analysis_history_path=history_path,
        video_description=description_dict,
        video_summary=video_summary,
        model_usage_path=model_usage_path,
        model_usage_summary=context.model_usage_summary(),
        model_usage_cumulative_summary=context.model_usage_summary(
            include_prior=True
        ),
    )


class MaterialAnalystAgent:
    """M agent: build reusable memory for video and music Materials."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def analyse(
        self,
        video_path: Path,
        video_title: str,
        provided_subtitle: Path | None,
        *,
        material_directory: Path,
    ) -> _MaterialAnalysisArtifacts:
        return _analyse_video_material(
            video_path,
            video_title,
            provided_subtitle,
            self.config.analyser.shot_detection,
            self.config.analyser.asr,
            self.config.analyser.scene_segmentation,
            self.config.analyser.shot_annotation,
            self.config.llm,
            self.config.vlm,
            material_directory,
        )

    def analyse_music(self, audio_path: Path) -> dict[str, Any]:
        """Build edit-independent Music Memory for the complete source track."""
        return analyze_music_memory(audio_path)


__all__ = ["MaterialAnalystAgent"]
