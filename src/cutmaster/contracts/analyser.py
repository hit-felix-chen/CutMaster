"""Public contracts for reusable source-material analysis."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AnalysisRequest:
    video_path: Path
    output_dir: Path
    video_title: str = ""
    subtitle_path: Path | None = None


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
        missing = [name for name, value in required.items() if not Path(value).exists()]
        if missing:
            raise FileNotFoundError(
                f"Analysis result references missing artifacts: {sorted(missing)}"
            )

    def load_video_description(self) -> dict[str, Any]:
        return json.loads(Path(self.video_description).read_text(encoding="utf-8"))

    def load_video_summary(self) -> dict[str, Any]:
        return json.loads(Path(self.video_summary).read_text(encoding="utf-8"))


__all__ = ["AnalysisRequest", "AnalysisResult"]
