"""Optimize source windows for compiled ASTER script clips."""

from __future__ import annotations

import bisect
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import (
    ShotDetectionConfig,
    SourceWindowOptimizationConfig,
)
from cutmaster.workflow.planners.tools.segment_media import SegmentMediaReader
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.infrastructure.observability.progress import progress_bar
from cutmaster.workflow.shared.shot_detection import detect_source_cuts
from cutmaster.workflow.shared.timecode import format_range, parse_range


_TIMECODE_QUANTIZATION_TOLERANCE_SEC = 0.001001


@dataclass(frozen=True)
class SourceWindowOptimization:
    source_start_sec: float
    source_shift_sec: float
    internal_source_cuts_sec: tuple[float, ...]
    internal_output_cuts_sec: tuple[float, ...]
    max_beat_distance_sec: float
    effective_min_boundary_distance_sec: float
    fallback_level: int


def _trajectory_identity(item: dict[str, Any]) -> tuple[str, str] | None:
    group_id = item.get("group_id")
    trajectory_id = item.get("trajectory_id")
    has_group = isinstance(group_id, str) and bool(group_id.strip())
    has_trajectory = isinstance(trajectory_id, str) and bool(trajectory_id.strip())
    if has_group != has_trajectory:
        raise ValueError(
            "Source-window optimization requires both group_id and trajectory_id"
        )
    if not has_group:
        return None
    return group_id.strip(), trajectory_id.strip()


def _source_window_end_cap(
    item: dict[str, Any],
    video_description: dict[str, Any],
) -> float | None:
    caps: list[float] = []
    planning_end_ms = item.get("planning_segment_end_ms")
    if planning_end_ms is not None:
        if (
            isinstance(planning_end_ms, bool)
            or not isinstance(planning_end_ms, int)
            or planning_end_ms <= 0
        ):
            raise ValueError("planning_segment_end_ms must be a positive integer")
        caps.append(planning_end_ms / 1000.0)

    source_segment_id = item.get("source_segment_id")
    if source_segment_id is None:
        if item.get("source_shot_ids") is not None:
            raise ValueError("source_shot_ids require source_segment_id")
        return min(caps) if caps else None
    if not isinstance(source_segment_id, str) or not source_segment_id.strip():
        raise ValueError("source_segment_id must be a non-empty string")
    source_segment_id = source_segment_id.strip()
    segment = next(
        (
            raw
            for raw in video_description.get("segments", [])
            if str(raw.get("segment_id") or "") == source_segment_id
        ),
        None,
    )
    if segment is None:
        raise ValueError(f"Unknown source Segment: {source_segment_id}")
    caps.append(float(segment["time_range"]["end_sec"]))

    raw_shot_ids = item.get("source_shot_ids")
    if raw_shot_ids is None:
        return min(caps)
    if not isinstance(raw_shot_ids, list) or not raw_shot_ids:
        raise ValueError("source_shot_ids must be a non-empty array")
    source_shot_ids = {
        shot_id.strip()
        for shot_id in raw_shot_ids
        if isinstance(shot_id, str) and shot_id.strip()
    }
    if len(source_shot_ids) != len(raw_shot_ids):
        raise ValueError("source_shot_ids must contain unique non-empty strings")
    shots_by_id = {
        str(shot.get("shot_id") or ""): shot
        for shot in segment.get("shots", [])
    }
    missing = source_shot_ids - set(shots_by_id)
    if missing:
        raise ValueError(
            f"Unknown source Shot(s) for {source_segment_id}: "
            + ", ".join(sorted(missing))
        )
    caps.append(
        max(
            float(shots_by_id[shot_id]["time_range"]["end_sec"])
            for shot_id in source_shot_ids
        )
    )
    return min(caps)


