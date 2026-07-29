from pathlib import Path

import numpy as np
import pytest

from cutmaster.planners.tools.music_analysis import detect_beats


def test_detect_beats_uses_librosa_tracker_and_repeats_bgm(monkeypatch) -> None:
    samples = np.ones(22050 * 2, dtype=np.float32)
    onset_envelope = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr("cutmaster.planners.tools.music_analysis.librosa.load", lambda *_args, **_kwargs: (samples, 22050))
    monkeypatch.setattr(
        "cutmaster.planners.tools.music_analysis.librosa.onset.onset_strength",
        lambda **_kwargs: onset_envelope,
    )

    def fake_track(**kwargs):
        assert kwargs["onset_envelope"] is onset_envelope
        assert kwargs["units"] == "frames"
        assert kwargs["trim"] is False
        return 120.0, np.array([10, 30])

    monkeypatch.setattr("cutmaster.planners.tools.music_analysis.librosa.beat.beat_track", fake_track)
    monkeypatch.setattr(
        "cutmaster.planners.tools.music_analysis.librosa.frames_to_time",
        lambda *_args, **_kwargs: np.array([0.5, 1.5]),
    )
    monkeypatch.setattr("cutmaster.planners.tools.music_analysis.librosa.get_duration", lambda **_kwargs: 2.0)

    beats = detect_beats(Path("music.mp3"), duration_sec=5.0)

    assert beats == pytest.approx([0.5, 1.5, 2.5, 3.5, 4.5])


def test_detect_beats_rejects_audio_without_tracked_beats(monkeypatch) -> None:
    monkeypatch.setattr(
        "cutmaster.planners.tools.music_analysis.librosa.load",
        lambda *_args, **_kwargs: (np.ones(22050, dtype=np.float32), 22050),
    )
    monkeypatch.setattr(
        "cutmaster.planners.tools.music_analysis.librosa.onset.onset_strength",
        lambda **_kwargs: np.ones(10, dtype=np.float32),
    )
    monkeypatch.setattr(
        "cutmaster.planners.tools.music_analysis.librosa.beat.beat_track",
        lambda **_kwargs: (0.0, np.array([], dtype=int)),
    )
    monkeypatch.setattr(
        "cutmaster.planners.tools.music_analysis.librosa.frames_to_time",
        lambda *_args, **_kwargs: np.array([], dtype=float),
    )

    with pytest.raises(ValueError, match="Could not detect audio beats"):
        detect_beats(Path("silent.mp3"), duration_sec=5.0)
