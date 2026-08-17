from pathlib import Path

import numpy as np
import pytest

from cutmaster.workflow.analyser.tools.music_analysis import (
    MUSIC_MEMORY_FIELDS,
    MUSIC_MEMORY_SCHEMA_VERSION,
    _duration_range,
    _normalize,
    _section_boundaries,
    analyze_music_memory,
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


def test_analyze_music_memory_uses_the_complete_source_track(monkeypatch) -> None:
    samples = np.ones(32, dtype=np.float32)
    feature_values = np.array([0.1, 0.3, 0.8, 0.2], dtype=np.float32)
    inspected_samples: list[np.ndarray] = []

    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.load",
        lambda *_args, **_kwargs: (samples, 4),
    )
    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.get_duration",
        lambda **_kwargs: 8.0,
    )

    def fake_rms(**kwargs):
        inspected_samples.append(kwargs["y"])
        return np.array([feature_values])

    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.feature.rms",
        fake_rms,
    )

    def fake_onset(**kwargs):
        inspected_samples.append(kwargs["y"])
        if kwargs["hop_length"] == 2:
            return np.array([0.0, 1.0, 0.0, 3.0], dtype=np.float32)
        return feature_values

    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.onset.onset_strength",
        fake_onset,
    )

    def fake_centroid(**kwargs):
        inspected_samples.append(kwargs["y"])
        return np.array([feature_values])

    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.feature.spectral_centroid",
        fake_centroid,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.beat.beat_track",
        lambda **_kwargs: (np.array([120.0]), np.array([1, 3])),
    )
    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.frames_to_time",
        lambda *_args, **_kwargs: np.array([1.0, 7.0]),
    )

    memory = analyze_music_memory(
        Path("complete-song.mp3"),
        sample_rate=4,
        hop_length=2,
        energy_step_sec=1.0,
    )

    assert inspected_samples
    assert all(value is samples for value in inspected_samples)
    assert set(memory) == MUSIC_MEMORY_FIELDS
    assert memory["schema_version"] == MUSIC_MEMORY_SCHEMA_VERSION
    assert "audio_path" not in memory
    assert memory["source_duration_sec"] == 8.0
    assert memory["beats_sec"] == [1.0, 7.0]
    assert memory["accents_sec"] == [7.0]
    assert memory["sections"][-1]["end_sec"] == 8.0
    assert "planned_duration_sec" not in memory


def test_analyze_music_memory_rejects_empty_audio(monkeypatch) -> None:
    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.music_analysis.librosa.load",
        lambda *_args, **_kwargs: (np.array([], dtype=np.float32), 22050),
    )

    with pytest.raises(ValueError, match="contains no audio samples"):
        analyze_music_memory(Path("empty.mp3"))
