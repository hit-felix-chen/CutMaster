import pytest

from cutmaster.planners.tools.plan_compiler import (
    adapt_script,
    align_cut_boundaries,
    script_duration,
)
from cutmaster.timecode import format_time, parse_range, parse_time


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


def test_align_cut_boundaries_honors_explicit_clip_cap() -> None:
    with pytest.raises(ValueError, match="No audio beat"):
        align_cut_boundaries([4.0], [3.9, 4.1], 8.0, max_clip_duration_sec=4.0)
