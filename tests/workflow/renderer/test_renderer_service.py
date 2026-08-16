import shutil
import subprocess
from pathlib import Path

import pytest

from cutmaster.configuration.schema import DialogueAudioConfig, RendererConfig
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint, MaterialType
from cutmaster.workflow.contracts.material import (
    MaterialRuntimeHandle,
    RenderRuntimeBindings,
)
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.contracts.rendering import (
    RenderOptions,
    RenderOutputTarget,
    RenderRequest,
)
from cutmaster.workflow.renderer import Renderer


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_renderer_reuses_montage_without_mutating_plan(tmp_path: Path) -> None:
    source = (tmp_path / "source.mp4").resolve()
    bgm = (tmp_path / "bgm.wav").resolve()
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=2", str(bgm),
        ],
        check=True,
    )
    video_memory = (tmp_path / "video-memory").resolve()
    music_memory = (tmp_path / "music-memory").resolve()
    video_memory.mkdir()
    music_memory.mkdir()
    video = MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=MaterialType.VIDEO,
        material_name="source",
        expected_fingerprint=MaterialFingerprint("a" * 64),
        source_path=source,
        memory_root=video_memory,
    )
    music = MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=MaterialType.MUSIC,
        material_name="bgm",
        expected_fingerprint=MaterialFingerprint("b" * 64),
        source_path=bgm,
        memory_root=music_memory,
    )
    bindings = RenderRuntimeBindings(video=video, music=music)
    plan = RenderPlan.create(
        video_material_id=video.material_id,
        video_expected_fingerprint=video.expected_fingerprint,
        music_material_id=music.material_id,
        music_expected_fingerprint=music.expected_fingerprint,
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
        planners_metadata={"editing_intent": "test"},
    )
    plan_path = tmp_path / "planners" / "render_plan.json"
    plan.write(plan_path)
    original_plan = plan_path.read_bytes()
    output_dir = (tmp_path / "renderer").resolve()
    renderer = Renderer(
        RendererConfig(
            width=160,
            height=90,
            fps=30,
            encoder="libx264",
            threads=1,
            dialogue_audio=DialogueAudioConfig(enable_vocal_separation=False),
        )
    )

    first = renderer.render(
        RenderRequest(
            plan=plan,
            bindings=bindings,
            options=RenderOptions(audio_mode="bgm_only"),
            output_target=RenderOutputTarget(output_dir, "first.mp4"),
        )
    )
    second = renderer.render(
        RenderRequest(
            plan=plan,
            bindings=bindings,
            options=RenderOptions(audio_mode="bgm_only"),
            output_target=RenderOutputTarget(output_dir, "second.mp4"),
        ),
        overwrite=True,
    )

    assert first.montage_reused is False
    assert second.montage_reused is True
    assert plan_path.read_bytes() == original_plan
    assert (output_dir / "render_result.json").is_file()
    assert second.duration_sec == pytest.approx(2.0, abs=0.05)
