"""Build reusable, edit-independent memory for a complete music track."""

from __future__ import annotations

import json
import math
import os
import tempfile
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


def _section_boundaries(
    energy: np.ndarray,
    step_sec: float,
    duration_sec: float,
) -> list[float]:
    if duration_sec <= 8.0 or energy.size < 4:
        return [0.0, duration_sec]
    desired = max(2, min(10, round(duration_sec / 8.0)))
    novelty = np.abs(np.diff(energy, prepend=energy[0]))
    candidates = sorted(
        range(1, len(novelty)),
        key=lambda index: float(novelty[index]),
        reverse=True,
    )
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
    return [
        round(max(1.5, center - 0.7), 2),
        round(min(6.5, center + 0.7), 2),
    ]


def analyze_music_memory(
    audio_path: Path,
    *,
    sample_rate: int = 22050,
    hop_length: int = 512,
    energy_step_sec: float = 0.5,
) -> dict[str, Any]:
    """Analyse the complete source track without any edit-duration input."""
    try:
        samples, sr = librosa.load(audio_path, sr=sample_rate, mono=True)
    except Exception as exc:
        raise RuntimeError(f"Could not decode music material: {audio_path}") from exc
    if samples.size == 0:
        raise ValueError(f"Music material contains no audio samples: {audio_path}")

    source_duration = float(librosa.get_duration(y=samples, sr=sr))
    if source_duration <= 0.0:
        raise ValueError(f"Music material has no positive duration: {audio_path}")

    frame_length = max(512, int(energy_step_sec * sr))
    feature_hop = frame_length
    rms = librosa.feature.rms(
        y=samples,
        frame_length=frame_length,
        hop_length=feature_hop,
    )[0]
    onset = librosa.onset.onset_strength(
        y=samples,
        sr=sr,
        hop_length=feature_hop,
    )
    centroid = librosa.feature.spectral_centroid(
        y=samples,
        sr=sr,
        n_fft=frame_length,
        hop_length=feature_hop,
    )[0]
    feature_count = min(len(rms), len(onset), len(centroid))
    energy = (
        0.5 * _normalize(rms[:feature_count])
        + 0.35 * _normalize(onset[:feature_count])
        + 0.15 * _normalize(centroid[:feature_count])
    )
    if len(energy) >= 3:
        energy = np.convolve(energy, np.ones(3) / 3.0, mode="same")

    fine_onset = librosa.onset.onset_strength(
        y=samples,
        sr=sr,
        hop_length=hop_length,
    )
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=fine_onset,
        sr=sr,
        hop_length=hop_length,
        units="frames",
        trim=False,
    )
    source_beats = librosa.frames_to_time(
        beat_frames,
        sr=sr,
        hop_length=hop_length,
    )
    beat_indices = np.asarray(beat_frames, dtype=int)
    beat_strengths = fine_onset[beat_indices] if beat_indices.size else np.array([])
    accent_threshold = (
        float(np.percentile(beat_strengths, 70))
        if beat_strengths.size
        else math.inf
    )
    source_accents = [
        float(time)
        for time, strength in zip(source_beats, beat_strengths, strict=True)
        if float(strength) >= accent_threshold
    ]

    curve = [
        {
            "time_sec": round(index * energy_step_sec, 3),
            "energy": round(float(value), 4),
        }
        for index, value in enumerate(energy)
        if index * energy_step_sec < source_duration
    ]
    boundaries = _section_boundaries(energy, energy_step_sec, source_duration)
    sections: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(
        zip(boundaries[:-1], boundaries[1:], strict=True)
    ):
        start_i = min(len(energy), int(start / energy_step_sec))
        end_i = min(
            len(energy),
            max(start_i + 1, int(math.ceil(end / energy_step_sec))),
        )
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
                "energy_trend": (
                    "rising"
                    if trend > 0.08
                    else "falling"
                    if trend < -0.08
                    else "stable"
                ),
                "suggested_clip_duration_sec": _duration_range(mean),
            }
        )

    return {
        "schema_version": "1.0",
        "audio_path": str(audio_path),
        "source_duration_sec": round(source_duration, 3),
        "tempo_bpm": round(float(np.asarray(tempo).reshape(-1)[0]), 3),
        "beats_sec": [round(float(value), 6) for value in source_beats],
        "accents_sec": [round(value, 6) for value in source_accents],
        "energy_step_sec": energy_step_sec,
        "energy_curve": curve,
        "sections": sections,
    }


def write_music_memory(path: Path, memory: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(memory, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


__all__ = [
    "analyze_music_memory",
    "write_music_memory",
]
