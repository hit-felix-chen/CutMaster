"""Public service for reusable video and music Material analysis."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.workflow.analyser.material_analyst import MaterialAnalystAgent
from cutmaster.workflow.analyser.tools.music_analysis import (
    validate_music_memory,
    write_music_memory,
)
from cutmaster.workflow.contracts.analysis import (
    AnalyseMusicRequest,
    AnalyseVideoRequest,
    MusicAnalysisResult,
    VideoAnalysisResult,
)
from cutmaster.workflow.contracts.material import (
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
)
from cutmaster.workflow.ports import CancellationToken, raise_if_cancelled


def _valid_music_memory(path: Path) -> dict[str, Any] | None:
    """Return a complete current-schema Music Memory."""

    if not path.is_file() or path.is_symlink():
        return None
    try:
        memory = json.loads(path.read_text(encoding="utf-8"))
        validate_music_memory(memory)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return memory


def _require_source(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"{label} Material source is unavailable: {resolved}")
    return resolved


def _prepare_workspace(root: Path) -> Path:
    resolved = root.resolve()
    if resolved.exists() and (not resolved.is_dir() or resolved.is_symlink()):
        raise ValueError(f"Analysis workspace is not a regular directory: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


class Analyser:
    """Build reusable Material Memory from application-issued runtime handles.

    Material registration, name resolution, fingerprint verification, leasing,
    and deletion belong to the Application Layer.  This stage only analyses the
    verified source and memory locations carried by its request.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def analyse(
        self,
        request: AnalyseVideoRequest,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> VideoAnalysisResult:
        raise_if_cancelled(cancellation_token)
        source_video = _require_source(request.material.source_path, "Video")
        subtitle_path = request.material.subtitle_path
        if subtitle_path is not None:
            subtitle_path = _require_source(subtitle_path, "Subtitle")
        memory_root = request.material.memory_root.resolve()
        if not memory_root.is_dir() or memory_root.is_symlink():
            raise FileNotFoundError(
                f"Video Material Memory is unavailable: {memory_root}"
            )
        workspace = _prepare_workspace(request.workspace.root)

        started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Video Material analysis started",
            stage="video_material_analysis",
            material_id=str(request.material.material_id),
            material_name=request.material.material_name,
        )
        analyst = MaterialAnalystAgent(self.config)
        video_title = request.options.video_title or request.material.material_name
        artifacts = (
            analyst.analyse(
                source_video,
                video_title,
                subtitle_path,
                source_fingerprint=str(request.material.expected_fingerprint),
                material_directory=memory_root,
            )
            if cancellation_token is None
            else analyst.analyse(
                source_video,
                video_title,
                subtitle_path,
                source_fingerprint=str(request.material.expected_fingerprint),
                material_directory=memory_root,
                cancellation_token=cancellation_token,
            )
        )
        raise_if_cancelled(cancellation_token)
        memory_schema_version = str(
            artifacts.video_description.get("schema_version") or ""
        ).strip()
        if not memory_schema_version:
            raise ValueError("Video Memory does not declare a schema version")
        elapsed = time.monotonic() - started
        result = VideoAnalysisResult(
            status="success",
            video=AnalysedVideoRuntimeHandle(
                material=request.material,
                memory_schema_version=memory_schema_version,
                video_description_path=artifacts.video_description_path.resolve(),
                video_summary_path=artifacts.video_summary_path.resolve(),
                dialogues_path=artifacts.dialogues_json.resolve(),
            ),
            source_srt_path=artifacts.source_srt.resolve(),
            processed_subtitle_path=artifacts.processed_subtitle.resolve(),
            analysis_history_path=artifacts.analysis_history_path.resolve(),
            elapsed_sec=elapsed,
            analysis_reused=artifacts.analysis_reused,
            model_usage_path=(
                artifacts.model_usage_path.resolve()
                if artifacts.model_usage_path is not None
                else None
            ),
            model_usage_summary=artifacts.model_usage_summary,
            model_usage_cumulative_summary=(artifacts.model_usage_cumulative_summary),
        )
        # Result publication belongs to the Application Material service. The
        # stage writes only its invocation workspace and never marks a Catalog
        # Material READY by creating a canonical file directly.
        raise_if_cancelled(cancellation_token)
        result.write(workspace / "analysis_result.json")
        log_event(
            "SUCCESS",
            "analyser",
            "stage.complete",
            "Video Material analysis completed",
            stage="video_material_analysis",
            elapsed_sec=elapsed,
            material_id=str(request.material.material_id),
            material_name=request.material.material_name,
            analysis_reused=artifacts.analysis_reused,
        )
        return result

    def analyse_music(
        self,
        request: AnalyseMusicRequest,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> MusicAnalysisResult:
        raise_if_cancelled(cancellation_token)
        source_audio = _require_source(request.material.source_path, "Music")
        memory_root = request.material.memory_root.resolve()
        if not memory_root.is_dir() or memory_root.is_symlink():
            raise FileNotFoundError(
                f"Music Material Memory is unavailable: {memory_root}"
            )
        workspace = _prepare_workspace(request.workspace.root)
        memory_path = memory_root / "music_memory.json"
        started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Music Material analysis started",
            stage="music_material_analysis",
            material_id=str(request.material.material_id),
            material_name=request.material.material_name,
        )
        memory = _valid_music_memory(memory_path)
        analysis_reused = memory is not None
        if memory is None:
            analyst = MaterialAnalystAgent(self.config)
            if cancellation_token is None:
                memory = analyst.analyse_music(source_audio)
            else:
                memory = analyst.analyse_music(
                    source_audio,
                    cancellation_token=cancellation_token,
                )
            raise_if_cancelled(cancellation_token)
            write_music_memory(memory_path, memory)
        raise_if_cancelled(cancellation_token)
        elapsed = time.monotonic() - started
        result = MusicAnalysisResult(
            status="success",
            music=AnalysedMusicRuntimeHandle(
                material=request.material,
                memory_schema_version=str(memory["schema_version"]),
                music_memory_path=memory_path.resolve(),
            ),
            elapsed_sec=elapsed,
            analysis_reused=analysis_reused,
        )
        raise_if_cancelled(cancellation_token)
        result.write(workspace / "music_analysis_result.json")
        log_event(
            "SUCCESS",
            "analyser",
            "stage.complete",
            "Music Material analysis completed",
            stage="music_material_analysis",
            elapsed_sec=elapsed,
            material_id=str(request.material.material_id),
            material_name=request.material.material_name,
            analysis_reused=analysis_reused,
        )
        return result


__all__ = ["Analyser"]
