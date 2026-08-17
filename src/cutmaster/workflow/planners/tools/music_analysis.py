from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


MUSIC_MEMORY_SCHEMA_VERSION = "2.0"
MUSIC_PROFILE_SCHEMA_VERSION = "2.0"
_MUSIC_MEMORY_FIELDS = frozenset(
    {
        "schema_version",
        "source_duration_sec",
        "tempo_bpm",
        "beats_sec",
        "accents_sec",
        "energy_step_sec",
        "energy_curve",
        "sections",
    }
)
_ENERGY_POINT_FIELDS = frozenset({"time_sec", "energy"})
_SECTION_FIELDS = frozenset(
    {
        "section_id",
        "start_sec",
        "end_sec",
        "role",
        "mean_energy",
        "energy_trend",
        "suggested_clip_duration_sec",
    }
)


def _validate_music_memory(memory: dict[str, Any]) -> None:
    if not isinstance(memory, dict):
        raise TypeError("Music Memory must be a mapping")
    if set(memory) != _MUSIC_MEMORY_FIELDS:
        raise ValueError("Music Memory fields do not match schema 2.0")
    if memory.get("schema_version") != MUSIC_MEMORY_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Music Memory schema: {memory.get('schema_version')!r}"
        )
    for field_name in ("beats_sec", "accents_sec", "energy_curve", "sections"):
        if not isinstance(memory[field_name], list):
            raise TypeError(f"Music Memory {field_name} must be a list")
    for point in memory["energy_curve"]:
        if not isinstance(point, dict) or set(point) != _ENERGY_POINT_FIELDS:
            raise ValueError("Music Memory energy point fields do not match schema 2.0")
    for section in memory["sections"]:
        if not isinstance(section, dict) or set(section) != _SECTION_FIELDS:
            raise ValueError("Music Memory section fields do not match schema 2.0")


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
            "time_sec": round(source_time + repeat * source_duration_sec, 3),
            "energy": float(point["energy"]),
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
                    "section_id": section_id,
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                    "role": str(section["role"]),
                    "mean_energy": float(section["mean_energy"]),
                    "energy_trend": str(section["energy_trend"]),
                    "suggested_clip_duration_sec": [
                        float(value)
                        for value in section["suggested_clip_duration_sec"]
                    ],
                }
            )
    return projected


def project_music_profile(
    music_memory: dict[str, Any],
    target_duration_sec: float,
) -> dict[str, Any]:
    """Project complete-track Music Memory onto one ASTER run duration."""
    _validate_music_memory(music_memory)
    if target_duration_sec <= 0.0:
        raise ValueError("Music Profile target duration must be positive")
    source_duration_sec = float(music_memory["source_duration_sec"])
    if source_duration_sec <= 0.0:
        raise ValueError("Music Memory source duration must be positive")

    return {
        "schema_version": MUSIC_PROFILE_SCHEMA_VERSION,
        "source_duration_sec": source_duration_sec,
        "planned_duration_sec": round(target_duration_sec, 3),
        "tempo_bpm": float(music_memory["tempo_bpm"]),
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
        "energy_step_sec": float(music_memory["energy_step_sec"]),
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
    "MUSIC_PROFILE_SCHEMA_VERSION",
    "build_music_profile",
    "compact_music_profile",
    "project_music_profile",
    "write_music_profile",
]
