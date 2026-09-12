import pytest

import numpy as np
from scenedetect import FrameTimecode

from cutmaster.workflow.shared.shot_detection import detect_source_cuts
from cutmaster.workflow.shared.timecode import parse_range
from cutmaster.workflow.planners.tools.source_window_optimizer import (
    _detect_used_segment_cuts,
    choose_source_window,
    optimize_script_source_windows,
)
from cutmaster.configuration.schema import (
    ShotDetectionConfig,
    SourceWindowOptimizationConfig,
)


def test_trajectory_window_can_shift_beyond_former_semantic_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: ((13.0,), 30.0, 60.0),
    )
    item = {
        "group_id": "group_01", "trajectory_id": "trajectory_01",
        "timestamp": "00:00:10,000-00:00:14,000",
        "output_frame_range": [0, 120], "planning_segment_end_ms": 14000,
    }
    result = optimize_script_source_windows(
        tmp_path / "source.mp4", tmp_path / "segments", [item], [1.5], {},
        output_fps=30, detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(),
    )[0]
    audit = result["cut_optimization"]
    assert audit["source_shift_sec"] == pytest.approx(1.5)
    assert audit["max_beat_distance_sec"] == pytest.approx(0.0)
    assert audit["fallback_level"] == 0
    assert result["output_frame_range"] == [0, 120]


def test_detect_source_cuts_filters_near_duplicate_frames(monkeypatch, tmp_path) -> None:
    frames = [
        np.full((8, 8, 3), value, dtype=np.uint8)
        for value in (0, 0, 10, 10, 20)
    ]

    class FakeCapture:
        def release(self) -> None:
            pass

    class FakeVideo:
        frame_rate = 10.0
        capture = FakeCapture()

        def __init__(self) -> None:
            self.index = -1
            self.position = FrameTimecode(0, self.frame_rate)

        def seek(self, _target: float) -> None:
            self.index = -1

        def read(self):
            self.index += 1
            if self.index >= len(frames):
                return False
            self.position = FrameTimecode(self.index, self.frame_rate)
            return frames[self.index]

    processed_positions: list[int] = []

    class FakeDetector:
        def __init__(self, **_kwargs) -> None:
            pass

        def process_frame(self, position, _frame):
            processed_positions.append(position.frame_num)
            return []

    monkeypatch.setattr(
        "cutmaster.workflow.shared.shot_detection.open_video",
        lambda _path: FakeVideo(),
    )
    monkeypatch.setattr(
        "cutmaster.workflow.shared.shot_detection.AdaptiveDetector",
        FakeDetector,
    )

    cuts, frame_rate = detect_source_cuts(tmp_path / "video.mp4", 0.0, 1.0)

    assert cuts == []
    assert frame_rate == 10.0
    assert processed_positions == [0, 2, 4]


def test_choose_source_window_minimizes_worst_cut_distance() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[11.2, 12.4],
        beat_times=[1.0, 2.0, 3.0],
        source_duration_sec=60.0,
        frame_rate=10.0,
        search_margin_sec=0.5,
    )

    assert result.source_start_sec == pytest.approx(10.1)
    assert result.internal_output_cuts_sec == pytest.approx((1.1, 2.3))
    assert result.max_beat_distance_sec == pytest.approx(0.3)


def test_choose_source_window_keeps_detected_cuts_inside() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=5.0,
        internal_source_cuts_sec=[10.1, 12.1],
        candidate_source_cuts_sec=[10.1, 12.1, 14.5],
        beat_times=[5.0, 6.0],
        source_duration_sec=60.0,
        frame_rate=30.0,
        search_margin_sec=2.0,
    )

    assert result.source_start_sec >= 10.0
    for output_cut in result.internal_output_cuts_sec:
        relative_cut = output_cut - 5.0
        assert relative_cut > 1.0
        assert 4.0 - relative_cut > 1.0


def test_choose_source_window_never_moves_backward() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[10.5, 12.5],
        candidate_source_cuts_sec=[10.5, 12.5, 14.5],
        beat_times=[1.0, 2.0, 3.0],
        source_duration_sec=60.0,
        frame_rate=30.0,
    )

    assert 10.0 <= result.source_start_sec <= 12.0


def test_choose_source_window_preserves_already_aligned_millisecond_start() -> None:
    result = choose_source_window(
        original_start_sec=6902.567,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[6904.0],
        candidate_source_cuts_sec=[6904.0],
        beat_times=[1.433],
        source_duration_sec=7200.0,
        frame_rate=30.0,
    )

    assert result.source_start_sec == pytest.approx(6902.567)
    assert result.source_shift_sec == pytest.approx(0.0)


