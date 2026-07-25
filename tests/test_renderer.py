import json
import shutil
import subprocess

import pytest

from cutmaster.configuration.schema import RenderConfig
from cutmaster.editing.renderer import build_final_audio_filter, concatenate_clips, render_clip


def test_final_audio_filter_uses_only_bgm_when_source_is_muted() -> None:
    audio_filter = build_final_audio_filter(
        RenderConfig(bgm_volume=0.3, original_volume=0.0),
        duration=60.0,
    )
    assert "[1:a]" in audio_filter
    assert "[0:a]" not in audio_filter
    assert "amix" not in audio_filter
    assert audio_filter.endswith("[aout]")


def test_final_audio_filter_mixes_source_when_enabled() -> None:
    audio_filter = build_final_audio_filter(
        RenderConfig(bgm_volume=0.3, original_volume=0.5),
        duration=60.0,
    )
    assert "[0:a]volume=0.5" in audio_filter
    assert "[1:a]volume=0.3" in audio_filter
    assert "amix" in audio_filter


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
    config = RenderConfig(width=320, height=180, fps=30, threads=1)
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
