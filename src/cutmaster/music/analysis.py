from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import librosa
import numpy as np


def _normalize(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values.astype(float)
    low, high = np.percentile(values, [5, 95])
    if high <= low + 1e-9:
        return np.zeros_like(values, dtype=float)
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def _section_boundaries(energy: np.ndarray, step_sec: float, duration_sec: float) -> list[float]:
    if duration_sec <= 8.0 or energy.size < 4:
        return [0.0, duration_sec]
    desired = max(2, min(10, round(duration_sec / 8.0)))
    novelty = np.abs(np.diff(energy, prepend=energy[0]))
    candidates = sorted(range(1, len(novelty)), key=lambda i: float(novelty[i]), reverse=True)
    selected: list[float] = []
    for index in candidates:
        boundary = index * step_sec
        if boundary < 4.0 or duration_sec - boundary < 4.0:
            continue
        if any(abs(boundary - other) < 4.0 for other in selected):
            continue
        selected.append(boundary)
        if len(selected) >= desired - 1:
            break
    return [0.0, *sorted(selected), duration_sec]


def _role(index: int, count: int, mean_energy: float, trend: float) -> str:
    if index == 0:
        return "intro"
    if index == count - 1:
        return "outro"
    if mean_energy >= 0.72:
        return "climax"
    if trend >= 0.12:
        return "build"
    if trend <= -0.12:
        return "release"
    return "development"


def _duration_range(energy: float) -> list[float]:
    center = 6.0 - 3.8 * energy
    return [round(max(1.5, center - 0.7), 2), round(min(6.5, center + 0.7), 2)]


def analyze_music(
    audio_path: Path,
    duration_sec: float,
    *,
    sample_rate: int = 22050,
    hop_length: int = 512,
    energy_step_sec: float = 0.5,
) -> dict[str, Any]:
    """Create a repeat-aware music profile for semantic and kinetic planning."""
    samples, sr = librosa.load(audio_path, sr=sample_rate, mono=True)
    if samples.size == 0:
        raise ValueError(f"BGM contains no audio samples: {audio_path}")
    source_duration = float(librosa.get_duration(y=samples, sr=sr))
    analysis_duration = min(source_duration, duration_sec)
    samples = samples[: int(analysis_duration * sr)]

    frame_length = max(512, int(energy_step_sec * sr))
    feature_hop = frame_length
    rms = librosa.feature.rms(y=samples, frame_length=frame_length, hop_length=feature_hop)[0]
    onset = librosa.onset.onset_strength(y=samples, sr=sr, hop_length=feature_hop)
    centroid = librosa.feature.spectral_centroid(
        y=samples,
        sr=sr,
        n_fft=frame_length,
        hop_length=feature_hop,
    )[0]
    n = min(len(rms), len(onset), len(centroid))
    energy = 0.5 * _normalize(rms[:n]) + 0.35 * _normalize(onset[:n]) + 0.15 * _normalize(centroid[:n])
    if len(energy) >= 3:
        energy = np.convolve(energy, np.ones(3) / 3.0, mode="same")

    fine_onset = librosa.onset.onset_strength(y=samples, sr=sr, hop_length=hop_length)
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=fine_onset,
        sr=sr,
        hop_length=hop_length,
        units="frames",
        trim=False,
    )
    source_beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop_length)
    beat_strengths = fine_onset[np.asarray(beat_frames, dtype=int)] if len(beat_frames) else np.array([])
    accent_threshold = float(np.percentile(beat_strengths, 70)) if beat_strengths.size else math.inf
    source_accents = [
        float(time)
        for time, strength in zip(source_beats, beat_strengths, strict=True)
        if float(strength) >= accent_threshold
    ]
    repeats = max(1, math.ceil(duration_sec / source_duration))
    beats = [
        float(beat + repeat * source_duration)
        for repeat in range(repeats)
        for beat in source_beats
        if beat + repeat * source_duration < duration_sec
    ]
    accents = [
        float(accent + repeat * source_duration)
        for repeat in range(repeats)
        for accent in source_accents
        if accent + repeat * source_duration < duration_sec
    ]

    curve = [
        {"time_sec": round(i * energy_step_sec, 3), "energy": round(float(value), 4)}
        for i, value in enumerate(energy)
        if i * energy_step_sec < analysis_duration
    ]
    boundaries = _section_boundaries(energy, energy_step_sec, analysis_duration)
    sections: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:], strict=True)):
        start_i = min(len(energy), int(start / energy_step_sec))
        end_i = min(len(energy), max(start_i + 1, int(math.ceil(end / energy_step_sec))))
        values = energy[start_i:end_i]
        mean = float(values.mean()) if values.size else 0.0
        trend = float(values[-1] - values[0]) if values.size > 1 else 0.0
        sections.append(
            {
                "section_id": f"music_{index + 1:02d}",
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "role": _role(index, len(boundaries) - 1, mean, trend),
                "mean_energy": round(mean, 4),
                "energy_trend": "rising" if trend > 0.08 else "falling" if trend < -0.08 else "stable",
                "suggested_clip_duration_sec": _duration_range(mean),
            }
        )

    return {
        "schema_version": "1.0",
        "audio_path": str(audio_path),
        "source_duration_sec": round(source_duration, 3),
        "planned_duration_sec": round(duration_sec, 3),
        "tempo_bpm": round(float(np.asarray(tempo).reshape(-1)[0]), 3),
        "beats_sec": [round(value, 6) for value in beats],
        "accents_sec": [round(value, 6) for value in accents],
        "energy_step_sec": energy_step_sec,
        "energy_curve": curve,
        "sections": sections,
    }


def write_music_profile(path: Path, profile: dict[str, Any]) -> None:
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
