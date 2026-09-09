"""Handle-only requests for reusable video and music analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialType
from cutmaster.workflow.contracts.material import (
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
    MaterialRuntimeHandle,
)


ANALYSIS_RESULT_SCHEMA_VERSION = "3.0"
VIDEO_ANALYSIS_NODE_IDS = (
    "shot_detection",
    "dialogue_preparation",
    "scene_segmentation",
    "segment_clip_preparation",
    "shot_annotation",
    "segment_summarization",
    "video_summary",
)
MUSIC_ANALYSIS_NODE_IDS = ("music_analysis",)
_VIDEO_ANALYSIS_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "material_id",
        "material_type",
        "material_name",
        "material_fingerprint",
        "memory_schema_version",
        "elapsed_sec",
        "material_reused",
        "analysis_reused",
        "model_usage_summary",
        "model_usage_cumulative_summary",
    }
)
_MUSIC_ANALYSIS_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "material_id",
        "material_type",
        "material_name",
        "material_fingerprint",
        "memory_schema_version",
        "elapsed_sec",
        "material_reused",
        "analysis_reused",
    }
)


def _read_result_document(
    path: Path,
    label: str,
    expected_fields: frozenset[str],
) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    if value.get("schema_version") != ANALYSIS_RESULT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported {label.lower()} schema: "
            f"{value.get('schema_version')!r}"
        )
    if set(value) != expected_fields:
        raise ValueError(f"{label} fields do not match schema 3.0")
    return value


def _require_result_material(
    value: dict[str, Any],
    material: MaterialRuntimeHandle,
    expected_type: MaterialType,
) -> None:
    expected = {
        "material_id": str(material.material_id),
        "material_type": expected_type.value,
        "material_name": material.material_name,
        "material_fingerprint": str(material.expected_fingerprint),
    }
    if material.material_type is not expected_type or any(
        value.get(key) != expected_value
        for key, expected_value in expected.items()
    ):
        raise ValueError("Analysis result does not match its active Material")


def _result_material_id(
    path: Path,
    expected_type: MaterialType,
    expected_fields: frozenset[str],
) -> MaterialId:
    value = _read_result_document(
        path,
        f"{expected_type.value} analysis result",
        expected_fields,
    )
    if value.get("material_type") != expected_type.value:
        raise ValueError(
            f"Analysis result is not for a {expected_type.value} Material"
        )
    return MaterialId.parse(value["material_id"])


@dataclass(frozen=True)
class AnalysisWorkspace:
    """Application-allocated workspace for one Analyser invocation."""

    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("Analysis workspace root must be a pathlib.Path")
        if not self.root.is_absolute():
            raise ValueError("Analysis workspace root must be an absolute path")


@dataclass(frozen=True)
class VideoAnalysisOptions:
    video_title: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.video_title, str):
            raise TypeError("video_title must be a string")


@dataclass(frozen=True)
class MusicAnalysisOptions:
    """Reserved for transport-neutral Music Memory controls."""


@dataclass(frozen=True)
class AnalyseVideoRequest:
    material: MaterialRuntimeHandle
    options: VideoAnalysisOptions
    workspace: AnalysisWorkspace

    def __post_init__(self) -> None:
        if not isinstance(self.material, MaterialRuntimeHandle):
            raise TypeError("material must be a MaterialRuntimeHandle")
        if not isinstance(self.options, VideoAnalysisOptions):
            raise TypeError("options must be VideoAnalysisOptions")
        if not isinstance(self.workspace, AnalysisWorkspace):
            raise TypeError("workspace must be AnalysisWorkspace")
        if self.material.material_type is not MaterialType.VIDEO:
            raise ValueError("Video analysis requires a video Material")


@dataclass(frozen=True)
class AnalyseMusicRequest:
    material: MaterialRuntimeHandle
    options: MusicAnalysisOptions
    workspace: AnalysisWorkspace

    def __post_init__(self) -> None:
        if not isinstance(self.material, MaterialRuntimeHandle):
            raise TypeError("material must be a MaterialRuntimeHandle")
        if not isinstance(self.options, MusicAnalysisOptions):
            raise TypeError("options must be MusicAnalysisOptions")
        if not isinstance(self.workspace, AnalysisWorkspace):
            raise TypeError("workspace must be AnalysisWorkspace")
        if self.material.material_type is not MaterialType.MUSIC:
            raise ValueError("Music analysis requires a music Material")


@dataclass(frozen=True)
class VideoAnalysisResult:
    status: str
    video: AnalysedVideoRuntimeHandle
    source_srt_path: Path
    processed_subtitle_path: Path
    analysis_history_path: Path
    elapsed_sec: float
    material_reused: bool = False
    analysis_reused: bool = False
    model_usage_path: Path | None = None
    model_usage_summary: dict[str, Any] = field(default_factory=dict)
    model_usage_cumulative_summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status != "success":
            raise ValueError("Video analysis result must be successful")
        if not isinstance(self.video, AnalysedVideoRuntimeHandle):
            raise TypeError("video must be an AnalysedVideoRuntimeHandle")

    def to_dict(self) -> dict[str, Any]:
        material = self.video.material
        return {
            "schema_version": ANALYSIS_RESULT_SCHEMA_VERSION,
            "status": self.status,
            "material_id": str(material.material_id),
            "material_type": material.material_type.value,
            "material_name": material.material_name,
            "material_fingerprint": str(material.expected_fingerprint),
            "memory_schema_version": self.video.memory_schema_version,
            "elapsed_sec": self.elapsed_sec,
            "material_reused": self.material_reused,
            "analysis_reused": self.analysis_reused,
            "model_usage_summary": self.model_usage_summary,
            "model_usage_cumulative_summary": (
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
    def material_id(cls, path: Path) -> MaterialId:
        return _result_material_id(
            path,
            MaterialType.VIDEO,
            _VIDEO_ANALYSIS_RESULT_FIELDS,
        )

    @classmethod
    def read(
        cls,
        path: Path,
        material: MaterialRuntimeHandle,
    ) -> "VideoAnalysisResult":
        value = _read_result_document(
            path,
            "Video analysis result",
            _VIDEO_ANALYSIS_RESULT_FIELDS,
        )
        _require_result_material(value, material, MaterialType.VIDEO)
        memory_root = material.memory_root
        model_usage_path = memory_root / "model_usage.json"
        return cls(
            status=value["status"],
            video=AnalysedVideoRuntimeHandle(
                material=material,
                memory_schema_version=value["memory_schema_version"],
                video_description_path=memory_root / "video_description.json",
                video_summary_path=memory_root / "video_summary.json",
                dialogues_path=memory_root / "dialogues.json",
            ),
            source_srt_path=memory_root / "source.srt",
            processed_subtitle_path=memory_root / "dialogue_merged.srt",
            analysis_history_path=memory_root / "analysis_history.json",
            elapsed_sec=float(value["elapsed_sec"]),
            material_reused=bool(value.get("material_reused")),
            analysis_reused=bool(value.get("analysis_reused")),
            model_usage_path=(
                model_usage_path if model_usage_path.is_file() else None
            ),
            model_usage_summary=dict(value.get("model_usage_summary") or {}),
            model_usage_cumulative_summary=dict(
                value.get("model_usage_cumulative_summary") or {}
            ),
        )


@dataclass(frozen=True)
class MusicAnalysisResult:
    status: str
    music: AnalysedMusicRuntimeHandle
    elapsed_sec: float
    material_reused: bool = False
    analysis_reused: bool = False

    def __post_init__(self) -> None:
        if self.status != "success":
            raise ValueError("Music analysis result must be successful")
        if not isinstance(self.music, AnalysedMusicRuntimeHandle):
            raise TypeError("music must be an AnalysedMusicRuntimeHandle")

    def to_dict(self) -> dict[str, Any]:
        material = self.music.material
        return {
            "schema_version": ANALYSIS_RESULT_SCHEMA_VERSION,
            "status": self.status,
            "material_id": str(material.material_id),
            "material_type": material.material_type.value,
            "material_name": material.material_name,
            "material_fingerprint": str(material.expected_fingerprint),
            "memory_schema_version": self.music.memory_schema_version,
            "elapsed_sec": self.elapsed_sec,
            "material_reused": self.material_reused,
            "analysis_reused": self.analysis_reused,
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def material_id(cls, path: Path) -> MaterialId:
        return _result_material_id(
            path,
            MaterialType.MUSIC,
            _MUSIC_ANALYSIS_RESULT_FIELDS,
        )

    @classmethod
    def read(
        cls,
        path: Path,
        material: MaterialRuntimeHandle,
    ) -> "MusicAnalysisResult":
        value = _read_result_document(
            path,
            "Music analysis result",
            _MUSIC_ANALYSIS_RESULT_FIELDS,
        )
        _require_result_material(value, material, MaterialType.MUSIC)
        return cls(
            status=value["status"],
            music=AnalysedMusicRuntimeHandle(
                material=material,
                memory_schema_version=value["memory_schema_version"],
                music_memory_path=material.memory_root / "music_memory.json",
            ),
            elapsed_sec=float(value["elapsed_sec"]),
            material_reused=bool(value.get("material_reused")),
            analysis_reused=bool(value.get("analysis_reused")),
        )


__all__ = [
    "ANALYSIS_RESULT_SCHEMA_VERSION",
    "AnalyseMusicRequest",
    "AnalyseVideoRequest",
    "AnalysisWorkspace",
    "MUSIC_ANALYSIS_NODE_IDS",
    "MusicAnalysisResult",
    "MusicAnalysisOptions",
    "VideoAnalysisResult",
    "VideoAnalysisOptions",
    "VIDEO_ANALYSIS_NODE_IDS",
]
