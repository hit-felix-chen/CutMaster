"""Offline compiler-to-renderer and deterministic source-window boundary audit."""

from __future__ import annotations

import random
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from cutmaster.configuration.schema import (
    RendererConfig,
    ShotDetectionConfig,
    SourceWindowOptimizationConfig,
)
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint
from cutmaster.infrastructure.media.ffprobe import media_frame_count, probe_media
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.planners.tools import plan_compiler
from cutmaster.workflow.planners.tools import visual_scoring
from cutmaster.workflow.renderer import renderer as renderer_module
from cutmaster.workflow.renderer.ffmpeg import RenderError
from cutmaster.workflow.shared.timecode import format_range, parse_range


def _compile_fixture(
    tmp_path,
    monkeypatch,
    raw_script,
    *,
    source_duration=10.0,
    output_fps=30,
    source_fps=30.0,
    cuts=(),
    beats=(),
    sample_frames=4,
    video_description=None,
):
    request = SimpleNamespace(
        target_output_length_sec=raw_script[-1]["output_end_sec"],
        target_shot_length_sec=2.0,
        max_clip_duration_sec=None,
        video_path=tmp_path / "video.mp4",
        video=SimpleNamespace(
            material=SimpleNamespace(
                material_id=MaterialId.new(),
                expected_fingerprint=MaterialFingerprint("a" * 64),
                memory_root=tmp_path / "video-memory",
            ),
        ),
        music=SimpleNamespace(
            material=SimpleNamespace(
                material_id=MaterialId.new(),
                expected_fingerprint=MaterialFingerprint("b" * 64),
            ),
        ),
        prompt="boundary audit",
        prompt_type="event",
        video_title="Test Video",
        video_material_name="video",
        music_material_name="music",
    )
    config = SimpleNamespace(
        renderer=RendererConfig(fps=output_fps),
        analyser=SimpleNamespace(shot_detection=ShotDetectionConfig()),
        planners=SimpleNamespace(
            source_window_optimization=SourceWindowOptimizationConfig(),
            candidate_retrieval=SimpleNamespace(visual_sample_frames=sample_frames),
        ),
    )
    monkeypatch.setattr(plan_compiler, "media_duration", lambda _: source_duration)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: (tuple(cuts), source_fps, source_duration),
    )
    plan = plan_compiler.compile_render_plan(
        request=request,
        raw_script=raw_script,
        music_profile={"accents_sec": [], "beats_sec": list(beats)},
        video_description=video_description or {
            "source": {"fps": source_fps, "duration_sec": source_duration},
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": source_duration},
                }
            ],
        },
        config=config,
    )
    return request, plan


def _tail_raw(*, anchor: bool = True, source_start: float = 7.967):
    result = {
        "slot_id": "slot_01",
        "group_id": "group_001_anchor_01",
        "trajectory_id": "anchor_trajectory_01",
        "candidate_id": "anchor_candidate_01",
        "source_segment_id": "segment_0001",
        "timestamp": format_range(source_start, source_start + 2.033),
        "planned_duration_ms": 2033,
        "planned_duration_sec": 2.033,
        "output_start_sec": 0.0,
        "output_end_sec": 2.033,
        "dialogue_anchor": {
            "anchor_id": "anchor_01",
            "source_audio_start_sec": source_start,
            "source_audio_end_sec": source_start + 2.033,
            "output_audio_start_sec": 0.0,
            "output_audio_end_sec": 2.033,
        },
    }
    if not anchor:
        result.pop("dialogue_anchor")
    return result


@pytest.mark.parametrize("anchor", [True, False], ids=["anchor", "trajectory"])
def test_compiled_tail_anchor_is_not_shifted_by_renderer_quantization(
    tmp_path, monkeypatch, anchor,
) -> None:
    """A legal 2033-ms window becomes 61 output frames without moving picture."""

    raw = _tail_raw(anchor=anchor)
    request, plan = _compile_fixture(tmp_path, monkeypatch, [raw])
    assert plan.clips[0]["timestamp"] == raw["timestamp"]
    assert plan.clips[0]["output_frame_range"] == [0, 61]
    expected_mode = "dialogue_anchor_locked" if anchor else "beat_optimized"
    assert plan.clips[0]["cut_optimization"]["mode"] == expected_mode

    commands: list[list[str]] = []
    monkeypatch.setattr(renderer_module, "check_media_tools", lambda: None)
    monkeypatch.setattr(renderer_module, "probe_media", lambda _: {"video_duration": 10.0})
    monkeypatch.setattr(renderer_module, "select_encoder", lambda _: "libx264")
    monkeypatch.setattr(renderer_module, "run_media_command", commands.append)
    monkeypatch.setattr(renderer_module, "concatenate_clips", lambda *_: None)
    monkeypatch.setattr(renderer_module, "mix_bgm", lambda *_args, **_kwargs: None)

    renderer_module.render_montage(
        request.video_path,
        tmp_path / "music.wav",
        list(plan.clips),
        tmp_path / "render",
        RendererConfig(fps=30, encoder="libx264"),
        include_dialogue_audio=False,
    )

    assert len(commands) == 1
    seek = float(commands[0][commands[0].index("-ss") + 1])
    assert seek == parse_range(raw["timestamp"])[0]
    video_filter = commands[0][commands[0].index("-vf") + 1]
    assert "tpad=stop_mode=clone:stop=1" in video_filter
    assert video_filter.index("tpad=") < video_filter.index("fps=")


