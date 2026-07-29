from __future__ import annotations

import base64
import math
from typing import Any

import cv2

from cutmaster.planners.tools.segment_media import SegmentMediaReader
from cutmaster.timecode import parse_range

def _sampled_contact_sheet_data_url(
    media: SegmentMediaReader,
    candidate: dict[str, Any],
    sample_times: list[float],
    label: str,
) -> str:
    count = len(sample_times)
    if count < 1:
        raise ValueError("A contact sheet requires at least one sample time")
    frame_width = 480
    frame_height = 270
    frames = [
        cv2.resize(frame, (frame_width, frame_height))
        for frame in media.sample_frames(sample_times)
    ]
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    sheet = __import__("numpy").zeros(
        (rows * frame_height + 34, columns * frame_width, 3), dtype="uint8"
    )
    for index, frame in enumerate(frames):
        row, column = divmod(index, columns)
        sheet[
            34 + row * frame_height : 34 + (row + 1) * frame_height,
            column * frame_width : (column + 1) * frame_width,
        ] = frame
    cv2.putText(
        sheet,
        label,
        (8, 23),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    ok, encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise RuntimeError(f"Could not encode visual sample for {candidate['candidate_id']}")
    return "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")


def _contact_sheet_data_url(
    media: SegmentMediaReader,
    candidate: dict[str, Any],
    sample_frames: int,
) -> str:
    start, end = parse_range(candidate["timestamp"])
    count = max(1, sample_frames)
    sample_times = [
        start + (end - start) * (index + 0.5) / count
        for index in range(count)
    ]
    return _sampled_contact_sheet_data_url(
        media,
        candidate,
        sample_times,
        f"{candidate['candidate_id']}  {candidate['timestamp']}",
    )


def _edge_contact_sheet_data_url(
    media: SegmentMediaReader,
    candidate: dict[str, Any],
    edge: str,
    sample_frames: int,
    window_sec: float = 1.0,
) -> str:
    start, end = parse_range(candidate["timestamp"])
    count = max(2, sample_frames)
    if edge == "head":
        window_start, window_end = start, min(end, start + window_sec)
    elif edge == "tail":
        window_start, window_end = max(start, end - window_sec), end
    else:
        raise ValueError(f"Unsupported candidate edge: {edge}")
    sample_times = [
        window_start + (window_end - window_start) * (index + 0.5) / count
        for index in range(count)
    ]
    return _sampled_contact_sheet_data_url(
        media,
        candidate,
        sample_times,
        f"{candidate['candidate_id']}  {edge.upper()}",
    )

def _normalize_likert_score(score: int | float) -> float:
    return (float(score) - 1.0) / 4.0
