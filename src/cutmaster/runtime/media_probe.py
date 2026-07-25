"""Read-only media inspection shared by workflow stages."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class MediaToolError(RuntimeError):
    pass


def _frame_rate(value: Any) -> float:
    text = str(value or "0")
    if "/" not in text:
        return float(text)
    numerator, denominator = text.split("/", 1)
    divisor = float(denominator)
    return float(numerator) / divisor if divisor else 0.0


def check_media_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise MediaToolError(f"Missing required media tools: {', '.join(missing)}")


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        {},
    )
    duration = float(
        (data.get("format") or {}).get("duration") or video.get("duration") or 0.0
    )
    return {
        "duration": duration,
        "fps": _frame_rate(
            video.get("avg_frame_rate") or video.get("r_frame_rate")
        ),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(
            stream.get("codec_type") == "audio" for stream in streams
        ),
    }


def media_duration(path: Path) -> float:
    return float(probe_media(path)["duration"])
