"""FFmpeg command execution and encoder selection."""

from __future__ import annotations

import platform
import subprocess

from cutmaster.infrastructure.observability.logging import log_event


class RenderError(RuntimeError):
    pass


def run_media_command(command: list[str]) -> None:
    log_event(
        "DEBUG",
        "renderer",
        "stage.progress",
        "Media command started",
        executable=command[0],
        arguments=len(command) - 1,
    )
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RenderError(
            f"FFmpeg command failed with exit code {exc.returncode}"
        ) from exc


def available_encoders() -> str:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def select_encoder(requested: str) -> str:
    requested = requested.strip().lower()
    if requested != "auto":
        return requested
    encoders = available_encoders()
    if platform.system() == "Darwin" and "h264_videotoolbox" in encoders:
        return "h264_videotoolbox"
    if "h264_nvenc" in encoders:
        return "h264_nvenc"
    return "libx264"


def encoder_args(encoder: str, threads: int) -> list[str]:
    if encoder == "h264_videotoolbox":
        return ["-c:v", encoder, "-q:v", "65"]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "fast", "-cq", "23"]
    return [
        "-c:v",
        encoder,
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-threads",
        str(threads),
    ]
