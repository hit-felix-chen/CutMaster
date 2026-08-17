"""Deterministic, annotation-free JPEG covers derived from video sources."""

from __future__ import annotations

import math
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import cv2

from cutmaster.workflow.ports import CancellationToken, raise_if_cancelled

VIDEO_COVER_FILENAME = "cover.jpg"
VIDEO_COVER_MAX_SIDE = 640
VIDEO_COVER_JPEG_QUALITY = 85
VIDEO_COVER_MAX_BYTES = 2 * 1024 * 1024


def is_video_cover_file(path: Path) -> bool:
    """Return whether *path* is one bounded, non-symlink JPEG file."""

    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 < metadata.st_size <= VIDEO_COVER_MAX_BYTES
    ):
        return False
    try:
        with path.open("rb") as stream:
            head = stream.read(3)
            stream.seek(-2, os.SEEK_END)
            tail = stream.read(2)
    except OSError:
        return False
    return head == b"\xff\xd8\xff" and tail == b"\xff\xd9"


def write_video_cover(
    source_path: Path,
    destination_path: Path,
    candidate_times_sec: Sequence[float],
    *,
    cancellation_token: CancellationToken | None = None,
) -> Path:
    """Decode the first usable candidate time and atomically publish a cover.

    The decoded frame is resized only.  No Shot identifier, timestamp, filename,
    or other annotation is painted into the card image.
    """

    if not candidate_times_sec:
        raise ValueError("Video cover requires at least one candidate time")
    candidates: list[float] = []
    for raw in candidate_times_sec:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError("Video cover candidate times must be numbers")
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Video cover candidate times must be finite and non-negative")
        candidates.append(value)

    raise_if_cancelled(cancellation_token)
    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open source video for cover: {source_path}")
    frame = None
    try:
        for time_sec in candidates:
            raise_if_cancelled(cancellation_token)
            capture.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000.0)
            ok, decoded = capture.read()
            if ok and decoded is not None and getattr(decoded, "size", 0) > 0:
                frame = _resize_cover(decoded)
                break
    finally:
        capture.release()
    if frame is None:
        raise RuntimeError("Could not decode any candidate frame for video cover")

    raise_if_cancelled(cancellation_token)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(
        f".{destination_path.stem}.{uuid4().hex}.tmp.jpg"
    )
    try:
        if not cv2.imwrite(
            str(temporary),
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_COVER_JPEG_QUALITY],
        ):
            raise RuntimeError(f"Could not write video cover: {destination_path}")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination_path)
        _fsync_directory(destination_path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    raise_if_cancelled(cancellation_token)
    return destination_path


def _resize_cover(frame):
    height, width = frame.shape[:2]
    largest = max(height, width)
    if largest <= VIDEO_COVER_MAX_SIDE:
        return frame
    scale = VIDEO_COVER_MAX_SIDE / largest
    return cv2.resize(
        frame,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "VIDEO_COVER_FILENAME",
    "VIDEO_COVER_JPEG_QUALITY",
    "VIDEO_COVER_MAX_BYTES",
    "VIDEO_COVER_MAX_SIDE",
    "is_video_cover_file",
    "write_video_cover",
]
