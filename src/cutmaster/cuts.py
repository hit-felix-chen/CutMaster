from __future__ import annotations

import bisect
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from loguru import logger
from scenedetect import open_video
from scenedetect.detectors import AdaptiveDetector

from cutmaster.models import ShotDetectionConfig, SourceWindowOptimizationConfig
from cutmaster.timecode import format_range, parse_range


ADAPTIVE_THRESHOLD = 2.0
ADAPTIVE_MIN_CONTENT_VAL = 15.0
ADAPTIVE_MIN_SCENE_LEN_SEC = 0.25
DUPLICATE_FRAME_THRESHOLD = 1.0


@dataclass(frozen=True)
class SourceWindowOptimization:
    source_start_sec: float
    source_shift_sec: float
    internal_source_cuts_sec: tuple[float, ...]
    internal_output_cuts_sec: tuple[float, ...]
    max_beat_distance_sec: float
    effective_min_boundary_distance_sec: float
    fallback_level: int


def _nearest_beat_distance(time_sec: float, beat_times: list[float]) -> float:
    index = bisect.bisect_left(beat_times, time_sec)
    distances: list[float] = []
    if index < len(beat_times):
        distances.append(abs(beat_times[index] - time_sec))
    if index > 0:
        distances.append(abs(beat_times[index - 1] - time_sec))
    return min(distances) if distances else float("inf")


def _frame_signature(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)


def _mean_frame_difference(current: np.ndarray, previous: np.ndarray) -> float:
    return float(np.mean(cv2.absdiff(current, previous)))


def detect_source_cuts(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    *,
    adaptive_threshold: float = ADAPTIVE_THRESHOLD,
    adaptive_min_content_val: float = ADAPTIVE_MIN_CONTENT_VAL,
    adaptive_min_scene_len_sec: float = ADAPTIVE_MIN_SCENE_LEN_SEC,
    duplicate_frame_threshold: float = DUPLICATE_FRAME_THRESHOLD,
) -> tuple[list[float], float]:
    if end_sec <= start_sec:
        return [], 30.0

    video = open_video(str(video_path))
    try:
        frame_rate = float(video.frame_rate)
        video.seek(start_sec)
        detector = AdaptiveDetector(
            adaptive_threshold=adaptive_threshold,
            min_content_val=adaptive_min_content_val,
            min_scene_len=f"{adaptive_min_scene_len_sec}s",
        )
        cuts: list[float] = []
        previous_signature: np.ndarray | None = None
        while True:
            frame = video.read()
            if frame is False:
                break
            position = video.position
            if float(position.seconds) >= end_sec:
                break

            signature = _frame_signature(frame)
            if (
                previous_signature is not None
                and _mean_frame_difference(signature, previous_signature)
                < duplicate_frame_threshold
            ):
                continue

            cuts.extend(float(cut.seconds) for cut in detector.process_frame(position, frame))
            previous_signature = signature
    finally:
        video.capture.release()
    return cuts, frame_rate