def _merge_intervals(
    intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1e-6:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _detect_used_segment_cuts(
    source_video: Path,
    segment_cache_directory: Path,
    items: list[dict[str, Any]],
    video_description: dict[str, Any],
    detection_config: ShotDetectionConfig,
    optimization_config: SourceWindowOptimizationConfig,
) -> tuple[tuple[float, ...], float, float]:
    source = video_description["source"]
    source_duration_sec = float(source["duration_sec"])
    frame_rate = float(source["fps"])
    if source_duration_sec <= 0.0:
        raise ValueError("Cached video description has an invalid source duration")
    if frame_rate <= 0.0:
        raise ValueError("Cached video description has an invalid source frame rate")

    media = SegmentMediaReader(
        source_video,
        segment_cache_directory,
        video_description,
    )
    requested_by_segment: dict[
        str,
        tuple[dict[str, Any], Path, list[tuple[float, float]]],
    ] = {}
    for item in items:
        if item.get("dialogue_anchor") is not None:
            continue
        source_start, source_end = parse_range(str(item["timestamp"]))
        detection_end = min(
            source_duration_sec,
            source_end + optimization_config.search_margin_sec,
        )
        for segment, clip_path in media.cached_segments_for_range(
            source_start,
            detection_end,
        ):
            segment_id = str(segment["segment_id"])
            segment_start = float(segment["time_range"]["start_sec"])
            segment_end = float(segment["time_range"]["end_sec"])
            local_start = max(source_start, segment_start) - segment_start
            local_end = min(detection_end, segment_end) - segment_start
            entry = requested_by_segment.setdefault(
                segment_id,
                (segment, clip_path, []),
            )
            entry[2].append((local_start, local_end))

    jobs = [
        (segment, clip_path, local_start, local_end)
        for segment, clip_path, intervals in requested_by_segment.values()
        for local_start, local_end in _merge_intervals(intervals)
    ]
    cuts: set[float] = set()

    def detect_job(
        job: tuple[dict[str, Any], Path, float, float],
    ) -> tuple[dict[str, Any], list[float]]:
        segment, clip_path, local_start, local_end = job
        local_cuts, _ = detect_source_cuts(
            clip_path,
            local_start,
            local_end,
            adaptive_threshold=detection_config.adaptive_threshold,
            adaptive_min_content_val=(
                detection_config.adaptive_min_content_val
            ),
            adaptive_min_scene_len_sec=(
                detection_config.adaptive_min_scene_len_sec
            ),
            duplicate_frame_threshold=(
                detection_config.duplicate_frame_threshold
            ),
        )
        return segment, local_cuts

    worker_count = max(
        1,
        min(optimization_config.max_workers, len(jobs) or 1),
    )
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="segment-cut-detector",
    ) as executor:
        for segment, local_cuts in executor.map(detect_job, jobs):
            segment_start = float(segment["time_range"]["start_sec"])
            cuts.update(
                round(segment_start + local_cut, 6)
                for local_cut in local_cuts
                if 0.0 < segment_start + local_cut < source_duration_sec
            )

    used_segments = [
        entry[0] for entry in requested_by_segment.values()
    ]
    cuts.update(
        round(float(segment["time_range"]["end_sec"]), 6)
        for segment in used_segments
        if 0.0
        < float(segment["time_range"]["end_sec"])
        < source_duration_sec
        and any(
            float(other["time_range"]["start_sec"])
            == float(segment["time_range"]["end_sec"])
            for other in used_segments
        )
    )
    log_event(
        "INFO",
        "source_window",
        "stage.complete",
        "Used Segment caches inspected for source-window cut optimization",
        segments=len(requested_by_segment),
        intervals=len(jobs),
        cuts=len(cuts),
    )
    return tuple(sorted(cuts)), frame_rate, source_duration_sec


