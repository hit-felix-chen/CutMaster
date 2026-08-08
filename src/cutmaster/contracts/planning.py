"""Contracts emitted by planning and consumed by rendering."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cutmaster.runtime.media_probe import media_duration
from cutmaster.timecode import parse_range


@dataclass(frozen=True)
class PlanningRequest:
    video_path: Path
    audio_path: Path
    prompt: str
    output_dir: Path
    target_output_length_sec: float = 60.0
    target_shot_length_sec: float = 4.0
    prompt_type: str = "event"
    video_title: str = ""
    max_clip_duration_sec: float | None = None
    overwrite: bool = False


@dataclass(frozen=True)
class MediaReference:
    path: str
    size_bytes: int
    mtime_ns: int
    duration_sec: float

    @classmethod
    def from_path(cls, path: Path) -> "MediaReference":
        resolved = path.resolve()
        stat = resolved.stat()
        return cls(
            path=str(resolved),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            duration_sec=media_duration(resolved),
        )

    def validate(self) -> None:
        path = Path(self.path)
        if not path.is_file():
            raise FileNotFoundError(f"Planned media is missing: {path}")
        stat = path.stat()
        if stat.st_size != self.size_bytes or stat.st_mtime_ns != self.mtime_ns:
            raise ValueError(f"Planned media has changed since planning: {path}")


@dataclass(frozen=True)
class RenderPlan:
    plan_id: str
    source_video: MediaReference
    background_music: MediaReference
    fps: int
    total_frames: int
    duration_sec: float
    clips: list[dict[str, Any]]
    dialogue_anchors: list[dict[str, Any]]
    planning_metadata: dict[str, Any]
    schema_version: str = "1.0"

    @classmethod
    def create(
        cls,
        *,
        source_video: Path,
        background_music: Path,
        fps: int,
        clips: list[dict[str, Any]],
        planning_metadata: dict[str, Any],
    ) -> "RenderPlan":
        anchors = [
            dict(item["dialogue_anchor"])
            for item in clips
            if item.get("dialogue_anchor") is not None
        ]
        total_frames = int(clips[-1]["output_frame_range"][1]) if clips else 0
        payload = {
            "source_video": asdict(MediaReference.from_path(source_video)),
            "background_music": asdict(MediaReference.from_path(background_music)),
            "fps": fps,
            "total_frames": total_frames,
            "clips": clips,
            "dialogue_anchors": anchors,
            "planning_metadata": planning_metadata,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        plan = cls(
            plan_id=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
            source_video=MediaReference(**payload["source_video"]),
            background_music=MediaReference(**payload["background_music"]),
            fps=fps,
            total_frames=total_frames,
            duration_sec=total_frames / fps if fps else 0.0,
            clips=[dict(item) for item in clips],
            dialogue_anchors=anchors,
            planning_metadata=dict(planning_metadata),
        )
        plan.validate(validate_media=False)
        return plan

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path, *, validate_media: bool = True) -> "RenderPlan":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source_video"] = MediaReference(**data["source_video"])
        data["background_music"] = MediaReference(**data["background_music"])
        plan = cls(**data)
        plan.validate(validate_media=validate_media)
        return plan

    def validate(self, *, validate_media: bool = True) -> None:
        if self.schema_version != "1.0":
            raise ValueError(f"Unsupported render plan schema: {self.schema_version}")
        if self.fps <= 0 or self.total_frames <= 0 or not self.clips:
            raise ValueError("Render plan must contain a positive frame timeline")
        expected_start = 0
        for index, clip in enumerate(self.clips, start=1):
            output_range = clip.get("output_frame_range")
            if not isinstance(output_range, list) or len(output_range) != 2:
                raise ValueError(f"Render clip {index} has no output frame range")
            start, end = map(int, output_range)
            if start != expected_start or end <= start:
                raise ValueError(f"Render clip {index} creates a timeline gap or overlap")
            source_start, source_end = parse_range(str(clip.get("timestamp") or ""))
            if source_end <= source_start:
                raise ValueError(f"Render clip {index} has an invalid source range")
            anchor = clip.get("dialogue_anchor")
            if anchor is not None and "prepared_audio_path" in anchor:
                raise ValueError("Render plans must not contain prepared audio paths")
            expected_start = end
        if expected_start != self.total_frames:
            raise ValueError("Render plan total_frames does not match its clips")
        if abs(self.duration_sec - self.total_frames / self.fps) > 1e-6:
            raise ValueError("Render plan duration is inconsistent with its frame grid")
        if validate_media:
            self.source_video.validate()
            self.background_music.validate()


@dataclass(frozen=True)
class PlanningResult:
    status: str
    render_plan: str
    music_profile: str
    edit_plan: str
    dialogue_anchors: str
    candidate_pool: str
    raw_script: str
    selection_diagnostics: str
    planning_history: str
    planning_calls: str
    target_output_length_sec: float
    planned_output_length_sec: float
    num_raw_clips: int
    num_planned_clips: int
    stage_timings_sec: dict[str, float]
    wall_clock_sec: float
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path) -> "PlanningResult":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


__all__ = [
    "MediaReference",
    "PlanningRequest",
    "PlanningResult",
    "RenderPlan",
]