@pytest.mark.parametrize("anchor", [True, False], ids=["anchor", "trajectory"])
def test_renderer_rejects_true_frozen_eof_overflow_without_reseeking(
    tmp_path, monkeypatch, anchor,
) -> None:
    # Model the documented difference between container and video-stream duration.
    raw = _tail_raw(anchor=anchor, source_start=2.0)
    request, plan = _compile_fixture(
        tmp_path, monkeypatch, [raw], source_duration=4.033,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(renderer_module, "check_media_tools", lambda: None)
    monkeypatch.setattr(renderer_module, "probe_media", lambda _: {"video_duration": 4.0})
    monkeypatch.setattr(renderer_module, "select_encoder", lambda _: "libx264")
    monkeypatch.setattr(renderer_module, "run_media_command", commands.append)
    monkeypatch.setattr(renderer_module, "concatenate_clips", lambda *_: None)
    monkeypatch.setattr(renderer_module, "mix_bgm", lambda *_args, **_kwargs: None)

    with pytest.raises(RenderError, match="Frozen source window.*video"):
        renderer_module.render_montage(
            request.video_path,
            tmp_path / "music.wav",
            list(plan.clips),
            tmp_path / "render",
            RendererConfig(fps=30, encoder="libx264"),
            include_dialogue_audio=False,
        )
    assert commands == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_real_tail_anchor_render_preserves_seek_and_exact_frames(
    tmp_path, monkeypatch,
) -> None:
    raw = _tail_raw()
    request, plan = _compile_fixture(tmp_path, monkeypatch, [raw])
    music = tmp_path / "music.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=10",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
            str(request.video_path),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(music),
        ],
        check=True,
    )
    commands: list[list[str]] = []
    run_command = renderer_module.run_media_command

    def capture_and_run(command):
        commands.append(command)
        run_command(command)

    monkeypatch.setattr(renderer_module, "run_media_command", capture_and_run)
    _, output = renderer_module.render_montage(
        request.video_path,
        music,
        list(plan.clips),
        tmp_path / "render",
        RendererConfig(width=160, height=90, fps=30, encoder="libx264", threads=1),
        include_dialogue_audio=False,
    )
    assert media_frame_count(output) == 61
    clip_command = commands[0]
    assert float(clip_command[clip_command.index("-ss") + 1]) == 7.967


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
@pytest.mark.parametrize(
    ("source_rate", "source_frames", "output_fps"),
    [
        ("24000/1001", 120, 30),
        ("24000/1001", 240, 30),
        ("30000/1001", 150, 30),
        ("30000/1001", 300, 30),
        ("30", 300, 30),
        ("24000/1001", 120, 60),
    ],
)
def test_real_frozen_exact_eof_off_source_frame_grid_has_exact_frame_count(
    tmp_path, monkeypatch, source_rate, source_frames, output_fps,
) -> None:
    source = tmp_path / "video.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={source_rate}",
            "-frames:v", str(source_frames),
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )
    source_metadata = probe_media(source)
    source_duration = source_metadata["video_duration"]
    start = round(source_duration - 2.0, 3)
    raw = {
        **_tail_raw(anchor=False, source_start=start),
        "timestamp": format_range(start, source_duration),
        "planned_duration_ms": 2000,
        "planned_duration_sec": 2.0,
        "output_end_sec": 2.0,
    }
    _, plan = _compile_fixture(
        tmp_path, monkeypatch, [raw], source_duration=source_duration,
        source_fps=source_metadata["fps"],
        output_fps=output_fps,
    )
    frame_count = output_fps * 2
    assert parse_range(plan.clips[0]["timestamp"])[0] + frame_count / output_fps == source_duration
    commands: list[list[str]] = []
    run_command = renderer_module.run_media_command

    def capture_and_run(command):
        commands.append(command)
        run_command(command)

    monkeypatch.setattr(renderer_module, "run_media_command", capture_and_run)
    monkeypatch.setattr(renderer_module, "concatenate_clips", lambda *_: None)
    monkeypatch.setattr(renderer_module, "mix_bgm", lambda *_args, **_kwargs: None)
    output_dir = tmp_path / "render"
    renderer_module.render_montage(
        source,
        tmp_path / "unused-music.wav",
        list(plan.clips),
        output_dir,
        RendererConfig(width=160, height=90, fps=output_fps, encoder="libx264", threads=1),
        include_dialogue_audio=False,
    )
    assert float(commands[0][commands[0].index("-ss") + 1]) == start
    assert media_frame_count(output_dir / "clips" / "clip_0001.mp4") == frame_count


