"""Portable, identity-bound render plan exchanged by Planners and Renderer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint


SUPPORTED_RENDER_PLAN_SCHEMA_VERSIONS = frozenset({"2.0"})


_RUNTIME_MEDIA_KEYS = frozenset(
    {
        "source_video",
        "background_music",
        "video_path",
        "audio_path",
        "clip_path",
        "prepared_audio_path",
    }
)


def _parse_timestamp_range(value: str) -> tuple[float, float]:
    """Parse the canonical HH:MM:SS,mmm-HH:MM:SS,mmm plan range.

    Contracts keep this small syntax check local instead of depending on
    executable workflow helpers.
    """

    separator = " --> " if " --> " in value else "-"
    parts = value.split(separator)
    if len(parts) != 2:
        raise ValueError(f"Invalid timestamp range: {value!r}")

    def parse_time(item: str) -> float:
        fields = item.strip().replace(".", ",").split(":")
        if len(fields) != 3 or "," not in fields[2]:
            raise ValueError(f"Invalid timestamp: {item!r}")
        seconds, milliseconds = fields[2].split(",", 1)
        if len(milliseconds) != 3:
            raise ValueError(f"Invalid timestamp: {item!r}")
        try:
            return (
                int(fields[0]) * 3600
                + int(fields[1]) * 60
                + int(seconds)
                + int(milliseconds) / 1000
            )
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp: {item!r}") from exc

    return parse_time(parts[0]), parse_time(parts[1])


def _reject_runtime_media_paths(value: Any, location: str = "RenderPlan") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if key_text == "prepared_audio_path":
                raise ValueError(
                    "Portable RenderPlans must not contain prepared audio paths"
                )
            if key_text in _RUNTIME_MEDIA_KEYS or key_text.endswith("_media_path"):
                raise ValueError(
                    f"Portable {location} must not contain runtime media field "
                    f"{key_text!r}"
                )
            _reject_runtime_media_paths(item, f"{location}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_runtime_media_paths(item, f"{location}[{index}]")


@dataclass(frozen=True)
class RenderPlan:
    plan_id: str
    video_material_id: MaterialId
    video_expected_fingerprint: MaterialFingerprint
    music_material_id: MaterialId
    music_expected_fingerprint: MaterialFingerprint
    fps: int
    total_frames: int
    duration_sec: float
    clips: tuple[dict[str, Any], ...]
    dialogue_anchors: tuple[dict[str, Any], ...]
    planners_metadata: dict[str, Any]
    schema_version: str = "2.0"

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_RENDER_PLAN_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported render plan schema: {self.schema_version}"
            )
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ValueError("RenderPlan plan_id must not be empty")
        if not isinstance(self.video_material_id, MaterialId):
            raise TypeError("video_material_id must be a MaterialId")
        if not isinstance(self.music_material_id, MaterialId):
            raise TypeError("music_material_id must be a MaterialId")
        if not isinstance(self.video_expected_fingerprint, MaterialFingerprint):
            raise TypeError(
                "video_expected_fingerprint must be a MaterialFingerprint"
            )
        if not isinstance(self.music_expected_fingerprint, MaterialFingerprint):
            raise TypeError(
                "music_expected_fingerprint must be a MaterialFingerprint"
            )
        if isinstance(self.fps, bool) or not isinstance(self.fps, int) or self.fps <= 0:
            raise ValueError("RenderPlan fps must be a positive integer")
        if (
            isinstance(self.total_frames, bool)
            or not isinstance(self.total_frames, int)
            or self.total_frames <= 0
        ):
            raise ValueError("RenderPlan total_frames must be a positive integer")
        if not isinstance(self.duration_sec, (int, float)) or not isfinite(
            float(self.duration_sec)
        ):
            raise ValueError("RenderPlan duration must be finite")
        if not self.clips:
            raise ValueError("RenderPlan must contain clips")
        _reject_runtime_media_paths(self.clips, "RenderPlan.clips")
        _reject_runtime_media_paths(
            self.planners_metadata,
            "RenderPlan.planners_metadata",
        )
        expected_start = 0
        anchors: list[dict[str, Any]] = []
        trajectory_contract: bool | None = None
        current_group_id: str | None = None
        closed_group_ids: set[str] = set()
        trajectory_by_group: dict[str, str] = {}
        candidate_ids: set[str] = set()
        previous_source_end: float | None = None
        for index, clip in enumerate(self.clips, start=1):
            if not isinstance(clip, dict):
                raise TypeError(f"Render clip {index} must be an object")
            output_range = clip.get("output_frame_range")
            if not isinstance(output_range, list) or len(output_range) != 2:
                raise ValueError(f"Render clip {index} has no output frame range")
            start, end = map(int, output_range)
            if start != expected_start or end <= start:
                raise ValueError(
                    f"Render clip {index} creates a timeline gap or overlap"
                )
            source_start, source_end = _parse_timestamp_range(
                str(clip.get("timestamp") or "")
            )
            if source_end <= source_start:
                raise ValueError(f"Render clip {index} has an invalid source range")
            group_id = clip.get("group_id")
            trajectory_id = clip.get("trajectory_id")
            has_group = isinstance(group_id, str) and bool(group_id.strip())
            has_trajectory = isinstance(trajectory_id, str) and bool(
                trajectory_id.strip()
            )
            uses_trajectory_contract = has_group or has_trajectory
            if trajectory_contract is None:
                trajectory_contract = uses_trajectory_contract
            elif trajectory_contract != uses_trajectory_contract:
                raise ValueError(
                    "RenderPlan must not mix trajectory-bound and legacy clips"
                )
            if uses_trajectory_contract:
                slot_id = clip.get("slot_id")
                candidate_id = clip.get("candidate_id")
                if (
                    not has_group
                    or not has_trajectory
                    or not isinstance(slot_id, str)
                    or not slot_id.strip()
                    or not isinstance(candidate_id, str)
                    or not candidate_id.strip()
                ):
                    raise ValueError(
                        f"Render clip {index} must carry complete trajectory identity"
                    )
                normalized_group_id = group_id.strip()
                normalized_trajectory_id = trajectory_id.strip()
                normalized_candidate_id = candidate_id.strip()
                if normalized_group_id != current_group_id:
                    if current_group_id is not None:
                        closed_group_ids.add(current_group_id)
                    if normalized_group_id in closed_group_ids:
                        raise ValueError(
                            f"RenderPlan Group {normalized_group_id} is not contiguous"
                        )
                    current_group_id = normalized_group_id
                selected = trajectory_by_group.setdefault(
                    normalized_group_id,
                    normalized_trajectory_id,
                )
                if selected != normalized_trajectory_id:
                    raise ValueError(
                        f"RenderPlan Group {normalized_group_id} mixes trajectories"
                    )
                if normalized_candidate_id in candidate_ids:
                    raise ValueError(
                        f"RenderPlan contains duplicate Candidate {normalized_candidate_id}"
                    )
                candidate_ids.add(normalized_candidate_id)
                if previous_source_end is not None and source_start < previous_source_end:
                    raise ValueError(
                        f"Render clip {index} creates a source overlap or reversal"
                    )
                previous_source_end = source_end
            anchor = clip.get("dialogue_anchor")
            if anchor is not None:
                if not isinstance(anchor, dict):
                    raise TypeError(f"Render clip {index} dialogue anchor must be an object")
                if "prepared_audio_path" in anchor:
                    raise ValueError(
                        "Portable RenderPlans must not contain prepared audio paths"
                    )
                anchors.append(dict(anchor))
            expected_start = end
        if expected_start != self.total_frames:
            raise ValueError("RenderPlan total_frames does not match its clips")
        if abs(float(self.duration_sec) - self.total_frames / self.fps) > 1e-6:
            raise ValueError("RenderPlan duration is inconsistent with its frame grid")
        if tuple(anchors) != tuple(self.dialogue_anchors):
            raise ValueError("RenderPlan dialogue_anchors do not match its clips")
        object.__setattr__(self, "duration_sec", float(self.duration_sec))
        object.__setattr__(self, "clips", tuple(dict(item) for item in self.clips))
        object.__setattr__(
            self,
            "dialogue_anchors",
            tuple(dict(item) for item in self.dialogue_anchors),
        )
        object.__setattr__(self, "planners_metadata", dict(self.planners_metadata))

    @classmethod
    def create(
        cls,
        *,
        video_material_id: MaterialId,
        video_expected_fingerprint: MaterialFingerprint,
        music_material_id: MaterialId,
        music_expected_fingerprint: MaterialFingerprint,
        fps: int,
        clips: list[dict[str, Any]],
        planners_metadata: dict[str, Any],
    ) -> "RenderPlan":
        anchors = tuple(
            dict(item["dialogue_anchor"])
            for item in clips
            if item.get("dialogue_anchor") is not None
        )
        total_frames = int(clips[-1]["output_frame_range"][1]) if clips else 0
        identity_payload = {
            "video_material_id": str(video_material_id),
            "video_expected_fingerprint": str(video_expected_fingerprint),
            "music_material_id": str(music_material_id),
            "music_expected_fingerprint": str(music_expected_fingerprint),
            "fps": fps,
            "total_frames": total_frames,
            "clips": clips,
            "dialogue_anchors": anchors,
            "planners_metadata": planners_metadata,
        }
        canonical = json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            plan_id=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
            video_material_id=video_material_id,
            video_expected_fingerprint=video_expected_fingerprint,
            music_material_id=music_material_id,
            music_expected_fingerprint=music_expected_fingerprint,
            fps=fps,
            total_frames=total_frames,
            duration_sec=total_frames / fps if fps else 0.0,
            clips=tuple(dict(item) for item in clips),
            dialogue_anchors=anchors,
            planners_metadata=dict(planners_metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "video_material_id": str(self.video_material_id),
            "video_expected_fingerprint": str(self.video_expected_fingerprint),
            "music_material_id": str(self.music_material_id),
            "music_expected_fingerprint": str(self.music_expected_fingerprint),
            "fps": self.fps,
            "total_frames": self.total_frames,
            "duration_sec": self.duration_sec,
            "clips": [dict(item) for item in self.clips],
            "dialogue_anchors": [dict(item) for item in self.dialogue_anchors],
            "planners_metadata": dict(self.planners_metadata),
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path) -> "RenderPlan":
        value = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RenderPlan":
        if not isinstance(value, dict):
            raise TypeError("RenderPlan payload must be an object")
        return cls(
            schema_version=value.get("schema_version", ""),
            plan_id=value["plan_id"],
            video_material_id=MaterialId.parse(value["video_material_id"]),
            video_expected_fingerprint=MaterialFingerprint(
                value["video_expected_fingerprint"]
            ),
            music_material_id=MaterialId.parse(value["music_material_id"]),
            music_expected_fingerprint=MaterialFingerprint(
                value["music_expected_fingerprint"]
            ),
            fps=value["fps"],
            total_frames=value["total_frames"],
            duration_sec=value["duration_sec"],
            clips=tuple(value["clips"]),
            dialogue_anchors=tuple(value["dialogue_anchors"]),
            planners_metadata=dict(value["planners_metadata"]),
        )


__all__ = [
    "RenderPlan",
    "SUPPORTED_RENDER_PLAN_SCHEMA_VERSIONS",
]
