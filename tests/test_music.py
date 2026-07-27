import numpy as np

from cutmaster.music.analysis import (
    _duration_range,
    _normalize,
    _section_boundaries,
    compact_music_profile,
)


def test_music_energy_helpers() -> None:
    normalized = _normalize(np.array([0.0, 1.0, 2.0]))
    assert normalized[0] == 0.0
    assert normalized[-1] == 1.0
    assert _duration_range(0.9)[1] < _duration_range(0.1)[0]
    boundaries = _section_boundaries(
        np.array([0.1] * 10 + [0.9] * 10 + [0.2] * 10),
        0.5,
        15.0,
    )
    assert boundaries[0] == 0.0
    assert boundaries[-1] == 15.0
    assert boundaries == sorted(boundaries)


def test_compact_music_profile_keeps_only_macro_planning_context() -> None:
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
