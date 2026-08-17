"""Scene-VLM semantic scene segmentation over PySceneDetect shots."""

from __future__ import annotations

import base64
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2

from cutmaster.configuration.schema import SceneSegmentationConfig, VLMConfig
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.infrastructure.observability.progress import progress_bar
from cutmaster.workflow.analyser.tools.cache import (
    read_json_checkpoint,
    write_json_checkpoint,
)
from cutmaster.workflow.contracts.video import SpeechMode, TimelineRole
from cutmaster.workflow.ports import CancellationToken, raise_if_cancelled
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.analyser import (
    SCENE_BOUNDARY_PROMPT_VERSION,
    SceneBoundaryDetectionDetails,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext

_FRAME_MAX_SIDE = 640
_FRAME_JPEG_QUALITY = 85
SCENE_SEGMENTATION_VERSION = SCENE_BOUNDARY_PROMPT_VERSION
SCENE_FRAME_CACHE_VERSION = "2.0"


@dataclass(frozen=True)
class SceneWindow:
    window_id: str
    context_indexes: tuple[int, ...]
    focus_indexes: tuple[int, ...]


def build_scene_windows(
    shot_count: int,
    config: SceneSegmentationConfig,
) -> list[SceneWindow]:
    """Partition every non-final Shot decision into one centered focus window."""
    if shot_count <= 1:
        return []
    target_count = shot_count - 1
    context_length = min(config.context_shots, shot_count)
    margin = max(0, (config.context_shots - config.focus_shots) // 2)
    windows: list[SceneWindow] = []
    for window_number, focus_start in enumerate(
        range(0, target_count, config.focus_shots),
        1,
    ):
        focus_end = min(focus_start + config.focus_shots, target_count)
        context_start = max(
            0,
            min(focus_start - margin, shot_count - context_length),
        )
        context_end = context_start + context_length
        windows.append(
            SceneWindow(
                window_id=f"scene_window_{window_number:05d}",
                context_indexes=tuple(range(context_start, context_end)),
                focus_indexes=tuple(range(focus_start, focus_end)),
            )
        )
    covered = [index for window in windows for index in window.focus_indexes]
    if covered != list(range(target_count)):
        raise ValueError("Scene focus windows do not cover every boundary exactly once")
    return windows


def _frame_times(shot: dict[str, Any], count: int) -> list[float]:
    start = float(shot["time_range"]["start_sec"])
    end = float(shot["time_range"]["end_sec"])
    duration = end - start
    return [
        round(start + duration * (index + 0.5) / count, 6) for index in range(count)
    ]


def _resize_for_vlm(frame: Any) -> Any:
    height, width = frame.shape[:2]
    largest = max(height, width)
    if largest <= _FRAME_MAX_SIDE:
        return frame
    scale = _FRAME_MAX_SIDE / largest
    return cv2.resize(
        frame,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _overlay_shot_marker(
    frame: Any,
    shot_id: str,
    frame_index: int,
    frame_count: int,
) -> Any:
    label = f"{shot_id}  {frame_index}/{frame_count}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    thickness = 2
    (text_width, text_height), baseline = cv2.getTextSize(
        label,
        font,
        font_scale,
        thickness,
    )
    cv2.rectangle(
        frame,
        (0, 0),
        (text_width + 14, text_height + baseline + 12),
        (0, 0, 0),
        -1,
    )
    cv2.putText(
        frame,
        label,
        (7, text_height + 5),
        font,
        font_scale,
        (255, 255, 255),
        thickness,
        cv2.LINE_AA,
    )
    return frame


def _resolve_frame_file(frame_directory: Path, file_ref: object) -> Path:
    """Resolve one filename-only cache reference inside its owning directory."""

    if not isinstance(file_ref, str) or not file_ref:
        raise ValueError("Scene frame file reference must be a non-empty string")
    relative = Path(file_ref)
    if relative.is_absolute() or relative.name != file_ref:
        raise ValueError("Scene frame file reference must be a filename")
    root = frame_directory.resolve()
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError("Scene frame cache files cannot be symlinks")
    try:
        candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("Scene frame file reference escapes its cache") from exc
    return candidate


def prepare_scene_frames(
    video_path: Path,
    shots: list[dict[str, Any]],
    config: SceneSegmentationConfig,
    frame_directory: Path,
    cancellation_token: CancellationToken | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Persist three labelled Scene-VLM frames per Shot for resumable reuse."""
    raise_if_cancelled(cancellation_token)
    frame_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = frame_directory / "manifest.json"
    manifest = read_json_checkpoint(manifest_path)
    expected: dict[str, list[dict[str, Any]]] = {}
    for shot in shots:
        shot_id = str(shot["shot_id"])
        expected[shot_id] = []
        for frame_index, time_sec in enumerate(
            _frame_times(shot, config.frames_per_shot),
            1,
        ):
            expected[shot_id].append(
                {
                    "file": f"{shot_id}_{frame_index:02d}.jpg",
                    "time_sec": time_sec,
                }
            )
    expected_manifest = {
        "schema_version": "2.0",
        "frame_cache_version": SCENE_FRAME_CACHE_VERSION,
        "frames_per_shot": config.frames_per_shot,
        "shots": expected,
    }
    force_rebuild = manifest != expected_manifest
    missing = 0
    for samples in expected.values():
        for sample in samples:
            path = _resolve_frame_file(frame_directory, sample["file"])
            if force_rebuild or not path.is_file() or path.stat().st_size <= 0:
                missing += 1
    if missing == 0:
        log_event(
            "INFO",
            "analyser",
            "checkpoint.resume",
            "Scene-VLM frame cache resumed",
            shots=len(shots),
            frames=sum(len(items) for items in expected.values()),
        )
        return expected

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open source video for Scene-VLM: {video_path}")
    try:
        with progress_bar(
            shots,
            total=len(shots),
            description="Scene-VLM frame preparation",
            unit="shot",
        ) as progress:
            for shot in progress:
                raise_if_cancelled(cancellation_token)
                shot_id = str(shot["shot_id"])
                previous_path: Path | None = None
                for frame_index, sample in enumerate(expected[shot_id], 1):
                    output = _resolve_frame_file(frame_directory, sample["file"])
                    if (
                        not force_rebuild
                        and output.is_file()
                        and output.stat().st_size > 0
                    ):
                        previous_path = output
                        continue
                    capture.set(
                        cv2.CAP_PROP_POS_MSEC,
                        float(sample["time_sec"]) * 1000.0,
                    )
                    ok, frame = capture.read()
                    if not ok:
                        if previous_path is None:
                            raise RuntimeError(
                                f"Could not sample any Scene-VLM frame for {shot_id}"
                            )
                        shutil.copyfile(previous_path, output)
                        log_event(
                            "WARNING",
                            "analyser",
                            "fallback.apply",
                            "Last decodable Scene-VLM Shot frame repeated",
                            shot_id=shot_id,
                            frame_index=frame_index,
                            failed_sample_time_sec=sample["time_sec"],
                        )
                        previous_path = output
                        continue
                    frame = _resize_for_vlm(frame)
                    frame = _overlay_shot_marker(
                        frame,
                        shot_id,
                        frame_index,
                        config.frames_per_shot,
                    )
                    if not cv2.imwrite(
                        str(output),
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), _FRAME_JPEG_QUALITY],
                    ):
                        raise RuntimeError(f"Could not write Scene-VLM frame: {output}")
                    previous_path = output
                raise_if_cancelled(cancellation_token)
    finally:
        capture.release()
    write_json_checkpoint(
        manifest_path,
        expected_manifest,
    )
    raise_if_cancelled(cancellation_token)
    return expected


def _data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode(
        "ascii"
    )


def _shots_with_dialogue(
    shots: list[dict[str, Any]],
    dialogue: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    dialogue_by_shot: dict[str, list[dict[str, Any]]] = {
        str(shot["shot_id"]): [] for shot in shots
    }
    for line in dialogue:
        for shot_id in line.get("covering_shot_ids") or []:
            dialogue_by_shot[str(shot_id)].append(line)
    return [
        {
            **shot,
            "dialogue": dialogue_by_shot[str(shot["shot_id"])],
        }
        for shot in shots
    ]


def _validate_window_decisions(
    parsed: dict[str, Any],
    focus_shot_ids: list[str],
) -> list[dict[str, Any]]:
    raw = parsed.get("decisions")
    if not isinstance(raw, list):
        raise ValueError("Scene boundary response must contain decisions")
    actual_ids = [str(item.get("shot_id") or "") for item in raw]
    if actual_ids != focus_shot_ids:
        raise ValueError(
            "Scene boundary decisions must match the exact focus Shot order: "
            f"expected={focus_shot_ids}, actual={actual_ids}"
        )
    return [
        {
            "shot_id": shot_id,
            "is_scene_end": bool(item["is_scene_end"]),
            "confidence_likert": int(item["confidence_likert"]),
        }
        for shot_id, item in zip(focus_shot_ids, raw, strict=True)
    ]


def detect_scene_boundaries(
    video_path: Path,
    shots: list[dict[str, Any]],
    dialogue: list[dict[str, Any]],
    context: WorkflowContext,
    vlm_config: VLMConfig,
    scene_config: SceneSegmentationConfig,
    frame_directory: Path,
    checkpoint_directory: Path,
    cancellation_token: CancellationToken | None = None,
) -> list[dict[str, Any]]:
    """Run context-focus Scene boundary classification with window checkpoints."""
    raise_if_cancelled(cancellation_token)
    windows = build_scene_windows(len(shots), scene_config)
    if not windows:
        return []
    checkpoint_directory.mkdir(parents=True, exist_ok=True)
    frames = prepare_scene_frames(
        video_path,
        shots,
        scene_config,
        frame_directory,
        cancellation_token,
    )
    contextual_shots = _shots_with_dialogue(shots, dialogue)
    progress = progress_bar(
        total=len(windows),
        description="Scene-VLM boundary detection",
        unit="window",
    )

    def process(window: SceneWindow) -> list[dict[str, Any]]:
        raise_if_cancelled(cancellation_token)
        window_shots = [contextual_shots[index] for index in window.context_indexes]
        focus_shot_ids = [
            str(contextual_shots[index]["shot_id"]) for index in window.focus_indexes
        ]
        package = prompt_registry.build(
            PromptStage.ANALYSER,
            PromptTask.SCENE_BOUNDARY_DETECTION,
            SceneBoundaryDetectionDetails(
                window_id=window.window_id,
                shots=window_shots,
                focus_shot_ids=focus_shot_ids,
            ),
        )
        checkpoint_path = checkpoint_directory / f"{window.window_id}.json"
        checkpoint = read_json_checkpoint(checkpoint_path)
        decisions: list[dict[str, Any]] | None = None
        if (
            isinstance(checkpoint, dict)
            and checkpoint.get("prompt_fingerprint") == package.fingerprint
            and checkpoint.get("contract_fingerprint")
            == package.response_contract.fingerprint
            and checkpoint.get("context_shot_ids")
            == [str(shot["shot_id"]) for shot in window_shots]
            and checkpoint.get("focus_shot_ids") == focus_shot_ids
            and isinstance(checkpoint.get("response"), dict)
        ):
            try:
                structured = package.response_contract.validate_structure(
                    checkpoint["response"]
                )
                decisions = _validate_window_decisions(
                    structured,
                    focus_shot_ids,
                )
            except ValueError:
                decisions = None
        if decisions is None:
            raise_if_cancelled(cancellation_token)
            image_data_urls = [
                _data_url(_resolve_frame_file(frame_directory, sample["file"]))
                for shot in window_shots
                for sample in frames[str(shot["shot_id"])]
            ]
            image_labels = [
                (
                    f"{shot['shot_id']} frame {frame_index}/"
                    f"{scene_config.frames_per_shot} at "
                    f"{float(sample['time_sec']):.3f}s"
                )
                for shot in window_shots
                for frame_index, sample in enumerate(
                    frames[str(shot["shot_id"])],
                    1,
                )
            ]

            def validate(parsed: dict[str, Any]) -> list[dict[str, Any]]:
                return _validate_window_decisions(parsed, focus_shot_ids)

            decisions = context.call_prompt(
                package=package,
                config=vlm_config,
                validate_business=validate,
                image_data_urls=image_data_urls,
                image_labels=image_labels,
            )
            write_json_checkpoint(
                checkpoint_path,
                {
                    "schema_version": "1.0",
                    "window_id": window.window_id,
                    "prompt_id": package.prompt_id,
                    "prompt_version": package.prompt_version,
                    "prompt_fingerprint": package.fingerprint,
                    "contract_fingerprint": package.response_contract.fingerprint,
                    "context_shot_ids": [str(shot["shot_id"]) for shot in window_shots],
                    "focus_shot_ids": focus_shot_ids,
                    "response": {"decisions": decisions},
                },
            )
            raise_if_cancelled(cancellation_token)
        else:
            raise_if_cancelled(cancellation_token)
        progress.update()
        return decisions

    worker_count = max(1, min(vlm_config.max_concurrency, len(windows)))
    log_event(
        "INFO",
        "analyser",
        "stage.progress",
        "Scene-VLM window concurrency configured",
        windows=len(windows),
        workers=worker_count,
        context_shots=scene_config.context_shots,
        focus_shots=scene_config.focus_shots,
        frames_per_shot=scene_config.frames_per_shot,
    )
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="scene-vlm",
        ) as executor:
            decisions = [
                decision
                for window_decisions in executor.map(process, windows)
                for decision in window_decisions
            ]
    finally:
        progress.close()
    expected_ids = [str(shot["shot_id"]) for shot in shots[:-1]]
    raise_if_cancelled(cancellation_token)
    if [decision["shot_id"] for decision in decisions] != expected_ids:
        raise ValueError(
            "Aggregated Scene decisions do not cover every Shot exactly once"
        )
    return decisions


def build_segments_from_scene_boundaries(
    shots: list[dict[str, Any]],
    dialogue: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create a full Shot partition from validated Scene-end decisions."""
    expected_decision_ids = [str(shot["shot_id"]) for shot in shots[:-1]]
    if [str(item["shot_id"]) for item in decisions] != expected_decision_ids:
        raise ValueError(
            "Scene decisions must cover all non-final Shots in source order"
        )
    scene_end_ids = {
        str(item["shot_id"]) for item in decisions if bool(item["is_scene_end"])
    }
    dialogue_by_shot: dict[str, list[dict[str, Any]]] = {
        str(shot["shot_id"]): [] for shot in shots
    }
    for line in dialogue:
        for shot_id in line.get("covering_shot_ids") or []:
            dialogue_by_shot[str(shot_id)].append(line)

    shot_ranges: list[tuple[int, int]] = []
    first = 0
    for index, shot in enumerate(shots):
        if str(shot["shot_id"]) in scene_end_ids or index == len(shots) - 1:
            shot_ranges.append((first, index))
            first = index + 1

    segments: list[dict[str, Any]] = []
    for segment_index, (first_index, last_index) in enumerate(shot_ranges, 1):
        segment_shots: list[dict[str, Any]] = []
        dialogue_items: dict[int, dict[str, Any]] = {}
        for shot in shots[first_index : last_index + 1]:
            occurrences = []
            for line in dialogue_by_shot[str(shot["shot_id"])]:
                occurrence = {
                    "dialogue_id": int(line["dialogue_id"]),
                    "time_range": {
                        "start_sec": float(line["start_sec"]),
                        "end_sec": float(line["end_sec"]),
                    },
                    "speaker": str(line["speaker"]),
                    "text": str(line["text"]),
                }
                occurrences.append(occurrence)
                dialogue_items.setdefault(int(line["dialogue_id"]), occurrence)
            segment_shots.append({**shot, "dialogue": occurrences})
        ordered_dialogue = [dialogue_items[key] for key in sorted(dialogue_items)]
        speakers = {str(item["speaker"]) for item in ordered_dialogue}
        speech_mode = (
            SpeechMode.NONE
            if not ordered_dialogue
            else SpeechMode.MONOLOGUE
            if len(speakers) == 1
            else SpeechMode.DIALOGUE
        )
        segments.append(
            {
                "segment_id": f"segment_{segment_index:04d}",
                "time_range": {
                    "start_sec": float(segment_shots[0]["time_range"]["start_sec"]),
                    "end_sec": float(segment_shots[-1]["time_range"]["end_sec"]),
                },
                "has_dialogue": bool(ordered_dialogue),
                "speech_mode": speech_mode,
                # Segment summarization assigns the semantic role. BODY is only
                # a neutral typed placeholder before that model stage runs.
                "timeline_role": TimelineRole.BODY,
                "dialogue_items": ordered_dialogue,
                "shots": segment_shots,
            }
        )
    return segments