def choose_source_window(
    *,
    original_start_sec: float,
    clip_duration_sec: float,
    output_start_sec: float,
    internal_source_cuts_sec: list[float],
    candidate_source_cuts_sec: list[float] | None = None,
    beat_times: list[float],
    source_duration_sec: float,
    frame_rate: float,
    search_margin_sec: float = 2.0,
    min_boundary_distance_sec: float = 1.0,
) -> SourceWindowOptimization:
    initial_cuts = sorted(internal_source_cuts_sec)
    available_cuts = sorted(
        initial_cuts if candidate_source_cuts_sec is None else candidate_source_cuts_sec
    )
    beats = sorted(float(beat) for beat in beat_times)
    if not available_cuts:
        output_cuts = tuple(output_start_sec + cut - original_start_sec for cut in initial_cuts)
        max_distance = max((_nearest_beat_distance(cut, beats) for cut in output_cuts), default=0.0)
        return SourceWindowOptimization(
            source_start_sec=original_start_sec,
            source_shift_sec=0.0,
            internal_source_cuts_sec=tuple(initial_cuts),
            internal_output_cuts_sec=output_cuts,
            max_beat_distance_sec=max_distance,
            effective_min_boundary_distance_sec=min_boundary_distance_sec,
            fallback_level=0,
        )

    epsilon = 1e-6
    lower = max(0.0, original_start_sec)
    upper = min(
        source_duration_sec - clip_duration_sec,
        original_start_sec + search_margin_sec,
    )
    if upper < lower:
        raise ValueError("No forward source-window search range is available")

    first_frame = round(lower * frame_rate)
    last_frame = round(upper * frame_rate)
    candidates = {
        round(original_start_sec, 3),
        round(lower, 3),
        round(upper, 3),
        *(round(frame / frame_rate, 3) for frame in range(first_frame, last_frame + 1)),
    }
    candidates = {candidate for candidate in candidates if lower - epsilon <= candidate <= upper + epsilon}
    if not candidates:
        candidates = {min(max(original_start_sec, lower), upper)}

    def cuts_for_candidate(candidate: float, boundary_distance: float) -> list[float] | None:
        window_end = candidate + clip_duration_sec
        cuts = [cut for cut in available_cuts if candidate < cut < window_end]
        if any(
            cut - candidate <= boundary_distance + epsilon
            or window_end - cut <= boundary_distance + epsilon
            for cut in cuts
        ):
            return None
        return cuts

    fallback_distances = list(
        dict.fromkeys(
            max(0.0, min(min_boundary_distance_sec, distance))
            for distance in (min_boundary_distance_sec, 0.75, 0.5, 0.25, 0.0)
        )
    )
    feasible: dict[float, list[float]] = {}
    fallback_level = 0
    effective_boundary_distance = min_boundary_distance_sec
    for fallback_level, effective_boundary_distance in enumerate(fallback_distances):
        feasible = {
            candidate: cuts
            for candidate in candidates
            if (cuts := cuts_for_candidate(candidate, effective_boundary_distance)) is not None
        }
        if feasible:
            break
    if not feasible:
        raise ValueError("No frame-level forward source window is available")

    # Keep at least one detected cut when possible. A cutless window is only a
    # fallback when every cut-bearing candidate violates the edge constraint.
    if initial_cuts:
        cut_bearing = {candidate: cuts for candidate, cuts in feasible.items() if cuts}
        if cut_bearing:
            feasible = cut_bearing

    def objective(candidate: float) -> tuple[float, float, int, float]:
        cuts = feasible[candidate]
        output_cuts = [output_start_sec + cut - candidate for cut in cuts]
        worst_distance = (
            max((_nearest_beat_distance(cut, beats) for cut in output_cuts), default=0.0)
            if beats
            else 0.0
        )
        return worst_distance, candidate - original_start_sec, len(cuts), candidate

    selected = min(feasible, key=objective)
    selected_cuts = feasible[selected]
    selected_output_cuts = tuple(output_start_sec + cut - selected for cut in selected_cuts)
    return SourceWindowOptimization(
        source_start_sec=selected,
        source_shift_sec=selected - original_start_sec,
        internal_source_cuts_sec=tuple(selected_cuts),
        internal_output_cuts_sec=selected_output_cuts,
        max_beat_distance_sec=(
            max(
                (_nearest_beat_distance(cut, beats) for cut in selected_output_cuts),
                default=0.0,
            )
            if beats
            else 0.0
        ),
        effective_min_boundary_distance_sec=effective_boundary_distance,
        fallback_level=fallback_level,
    )