def _nearest_beat_distance(time_sec: float, beat_times: list[float]) -> float:
    index = bisect.bisect_left(beat_times, time_sec)
    distances: list[float] = []
    if index < len(beat_times):
        distances.append(abs(beat_times[index] - time_sec))
    if index > 0:
        distances.append(abs(beat_times[index - 1] - time_sec))
    return min(distances) if distances else float("inf")


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
    latest_source_start_sec: float | None = None,
) -> SourceWindowOptimization:
    initial_cuts = sorted(internal_source_cuts_sec)
    available_cuts = sorted(
        initial_cuts if candidate_source_cuts_sec is None else candidate_source_cuts_sec
    )
    beats = sorted(float(beat) for beat in beat_times)
    epsilon = 1e-6
    if (
        latest_source_start_sec is not None
        and latest_source_start_sec
        < original_start_sec - _TIMECODE_QUANTIZATION_TOLERANCE_SEC
    ):
        raise ValueError(
            "Existing source window exceeds its chronological or evidence boundary"
        )
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

    lower = max(0.0, original_start_sec)
    upper = min(
        source_duration_sec - clip_duration_sec,
        original_start_sec + search_margin_sec,
    )
    if latest_source_start_sec is not None:
        upper = min(upper, max(original_start_sec, latest_source_start_sec))
    if upper < lower - epsilon:
        raise ValueError("No forward source-window search range is available")
    # Subtracting legal decimal timestamps can put an exact-tail upper bound
    # just below its start (8.132 - 4.333 < 3.799). Preserve the existing window
    # for arithmetic noise only; a real overflow must still fail above.
    upper = max(lower, upper)

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

    def objective(candidate: float) -> float:
        cuts = feasible[candidate]
        output_cuts = [output_start_sec + cut - candidate for cut in cuts]
        return (
            max((_nearest_beat_distance(cut, beats) for cut in output_cuts), default=0.0)
            if beats
            else 0.0
        )

    selected = min(sorted(feasible), key=objective)
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
    item: dict[str, Any],
    beat_times: list[float],
    source_cuts: tuple[float, ...],
    source_duration_sec: float,
    frame_rate: float,
    output_fps: int,
    optimization_config: SourceWindowOptimizationConfig,
    visual_sample_frames: int,
    next_source_start_sec: float | None,
    source_window_end_sec: float | None,
) -> dict[str, Any]:
    if item.get("dialogue_anchor") is not None:
        result = dict(item)
        result["cut_optimization"] = {
            "mode": "dialogue_anchor_locked",
            "source_shift_sec": 0.0,
            "num_internal_cuts": 0,
            "fallback_level": 0,
            "max_beat_distance_sec": 0.0,
        }
        return result
    source_start, source_end = parse_range(str(item["timestamp"]))
    output_frames = item.get("output_frame_range")
    if not isinstance(output_frames, list) or len(output_frames) != 2:
        raise ValueError("Adapted script item is missing output_frame_range")
    output_start = int(output_frames[0]) / output_fps
    render_duration = (int(output_frames[1]) - int(output_frames[0])) / output_fps
    trajectory_identity = _trajectory_identity(item)
    clip_duration = (
        source_end - source_start
        if trajectory_identity is not None
        else render_duration
    )
    latest_source_starts = [
        boundary - clip_duration
        for boundary in (next_source_start_sec, source_window_end_sec)
        if boundary is not None
    ]
    visual_guard_start_sec: float | None = None
    semantic_shift_cap_sec: float | None = None
    if trajectory_identity is not None:
        sample_count = max(1, visual_sample_frames)
        visual_guard_start_sec = source_start + clip_duration / (2 * sample_count)
        semantic_latest_start = max(
            source_start,
            visual_guard_start_sec - 1.0 / frame_rate,
        )
        semantic_shift_cap_sec = semantic_latest_start - source_start
        latest_source_starts.append(semantic_latest_start)
    latest_source_start_sec = (
        min(latest_source_starts) if latest_source_starts else None
    )
    detection_start = source_start
    detection_end = min(
        source_duration_sec,
        source_end + optimization_config.search_margin_sec,
    )
    first_cut = bisect.bisect_right(source_cuts, detection_start)
    last_cut = bisect.bisect_left(source_cuts, detection_end)
    candidate_cuts = list(source_cuts[first_cut:last_cut])
    internal_cuts = [
        cut for cut in candidate_cuts if source_start < cut < source_end
    ]
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
        candidate_source_cuts_sec=candidate_cuts,
        beat_times=beat_times,
        source_duration_sec=source_duration_sec,
        frame_rate=frame_rate,
        search_margin_sec=optimization_config.search_margin_sec,
        min_boundary_distance_sec=(
            optimization_config.min_boundary_distance_sec
        ),
        latest_source_start_sec=latest_source_start_sec,
    )

    result = dict(item)
    result["timestamp"] = format_range(
        optimized.source_start_sec,
        optimized.source_start_sec + clip_duration,
    )
    cut_optimization = {
        "mode": "beat_optimized",
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
    if visual_guard_start_sec is not None and semantic_shift_cap_sec is not None:
        cut_optimization.update(
            {
                "visual_sample_frames": max(1, visual_sample_frames),
                "visual_guard_start_sec": round(visual_guard_start_sec, 6),
                "semantic_shift_cap_sec": round(semantic_shift_cap_sec, 6),
            }
        )
    result["cut_optimization"] = cut_optimization
    return result


def optimize_script_source_windows(
    source_video: Path,
    segment_cache_directory: Path,
    items: list[dict[str, Any]],
    beat_times: list[float],
    video_description: dict[str, Any],
    *,
    output_fps: int,
    detection_config: ShotDetectionConfig,
    optimization_config: SourceWindowOptimizationConfig,
    visual_sample_frames: int = 4,
) -> list[dict[str, Any]]:
    if not items:
        return []

    for item in items:
        _trajectory_identity(item)
    next_source_starts = [
        parse_range(str(items[index + 1]["timestamp"]))[0]
        if index + 1 < len(items)
        else None
        for index in range(len(items))
    ]
    source_window_ends = [
        None
        if item.get("dialogue_anchor") is not None
        else _source_window_end_cap(item, video_description)
        for item in items
    ]

    source_cuts, frame_rate, source_duration_sec = _detect_used_segment_cuts(
        source_video,
        segment_cache_directory,
        items,
        video_description,
        detection_config,
        optimization_config,
    )
    optimized: list[dict[str, Any] | None] = [None] * len(items)
    worker_count = max(
        1,
        min(optimization_config.max_workers, len(items)),
    )
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="cut-optimizer") as executor:
        futures = {
            executor.submit(
                _optimize_item,
                item,
                beat_times,
                source_cuts,
                source_duration_sec,
                frame_rate,
                output_fps,
                optimization_config,
                max(1, visual_sample_frames),
                next_source_starts[index],
                source_window_ends[index],
            ): index
            for index, item in enumerate(items)
        }
        with progress_bar(
            total=len(futures),
            description="Source-window optimization",
            unit="clip",
        ) as progress:
            for future in as_completed(futures):
                index = futures[future]
                optimized[index] = future.result()
                progress.update()
                metadata = optimized[index]["cut_optimization"]
                if metadata.get("fallback_level", 0) > 0:
                    log_event(
                        "WARNING",
                        "source_window",
                        "fallback.apply",
                        "Internal-cut edge distance was relaxed",
                        clip=index + 1,
                        clips=len(items),
                        original_distance_sec=optimization_config.min_boundary_distance_sec,
                        effective_distance_sec=metadata[
                            "effective_min_boundary_distance_sec"
                        ],
                        fallback_level=metadata["fallback_level"],
                    )
                log_event(
                    "DEBUG",
                    "source_window",
                    "stage.progress",
                    "Source window optimized",
                    clip=index + 1,
                    clips=len(items),
                    source_shift_sec=metadata.get("source_shift_sec", 0.0),
                    internal_cuts=metadata.get("num_internal_cuts", 0),
                    max_beat_distance_sec=metadata.get(
                        "max_beat_distance_sec",
                        0.0,
                    ),
                )
    return [item for item in optimized if item is not None]
