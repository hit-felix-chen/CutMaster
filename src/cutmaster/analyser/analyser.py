"""Public service for the reusable material-analysis stage."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from cutmaster.analyser.material_analyst import MaterialAnalystAgent
from cutmaster.analyser.tools.material_library import (
    Material,
    MaterialLibrary,
    MaterialType,
)
from cutmaster.analyser.tools.music_analysis import write_music_memory
from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.analyser import (
    AnalysisRequest,
    AnalysisResult,
    MusicAnalysisRequest,
    MusicAnalysisResult,
)
from cutmaster.runtime.observability import log_event


def _copy_artifact(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _valid_music_memory(path: Path, source_audio: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        memory = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(memory, dict) or memory.get("schema_version") != "1.0":
            return None
        if Path(str(memory.get("audio_path") or "")).resolve() != source_audio:
            return None
        if float(memory.get("source_duration_sec") or 0.0) <= 0.0:
            return None
        if not all(
            isinstance(memory.get(field), list)
            for field in ("beats_sec", "accents_sec", "energy_curve", "sections")
        ):
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return memory


def _require_video_binding(
    material: Material,
    result: AnalysisResult,
    manifest_path: Path,
) -> None:
    if (
        result.material_name != material.name
        or result.material_fingerprint != material.fingerprint
        or Path(result.source_video).resolve() != material.source_path
        or Path(result.material_directory).resolve() != material.analysis_dir
        or result.material_manifest is None
        or Path(result.material_manifest).resolve() != manifest_path
    ):
        raise ValueError(
            f"Video analysis result is not bound to Material {material.name!r}"
        )


def _require_music_binding(
    material: Material,
    result: MusicAnalysisResult,
    manifest_path: Path,
) -> None:
    if (
        result.material_name != material.name
        or result.material_fingerprint != material.fingerprint
        or Path(result.source_audio).resolve() != material.source_path
        or Path(result.material_directory).resolve() != material.analysis_dir
        or result.material_manifest is None
        or Path(result.material_manifest).resolve() != manifest_path
    ):
        raise ValueError(
            f"Music analysis result is not bound to Material {material.name!r}"
        )


class Analyser:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.material_library = MaterialLibrary(
            config.analyser.material_analysis.material_cache_dir
        )

    @staticmethod
    def _video_result_path(material: Material) -> Path:
        return material.analysis_dir / "analysis_result.json"

    @staticmethod
    def _music_result_path(material: Material) -> Path:
        return material.analysis_dir / "music_analysis_result.json"

    def resolve_video(self, material_name: str) -> AnalysisResult:
        """Resolve completed video analysis by public Material Name."""
        material = self.material_library.resolve(MaterialType.VIDEO, material_name)
        path = self._video_result_path(material)
        if not path.is_file():
            raise FileNotFoundError(
                f"Video Material {material.name!r} has not been analysed: {path}"
            )
        result = AnalysisResult.read(path)
        _require_video_binding(
            material,
            result,
            self.material_library.manifest_path,
        )
        return replace(
            result,
            material_reused=True,
            analysis_reused=True,
            elapsed_sec=0.0,
        )

    def resolve_music(self, material_name: str) -> MusicAnalysisResult:
        """Resolve completed Music Memory by public Material Name."""
        material = self.material_library.resolve(MaterialType.MUSIC, material_name)
        path = self._music_result_path(material)
        if not path.is_file():
            raise FileNotFoundError(
                f"Music Material {material.name!r} has not been analysed: {path}"
            )
        result = MusicAnalysisResult.read(path)
        _require_music_binding(
            material,
            result,
            self.material_library.manifest_path,
        )
        return replace(
            result,
            material_reused=True,
            analysis_reused=True,
            elapsed_sec=0.0,
        )

    def analyse(self, request: AnalysisRequest) -> AnalysisResult:
        if not request.video_path.is_file():
            raise FileNotFoundError(f"Video not found: {request.video_path}")
        if request.subtitle_path is not None and not request.subtitle_path.is_file():
            raise FileNotFoundError(f"Subtitle not found: {request.subtitle_path}")
        output_dir = request.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        library_material = self.material_library.add(
            request.video_path.resolve(),
            MaterialType.VIDEO,
            request.material_name or None,
        )
        with self.material_library.analysis_lock(library_material):
            return self._analyse_video_locked(
                request,
                output_dir,
                library_material,
            )

    def _analyse_video_locked(
        self,
        request: AnalysisRequest,
        output_dir: Path,
        library_material: Material,
    ) -> AnalysisResult:
        started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Video material analysis started",
            stage="video_material_analysis",
            source_video=request.video_path.name,
            material_name=library_material.name,
            material_reused=library_material.reused,
        )
        material = MaterialAnalystAgent(self.config).analyse(
            library_material.source_path,
            request.video_title or library_material.name,
            request.subtitle_path.resolve() if request.subtitle_path else None,
            material_directory=library_material.analysis_dir,
        )
        source_srt = output_dir / "source.srt"
        processed_subtitle = output_dir / "dialogue_merged.srt"
        dialogues_json = output_dir / "dialogues.json"
        _copy_artifact(material.source_srt, source_srt)
        _copy_artifact(material.processed_subtitle, processed_subtitle)
        _copy_artifact(material.dialogues_json, dialogues_json)
        elapsed = time.monotonic() - started
        result = AnalysisResult(
            status="success",
            source_video=str(library_material.source_path),
            material_directory=str(material.material_directory.resolve()),
            source_srt=str(material.source_srt.resolve()),
            processed_subtitle=str(material.processed_subtitle.resolve()),
            dialogues_json=str(material.dialogues_json.resolve()),
            video_description=str(material.video_description_path.resolve()),
            video_summary=str(material.video_summary_path.resolve()),
            analysis_history=str(material.analysis_history_path.resolve()),
            elapsed_sec=elapsed,
            material_name=library_material.name,
            material_fingerprint=library_material.fingerprint,
            material_manifest=str(self.material_library.manifest_path),
            material_reused=library_material.reused,
            analysis_reused=material.analysis_reused,
            model_usage=(
                str(material.model_usage_path.resolve())
                if material.model_usage_path is not None
                else None
            ),
            model_usage_summary=material.model_usage_summary,
        )
        result.write(self._video_result_path(library_material))
        result.write(output_dir / "analysis_result.json")
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Video material analysis completed",
            stage="video_material_analysis",
            elapsed_sec=elapsed,
            material_directory=material.material_directory,
            material_name=library_material.name,
            material_reused=library_material.reused,
            analysis_reused=material.analysis_reused,
        )
        return result

    def analyse_music(self, request: MusicAnalysisRequest) -> MusicAnalysisResult:
        if not request.audio_path.is_file():
            raise FileNotFoundError(f"Music not found: {request.audio_path}")
        output_dir = request.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        material = self.material_library.add(
            request.audio_path.resolve(),
            MaterialType.MUSIC,
            request.material_name or None,
        )
        with self.material_library.analysis_lock(material):
            return self._analyse_music_locked(request, output_dir, material)

    def _analyse_music_locked(
        self,
        request: MusicAnalysisRequest,
        output_dir: Path,
        material: Material,
    ) -> MusicAnalysisResult:
        source_audio = material.source_path.resolve()
        memory_path = material.analysis_dir / "music_memory.json"
        started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Music material analysis started",
            stage="music_material_analysis",
            source_audio=request.audio_path.name,
            material_name=material.name,
            material_reused=material.reused,
        )
        memory = _valid_music_memory(memory_path, source_audio)
        analysis_reused = memory is not None
        if memory is None:
            memory = MaterialAnalystAgent(self.config).analyse_music(source_audio)
            write_music_memory(memory_path, memory)
        _copy_artifact(memory_path, output_dir / "music_memory.json")
        elapsed = time.monotonic() - started
        result = MusicAnalysisResult(
            status="success",
            source_audio=str(source_audio),
            material_name=material.name,
            material_fingerprint=material.fingerprint,
            material_directory=str(material.analysis_dir.resolve()),
            music_memory=str(memory_path.resolve()),
            elapsed_sec=elapsed,
            material_manifest=str(self.material_library.manifest_path),
            material_reused=material.reused,
            analysis_reused=analysis_reused,
        )
        result.write(self._music_result_path(material))
        result.write(output_dir / "music_analysis_result.json")
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Music material analysis completed",
            stage="music_material_analysis",
            elapsed_sec=elapsed,
            material_name=material.name,
            material_reused=material.reused,
            analysis_reused=analysis_reused,
        )
        return result


__all__ = ["Analyser"]
