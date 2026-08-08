from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import DialogueAudioConfig, RendererConfig
from cutmaster.contracts.planning import RenderPlan
from cutmaster.contracts.renderer import RenderRequest, RenderResult
from cutmaster.renderer.dialogue_audio import prepare_dialogue_audio
from cutmaster.renderer.ffmpeg import (
    RenderError,
    encoder_args,
    run_media_command,
    select_encoder,
)
from cutmaster.runtime.observability import log_event
from cutmaster.runtime.media_probe import (
    check_media_tools,
    media_duration,
    media_frame_count,
    probe_media,
)
from cutmaster.runtime.progress import progress_bar
from cutmaster.timecode import parse_range


def _video_filter(config: RendererConfig) -> str:
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
    config: RendererConfig,
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
    command.extend(encoder_args(encoder, config.threads))
    command.extend([
        "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
        "-movflags", "+faststart", str(output),
    ])
    run_media_command(command)


def concatenate_clips(clips: list[Path], output: Path) -> None:
    if not clips:
        raise RenderError("No clips to concatenate")
    concat_path = output.with_suffix(".concat.txt")
    lines = []
    for clip in clips:
        escaped = str(clip.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_media_command([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-c", "copy", "-movflags", "+faststart", str(output),
    ])


def build_final_audio_filter(
    config: RendererConfig,
    duration: float,
    dialogue_anchors: list[dict[str, Any]] | None = None,
    dialogue_config: DialogueAudioConfig | None = None,
) -> str:
    dialogue_anchors = dialogue_anchors or []
    dialogue_config = dialogue_config or DialogueAudioConfig()
    fade_duration = min(3.0, max(0.1, duration))
    fade_start = max(0.0, duration - fade_duration)
    duck_condition = "+".join(
        "between(t\\,"
        f"{float(anchor['output_audio_start_sec']):.3f}\\,"
        f"{float(anchor['output_audio_end_sec']):.3f})"
        for anchor in dialogue_anchors
    )
    duck_volume = config.bgm_volume * dialogue_config.bgm_duck_factor
    volume = (
        f"volume='if({duck_condition}\\,"
        f"{duck_volume}\\,{config.bgm_volume})':eval=frame,"
        if duck_condition
        else f"volume={config.bgm_volume},"
    )
    bgm_filter = (
        f"[1:a]{volume}atrim=0:{duration:.3f},"
        f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f},asetpts=PTS-STARTPTS"
    )
    if not dialogue_anchors:
        return f"{bgm_filter}[aout]"
    parts = [f"{bgm_filter}[abgm]"]
    labels = ["[abgm]"]
    for index, anchor in enumerate(dialogue_anchors, start=2):
        anchor_duration = (
            float(anchor["source_audio_end_sec"])
            - float(anchor["source_audio_start_sec"])
        )
        fade = min(dialogue_config.fade_sec, anchor_duration / 2.0)
        delay_ms = round(float(anchor["output_audio_start_sec"]) * 1000)
        label = f"a{index}"
        parts.append(
            f"[{index}:a]volume={dialogue_config.dialogue_volume},"
            f"afade=t=in:st=0:d={fade:.3f},"
            f"afade=t=out:st={max(0.0, anchor_duration - fade):.3f}:d={fade:.3f},"
            f"asetpts=PTS-STARTPTS,adelay={delay_ms}:all=1[{label}]"
        )
        labels.append(f"[{label}]")
    parts.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:"
        "dropout_transition=0:normalize=0,"
        f"atrim=0:{duration:.3f}[aout]"
    )
    return ";".join(parts)


def mix_bgm(
    montage: Path,
    bgm: Path,
    output: Path,
    config: RendererConfig,
    duration: float | None = None,
    *,
    source_video: Path | None = None,
    script: list[dict[str, Any]] | None = None,
    dialogue_config: DialogueAudioConfig | None = None,
) -> None:
    duration = float(duration if duration is not None else media_duration(montage))
    dialogue_anchors = [
        dict(item["dialogue_anchor"])
        for item in (script or [])
        if item.get("dialogue_anchor") is not None
    ]
    if (
        any(not anchor.get("prepared_audio_path") for anchor in dialogue_anchors)
        and source_video is None
    ):
        raise RenderError("Dialogue anchors require the source video")
    audio_filter = build_final_audio_filter(
        config,
        duration,
        dialogue_anchors,
        dialogue_config,
    )
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(montage), "-stream_loop", "-1", "-i", str(bgm),
    ]
    for anchor in dialogue_anchors:
        prepared_audio_path = anchor.get("prepared_audio_path")
        if prepared_audio_path:
            command.extend(["-i", str(prepared_audio_path)])
        else:
            anchor_duration = (
                float(anchor["source_audio_end_sec"])
                - float(anchor["source_audio_start_sec"])
            )
            command.extend(
                [
                    "-ss",
                    f"{float(anchor['source_audio_start_sec']):.3f}",
                    "-t",
                    f"{anchor_duration:.3f}",
                    "-i",
                    str(source_video),
                ]
            )
    command.extend([
        "-filter_complex", audio_filter,
        "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(config.audio_sample_rate),
        "-ac", "2", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(output),
    ])
    run_media_command(command)


