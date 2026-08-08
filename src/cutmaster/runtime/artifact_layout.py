"""Canonical on-disk layout for one CutMaster workflow run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactLayout:
    """Resolve every stage artifact from a single workflow root."""

    root: Path

    @classmethod
    def create(cls, root: Path) -> "ArtifactLayout":
        layout = cls(root.resolve())
        for directory in (
            layout.root,
            layout.analyser_dir,
            layout.planners_dir,
            layout.planning_diagnostics_dir,
            layout.renderer_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return layout

    @property
    def analyser_dir(self) -> Path:
        return self.root / "analyser"

    @property
    def planners_dir(self) -> Path:
        return self.root / "planners"

    @property
    def planning_diagnostics_dir(self) -> Path:
        return self.planners_dir / "diagnostics"

    @property
    def renderer_dir(self) -> Path:
        return self.root / "renderer"

    @property
    def analysis_result(self) -> Path:
        return self.analyser_dir / "analysis_result.json"

    @property
    def planning_result(self) -> Path:
        return self.planners_dir / "planning_result.json"

    @property
    def render_plan(self) -> Path:
        return self.planners_dir / "render_plan.json"

    @property
    def render_result(self) -> Path:
        return self.renderer_dir / "render_result.json"

    @property
    def output_video(self) -> Path:
        return self.renderer_dir / "output.mp4"

    @property
    def workflow_result(self) -> Path:
        return self.root / "result.json"

    @property
    def workflow_log(self) -> Path:
        return self.root / "cutmaster.log"


__all__ = ["ArtifactLayout"]
