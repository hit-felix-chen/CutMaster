"""Handle-only requests for reusable video and music analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint, MaterialType
from cutmaster.workflow.contracts.material import (
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
    MaterialRuntimeHandle,
)


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
            "schema_version": "2.0",
            "status": self.status,
            "material_id": str(material.material_id),
            "material_type": material.material_type.value,
            "material_name": material.material_name,
            "material_fingerprint": str(material.expected_fingerprint),
            "source_video": str(material.source_path),
            "material_directory": str(material.memory_root),
            "memory_schema_version": self.video.memory_schema_version,
            "source_srt": str(self.source_srt_path),
            "processed_subtitle": str(self.processed_subtitle_path),
            "dialogues_json": str(self.video.dialogues_path),
            "video_description": str(self.video.video_description_path),
            "video_summary": str(self.video.video_summary_path),
            "analysis_history": str(self.analysis_history_path),
            "elapsed_sec": self.elapsed_sec,
            "material_reused": self.material_reused,
            "analysis_reused": self.analysis_reused,
            "model_usage": (
                str(self.model_usage_path) if self.model_usage_path else None
            ),
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
    def read(cls, path: Path) -> "VideoAnalysisResult":
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "2.0":
            raise ValueError(
                f"Unsupported video analysis schema: {value.get('schema_version')!r}"
            )
        material = MaterialRuntimeHandle(
            material_id=MaterialId.parse(value["material_id"]),
            material_type=MaterialType(value["material_type"]),
            material_name=value["material_name"],
            expected_fingerprint=MaterialFingerprint(
                value["material_fingerprint"]
            ),
            source_path=Path(value["source_video"]),
            memory_root=Path(value["material_directory"]),
        )
        return cls(
            status=value["status"],
            video=AnalysedVideoRuntimeHandle(
                material=material,
                memory_schema_version=value["memory_schema_version"],
                video_description_path=Path(value["video_description"]),
                video_summary_path=Path(value["video_summary"]),
                dialogues_path=Path(value["dialogues_json"]),
            ),
            source_srt_path=Path(value["source_srt"]),
            processed_subtitle_path=Path(value["processed_subtitle"]),
            analysis_history_path=Path(value["analysis_history"]),
            elapsed_sec=float(value["elapsed_sec"]),
            material_reused=bool(value.get("material_reused")),
            analysis_reused=bool(value.get("analysis_reused")),
            model_usage_path=(
                Path(value["model_usage"]) if value.get("model_usage") else None
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
            "schema_version": "2.0",
            "status": self.status,
            "material_id": str(material.material_id),
            "material_type": material.material_type.value,
            "material_name": material.material_name,
            "material_fingerprint": str(material.expected_fingerprint),
            "source_audio": str(material.source_path),
            "material_directory": str(material.memory_root),
            "memory_schema_version": self.music.memory_schema_version,
            "music_memory": str(self.music.music_memory_path),
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
    def read(cls, path: Path) -> "MusicAnalysisResult":
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "2.0":
            raise ValueError(
                f"Unsupported music analysis schema: {value.get('schema_version')!r}"
            )
        material = MaterialRuntimeHandle(
            material_id=MaterialId.parse(value["material_id"]),
            material_type=MaterialType(value["material_type"]),
            material_name=value["material_name"],
            expected_fingerprint=MaterialFingerprint(
                value["material_fingerprint"]
            ),
            source_path=Path(value["source_audio"]),
            memory_root=Path(value["material_directory"]),
        )
        return cls(
            status=value["status"],
            music=AnalysedMusicRuntimeHandle(
                material=material,
                memory_schema_version=value["memory_schema_version"],
                music_memory_path=Path(value["music_memory"]),
            ),
            elapsed_sec=float(value["elapsed_sec"]),
            material_reused=bool(value.get("material_reused")),
            analysis_reused=bool(value.get("analysis_reused")),
        )


__all__ = [
    "AnalyseMusicRequest",
    "AnalyseVideoRequest",
    "AnalysisWorkspace",
    "MusicAnalysisResult",
    "MusicAnalysisOptions",
    "VideoAnalysisResult",
    "VideoAnalysisOptions",
]