@pytest.mark.parametrize("seed", [127, 811, 20260902])
def test_seeded_compilation_preserves_media_bounds_and_frame_grid(
    tmp_path, monkeypatch, seed,
) -> None:
    randomizer = random.Random(seed)
    sampled: dict[str, list[float]] = {}

    def record_samples(_media, candidate, sample_times, _label):
        sampled[candidate["candidate_id"]] = sample_times
        return "unused-audit-image"

    monkeypatch.setattr(
        visual_scoring, "_sampled_contact_sheet_data_url", record_samples,
    )
    for case in range(32):
        fps = randomizer.choice([24, 25, 30, 60])
        source_fps = randomizer.choice([23.976, 29.97, 30.0, 60.0])
        sample_frames = randomizer.choice([1, 2, 4, 7])
        raw_script = []
        shots = []
        cuts = []
        caps = []
        source_cursor_ms = randomizer.randint(0, 2000)
        output_cursor_frames = 0
        previous_shot_end_ms = 0
        for index in range(3):
            frame_count = randomizer.randint(fps, 5 * fps)
            output_start_ms = round(output_cursor_frames * 1000 / fps)
            output_cursor_frames += frame_count
            output_end_ms = round(output_cursor_frames * 1000 / fps)
            duration_ms = output_end_ms - output_start_ms
            source_start_ms = source_cursor_ms
            source_end_ms = source_start_ms + duration_ms
            gap_ms = randomizer.choice([0, 100, 500, 1500])
            # Every eighth final clip reaches source EOF exactly.
            if index == 2 and case % 8 == 0:
                gap_ms = 0
            shot_end_ms = source_end_ms + gap_ms // 2
            planning_end_ms = source_end_ms + randomizer.randint(0, gap_ms)
            cap_ms = min(shot_end_ms, planning_end_ms)
            shot_id = f"shot_{index:04d}"
            shots.append(
                {
                    "shot_id": shot_id,
                    "time_range": {
                        "start_sec": previous_shot_end_ms / 1000,
                        "end_sec": shot_end_ms / 1000,
                    },
                }
            )
            previous_shot_end_ms = shot_end_ms
            source_cursor_ms = source_end_ms + gap_ms
            raw = {
                "slot_id": f"slot_{index:02d}",
                "group_id": f"group_{index:03d}",
                "trajectory_id": f"trajectory_{index:03d}",
                "candidate_id": f"candidate_{index:03d}",
                "source_segment_id": "segment_0001",
                "source_shot_ids": [shot_id],
                "planning_segment_id": f"planning_{index:03d}",
                "planning_segment_start_ms": source_start_ms,
                "planning_segment_end_ms": planning_end_ms,
                "timestamp": format_range(source_start_ms / 1000, source_end_ms / 1000),
                "planned_duration_ms": duration_ms,
                "planned_duration_sec": duration_ms / 1000,
                "output_start_sec": output_start_ms / 1000,
                "output_end_sec": output_end_ms / 1000,
            }
            if index == 1 and case % 4 == 0:
                raw["dialogue_anchor"] = {"anchor_id": "middle_anchor"}
            visual_scoring._contact_sheet_data_url(None, raw, sample_frames)
            raw_script.append(raw)
            caps.append(cap_ms / 1000)
            cuts.extend(
                (source_start_ms + duration_ms * fraction) / 1000
                for fraction in (0.2, 0.65)
            )
        source_duration = source_cursor_ms / 1000
        description = {
            "source": {"fps": source_fps, "duration_sec": source_duration},
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": source_duration},
                    "shots": shots,
                }
            ],
        }
        _, plan = _compile_fixture(
            tmp_path,
            monkeypatch,
            raw_script,
            source_duration=source_duration,
            output_fps=fps,
            source_fps=source_fps,
            cuts=sorted(cuts),
            beats=[index * 0.5 for index in range(32)],
            sample_frames=sample_frames,
            video_description=description,
        )
        restored = RenderPlan.from_dict(plan.to_dict())
        assert restored.total_frames == output_cursor_frames
        assert len(restored.clips) == 3
        prior_source_end = -1.0
        for raw, clip, cap in zip(raw_script, restored.clips, caps, strict=True):
            before_start, before_end = parse_range(raw["timestamp"])
            after_start, after_end = parse_range(clip["timestamp"])
            assert clip["candidate_id"] == raw["candidate_id"]
            assert clip["trajectory_id"] == raw["trajectory_id"]
            assert clip["group_id"] == raw["group_id"]
            assert after_start >= before_start - 1e-9
            assert after_end <= source_duration + 1e-9
            assert after_start - before_start <= 2.0 + 1e-9
            assert after_end - after_start == pytest.approx(before_end - before_start)
            render_frames = clip["output_frame_range"][1] - clip["output_frame_range"][0]
            assert abs((after_end - after_start) - render_frames / fps) <= 0.001001
            if raw.get("dialogue_anchor") is not None:
                assert clip["timestamp"] == raw["timestamp"]
                assert clip["dialogue_anchor"] == raw["dialogue_anchor"]
            prior_source_end = after_end
