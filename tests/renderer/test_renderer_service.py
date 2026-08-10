import shutil
import subprocess

import pytest

from cutmaster.configuration.schema import DialogueAudioConfig, RendererConfig
from cutmaster.contracts.planners import RenderPlan
from cutmaster.contracts.renderer import RenderRequest
from cutmaster.renderer import Renderer


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_renderer_reuses_montage_without_mutating_plan(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    bgm = tmp_path / "bgm.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=2",
            str(bgm),
        ],
        check=True,
    )
    plan = RenderPlan.create(
        source_video=source,
        background_music=bgm,
        fps=30,
        clips=[
            {
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            },
            {
                "timestamp": "00:00:01,000-00:00:02,000",
                "output_frame_range": [30, 60],
            },
        ],
        planners_metadata={"prompt": "test"},
    )
    plan_path = tmp_path / "planners" / "render_plan.json"
    plan.write(plan_path)
    original_plan = plan_path.read_bytes()
    output_dir = tmp_path / "renderer"
    renderer = Renderer(
        RendererConfig(
            width=160,
            height=90,
            fps=30,
            encoder="libx264",
            threads=1,
            dialogue_audio=DialogueAudioConfig(
                enable_vocal_separation=False
            ),
        )
    )

    first = renderer.render(
        RenderRequest(
            plan_path=plan_path,
            output_dir=output_dir,
            audio_mode="bgm_only",
        )
    )
    second = renderer.render(
        RenderRequest(
            plan_path=plan_path,
            output_dir=output_dir,
            audio_mode="bgm_only",
            overwrite=True,
        )
    )

    assert first.montage_reused is False
    assert second.montage_reused is True
    assert plan_path.read_bytes() == original_plan
    assert (output_dir / "render_result.json").is_file()
    assert second.duration_sec == pytest.approx(2.0, abs=0.05)
