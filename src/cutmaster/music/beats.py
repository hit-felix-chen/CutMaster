from __future__ import annotations

import math
from pathlib import Path

import librosa


def detect_beats(
    audio_path: Path,
    duration_sec: float,
    *,
    sample_rate: int = 22050,
    hop_length: int = 512,
) -> list[float]:
    """Track musical beats and repeat them when the BGM loops."""
    try:
        samples, actual_sample_rate = librosa.load(
            audio_path,
            sr=sample_rate,
            mono=True,
        )
    except Exception as exc:
        raise RuntimeError(f"Could not decode BGM for beat detection: {audio_path}") from exc
    if samples.size == 0:
        raise ValueError(f"BGM contains no audio samples: {audio_path}")

    onset_envelope = librosa.onset.onset_strength(
        y=samples,
        sr=actual_sample_rate,
        hop_length=hop_length,
    )
    _, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_envelope,
        sr=actual_sample_rate,
        hop_length=hop_length,
        units="frames",
        trim=False,
    )
    source_beats = librosa.frames_to_time(
        beat_frames,
        sr=actual_sample_rate,
        hop_length=hop_length,
    ).tolist()
    if not source_beats:
        raise ValueError(f"Could not detect audio beats: {audio_path}")

    source_duration = librosa.get_duration(y=samples, sr=actual_sample_rate)
    repeats = max(1, math.ceil(duration_sec / source_duration))
    return [
        beat + repeat * source_duration
        for repeat in range(repeats)
        for beat in source_beats
        if beat + repeat * source_duration < duration_sec
    ]