@pytest.mark.parametrize("source_duration_sec", [8.132, 8.13])
def test_choose_source_window_distinguishes_float_tail_error_from_overflow(
    source_duration_sec,
) -> None:
    def choose():
        return choose_source_window(
            original_start_sec=3.799,
            clip_duration_sec=4.333,
            output_start_sec=0.0,
            internal_source_cuts_sec=[4.6656, 6.61545],
            beat_times=[1.0, 3.0],
            source_duration_sec=source_duration_sec,
            frame_rate=30.0,
        )

    if source_duration_sec == 8.132:
        result = choose()
        assert result.source_start_sec == 3.799
        assert result.source_shift_sec == 0.0
    else:
        with pytest.raises(ValueError, match="No forward source-window search range"):
            choose()


def test_choose_source_window_relaxes_edge_constraint_when_strict_search_is_impossible() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=2.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[10.4, 11.0, 11.6],
        candidate_source_cuts_sec=[10.4, 11.0, 11.6, 12.4, 13.0, 13.6],
        beat_times=[0.5, 1.0, 1.5],
        source_duration_sec=60.0,
        frame_rate=10.0,
    )

    assert result.source_start_sec >= 10.0
    assert result.fallback_level > 0
    assert result.effective_min_boundary_distance_sec < 1.0


def test_choose_source_window_accounts_for_cuts_entering_shifted_window() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[12.0],
        candidate_source_cuts_sec=[9.5, 12.0, 14.5],
        beat_times=[2.0],
        source_duration_sec=60.0,
        frame_rate=10.0,
    )

    assert result.source_start_sec == pytest.approx(10.0)
    assert result.internal_source_cuts_sec == pytest.approx((12.0,))


def test_choose_source_window_without_cuts_preserves_range() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[],
        beat_times=[1.0, 2.0],
        source_duration_sec=60.0,
        frame_rate=30.0,
    )

    assert result.source_start_sec == 10.0
    assert result.max_beat_distance_sec == 0.0


def test_edge_constraint_is_enforced_without_audio_beats() -> None:
    result = choose_source_window(
        original_start_sec=10.0,
        clip_duration_sec=4.0,
        output_start_sec=0.0,
        internal_source_cuts_sec=[10.1, 12.1],
        candidate_source_cuts_sec=[10.1, 12.1, 14.5],
        beat_times=[],
        source_duration_sec=60.0,
        frame_rate=30.0,
    )

    assert result.source_start_sec >= 10.0
    assert all(
        1.0 < cut - result.source_start_sec < 3.0
        for cut in result.internal_source_cuts_sec
    )


def test_parallel_optimization_preserves_script_order(
    monkeypatch,
    tmp_path,
) -> None:
    items = [
        {
            "_id": index,
            "timestamp": f"00:00:{source:02d},000-00:00:{source + 4:02d},000",
            "output_timestamp": f"00:00:{output:02d},000-00:00:{output + 4:02d},000",
            "output_frame_range": [output * 10, (output + 4) * 10],
        }
        for index, (source, output) in enumerate(((10, 0), (20, 4), (30, 8)), start=1)
    ]
    video_description = {
        "source": {"duration_sec": 60.0, "fps": 10.0},
        "segments": [],
    }
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: (
            (12.0, 22.0, 32.0),
            10.0,
            60.0,
        ),
    )

    optimized = optimize_script_source_windows(
        tmp_path / "source.mp4",
        tmp_path / "segments",
        items,
        beat_times=[2.0, 6.0, 10.0],
        video_description=video_description,
        output_fps=10,
        detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(max_workers=3),
    )

    assert [item["_id"] for item in optimized] == [1, 2, 3]
    assert all(item["cut_optimization"]["max_beat_distance_sec"] == 0.0 for item in optimized)


