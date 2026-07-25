from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from cutmaster.models import RenderConfig
from cutmaster.observability import log_event
from cutmaster.progress import progress_bar
from cutmaster.timecode import parse_range


class RenderError(RuntimeError):
    pass


def _run(command: list[str]) -> None:
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
        raise RenderError(f"FFmpeg command failed with exit code {exc.returncode}") from exc


def check_media_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RenderError(f"Missing required media tools: {', '.join(missing)}")


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0.0)
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def media_duration(path: Path) -> float:
    return float(probe_media(path)["duration"])


def _available_encoders() -> str:
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
    encoders = _available_encoders()
    if platform.system() == "Darwin" and "h264_videotoolbox" in encoders:
        return "h264_videotoolbox"
    if "h264_nvenc" in encoders:
        return "h264_nvenc"
    return "libx264"


def _encoder_args(encoder: str, threads: int) -> list[str]:
    if encoder == "h264_videotoolbox":
        return ["-c:v", encoder, "-q:v", "65"]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "fast", "-cq", "23"]
    return ["-c:v", encoder, "-preset", "veryfast", "-crf", "23", "-threads", str(threads)]


def _video_filter(config: RenderConfig) -> str:
    return (
        f"scale={config.width}:{config.height}:force_original_aspect_ratio=decrease,"
        f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={config.fps}"
    )


def render_clip(
    source: Path,
    output: Path,
    start: float,
    frame_count: int,
    config: RenderConfig,
    encoder: str,
) -> None:
    if frame_count <= 0:
        raise RenderError("Rendered clip must contain at least one frame")
    duration = frame_count / config.fps
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
    ]
    command.extend(["-map", "0:v:0", "-an"])
    command.extend([
        "-vf",
        f"{_video_filter(config)},trim=end_frame={frame_count},setpts=N/({config.fps}*TB)",
        "-frames:v",
        str(frame_count),
    ])
    command.extend(_encoder_args(encoder, config.threads))
    command.extend([
        "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(output),
    ])
    _run(command)


def concatenate_clips(clips: list[Path], output: Path) -> None:
    if not clips:
        raise RenderError("No clips to concatenate")
    concat_path = output.with_suffix(".concat.txt")
    lines = []
    for clip in clips:
        escaped = str(clip.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-c", "copy", "-movflags", "+faststart", str(output),
    ])


def build_final_audio_filter(config: RenderConfig, duration: float) -> str:
    fade_duration = min(3.0, max(0.1, duration))
    fade_start = max(0.0, duration - fade_duration)
    bgm_filter = (
        f"[1:a]volume={config.bgm_volume},atrim=0:{duration:.3f},"
        f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f},asetpts=PTS-STARTPTS"
    )
    if config.original_volume <= 0:
        return f"{bgm_filter}[aout]"
    return (
        f"[0:a]volume={config.original_volume},atrim=0:{duration:.3f},asetpts=PTS-STARTPTS[a0];"
        f"{bgm_filter}[a1];"
        f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        f"atrim=0:{duration:.3f}[aout]"
    )


def mix_bgm(
    montage: Path,
    bgm: Path,
    output: Path,
    config: RenderConfig,
    duration: float | None = None,
) -> None:
    duration = float(duration if duration is not None else media_duration(montage))
    audio_filter = build_final_audio_filter(config, duration)
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(montage), "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex", audio_filter,
        "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(config.audio_sample_rate),
        "-ac", "2", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(output),
    ])


def render_montage(
    video_path: Path,
    audio_path: Path,
    script: list[dict[str, Any]],
    output_dir: Path,
    config: RenderConfig,
) -> tuple[Path, Path]:
    check_media_tools()
    if config.original_volume > 0:
        raise RenderError("Frame-exact rendering requires muted source audio; set original_volume = 0")
    source_meta = probe_media(video_path)
    encoder = select_encoder(config.encoder)
    log_event(
        "INFO",
        "renderer",
        "stage.progress",
        "Clip rendering configured",
        clips=len(script),
        encoder=encoder,
        fps=config.fps,
        width=config.width,
        height=config.height,
    )
    clips_dir = output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_paths: list[Path] = []
    with progress_bar(
        enumerate(script, start=1),
        total=len(script),
        description="Final clip rendering",
        unit="clip",
    ) as progress:
        for index, item in progress:
            start, _ = parse_range(str(item["timestamp"]))
            output_frames = item.get("output_frame_range")
            if not isinstance(output_frames, list) or len(output_frames) != 2:
                raise RenderError(f"Clip {index} is missing output_frame_range")
            output_start_frame, output_end_frame = map(int, output_frames)
            frame_count = output_end_frame - output_start_frame
            end = start + frame_count / config.fps
            if end > source_meta["duration"] + 0.25:
                raise RenderError(
                    f"Clip {index} ends at {end:.3f}s beyond source duration "
                    f"{source_meta['duration']:.3f}s"
                )
            clip_path = clips_dir / f"clip_{index:04d}.mp4"
            log_event(
                "DEBUG",
                "renderer",
                "stage.progress",
                "Rendering source clip",
                clip=index,
                clips=len(script),
                source_start_sec=start,
                source_end_sec=end,
                frames=frame_count,
            )
            render_clip(
                video_path,
                clip_path,
                start,
                frame_count,
                config,
                encoder,
            )
            clip_paths.append(clip_path)

    montage_path = output_dir / "montage.mp4"
    concatenate_clips(clip_paths, montage_path)
    output_path = output_dir / "output.mp4"
    expected_frames = sum(
        int(item["output_frame_range"][1]) - int(item["output_frame_range"][0])
        for item in script
    )
    expected_duration = expected_frames / config.fps
    mix_bgm(montage_path, audio_path, output_path, config, expected_duration)
    return montage_path, output_path
