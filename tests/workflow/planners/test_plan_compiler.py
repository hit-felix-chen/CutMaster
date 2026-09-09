from pathlib import Path
from types import SimpleNamespace

import pytest

from cutmaster.configuration.schema import (
    ShotDetectionConfig,
    SourceWindowOptimizationConfig,
)
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.planners.tools import plan_compiler
from cutmaster.workflow.planners.tools.plan_compiler import (
    adapt_script,
    align_cut_boundaries,
    script_duration,
)
from cutmaster.workflow.shared.timecode import format_time, parse_range, parse_time


def test_timecode_round_trip() -> None:
    assert parse_time("01:02:03,456") == pytest.approx(3723.456)
    assert parse_time("01:02:03.456") == pytest.approx(3723.456)
    assert format_time(3723.456) == "01:02:03,456"
    assert parse_range("00:00:01,000 --> 00:00:02,500") == (1.0, 2.5)


def _raw_script(first_end: int, second_end: int):
    return [
        {
            "_id": 1,
            "timestamp": f"00:00:10,000-00:00:{first_end:02d},000",
            "picture": "First event",
            "OST": 1,
        },
        {
            "_id": 2,
            "timestamp": f"00:00:30,000-00:00:{second_end:02d},000",
            "picture": "Second event",
            "OST": 1,
        },
    ]


def test_adapt_script() -> None:
    raw = _raw_script(20, 40)
    adapted = adapt_script(raw, target_output_length_sec=7.0, target_shot_length_sec=4.0)
    assert len(adapted) == 2
    assert adapted[0]["timestamp"] == "00:00:10,000-00:00:14,000"
    assert adapted[1]["timestamp"] == "00:00:30,000-00:00:33,000"
    assert script_duration(adapted) == pytest.approx(7.0)
    assert all(item["OST"] == 1 for item in adapted)


def test_adapt_script_aligns_internal_cuts_to_audio_beats() -> None:
    raw = _raw_script(15, 35)
    adapted = adapt_script(
        raw,
        target_output_length_sec=8.0,
        target_shot_length_sec=4.0,
        beat_times=[3.9, 8.1],
        source_duration_sec=60.0,
    )
    assert adapted[0]["timestamp"] == "00:00:10,000-00:00:13,900"
    assert adapted[0]["output_timestamp"] == "00:00:00,000-00:00:03,900"
    assert adapted[1]["timestamp"] == "00:00:30,000-00:00:34,100"
    assert adapted[1]["output_timestamp"] == "00:00:03,900-00:00:08,000"
    assert script_duration(adapted) == pytest.approx(8.0)


def test_adapt_script_uses_output_frame_grid_as_timeline_source() -> None:
    raw = _raw_script(15, 35)
    adapted = adapt_script(
        raw,
        target_output_length_sec=8.0,
        target_shot_length_sec=4.0,
        beat_times=[4.05],
        source_duration_sec=60.0,
        output_fps=30,
    )

    assert adapted[0]["output_frame_range"] == [0, 122]
    assert adapted[1]["output_frame_range"] == [122, 240]
    assert adapted[0]["output_timestamp"] == "00:00:00,000-00:00:04,067"
    assert adapted[1]["output_timestamp"] == "00:00:04,067-00:00:08,000"


def test_adapt_script_respects_planned_variable_durations() -> None:
    raw = [
        {
            "_id": 1,
            "timestamp": "00:00:00,000-00:00:10,000",
            "picture": "slow introduction",
            "planned_duration_sec": 5.5,
        },
        {
            "_id": 2,
            "timestamp": "00:00:20,000-00:00:30,000",
            "picture": "fast climax",
            "planned_duration_sec": 2.5,
        },
    ]
    adapted = adapt_script(
        raw,
        target_output_length_sec=8.0,
        target_shot_length_sec=4.0,
        beat_times=[],
    )
    durations = [
        parse_range(item["timestamp"])[1] - parse_range(item["timestamp"])[0]
        for item in adapted
    ]
    assert durations == pytest.approx([5.5, 2.5])


def test_adapt_script_preserves_prealigned_output_timeline() -> None:
    raw = [
        {
            "_id": 1,
            "timestamp": "00:00:00,000-00:00:10,000",
            "picture": "introduction",
            "output_start_sec": 0.0,
            "output_end_sec": 3.9,
            "planned_duration_sec": 3.9,
        },
        {
            "_id": 2,
            "timestamp": "00:00:20,000-00:00:30,000",
            "picture": "climax",
            "output_start_sec": 3.9,
            "output_end_sec": 8.0,
            "planned_duration_sec": 4.1,
        },
    ]
    adapted = adapt_script(
        raw,
        target_output_length_sec=8.0,
        target_shot_length_sec=4.0,
        beat_times=[2.0, 6.0],
    )
    assert adapted[0]["output_timestamp"] == "00:00:00,000-00:00:03,900"
    assert adapted[1]["output_timestamp"] == "00:00:03,900-00:00:08,000"