def _optimize_item(
    video_path: Path,
    item: dict[str, Any],
    beat_times: list[float],
    source_duration_sec: float,
    output_fps: int,
    detection_config: ShotDetectionConfig,
    optimization_config: SourceWindowOptimizationConfig,
) -> dict[str, Any]:
    source_start, source_end = parse_range(str(item["timestamp"]))
    output_frames = item.get("output_frame_range")
    if not isinstance(output_frames, list) or len(output_frames) != 2:
        raise ValueError("Adapted script item is missing output_frame_range")
    output_start = int(output_frames[0]) / output_fps
    clip_duration = (int(output_frames[1]) - int(output_frames[0])) / output_fps
    detection_start = source_start
    detection_end = min(
        source_duration_sec,
        source_end + optimization_config.search_margin_sec,
    )
    source_cuts, frame_rate = detect_source_cuts(
        video_path,
        detection_start,
        detection_end,
        adaptive_threshold=detection_config.adaptive_threshold,
        adaptive_min_content_val=detection_config.adaptive_min_content_val,
        adaptive_min_scene_len_sec=detection_config.adaptive_min_scene_len_sec,
        duplicate_frame_threshold=detection_config.duplicate_frame_threshold,
    )
    internal_cuts = [cut for cut in source_cuts if source_start < cut < source_end]
    initial_output_cuts = [output_start + cut - source_start for cut in internal_cuts]
    initial_max_distance = max(
        (_nearest_beat_distance(cut, sorted(beat_times)) for cut in initial_output_cuts),
        default=0.0,
    )
    optimized = choose_source_window(
        original_start_sec=source_start,
        clip_duration_sec=clip_duration,
        output_start_sec=output_start,
        internal_source_cuts_sec=internal_cuts,
        candidate_source_cuts_sec=source_cuts,
        beat_times=beat_times,
        source_duration_sec=source_duration_sec,
        frame_rate=frame_rate,
        search_margin_sec=optimization_config.search_margin_sec,
        min_boundary_distance_sec=(
            optimization_config.min_boundary_distance_sec
        ),
    )

    result = dict(item)
    result["timestamp"] = format_range(
        optimized.source_start_sec,
        optimized.source_start_sec + clip_duration,
    )
    result["cut_optimization"] = {
        "search_direction": "forward",
        "search_margin_sec": optimization_config.search_margin_sec,
        "source_shift_sec": round(optimized.source_shift_sec, 6),
        "num_internal_cuts": len(optimized.internal_source_cuts_sec),
        "initial_max_beat_distance_sec": round(initial_max_distance, 6),
        "min_boundary_distance_sec": (
            optimization_config.min_boundary_distance_sec
        ),
        "effective_min_boundary_distance_sec": round(
            optimized.effective_min_boundary_distance_sec,
            6,
        ),
        "fallback_level": optimized.fallback_level,
        "internal_source_cuts_sec": [
            round(cut, 6) for cut in optimized.internal_source_cuts_sec
        ],
        "internal_output_cuts_sec": [
            round(cut, 6) for cut in optimized.internal_output_cuts_sec
        ],
        "max_beat_distance_sec": round(optimized.max_beat_distance_sec, 6),
    }
    return result


def optimize_script_source_windows(
    video_path: Path,
    items: list[dict[str, Any]],
    beat_times: list[float],
    source_duration_sec: float,
    *,
    output_fps: int,
    detection_config: ShotDetectionConfig,
    optimization_config: SourceWindowOptimizationConfig,
) -> list[dict[str, Any]]:
    if not items:
        return []

    optimized: list[dict[str, Any] | None] = [None] * len(items)
    worker_count = max(
        1,
        min(optimization_config.max_workers, len(items)),
    )
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="cut-optimizer") as executor:
        futures = {
            executor.submit(
                _optimize_item,
                video_path,
                item,
                beat_times,
                source_duration_sec,
                output_fps,
                detection_config,
                optimization_config,
            ): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            optimized[index] = future.result()
            metadata = optimized[index]["cut_optimization"]
            if metadata["fallback_level"] > 0:
                logger.warning(
                    "Clip {}/{} relaxed internal-cut edge distance: {:.3f}s -> {:.3f}s (level {})",
                    index + 1,
                    len(items),
                    optimization_config.min_boundary_distance_sec,
                    metadata["effective_min_boundary_distance_sec"],
                    metadata["fallback_level"],
                )
            logger.info(
                "Optimized clip {}/{}: shift={:+.3f}s, internal_cuts={}, worst_distance={:.3f}s",
                index + 1,
                len(items),
                metadata["source_shift_sec"],
                metadata["num_internal_cuts"],
                metadata["max_beat_distance_sec"],
            )
    return [item for item in optimized if item is not None]
