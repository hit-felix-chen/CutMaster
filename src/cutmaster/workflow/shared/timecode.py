from __future__ import annotations

import re


TIME_RANGE_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}:\d{2}(?:[,.]\d{1,3})?)\s*(?:-->|-)\s*"
    r"(\d{1,2}:\d{2}:\d{2}(?:[,.]\d{1,3})?)\s*$"
)


def parse_time(value: str) -> float:
    normalized = str(value).strip().replace(".", ",")
    main, ms_raw = normalized.split(",", 1) if "," in normalized else (normalized, "0")
    parts = main.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid timecode: {value}")
    hours, minutes, seconds = [int(part) for part in parts]
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Invalid timecode: {value}")
    milliseconds = int(ms_raw[:3].ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0


def format_time(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def parse_range(value: str) -> tuple[float, float]:
    match = TIME_RANGE_RE.match(str(value))
    if not match:
        raise ValueError(f"Invalid timestamp range: {value}")
    start, end = parse_time(match.group(1)), parse_time(match.group(2))
    if end <= start:
        raise ValueError(f"Timestamp end must be after start: {value}")
    return start, end


def format_range(start: float, end: float) -> str:
    return f"{format_time(start)}-{format_time(end)}"

