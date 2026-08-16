"""Handle-only request contracts for deterministic rendering."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath
from typing import Literal

from cutmaster.workflow.contracts.material import RenderRuntimeBindings
from cutmaster.workflow.contracts.render_plan import (
    SUPPORTED_RENDER_PLAN_SCHEMA_VERSIONS,
    RenderPlan,
)


AudioMode = Literal["bgm_only", "dialogue"]


@dataclass(frozen=True)
class RenderOptions:
    audio_mode: AudioMode = "dialogue"

    def __post_init__(self) -> None:
        if not isinstance(self.audio_mode, str):
            raise TypeError("Audio mode must be a string")
        if self.audio_mode not in {"bgm_only", "dialogue"}:
            raise ValueError(f"Unsupported audio mode: {self.audio_mode}")


@dataclass(frozen=True)
class RenderOutputTarget:
    """Application-owned destination without overwrite policy or allocation."""

    root: Path
    output_filename: str = "output.mp4"

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("Render output root must be a pathlib.Path")
        if not self.root.is_absolute():
            raise ValueError("Render output root must be an absolute path")
        if not isinstance(self.output_filename, str):
            raise TypeError("Render output filename must be a string")
        filename = PurePath(self.output_filename)
        if (
            not self.output_filename
            or "/" in self.output_filename
            or "\\" in self.output_filename
            or filename.is_absolute()
            or len(filename.parts) != 1
            or self.output_filename in {".", ".."}
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.output_filename
            )
        ):
            raise ValueError("Render output filename must be one file name")


@dataclass(frozen=True)
class RenderRequest:
    plan: RenderPlan
    bindings: RenderRuntimeBindings
    options: RenderOptions
    output_target: RenderOutputTarget

    def __post_init__(self) -> None:
        if not isinstance(self.plan, RenderPlan):
            raise TypeError("plan must be a RenderPlan")
        if not isinstance(self.bindings, RenderRuntimeBindings):
            raise TypeError("bindings must be RenderRuntimeBindings")
        if not isinstance(self.options, RenderOptions):
            raise TypeError("options must be RenderOptions")
        if not isinstance(self.output_target, RenderOutputTarget):
            raise TypeError("output_target must be RenderOutputTarget")
        if self.plan.schema_version not in SUPPORTED_RENDER_PLAN_SCHEMA_VERSIONS:
            raise ValueError(
                "Renderer requires a supported portable RenderPlan v2"
            )
        if self.plan.video_material_id != self.bindings.video.material_id:
            raise ValueError("RenderPlan video Material does not match its binding")
        if self.plan.music_material_id != self.bindings.music.material_id:
            raise ValueError("RenderPlan music Material does not match its binding")
        if (
            self.plan.video_expected_fingerprint
            != self.bindings.video.expected_fingerprint
        ):
            raise ValueError("RenderPlan video fingerprint does not match its binding")
        if (
            self.plan.music_expected_fingerprint
            != self.bindings.music.expected_fingerprint
        ):
            raise ValueError("RenderPlan music fingerprint does not match its binding")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "2.0",
            "plan_id": self.plan.plan_id,
            "video_material_id": str(self.bindings.video.material_id),
            "music_material_id": str(self.bindings.music.material_id),
            "audio_mode": self.options.audio_mode,
            "output_root": str(self.output_target.root),
            "output_filename": self.output_target.output_filename,
        }


@dataclass(frozen=True)
class RenderResult:
    status: str
    plan_id: str
    render_id: str
    audio_mode: AudioMode
    output_video: Path
    montage_video: Path
    duration_sec: float
    frames: int
    montage_reused: bool
    dialogue_audio_reused: bool
    stage_timings_sec: dict[str, float]
    wall_clock_sec: float
    schema_version: str = "2.0"

    def __post_init__(self) -> None:
        if self.status != "success":
            raise ValueError("Render result must be successful")

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["output_video"] = str(self.output_video)
        value["montage_video"] = str(self.montage_video)
        return value

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path) -> "RenderResult":
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "2.0":
            raise ValueError(
                f"Unsupported Render result schema: {value.get('schema_version')!r}"
            )
        return cls(
            status=value["status"],
            plan_id=value["plan_id"],
            render_id=value["render_id"],
            audio_mode=value["audio_mode"],
            output_video=Path(value["output_video"]),
            montage_video=Path(value["montage_video"]),
            duration_sec=float(value["duration_sec"]),
            frames=int(value["frames"]),
            montage_reused=bool(value["montage_reused"]),
            dialogue_audio_reused=bool(value["dialogue_audio_reused"]),
            stage_timings_sec=dict(value["stage_timings_sec"]),
            wall_clock_sec=float(value["wall_clock_sec"]),
            schema_version=value["schema_version"],
        )


__all__ = [
    "AudioMode",
    "RenderOptions",
    "RenderOutputTarget",
    "RenderRequest",
    "RenderResult",
]
