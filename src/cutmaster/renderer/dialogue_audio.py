from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import DialogueAudioConfig
from cutmaster.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.renderer.ffmpeg import RenderError, run_media_command
from cutmaster.runtime.media_probe import media_duration
from cutmaster.runtime.observability import error_summary, log_event


_SEPARATOR_SAMPLE_RATE = 44100


def _separator_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch
    except ImportError as exc:
        raise RenderError(
            "Demucs vocal separation requires the project audio dependencies"
        ) from exc
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _anchor_specs(
    script: list[dict[str, Any]],
    source_duration_sec: float,
    padding_sec: float,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    reel_cursor = 0.0
    for script_index, item in enumerate(script):
        anchor = item.get("dialogue_anchor")
        if anchor is None:
            continue
        speech_start = float(anchor["source_audio_start_sec"])
        speech_end = float(anchor["source_audio_end_sec"])
        padded_start = max(0.0, speech_start - padding_sec)
        padded_end = min(source_duration_sec, speech_end + padding_sec)
        if speech_end <= speech_start or padded_end <= padded_start:
            raise RenderError(
                f"Dialogue anchor {anchor['anchor_id']} has an invalid audio range"
            )
        padded_duration = padded_end - padded_start
        specs.append(
            {
                "script_index": script_index,
                "anchor_id": str(anchor["anchor_id"]),
                "padded_start_sec": padded_start,
                "padded_duration_sec": padded_duration,
                "pre_padding_sec": speech_start - padded_start,
                "post_padding_sec": padded_end - speech_end,
                "speech_duration_sec": speech_end - speech_start,
                "speech_reel_start_sec": (
                    reel_cursor + speech_start - padded_start
                ),
            }
        )
        reel_cursor += padded_duration
    return specs


def _cache_key(
    video_path: Path,
    specs: list[dict[str, Any]],
    config: DialogueAudioConfig,
) -> str:
    stat = video_path.stat()
    payload = {
        "source_path": str(video_path.resolve()),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "ranges": [
            {
                "anchor_id": spec["anchor_id"],
                "padded_start_sec": round(spec["padded_start_sec"], 6),
                "padded_duration_sec": round(spec["padded_duration_sec"], 6),
                "speech_reel_start_sec": round(
                    spec["speech_reel_start_sec"],
                    6,
                ),
                "speech_duration_sec": round(spec["speech_duration_sec"], 6),
            }
            for spec in specs
        ],
        "model": config.separator_model,
        "segment_sec": config.separator_segment_sec,
        "shifts": config.separator_shifts,
        "padding_sec": config.separator_padding_sec,
        "loudness_lufs": config.separated_loudness_lufs,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _expected_anchor_paths(
    cache_dir: Path,
    specs: list[dict[str, Any]],
) -> list[Path]:
    return [
        cache_dir / f"anchor_{index:02d}_{spec['anchor_id']}.wav"
        for index, spec in enumerate(specs, 1)
    ]


def _build_dialogue_reel(
    video_path: Path,
    specs: list[dict[str, Any]],
    output_path: Path,
) -> None:
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for spec in specs:
        command.extend(
            [
                "-ss",
                f"{spec['padded_start_sec']:.6f}",
                "-t",
                f"{spec['padded_duration_sec']:.6f}",
                "-i",
                str(video_path),
            ]
        )
    filters: list[str] = []
    labels: list[str] = []
    for index, spec in enumerate(specs):
        label = f"anchor{index}"
        filters.append(
            f"[{index}:a]aresample={_SEPARATOR_SAMPLE_RATE},"
            "aformat=sample_fmts=s16:channel_layouts=stereo,"
            f"atrim=0:{spec['padded_duration_sec']:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        filters.append(f"{labels[0]}anull[reel]")
    else:
        filters.append(
            "".join(labels)
            + f"concat=n={len(labels)}:v=0:a=1[reel]"
        )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[reel]",
            "-ar",
            str(_SEPARATOR_SAMPLE_RATE),
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )
    run_media_command(command)


def _run_demucs(
    reel_path: Path,
    work_dir: Path,
    config: DialogueAudioConfig,
    device: str,
) -> Path:
    started = time.monotonic()
    command = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        config.separator_model,
        "--two-stems",
        "vocals",
        "--other-method",
        "none",
        "--segment",
        str(config.separator_segment_sec),
        "--shifts",
        str(config.separator_shifts),
        "-j",
        "1",
        "-d",
        device,
        "-o",
        str(work_dir),
        str(reel_path),
    ]
    log_event(
        "INFO",
        "dialogue_audio",
        "model.start",
        "Demucs vocal separation started",
        model=config.separator_model,
        device=device,
        segment_sec=config.separator_segment_sec,
        shifts=config.separator_shifts,
        input_duration_sec=media_duration(reel_path),
    )
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        failure = build_prompt_failure(
            PromptFailureCode.EXTERNAL_PROCESS_FAILED,
            operation="demucs_vocal_separation",
            error_type=type(exc).__name__,
            error_message=error_summary(exc),
        )
        log_event(
            "ERROR",
            "dialogue_audio",
            "model.fail",
            "Demucs vocal separation failed",
            model=config.separator_model,
            device=device,
            elapsed_sec=time.monotonic() - started,
            **failure,
        )
        raise RenderError(
            f"Demucs vocal separation failed with exit code {exc.returncode}"
        ) from exc
    vocals_path = (
        work_dir
        / config.separator_model
        / reel_path.stem
        / "vocals.wav"
    )
    if not vocals_path.is_file():
        raise RenderError(f"Demucs did not create vocals stem: {vocals_path}")
    log_event(
        "SUCCESS",
        "dialogue_audio",
        "model.complete",
        "Demucs vocal separation completed",
        model=config.separator_model,
        device=device,
        elapsed_sec=time.monotonic() - started,
    )
    return vocals_path


def _split_vocals(
    vocals_path: Path,
    specs: list[dict[str, Any]],
    outputs: list[Path],
    loudness_lufs: float,
) -> None:
    for spec, output_path in zip(specs, outputs, strict=True):
        run_media_command(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{spec['speech_reel_start_sec']:.6f}",
                "-t",
                f"{spec['speech_duration_sec']:.6f}",
                "-i",
                str(vocals_path),
                "-af",
                f"loudnorm=I={loudness_lufs}:LRA=7:TP=-1.5",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
        )


def _attach_prepared_audio(
    script: list[dict[str, Any]],
    specs: list[dict[str, Any]],
    outputs: list[Path],
    *,
    cache_key: str,
    model: str,
    device: str,
) -> list[dict[str, Any]]:
    result = [dict(item) for item in script]
    for spec, output_path in zip(specs, outputs, strict=True):
        item = result[int(spec["script_index"])]
        anchor = dict(item["dialogue_anchor"])
        anchor["prepared_audio_path"] = str(output_path.resolve())
        anchor["vocal_separation"] = {
            "backend": "demucs",
            "model": model,
            "device": device,
            "cache_key": cache_key,
            "padding_sec": round(
                min(spec["pre_padding_sec"], spec["post_padding_sec"]),
                6,
            ),
            "pre_padding_sec": round(spec["pre_padding_sec"], 6),
            "post_padding_sec": round(spec["post_padding_sec"], 6),
        }
        item["dialogue_anchor"] = anchor
    return result


def prepare_dialogue_audio(
    video_path: Path,
    script: list[dict[str, Any]],
    output_dir: Path,
    config: DialogueAudioConfig,
) -> tuple[list[dict[str, Any]], bool]:
    if not config.enable_vocal_separation:
        return [dict(item) for item in script], False
    if not any(item.get("dialogue_anchor") is not None for item in script):
        return [dict(item) for item in script], False
    source_duration_sec = media_duration(video_path)
    specs = _anchor_specs(
        script,
        source_duration_sec,
        config.separator_padding_sec,
    )
    if not specs:
        return [dict(item) for item in script], False
    device = _separator_device(config.separator_device)
    cache_key = _cache_key(video_path, specs, config)
    cache_dir = output_dir / "dialogue_audio" / cache_key
    cache_dir.mkdir(parents=True, exist_ok=True)
    outputs = _expected_anchor_paths(cache_dir, specs)
    if all(path.is_file() and path.stat().st_size > 0 for path in outputs):
        log_event(
            "INFO",
            "dialogue_audio",
            "cache.hit",
            "Separated dialogue audio cache reused",
            anchors=len(outputs),
            cache_key=cache_key,
            model=config.separator_model,
        )
        return (
            _attach_prepared_audio(
                script,
                specs,
                outputs,
                cache_key=cache_key,
                model=config.separator_model,
                device=device,
            ),
            True,
        )

    with tempfile.TemporaryDirectory(
        prefix="demucs-",
        dir=cache_dir,
    ) as temporary_directory:
        work_dir = Path(temporary_directory)
        reel_path = work_dir / "dialogue_reel.wav"
        _build_dialogue_reel(video_path, specs, reel_path)
        vocals_path = _run_demucs(reel_path, work_dir / "separated", config, device)
        _split_vocals(
            vocals_path,
            specs,
            outputs,
            config.separated_loudness_lufs,
        )
    manifest = {
        "cache_key": cache_key,
        "backend": "demucs",
        "model": config.separator_model,
        "device": device,
        "anchors": [
            {
                **spec,
                "prepared_audio_path": str(output.resolve()),
            }
            for spec, output in zip(specs, outputs, strict=True)
        ],
    }
    (cache_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return (
        _attach_prepared_audio(
            script,
            specs,
            outputs,
            cache_key=cache_key,
            model=config.separator_model,
            device=device,
        ),
        False,
    )


__all__ = ["prepare_dialogue_audio"]