def test_trajectory_clips_are_beat_optimized_without_losing_identity(
    monkeypatch,
    tmp_path,
) -> None:
    items = [
        {
            "slot_id": "slot_01",
            "group_id": "group_001",
            "trajectory_id": "group_001_trajectory_01",
            "candidate_id": "candidate_01",
            "timestamp": "00:00:10,000-00:00:14,000",
            "output_frame_range": [0, 40],
        },
        {
            "slot_id": "slot_02",
            "group_id": "group_001",
            "trajectory_id": "group_001_trajectory_01",
            "candidate_id": "candidate_02",
            "timestamp": "00:00:14,000-00:00:18,000",
            "output_frame_range": [40, 80],
        },
    ]

    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: (
            (11.2, 12.4, 15.2, 16.4),
            10.0,
            60.0,
        ),
    )

    optimized = optimize_script_source_windows(
        tmp_path / "source.mp4",
        tmp_path / "segments",
        items,
        beat_times=[1.0, 2.0, 3.0, 5.0, 6.0, 7.0],
        video_description={},
        output_fps=10,
        detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(),
    )

    first_start, first_end = (
        parse_range(optimized[0]["timestamp"])
    )
    second_start, _ = parse_range(optimized[1]["timestamp"])

    assert 10.0 < first_start <= 12.0
    assert first_end - first_start == pytest.approx(4.0)
    assert 14.0 < second_start <= 16.0
    assert [item["candidate_id"] for item in optimized] == [
        "candidate_01",
        "candidate_02",
    ]
    assert [item["group_id"] for item in optimized] == [
        "group_001",
        "group_001",
    ]
    assert [item["trajectory_id"] for item in optimized] == [
        "group_001_trajectory_01",
        "group_001_trajectory_01",
    ]
    assert optimized[0]["cut_optimization"]["mode"] == "beat_optimized"
    assert optimized[0]["cut_optimization"]["source_shift_sec"] > 0.0
    second_optimization = optimized[1]["cut_optimization"]
    assert 0.0 < second_optimization["source_shift_sec"] <= 2.0
    assert "semantic_shift_cap_sec" not in second_optimization
    assert "visual_guard_start_sec" not in second_optimization
    assert all(
        item["cut_optimization"].get("mode") != "trajectory_locked"
        for item in optimized
    )


def test_dialogue_anchor_is_the_only_trajectory_clip_that_stays_locked(
    monkeypatch,
    tmp_path,
) -> None:
    item = {
        "slot_id": "slot_01",
        "group_id": "anchor_group_001",
        "trajectory_id": "anchor_001",
        "candidate_id": "anchor_candidate_01",
        "timestamp": "00:00:10,000-00:00:12,000",
        "output_frame_range": [0, 20],
        "dialogue_anchor": {"anchor_id": "anchor_001"},
    }
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: ((11.0,), 10.0, 60.0),
    )

    optimized = optimize_script_source_windows(
        tmp_path / "source.mp4",
        tmp_path / "segments",
        [item],
        beat_times=[1.0],
        video_description={},
        output_fps=10,
        detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(),
    )

    assert optimized[0]["timestamp"] == item["timestamp"]
    assert optimized[0]["group_id"] == item["group_id"]
    assert optimized[0]["trajectory_id"] == item["trajectory_id"]
    assert optimized[0]["cut_optimization"] == {
        "mode": "dialogue_anchor_locked",
        "source_shift_sec": 0.0,
        "num_internal_cuts": 0,
        "fallback_level": 0,
        "max_beat_distance_sec": 0.0,
    }


def test_last_trajectory_clip_can_move_past_its_source_segment_end(
    monkeypatch,
    tmp_path,
) -> None:
    item = {
        "slot_id": "slot_01",
        "group_id": "group_001",
        "trajectory_id": "trajectory_001",
        "candidate_id": "candidate_01",
        "source_segment_id": "segment_0001",
        "timestamp": "00:00:16,000-00:00:20,000",
        "output_frame_range": [0, 40],
    }
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: ((17.2, 18.4), 10.0, 60.0),
    )

    optimized = optimize_script_source_windows(
        tmp_path / "source.mp4",
        tmp_path / "segments",
        [item],
        beat_times=[1.0, 2.0, 3.0],
        video_description={
            "source": {"duration_sec": 60.0, "fps": 10.0},
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": 20.0},
                    "shots": [],
                }
            ],
        },
        output_fps=10,
        detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(),
    )

    assert optimized[0]["timestamp"] != item["timestamp"]
    assert 0.0 < optimized[0]["cut_optimization"]["source_shift_sec"] <= 2.0


def test_trajectory_clip_can_move_into_an_unverified_shot(
    monkeypatch,
    tmp_path,
) -> None:
    item = {
        "slot_id": "slot_01",
        "group_id": "group_001",
        "trajectory_id": "trajectory_001",
        "candidate_id": "candidate_01",
        "source_segment_id": "segment_0001",
        "source_shot_ids": ["shot_0001"],
        "timestamp": "00:00:16,000-00:00:20,000",
        "output_frame_range": [0, 40],
    }
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: ((17.2, 18.4), 10.0, 60.0),
    )

    optimized = optimize_script_source_windows(
        tmp_path / "source.mp4",
        tmp_path / "segments",
        [item],
        beat_times=[1.0, 2.0, 3.0],
        video_description={
            "source": {"duration_sec": 60.0, "fps": 10.0},
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": 30.0},
                    "shots": [
                        {
                            "shot_id": "shot_0001",
                            "time_range": {"start_sec": 0.0, "end_sec": 20.0},
                        },
                        {
                            "shot_id": "shot_0002",
                            "time_range": {"start_sec": 20.0, "end_sec": 30.0},
                        },
                    ],
                }
            ],
        },
        output_fps=10,
        detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(),
    )

    assert optimized[0]["timestamp"] != item["timestamp"]
    assert 0.0 < optimized[0]["cut_optimization"]["source_shift_sec"] <= 2.0


