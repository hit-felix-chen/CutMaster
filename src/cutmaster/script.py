from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cutmaster.timecode import format_range, parse_range


def adapt_script(
    raw_script: list[dict[str, Any]],
    target_output_length_sec: float,
    target_shot_length_sec: float,
    max_clip_duration_sec: float | None = None,
    beat_times: list[float] | None = None,
    source_duration_sec: float | None = None,
    output_fps: int | None = None,
) -> list[dict[str, Any]]:
    planned: list[tuple[dict[str, Any], float, float]] = []
    has_aligned_timeline = all(
        "output_start_sec" in raw and "output_end_sec" in raw for raw in raw_script
    )
    total = 0.0
    default_clip_cap = float(max_clip_duration_sec or target_shot_length_sec)
    for raw in raw_script:
        remaining = target_output_length_sec - total
        if remaining <= 0.001:
            break
        start, end = parse_range(str(raw["timestamp"]))
        planned_duration = float(raw.get("planned_duration_sec") or default_clip_cap)
        if max_clip_duration_sec is not None:
            planned_duration = min(planned_duration, max_clip_duration_sec)
        duration = min(end - start, planned_duration, remaining)
        if duration <= 0.001:
            continue
        planned.append((raw, start, duration))
        total += duration
    if not planned:
        raise ValueError("No usable clips remain after script adaptation")
    if output_fps is not None:
        total = round(total * output_fps) / output_fps

    if has_aligned_timeline and len(planned) == len(raw_script):
        output_boundaries = [float(planned[0][0]["output_start_sec"])]
        output_boundaries.extend(float(raw["output_end_sec"]) for raw, _, _ in planned)
        if output_fps is not None:
            output_boundaries = [
                round(boundary * output_fps) / output_fps for boundary in output_boundaries
            ]
        if abs(output_boundaries[0]) > 1e-6:
            raise ValueError("Aligned output timeline must start at 0")
        if any(
            end <= start
            for start, end in zip(output_boundaries, output_boundaries[1:])
        ):
            raise ValueError("Aligned output timeline must be strictly increasing")
    else:
        ideal_boundaries: list[float] = []
        elapsed = 0.0
        for _, _, duration in planned[:-1]:
            elapsed += duration
            ideal_boundaries.append(elapsed)
        aligned_boundaries = align_cut_boundaries(
            ideal_boundaries,
            beat_times or [],
            total,
            max_clip_duration_sec,
            output_fps,
        )
        output_boundaries = [0.0, *aligned_boundaries, total]

    adapted: list[dict[str, Any]] = []
    for index, ((raw, source_start, _), output_start, output_end) in enumerate(
        zip(planned, output_boundaries[:-1], output_boundaries[1:], strict=True),
        start=1,
    ):
        duration = output_end - output_start
        if source_duration_sec is not None and source_start + duration > source_duration_sec:
            source_start = max(0.0, source_duration_sec - duration)
        item = dict(raw)
        item["_id"] = index
        item["timestamp"] = format_range(source_start, source_start + duration)
        item["output_timestamp"] = format_range(output_start, output_end)
        if output_fps is not None:
            item["output_frame_range"] = [
                round(output_start * output_fps),
                round(output_end * output_fps),
            ]
        item["narration"] = f"播放原片{item['_id']}"
        item["OST"] = 1
        adapted.append(item)
    return adapted


def align_cut_boundaries(
    ideal_boundaries: list[float],
    beat_times: list[float],
    total_duration_sec: float,
    max_clip_duration_sec: float | None = None,
    output_fps: int | None = None,
) -> list[float]:
    def snap_to_frame(value: float) -> float:
        return round(value * output_fps) / output_fps if output_fps else value

    if not ideal_boundaries:
        return []
    if not beat_times:
        return [snap_to_frame(boundary) for boundary in ideal_boundaries]

    beats = sorted(
        {
            snap_to_frame(float(beat))
            for beat in beat_times
            if 0.0 < beat < total_duration_sec
        }
    )
    if not beats:
        return ideal_boundaries

    aligned: list[float] = []
    previous = 0.0
    total_clips = len(ideal_boundaries) + 1
    for index, ideal in enumerate(ideal_boundaries, start=1):
        remaining_clips = total_clips - index
        lower = previous + 0.001
        upper = total_duration_sec - (remaining_clips * 0.001)
        if max_clip_duration_sec is not None:
            lower = max(lower, total_duration_sec - remaining_clips * max_clip_duration_sec)
            upper = min(upper, previous + max_clip_duration_sec)
        candidates = [beat for beat in beats if lower <= beat <= upper]
        if not candidates:
            raise ValueError(f"No audio beat can satisfy cut boundary {index} near {ideal:.3f}s")
        selected = min(candidates, key=lambda beat: (abs(beat - ideal), beat))
        aligned.append(selected)
        previous = selected
    return aligned


def script_duration(items: list[dict[str, Any]]) -> float:
    return sum(parse_range(str(item["timestamp"]))[1] - parse_range(str(item["timestamp"]))[0] for item in items)


def write_script(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
