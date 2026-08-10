"""Public contracts for reusable source-material analysis."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AnalysisRequest:
    video_path: Path
    output_dir: Path
    video_title: str = ""
    subtitle_path: Path | None = None
    material_name: str = ""


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    source_video: str
    material_directory: str
    source_srt: str
    processed_subtitle: str
    dialogues_json: str
    video_description: str
    video_summary: str
    analysis_history: str
    elapsed_sec: float
    material_name: str = ""
    material_fingerprint: str = ""
    material_manifest: str | None = None
    material_reused: bool = False
    analysis_reused: bool = False
    model_usage: str | None = None
    model_usage_summary: dict[str, Any] = field(default_factory=dict)
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
    def read(cls, path: Path) -> "AnalysisResult":
        data = json.loads(path.read_text(encoding="utf-8"))
        result = cls(**data)
        result.validate()
        return result

    def validate(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError(
                f"Unsupported analysis result schema: {self.schema_version}"
            )
        if self.status != "success":
            raise ValueError("Analysis result is not successful")
        required = {
            "source_video": self.source_video,
            "material_directory": self.material_directory,
            "source_srt": self.source_srt,
            "processed_subtitle": self.processed_subtitle,
            "dialogues_json": self.dialogues_json,
            "video_description": self.video_description,
            "video_summary": self.video_summary,
            "analysis_history": self.analysis_history,
        }
        if self.material_manifest is not None:
            required["material_manifest"] = self.material_manifest
        missing = [name for name, value in required.items() if not Path(value).exists()]
        if missing:
            raise FileNotFoundError(
                f"Analysis result references missing artifacts: {sorted(missing)}"
            )
        if self.material_fingerprint:
            actual = _file_sha256(Path(self.source_video))
            if actual != self.material_fingerprint:
                raise ValueError(
                    "Video Material fingerprint no longer matches its managed source"
                )

    def load_video_description(self) -> dict[str, Any]:
        return json.loads(Path(self.video_description).read_text(encoding="utf-8"))

    def load_video_summary(self) -> dict[str, Any]:
        return json.loads(Path(self.video_summary).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class MusicAnalysisRequest:
    audio_path: Path
    output_dir: Path
    material_name: str = ""


@dataclass(frozen=True)
class MusicAnalysisResult:
    status: str
    source_audio: str
    material_name: str
    material_fingerprint: str
    material_directory: str
    music_memory: str
    elapsed_sec: float
    material_manifest: str | None = None
    material_reused: bool = False
    analysis_reused: bool = False
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
    def read(cls, path: Path) -> "MusicAnalysisResult":
        result = cls(**json.loads(path.read_text(encoding="utf-8")))
        result.validate()
        return result

    def validate(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError(
                f"Unsupported music analysis result schema: {self.schema_version}"
            )
        if self.status != "success":
            raise ValueError("Music analysis result is not successful")
        required = {
            "source_audio": self.source_audio,
            "material_directory": self.material_directory,
            "music_memory": self.music_memory,
        }
        if self.material_manifest is not None:
            required["material_manifest"] = self.material_manifest
        missing = [name for name, value in required.items() if not Path(value).exists()]
        if missing:
            raise FileNotFoundError(
                "Music analysis result references missing artifacts: "
                f"{sorted(missing)}"
            )
        if self.material_fingerprint:
            actual = _file_sha256(Path(self.source_audio))
            if actual != self.material_fingerprint:
                raise ValueError(
                    "Music Material fingerprint no longer matches its managed source"
                )
        memory = self.load_music_memory()
        if memory.get("schema_version") != "1.0":
            raise ValueError("Unsupported Music Memory schema")
        memory_audio = Path(str(memory.get("audio_path") or ""))
        if memory_audio.resolve() != Path(self.source_audio).resolve():
            raise ValueError("Music Memory source does not match its Material")

    def load_music_memory(self) -> dict[str, Any]:
        return json.loads(Path(self.music_memory).read_text(encoding="utf-8"))


__all__ = [
    "AnalysisRequest",
    "AnalysisResult",
    "MusicAnalysisRequest",
    "MusicAnalysisResult",
]