def render_montage(
    video_path: Path,
    audio_path: Path,
    script: list[dict[str, Any]],
    output_dir: Path,
    config: RendererConfig,
    dialogue_config: DialogueAudioConfig | None = None,
    *,
    include_dialogue_audio: bool = True,
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
            try:
                render_clip(
                    video_path,
                    clip_path,
                    start,
                    frame_count,
                    config,
                    encoder,
                )
            except RenderError:
                if config.encoder != "auto" or encoder == "libx264":
                    raise
                log_event(
                    "WARNING",
                    "renderer",
                    "fallback.apply",
                    "Hardware encoder failed; retrying with libx264",
                    failed_encoder=encoder,
                    fallback_encoder="libx264",
                    clip=index,
                )
                encoder = "libx264"
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
    mix_bgm(
        montage_path,
        audio_path,
        output_path,
        config,
        expected_duration,
        source_video=video_path,
        script=script if include_dialogue_audio else None,
        dialogue_config=dialogue_config,
    )
    return montage_path, output_path


def _montage_signature(plan: RenderPlan, config: RendererConfig) -> dict[str, Any]:
    return {
        "render_schema_version": 2,
        "plan_id": plan.plan_id,
        "width": config.width,
        "height": config.height,
        "fps": config.fps,
        "encoder": config.encoder,
        "threads": config.threads,
    }


class Renderer:
    """Execute an immutable RenderPlan without invoking analysis or models."""

    def __init__(self, config: RendererConfig) -> None:
        self.config = config

    def render(self, request: RenderRequest) -> RenderResult:
        request.validate()
        plan = RenderPlan.read(request.plan_path)
        if plan.fps != self.config.fps:
            raise ValueError(
                f"Render plan fps {plan.fps} does not match renderer fps "
                f"{self.config.fps}"
            )
        output_dir = request.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "output.mp4"
        if output_path.exists() and not request.overwrite:
            raise FileExistsError(
                f"Rendered output already exists; pass --overwrite: {output_path}"
            )
        (output_dir / "render_request.json").write_text(
            json.dumps(request.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        timings: dict[str, float] = {}
        script = [dict(item) for item in plan.clips]
        dialogue_audio_reused = False
        stage_started = time.monotonic()
        if request.audio_mode == "dialogue":
            script, dialogue_audio_reused = prepare_dialogue_audio(
                Path(plan.source_video.path),
                script,
                output_dir,
                self.config.dialogue_audio,
            )
        timings["dialogue_audio_preparation"] = time.monotonic() - stage_started

        montage_path = output_dir / "montage.mp4"
        montage_manifest_path = output_dir / "montage_manifest.json"
        signature = _montage_signature(plan, self.config)
        montage_reused = False
        if montage_path.is_file() and montage_manifest_path.is_file():
            try:
                manifest = json.loads(
                    montage_manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                manifest = None
            montage_reused = manifest == signature

        stage_started = time.monotonic()
        if montage_reused:
            mix_bgm(
                montage_path,
                Path(plan.background_music.path),
                output_path,
                self.config,
                plan.duration_sec,
                source_video=Path(plan.source_video.path),
                script=script if request.audio_mode == "dialogue" else None,
                dialogue_config=self.config.dialogue_audio,
            )
        else:
            montage_path, output_path = render_montage(
                Path(plan.source_video.path),
                Path(plan.background_music.path),
                script,
                output_dir,
                self.config,
                self.config.dialogue_audio,
                include_dialogue_audio=request.audio_mode == "dialogue",
            )
        actual_frames = media_frame_count(output_path)
        if actual_frames != plan.total_frames:
            if montage_manifest_path.exists():
                montage_manifest_path.unlink()
            raise RenderError(
                "Rendered frame count does not match RenderPlan: "
                f"expected {plan.total_frames}, got {actual_frames}"
            )
        if not montage_reused:
            montage_manifest_path.write_text(
                json.dumps(signature, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        timings["rendering"] = time.monotonic() - stage_started
        canonical_request = json.dumps(
            {
                "plan_id": plan.plan_id,
                "audio_mode": request.audio_mode,
                "renderer": asdict(self.config),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        result = RenderResult(
            status="success",
            plan_id=plan.plan_id,
            render_id=hashlib.sha256(
                canonical_request.encode("utf-8")
            ).hexdigest()[:16],
            audio_mode=request.audio_mode,
            output_video=str(output_path.resolve()),
            montage_video=str(montage_path.resolve()),
            duration_sec=media_duration(output_path),
            frames=actual_frames,
            montage_reused=montage_reused,
            dialogue_audio_reused=dialogue_audio_reused,
            stage_timings_sec=timings,
            wall_clock_sec=time.monotonic() - started,
        )
        result.write(output_dir / "render_result.json")
        return result


__all__ = [
    "Renderer",
    "build_final_audio_filter",
    "concatenate_clips",
    "mix_bgm",
    "render_clip",
    "render_montage",
]
