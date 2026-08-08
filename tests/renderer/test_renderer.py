import json
import shutil
import subprocess

import pytest

from cutmaster.configuration.schema import DialogueAudioConfig, RendererConfig
from cutmaster.renderer.renderer import (
    build_final_audio_filter,
    concatenate_clips,
    mix_bgm,
    render_clip,
)


def test_final_audio_filter_uses_only_bgm_when_source_is_muted() -> None:
    audio_filter = build_final_audio_filter(
        RendererConfig(bgm_volume=0.3, original_volume=0.0),
        duration=60.0,
    )
    assert "[1:a]" in audio_filter
    assert "[0:a]" not in audio_filter
    assert "amix" not in audio_filter
    assert audio_filter.endswith("[aout]")


def test_final_audio_filter_mixes_selected_dialogue_and_ducks_bgm() -> None:
    audio_filter = build_final_audio_filter(
        RendererConfig(bgm_volume=0.3, original_volume=0.0),
        duration=60.0,
        dialogue_anchors=[
            {
                "source_audio_start_sec": 80.0,
                "source_audio_end_sec": 82.0,
                "output_audio_start_sec": 10.0,
                "output_audio_end_sec": 12.0,
            }
        ],
        dialogue_config=DialogueAudioConfig(
            dialogue_volume=1.0,
            bgm_duck_factor=0.5,
            fade_sec=0.05,
        ),
    )
    assert "[2:a]volume=1.0" in audio_filter
    assert "between(t\\,10.000\\,12.000)" in audio_filter
    assert "0.15" in audio_filter
    assert "adelay=10000:all=1" in audio_filter
    assert "amix" in audio_filter


def test_mix_bgm_uses_prepared_vocal_stem_without_source_seek(
    tmp_path,
    monkeypatch,
) -> None:
    commands = []
    monkeypatch.setattr(
        "cutmaster.renderer.renderer.run_media_command",
        lambda command: commands.append(command),
    )
    prepared = tmp_path / "anchor.wav"

    mix_bgm(
        tmp_path / "montage.mp4",
        tmp_path / "bgm.mp3",
        tmp_path / "output.mp4",
        RendererConfig(),
        duration=4.0,
        script=[
            {
                "dialogue_anchor": {
                    "source_audio_start_sec": 80.0,
                    "source_audio_end_sec": 82.0,
                    "output_audio_start_sec": 1.0,
                    "output_audio_end_sec": 3.0,
                    "prepared_audio_path": str(prepared),
                }
            }
        ],
        dialogue_config=DialogueAudioConfig(),
    )

    command = commands[0]
    assert str(prepared) in command
    assert "-ss" not in command


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_concatenated_clips_preserve_exact_total_frame_count(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=12",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )
    config = RendererConfig(width=320, height=180, fps=30, threads=1)
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    render_clip(source, first, 0.0, 114, config, "libx264")
    render_clip(source, second, 4.0, 129, config, "libx264")
    montage = tmp_path / "montage.mp4"
    concatenate_clips([first, second], montage)

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames,duration", "-of", "json", str(montage),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert int(stream["nb_read_frames"]) == 243
    assert float(stream["duration"]) == pytest.approx(8.1, abs=1e-6)

    for clip in (first, second):
        clip_probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=start_time", "-of", "json", str(clip),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        clip_stream = json.loads(clip_probe.stdout)["streams"][0]
        assert float(clip_stream["start_time"]) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_mix_bgm_accepts_exact_source_dialogue_anchor(tmp_path) -> None:
    montage = tmp_path / "montage.mp4"
    source = tmp_path / "source.mp4"
    bgm = tmp_path / "bgm.wav"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=160x90:rate=30:duration=4",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(montage),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=160x90:rate=30:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
            "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=4", str(bgm),
        ],
        check=True,
    )

    mix_bgm(
        montage,
        bgm,
        output,
        RendererConfig(width=160, height=90, fps=30),
        duration=4.0,
        source_video=source,
        script=[
            {
                "dialogue_anchor": {
                    "source_audio_start_sec": 1.0,
                    "source_audio_end_sec": 2.0,
                    "output_audio_start_sec": 2.0,
                    "output_audio_end_sec": 3.0,
                }
            }
        ],
        dialogue_config=DialogueAudioConfig(),
    )

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=duration", "-of", "json", str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert float(json.loads(probe.stdout)["streams"][0]["duration"]) == pytest.approx(
        4.0,
        abs=0.05,
    )
