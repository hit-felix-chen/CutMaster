"""Transport-neutral contracts for the ASTER Planners stage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any

from cutmaster.workflow.contracts.material import (
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
)
from cutmaster.workflow.contracts.render_plan import RenderPlan


def _finite_positive(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field_name} must be finite and positive") from exc
    if not isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return normalized


@dataclass(frozen=True)
class PlannersBrief:
    editing_intent: str
    target_duration_sec: float
    anchor_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_enabled, bool):
            raise TypeError("anchor_enabled must be a boolean")
        if not isinstance(self.editing_intent, str):
            raise TypeError("Editing Intent must be a string")
        if not self.editing_intent.strip():
            raise ValueError("Editing Intent must not be empty")
        object.__setattr__(
            self,
            "target_duration_sec",
            _finite_positive(self.target_duration_sec, "Target Duration"),
        )


@dataclass(frozen=True)
class PlannersOptions:
    """Advanced compatibility controls kept outside the Creative Brief."""

    target_shot_length_sec: float = 4.0
    prompt_type: str = "event"
    video_title: str = ""
    max_clip_duration_sec: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_shot_length_sec",
            _finite_positive(
                self.target_shot_length_sec,
                "Target Shot Duration",
            ),
        )
        if not isinstance(self.prompt_type, str):
            raise TypeError("Prompt type must be a string")
        if not self.prompt_type.strip():
            raise ValueError("Prompt type must not be empty")
        if not isinstance(self.video_title, str):
            raise TypeError("Video Title must be a string")
        if self.max_clip_duration_sec is not None:
            object.__setattr__(
                self,
                "max_clip_duration_sec",
                _finite_positive(
                    self.max_clip_duration_sec,
                    "Maximum Clip Duration",
                ),
            )


@dataclass(frozen=True)
class PlannersWorkspace:
    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("Planners workspace root must be a pathlib.Path")
        if not self.root.is_absolute():
            raise ValueError("Planners workspace root must be an absolute path")


@dataclass(frozen=True)
class PlannersRequest:
    video: AnalysedVideoRuntimeHandle
    music: AnalysedMusicRuntimeHandle
    brief: PlannersBrief
    options: PlannersOptions
    workspace: PlannersWorkspace

    def __post_init__(self) -> None:
        if not isinstance(self.video, AnalysedVideoRuntimeHandle):
            raise TypeError("video must be an AnalysedVideoRuntimeHandle")
        if not isinstance(self.music, AnalysedMusicRuntimeHandle):
            raise TypeError("music must be an AnalysedMusicRuntimeHandle")
        if not isinstance(self.brief, PlannersBrief):
            raise TypeError("brief must be a PlannersBrief")
        if not isinstance(self.options, PlannersOptions):
            raise TypeError("options must be PlannersOptions")
        if not isinstance(self.workspace, PlannersWorkspace):
            raise TypeError("workspace must be PlannersWorkspace")

    @property
    def video_path(self) -> Path:
        """Verified runtime source path; never serialized into RenderPlan."""

        return self.video.material.source_path

    @property
    def audio_path(self) -> Path:
        """Verified runtime music path; never serialized into RenderPlan."""

        return self.music.material.source_path

    @property
    def output_dir(self) -> Path:
        return self.workspace.root

    @property
    def prompt(self) -> str:
        return self.brief.editing_intent

    @property
    def target_output_length_sec(self) -> float:
        return self.brief.target_duration_sec

    @property
    def target_shot_length_sec(self) -> float:
        return self.options.target_shot_length_sec

    @property
    def prompt_type(self) -> str:
        return self.options.prompt_type

    @property
    def video_title(self) -> str:
        return self.options.video_title

    @property
    def max_clip_duration_sec(self) -> float | None:
        return self.options.max_clip_duration_sec

    @property
    def video_material_name(self) -> str:
        return self.video.material.material_name

    @property
    def music_material_name(self) -> str:
        return self.music.material.material_name


@dataclass(frozen=True)
class PlannersResult:
    status: str
    render_plan: RenderPlan
    render_plan_path: Path
    music_profile_path: Path
    edit_plan_path: Path
    planning_segments_path: Path
    planning_groups_path: Path
    dialogue_anchors_path: Path
    candidate_pool_path: Path
    raw_script_path: Path
    selection_diagnostics_path: Path
    planners_history_path: Path
    planners_calls_path: Path
    target_output_length_sec: float
    planned_output_length_sec: float
    num_raw_clips: int
    num_planned_clips: int
    stage_timings_sec: dict[str, float]
    wall_clock_sec: float
    music_memory_path: Path | None = None
    model_usage_path: Path | None = None
    model_usage_summary: dict[str, Any] = field(default_factory=dict)
    model_usage_cumulative_summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status != "success":
            raise ValueError("Planners result must be successful")
        if not isinstance(self.render_plan, RenderPlan):
            raise TypeError("render_plan must be a RenderPlan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "status": self.status,
            "render_plan": str(self.render_plan_path),
            "plan_id": self.render_plan.plan_id,
            "music_profile": str(self.music_profile_path),
            "edit_plan": str(self.edit_plan_path),
            "planning_segments": str(self.planning_segments_path),
            "planning_groups": str(self.planning_groups_path),
            "dialogue_anchors": str(self.dialogue_anchors_path),
            "candidate_pool": str(self.candidate_pool_path),
            "raw_script": str(self.raw_script_path),
            "selection_diagnostics": str(self.selection_diagnostics_path),
            "planners_history": str(self.planners_history_path),
            "planners_calls": str(self.planners_calls_path),
            "target_output_length_sec": self.target_output_length_sec,
            "planned_output_length_sec": self.planned_output_length_sec,
            "num_raw_clips": self.num_raw_clips,
            "num_planned_clips": self.num_planned_clips,
            "stage_timings_sec": dict(self.stage_timings_sec),
            "wall_clock_sec": self.wall_clock_sec,
            "music_memory": (
                str(self.music_memory_path)
                if self.music_memory_path is not None
                else None
            ),
            "model_usage": (
                str(self.model_usage_path)
                if self.model_usage_path is not None
                else None
            ),
            "model_usage_summary": dict(self.model_usage_summary),
            "model_usage_cumulative_summary": dict(
                self.model_usage_cumulative_summary
            ),
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path) -> "PlannersResult":
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "2.0":
            raise ValueError(
                f"Unsupported Planners result schema: {value.get('schema_version')!r}"
            )
        render_plan_path = Path(value["render_plan"])
        render_plan = RenderPlan.read(render_plan_path)
        if value.get("plan_id") != render_plan.plan_id:
            raise ValueError("Planners result plan_id does not match RenderPlan")
        return cls(
            status=value["status"],
            render_plan=render_plan,
            render_plan_path=render_plan_path,
            music_profile_path=Path(value["music_profile"]),
            edit_plan_path=Path(value["edit_plan"]),
            planning_segments_path=Path(value["planning_segments"]),
            planning_groups_path=Path(value["planning_groups"]),
            dialogue_anchors_path=Path(value["dialogue_anchors"]),
            candidate_pool_path=Path(value["candidate_pool"]),
            raw_script_path=Path(value["raw_script"]),
            selection_diagnostics_path=Path(value["selection_diagnostics"]),
            planners_history_path=Path(value["planners_history"]),
            planners_calls_path=Path(value["planners_calls"]),
            target_output_length_sec=float(value["target_output_length_sec"]),
            planned_output_length_sec=float(value["planned_output_length_sec"]),
            num_raw_clips=int(value["num_raw_clips"]),
            num_planned_clips=int(value["num_planned_clips"]),
            stage_timings_sec=dict(value["stage_timings_sec"]),
            wall_clock_sec=float(value["wall_clock_sec"]),
            music_memory_path=(
                Path(value["music_memory"])
                if value.get("music_memory")
                else None
            ),
            model_usage_path=(
                Path(value["model_usage"])
                if value.get("model_usage")
                else None
            ),
            model_usage_summary=dict(value.get("model_usage_summary") or {}),
            model_usage_cumulative_summary=dict(
                value.get("model_usage_cumulative_summary") or {}
            ),
        )

__all__ = [
    "PlannersRequest",
    "PlannersResult",
    "PlannersBrief",
    "PlannersOptions",
    "PlannersWorkspace",
]
