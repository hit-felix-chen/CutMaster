from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import librosa

from cutmaster.analyser.tools.music_analysis import analyze_music_memory


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
        raise RuntimeError(
            f"Could not decode BGM for beat detection: {audio_path}"
        ) from exc
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


def _project_times(
    source_values: list[Any],
    source_duration_sec: float,
    target_duration_sec: float,
) -> list[float]:
    repeats = max(1, math.ceil(target_duration_sec / source_duration_sec))
    return [
        round(source_time + repeat * source_duration_sec, 6)
        for repeat in range(repeats)
        for raw_value in source_values
        if 0.0 <= (source_time := float(raw_value)) < source_duration_sec
        and source_time + repeat * source_duration_sec < target_duration_sec
    ]


def _project_energy_curve(
    source_curve: list[dict[str, Any]],
    source_duration_sec: float,
    target_duration_sec: float,
) -> list[dict[str, Any]]:
    repeats = max(1, math.ceil(target_duration_sec / source_duration_sec))
    return [
        {
            **point,
            "time_sec": round(source_time + repeat * source_duration_sec, 3),
        }
        for repeat in range(repeats)
        for point in source_curve
        if 0.0 <= (source_time := float(point["time_sec"])) < source_duration_sec
        and source_time + repeat * source_duration_sec < target_duration_sec
    ]


def _project_sections(
    source_sections: list[dict[str, Any]],
    source_duration_sec: float,
    target_duration_sec: float,
) -> list[dict[str, Any]]:
    repeats = max(1, math.ceil(target_duration_sec / source_duration_sec))
    projected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for repeat in range(repeats):
        offset = repeat * source_duration_sec
        for section in source_sections:
            source_start = max(0.0, float(section["start_sec"]))
            source_end = min(source_duration_sec, float(section["end_sec"]))
            start = source_start + offset
            end = min(source_end + offset, target_duration_sec)
            if end <= start or start >= target_duration_sec:
                continue

            base_id = str(section["section_id"])
            section_id = base_id if repeat == 0 else f"{base_id}__loop_{repeat + 1:02d}"
            if section_id in used_ids:
                suffix = 2
                candidate = f"{section_id}__{suffix}"
                while candidate in used_ids:
                    suffix += 1
                    candidate = f"{section_id}__{suffix}"
                section_id = candidate
            used_ids.add(section_id)
            projected.append(
                {
                    **section,
                    "section_id": section_id,
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                }
            )
    return projected


def project_music_profile(
    music_memory: dict[str, Any],
    target_duration_sec: float,
) -> dict[str, Any]:
    """Project complete-track Music Memory onto one ASTER run duration."""
    if target_duration_sec <= 0.0:
        raise ValueError("Music Profile target duration must be positive")
    source_duration_sec = float(music_memory["source_duration_sec"])
    if source_duration_sec <= 0.0:
        raise ValueError("Music Memory source duration must be positive")

    return {
        **music_memory,
        "planned_duration_sec": round(target_duration_sec, 3),
        "beats_sec": _project_times(
            list(music_memory["beats_sec"]),
            source_duration_sec,
            target_duration_sec,
        ),
        "accents_sec": _project_times(
            list(music_memory["accents_sec"]),
            source_duration_sec,
            target_duration_sec,
        ),
        "energy_curve": _project_energy_curve(
            list(music_memory["energy_curve"]),
            source_duration_sec,
            target_duration_sec,
        ),
        "sections": _project_sections(
            list(music_memory["sections"]),
            source_duration_sec,
            target_duration_sec,
        ),
    }


def build_music_profile(
    music_memory: dict[str, Any],
    target_duration_sec: float,
) -> dict[str, Any]:
    """Build an ASTER run profile from reusable Music Memory."""
    return project_music_profile(music_memory, target_duration_sec)


def analyze_music(
    audio_path: Path,
    duration_sec: float,
    *,
    sample_rate: int = 22050,
    hop_length: int = 512,
    energy_step_sec: float = 0.5,
) -> dict[str, Any]:
    """Compatibility wrapper that analyses a track and immediately projects it."""
    memory = analyze_music_memory(
        audio_path,
        sample_rate=sample_rate,
        hop_length=hop_length,
        energy_step_sec=energy_step_sec,
    )
    return project_music_profile(memory, duration_sec)


def compact_music_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Keep only the macro musical structure useful to model reasoning."""
    return {
        "planned_duration_sec": float(profile["planned_duration_sec"]),
        "tempo_bpm": float(profile["tempo_bpm"]),
        "sections": [
            {
                "section_id": str(section["section_id"]),
                "start_sec": float(section["start_sec"]),
                "end_sec": float(section["end_sec"]),
                "role": str(section["role"]),
                "mean_energy": float(section["mean_energy"]),
                "energy_trend": str(section["energy_trend"]),
                "suggested_clip_duration_sec": [
                    float(value)
                    for value in section["suggested_clip_duration_sec"]
                ],
            }
            for section in profile["sections"]
        ],
    }


def write_music_profile(path: Path, profile: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "analyze_music",
    "build_music_profile",
    "compact_music_profile",
    "detect_beats",
    "project_music_profile",
    "write_music_profile",
]