def test_trajectory_clip_can_move_past_its_planning_segment_end(
    monkeypatch,
    tmp_path,
) -> None:
    item = {
        "slot_id": "slot_01",
        "group_id": "group_001",
        "trajectory_id": "trajectory_001",
        "candidate_id": "candidate_01",
        "planning_segment_end_ms": 20_000,
        "source_segment_id": "segment_0001",
        "source_shot_ids": ["shot_0001"],
        "timestamp": "00:00:16,000-00:00:20,000",
        "output_frame_range": [0, 40],
    }
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: ((17.2, 18.4), 10.0, 60.0),
    )

    optimized = optimize_script_source_windows(
        tmp_path / "source.mp4",
        tmp_path / "segments",
        [item],
        beat_times=[1.0, 2.0, 3.0],
        video_description={
            "source": {"duration_sec": 60.0, "fps": 10.0},
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": 30.0},
                    "shots": [
                        {
                            "shot_id": "shot_0001",
                            "time_range": {"start_sec": 0.0, "end_sec": 30.0},
                        }
                    ],
                }
            ],
        },
        output_fps=10,
        detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(),
    )

    assert optimized[0]["timestamp"] != item["timestamp"]
    assert 0.0 < optimized[0]["cut_optimization"]["source_shift_sec"] <= 2.0


def test_cutless_used_segment_preserves_source_window(
    monkeypatch,
    tmp_path,
) -> None:
    items = [
        {
            "timestamp": "00:00:10,000-00:00:14,000",
            "output_frame_range": [0, 40],
        }
    ]
    video_description = {
        "source": {"duration_sec": 60.0, "fps": 10.0},
        "segments": [],
    }
    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer._detect_used_segment_cuts",
        lambda *_args, **_kwargs: ((), 10.0, 60.0),
    )

    optimized = optimize_script_source_windows(
        tmp_path / "source.mp4",
        tmp_path / "segments",
        items,
        beat_times=[1.0, 2.0, 3.0],
        video_description=video_description,
        output_fps=10,
        detection_config=ShotDetectionConfig(),
        optimization_config=SourceWindowOptimizationConfig(),
    )

    assert optimized[0]["timestamp"] == "00:00:10,000-00:00:14,000"
    assert optimized[0]["cut_optimization"]["num_internal_cuts"] == 0


def test_used_segment_detection_skips_anchor_and_unused_segments(
    monkeypatch,
    tmp_path,
) -> None:
    segments = []
    segment_cache_directory = tmp_path / "segments"
    segment_cache_directory.mkdir()
    for index, (start, end) in enumerate(
        ((0.0, 10.0), (10.0, 20.0), (20.0, 30.0)),
        1,
    ):
        clip_path = segment_cache_directory / f"segment_{index:04d}.mp4"
        clip_path.write_bytes(b"cached")
        segments.append(
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {"start_sec": start, "end_sec": end},
            }
        )
    video_description = {
        "source": {"duration_sec": 30.0, "fps": 30.0},
        "segments": segments,
    }
    calls = []

    def fake_detect(clip_path, start_sec, end_sec, **kwargs):
        calls.append((clip_path.name, start_sec, end_sec, kwargs))
        return [3.5], 30.0

    monkeypatch.setattr(
        "cutmaster.workflow.planners.tools.source_window_optimizer.detect_source_cuts",
        fake_detect,
    )

    cuts, frame_rate, source_duration = _detect_used_segment_cuts(
        tmp_path / "source.mp4",
        segment_cache_directory,
        [
            {
                "timestamp": "00:00:12,000-00:00:16,000",
            },
            {
                "timestamp": "00:00:22,000-00:00:26,000",
                "dialogue_anchor": {"anchor_id": "anchor_01"},
            },
        ],
        video_description,
        ShotDetectionConfig(
            adaptive_threshold=3.0,
            adaptive_min_content_val=12.0,
            adaptive_min_scene_len_sec=0.4,
            duplicate_frame_threshold=2.0,
        ),
        SourceWindowOptimizationConfig(search_margin_sec=2.0),
    )

    assert cuts == (13.5,)
    assert frame_rate == 30.0
    assert source_duration == 30.0
    assert len(calls) == 1
    clip_name, start_sec, end_sec, kwargs = calls[0]
    assert clip_name == "segment_0002.mp4"
    assert start_sec == pytest.approx(2.0)
    assert end_sec == pytest.approx(8.0)
    assert kwargs == {
        "adaptive_threshold": 3.0,
        "adaptive_min_content_val": 12.0,
        "adaptive_min_scene_len_sec": 0.4,
        "duplicate_frame_threshold": 2.0,
    }