def test_adapt_script_keeps_trajectory_identity_and_uses_frame_output_times() -> None:
    raw = [
        {
            "slot_id": "slot_01",
            "group_id": "group_001",
            "trajectory_id": "group_001_trajectory_01",
            "candidate_id": "candidate_01",
            "timestamp": "00:00:10,000-00:00:12,033",
            "planned_duration_ms": 2033,
            "planned_duration_sec": 2.033,
            "output_start_sec": 0.0,
            "output_end_sec": 2.033,
        },
        {
            "slot_id": "slot_02",
            "group_id": "group_001",
            "trajectory_id": "group_001_trajectory_01",
            "candidate_id": "candidate_02",
            "timestamp": "00:00:12,033-00:00:14,000",
            "planned_duration_ms": 1967,
            "planned_duration_sec": 1.967,
            "output_start_sec": 2.033,
            "output_end_sec": 4.0,
        },
    ]

    adapted = adapt_script(
        raw,
        target_output_length_sec=4.0,
        target_shot_length_sec=2.0,
        output_fps=30,
        source_duration_sec=60.0,
    )

    assert [item["timestamp"] for item in adapted] == [
        "00:00:10,000-00:00:12,033",
        "00:00:12,033-00:00:14,000",
    ]
    assert [item["candidate_id"] for item in adapted] == [
        "candidate_01",
        "candidate_02",
    ]
    assert [item["output_frame_range"] for item in adapted] == [
        [0, 61],
        [61, 120],
    ]
    assert adapted[0]["output_end_sec"] == pytest.approx(61 / 30)
    assert adapted[1]["output_start_sec"] == pytest.approx(61 / 30)
    assert adapted[0]["render_duration_sec"] == pytest.approx(61 / 30)
    assert adapted[1]["render_duration_sec"] == pytest.approx(59 / 30)
    assert [item["planned_duration_ms"] for item in adapted] == [2033, 1967]


def test_adapt_script_rejects_trajectory_duration_beyond_frame_rounding() -> None:
    with pytest.raises(ValueError, match="candidate duration"):
        adapt_script(
            [
                {
                    "slot_id": "slot_01",
                    "group_id": "group_001",
                    "trajectory_id": "group_001_trajectory_01",
                    "candidate_id": "candidate_01",
                    "timestamp": "00:00:10,000-00:00:11,900",
                    "planned_duration_ms": 2000,
                    "planned_duration_sec": 2.0,
                    "output_start_sec": 0.0,
                    "output_end_sec": 2.0,
                }
            ],
            target_output_length_sec=2.0,
            target_shot_length_sec=2.0,
            output_fps=30,
            source_duration_sec=60.0,
        )


def test_compile_render_plan_preserves_group_trajectory_per_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One trajectory stays identifiable without collapsing its Slot clips."""

    video_material = SimpleNamespace(
        material_id=MaterialId.new(),
        expected_fingerprint=MaterialFingerprint("a" * 64),
        memory_root=tmp_path / "video-memory",
    )
    music_material = SimpleNamespace(
        material_id=MaterialId.new(),
        expected_fingerprint=MaterialFingerprint("b" * 64),
    )
    request = SimpleNamespace(
        target_output_length_sec=4.0,
        target_shot_length_sec=2.0,
        max_clip_duration_sec=None,
        video_path=tmp_path / "video.mp4",
        video=SimpleNamespace(material=video_material),
        music=SimpleNamespace(material=music_material),
        prompt="test",
        prompt_type="event",
        video_title="Test Video",
        video_material_name="video",
        music_material_name="music",
    )
    config = SimpleNamespace(
        renderer=SimpleNamespace(fps=30),
        analyser=SimpleNamespace(shot_detection=ShotDetectionConfig()),
        planners=SimpleNamespace(
            source_window_optimization=SourceWindowOptimizationConfig(),
            candidate_retrieval=SimpleNamespace(visual_sample_frames=4),
        ),
    )
    raw_script = [
        {
            "_id": 1,
            "slot_id": "slot_01",
            "group_id": "group_001",
            "trajectory_id": "group_001_trajectory_01",
            "candidate_id": "candidate_01",
            "timestamp": "00:00:10,000-00:00:12,000",
            "picture": "first event",
            "planned_duration_sec": 2.0,
            "output_start_sec": 0.0,
            "output_end_sec": 2.0,
        },
        {
            "_id": 2,
            "slot_id": "slot_02",
            "group_id": "group_001",
            "trajectory_id": "group_001_trajectory_01",
            "candidate_id": "candidate_02",
            "timestamp": "00:00:12,000-00:00:14,000",
            "picture": "second event",
            "planned_duration_sec": 2.0,
            "output_start_sec": 2.0,
            "output_end_sec": 4.0,
        },
    ]

    monkeypatch.setattr(plan_compiler, "media_duration", lambda _path: 60.0)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: ((), 30.0, 60.0),
    )
    plan = plan_compiler.compile_render_plan(
        request=request,
        raw_script=raw_script,
        music_profile={
            "schema_version": "2.0",
            "accents_sec": [],
            "beats_sec": [],
        },
        video_description={},
        config=config,
    )
    restored = RenderPlan.from_dict(plan.to_dict())

    assert len(restored.clips) == 2
    assert [clip["slot_id"] for clip in restored.clips] == ["slot_01", "slot_02"]
    assert [clip["group_id"] for clip in restored.clips] == [
        "group_001",
        "group_001",
    ]
    assert [clip["trajectory_id"] for clip in restored.clips] == [
        "group_001_trajectory_01",
        "group_001_trajectory_01",
    ]
    assert [clip["candidate_id"] for clip in restored.clips] == [
        "candidate_01",
        "candidate_02",
    ]
    assert [clip["output_frame_range"] for clip in restored.clips] == [
        [0, 60],
        [60, 120],
    ]
    assert [clip["timestamp"] for clip in restored.clips] == [
        "00:00:10,000-00:00:12,000",
        "00:00:12,000-00:00:14,000",
    ]
    assert all(
        clip["cut_optimization"].get("mode") != "trajectory_locked"
        for clip in restored.clips
    )


def test_align_cut_boundaries_honors_explicit_clip_cap() -> None:
    with pytest.raises(ValueError, match="No audio beat"):
        align_cut_boundaries([4.0], [3.9, 4.1], 8.0, max_clip_duration_sec=4.0)
