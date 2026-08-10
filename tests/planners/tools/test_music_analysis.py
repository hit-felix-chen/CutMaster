from copy import deepcopy
from pathlib import Path

import cutmaster.planners.tools.music_analysis as music_analysis
from cutmaster.planners.tools.music_analysis import (
    analyze_music,
    build_music_profile,
    compact_music_profile,
    project_music_profile,
)


def _music_memory() -> dict:
    return {
        "schema_version": "1.0",
        "audio_path": "/music/song.mp3",
        "source_duration_sec": 4.0,
        "tempo_bpm": 120.0,
        "beats_sec": [1.0, 3.0],
        "accents_sec": [3.0],
        "energy_step_sec": 2.0,
        "energy_curve": [
            {"time_sec": 0.0, "energy": 0.2},
            {"time_sec": 2.0, "energy": 0.8},
        ],
        "sections": [
            {
                "section_id": "intro",
                "start_sec": 0.0,
                "end_sec": 2.0,
                "role": "intro",
                "mean_energy": 0.2,
                "energy_trend": "rising",
                "suggested_clip_duration_sec": [4.0, 5.0],
            },
            {
                "section_id": "outro",
                "start_sec": 2.0,
                "end_sec": 4.0,
                "role": "outro",
                "mean_energy": 0.8,
                "energy_trend": "falling",
                "suggested_clip_duration_sec": [2.0, 3.0],
            },
        ],
    }


def test_project_music_profile_truncates_memory_without_mutating_it() -> None:
    memory = _music_memory()
    original = deepcopy(memory)

    profile = project_music_profile(memory, 2.5)

    assert memory == original
    assert profile["planned_duration_sec"] == 2.5
    assert profile["beats_sec"] == [1.0]
    assert profile["accents_sec"] == []
    assert profile["energy_curve"] == [
        {"time_sec": 0.0, "energy": 0.2},
        {"time_sec": 2.0, "energy": 0.8},
    ]
    assert [
        (section["start_sec"], section["end_sec"])
        for section in profile["sections"]
    ] == [
        (0.0, 2.0),
        (2.0, 2.5),
    ]


def test_project_music_profile_loops_all_timeline_features() -> None:
    profile = build_music_profile(_music_memory(), 9.0)

    assert profile["beats_sec"] == [1.0, 3.0, 5.0, 7.0]
    assert profile["accents_sec"] == [3.0, 7.0]
    assert [point["time_sec"] for point in profile["energy_curve"]] == [
        0.0,
        2.0,
        4.0,
        6.0,
        8.0,
    ]
    assert [
        (section["start_sec"], section["end_sec"])
        for section in profile["sections"]
    ] == [
        (0.0, 2.0),
        (2.0, 4.0),
        (4.0, 6.0),
        (6.0, 8.0),
        (8.0, 9.0),
    ]
    section_ids = [section["section_id"] for section in profile["sections"]]
    assert len(section_ids) == len(set(section_ids))


def test_analyze_music_remains_a_compatible_analyse_then_project_wrapper(
    monkeypatch,
) -> None:
    memory = _music_memory()
    calls: list[tuple[Path, dict]] = []

    def fake_analyze(path: Path, **kwargs):
        calls.append((path, kwargs))
        return memory

    monkeypatch.setattr(music_analysis, "analyze_music_memory", fake_analyze)

    profile = analyze_music(
        Path("song.mp3"),
        2.5,
        sample_rate=16000,
        hop_length=256,
        energy_step_sec=0.25,
    )

    assert calls == [
        (
            Path("song.mp3"),
            {
                "sample_rate": 16000,
                "hop_length": 256,
                "energy_step_sec": 0.25,
            },
        )
    ]
    assert profile["planned_duration_sec"] == 2.5


def test_compact_music_profile_keeps_only_macro_edit_context() -> None:
    compact = compact_music_profile(
        {
            "schema_version": "1.0",
            "audio_path": "/unused/music.mp3",
            "source_duration_sec": 96.0,
            "planned_duration_sec": 60.0,
            "tempo_bpm": 123.0,
            "beats_sec": [0.1, 0.6],
            "accents_sec": [0.1],
            "energy_step_sec": 0.5,
            "energy_curve": [{"time_sec": 0.0, "energy": 0.4}],
            "sections": [
                {
                    "section_id": "music_01",
                    "start_sec": 0.0,
                    "end_sec": 8.0,
                    "role": "intro",
                    "mean_energy": 0.4,
                    "energy_trend": "rising",
                    "suggested_clip_duration_sec": [3.5, 4.9],
                }
            ],
        }
    )

    assert compact == {
        "planned_duration_sec": 60.0,
        "tempo_bpm": 123.0,
        "sections": [
            {
                "section_id": "music_01",
                "start_sec": 0.0,
                "end_sec": 8.0,
                "role": "intro",
                "mean_energy": 0.4,
                "energy_trend": "rising",
                "suggested_clip_duration_sec": [3.5, 4.9],
            }
        ],
    }
