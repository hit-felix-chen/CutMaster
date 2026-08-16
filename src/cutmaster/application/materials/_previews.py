"""Bounded, path-free Material card preview projections."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cutmaster.domain.materials import MaterialType

MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
MAX_MUSIC_MEMORY_BYTES = 4 * 1024 * 1024
MAX_ENERGY_POINTS = 200_000
MAX_WAVEFORM_POINTS = 320

_FRAME_NAME = re.compile(r"^shot_([0-9]{5})_(01|02|03)\.jpg$")
_FRAME_PRIORITY = {"02": 0, "01": 1, "03": 2}


def preview_available(
    memory_root: Path,
    material_type: MaterialType,
    memory_document: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a real bounded preview can be projected cheaply."""

    if material_type is MaterialType.VIDEO:
        return _select_video_frame(memory_root) is not None
    memory = (
        memory_document
        if memory_document is not None
        else _read_music_memory(memory_root)
    )
    return _energy_points(memory) is not None


def build_preview(
    memory_root: Path,
    material_type: MaterialType,
) -> tuple[str, bytes] | None:
    """Build one small image without opening the immutable source media."""

    if material_type is MaterialType.VIDEO:
        frame = _select_video_frame(memory_root)
        if frame is None:
            return None
        content = _read_bounded_regular_file(
            frame,
            expected_parent=frame.parent,
            maximum=MAX_THUMBNAIL_BYTES,
        )
        if (
            content is None
            or not content.startswith(b"\xff\xd8\xff")
            or not content.endswith(b"\xff\xd9")
        ):
            return None
        return "image/jpeg", content

    memory = _read_music_memory(memory_root)
    points = _energy_points(memory)
    if points is None:
        return None
    return "image/svg+xml", _waveform_svg(points)


def _safe_directory(path: Path, *, expected_parent: Path | None = None) -> Path | None:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return None
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        return None
    try:
        resolved = path.resolve(strict=True)
        if expected_parent is not None:
            parent = expected_parent.resolve(strict=True)
            if resolved.parent != parent:
                return None
    except (FileNotFoundError, OSError, RuntimeError):
        return None
    return resolved


def _select_video_frame(memory_root: Path) -> Path | None:
    root = _safe_directory(memory_root)
    if root is None:
        return None
    frame_directory = _safe_directory(
        root / "scene_frames",
        expected_parent=root,
    )
    if frame_directory is None:
        return None

    candidates: list[tuple[int, int, Path]] = []
    try:
        for entry in frame_directory.iterdir():
            match = _FRAME_NAME.fullmatch(entry.name)
            if match is None:
                continue
            candidates.append(
                (
                    int(match.group(1)),
                    _FRAME_PRIORITY[match.group(2)],
                    entry,
                )
            )
    except OSError:
        return None

    for _shot_number, _priority, candidate in sorted(candidates):
        if _regular_file_within(
            candidate,
            expected_parent=frame_directory,
            maximum=MAX_THUMBNAIL_BYTES,
        ):
            return candidate
    return None


def _regular_file_within(
    path: Path,
    *,
    expected_parent: Path,
    maximum: int,
) -> bool:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return False
    return bool(
        not path.is_symlink()
        and stat.S_ISREG(metadata.st_mode)
        and 0 < metadata.st_size <= maximum
        and resolved.parent == expected_parent
    )


def _read_bounded_regular_file(
    path: Path,
    *,
    expected_parent: Path,
    maximum: int,
) -> bytes | None:
    if not _regular_file_within(
        path,
        expected_parent=expected_parent,
        maximum=maximum,
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(maximum + 1)
        if len(content) != metadata.st_size or len(content) > maximum:
            return None
        return content
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _read_music_memory(memory_root: Path) -> dict[str, Any] | None:
    root = _safe_directory(memory_root)
    if root is None:
        return None
    content = _read_bounded_regular_file(
        root / "music_memory.json",
        expected_parent=root,
        maximum=MAX_MUSIC_MEMORY_BYTES,
    )
    if content is None:
        return None
    try:
        value = json.loads(content, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON numeric constant: {value}")


def _energy_points(
    memory: Mapping[str, Any] | None,
) -> list[tuple[float, float]] | None:
    if not isinstance(memory, Mapping):
        return None
    raw_duration = memory.get("source_duration_sec")
    if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
        return None
    duration = float(raw_duration)
    if not math.isfinite(duration) or duration <= 0:
        return None
    curve = memory.get("energy_curve")
    if (
        not isinstance(curve, list)
        or not curve
        or len(curve) > MAX_ENERGY_POINTS
    ):
        return None

    points: list[tuple[float, float]] = []
    previous_time = -1.0
    for item in curve:
        if not isinstance(item, Mapping):
            return None
        raw_time = item.get("time_sec")
        raw_energy = item.get("energy")
        if (
            isinstance(raw_time, bool)
            or not isinstance(raw_time, (int, float))
            or isinstance(raw_energy, bool)
            or not isinstance(raw_energy, (int, float))
        ):
            return None
        time_sec = float(raw_time)
        energy = float(raw_energy)
        if (
            not math.isfinite(time_sec)
            or not math.isfinite(energy)
            or time_sec < previous_time
            or time_sec < 0
        ):
            return None
        points.append(
            (
                min(1.0, time_sec / duration),
                max(0.0, min(1.0, energy)),
            )
        )
        previous_time = time_sec
    return _downsample(points, MAX_WAVEFORM_POINTS)


def _downsample(
    points: list[tuple[float, float]],
    maximum: int,
) -> list[tuple[float, float]]:
    if len(points) <= maximum:
        return points
    selected: list[tuple[float, float]] = []
    for index in range(maximum):
        start = index * len(points) // maximum
        end = max(start + 1, (index + 1) * len(points) // maximum)
        selected.append(max(points[start:end], key=lambda item: item[1]))
    return selected


def _waveform_svg(points: list[tuple[float, float]]) -> bytes:
    width = 640.0
    height = 160.0
    centre = height / 2.0
    amplitude = 62.0
    top = [
        (ratio * width, centre - energy * amplitude) for ratio, energy in points
    ]
    bottom = [
        (ratio * width, centre + energy * amplitude)
        for ratio, energy in reversed(points)
    ]
    polygon = " ".join(f"{x:.2f},{y:.2f}" for x, y in (*top, *bottom))
    document = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 160" '
        'preserveAspectRatio="none">'
        '<path d="M0 80H640" stroke="#9388d8" stroke-opacity=".24"/>'
        f'<polygon points="{polygon}" fill="#7768d8" fill-opacity=".88"/>'
        "</svg>"
    )
    return document.encode("ascii")


__all__ = [
    "MAX_MUSIC_MEMORY_BYTES",
    "MAX_THUMBNAIL_BYTES",
    "MAX_WAVEFORM_POINTS",
    "build_preview",
    "preview_available",
]
