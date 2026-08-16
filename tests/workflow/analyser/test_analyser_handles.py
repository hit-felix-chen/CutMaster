from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cutmaster.workflow.analyser.analyser as analyser_module
from cutmaster.configuration.schema import (
    AnalyserConfig,
    AppConfig,
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    PlannersConfig,
    RendererConfig,
    SceneSegmentationConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint, MaterialType
from cutmaster.workflow.analyser import Analyser
from cutmaster.workflow.contracts.analysis import (
    AnalyseMusicRequest,
    AnalyseVideoRequest,
    AnalysisWorkspace,
    MusicAnalysisOptions,
    VideoAnalysisOptions,
)
from cutmaster.workflow.contracts.material import MaterialRuntimeHandle
from cutmaster.workflow.ports import WorkflowCancelledError


def _config(tmp_path: Path) -> AppConfig:
    model = {
        "model": "unused",
        "base_url": "https://example.invalid/v1",
        "api_key": "unused",
    }
    return AppConfig(
        llm=LLMConfig(**model),
        vlm=VLMConfig(**model),
        analyser=AnalyserConfig(
            material_analysis=MaterialAnalysisConfig(
                material_library_dir=tmp_path / "unused-catalog"
            ),
            shot_detection=ShotDetectionConfig(),
            asr=ASRConfig(backend="unused", api_key="unused"),
            scene_segmentation=SceneSegmentationConfig(),
            shot_annotation=ShotAnnotationConfig(),
        ),
        planners=PlannersConfig(),
        renderer=RendererConfig(),
    )


def _handle(tmp_path: Path, material_type: MaterialType) -> MaterialRuntimeHandle:
    suffix = ".mp4" if material_type is MaterialType.VIDEO else ".wav"
    source = (tmp_path / f"source{suffix}").resolve()
    source.write_bytes(b"media")
    memory = (tmp_path / "analysis").resolve()
    memory.mkdir()
    return MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=material_type,
        material_name="Feature" if material_type is MaterialType.VIDEO else "Score",
        expected_fingerprint=MaterialFingerprint("a" * 64),
        source_path=source,
        memory_root=memory,
    )


class _FakeMaterialAnalyst:
    video_builds = 0
    music_builds = 0

    def __init__(self, _config: AppConfig) -> None:
        pass

    def analyse(
        self,
        _video_path: Path,
        _video_title: str,
        _subtitle_path: Path | None,
        *,
        material_directory: Path,
    ) -> SimpleNamespace:
        paths = {
            "source_srt": material_directory / "source.srt",
            "processed_subtitle": material_directory / "dialogue_merged.srt",
            "dialogues_json": material_directory / "dialogues.json",
            "video_description": material_directory / "video_description.json",
            "video_summary": material_directory / "video_summary.json",
            "analysis_history": material_directory / "analysis_history.json",
        }
        reused = all(path.is_file() for path in paths.values())
        if not reused:
            type(self).video_builds += 1
            paths["source_srt"].write_text("", encoding="utf-8")
            paths["processed_subtitle"].write_text("", encoding="utf-8")
            paths["dialogues_json"].write_text("{}\n", encoding="utf-8")
            paths["video_description"].write_text(
                '{"schema_version":"1.0"}\n', encoding="utf-8"
            )
            paths["video_summary"].write_text("{}\n", encoding="utf-8")
            paths["analysis_history"].write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(
            material_directory=material_directory,
            source_srt=paths["source_srt"],
            processed_subtitle=paths["processed_subtitle"],
            dialogues_json=paths["dialogues_json"],
            video_description_path=paths["video_description"],
            video_summary_path=paths["video_summary"],
            analysis_history_path=paths["analysis_history"],
            video_description={"schema_version": "1.0"},
            video_summary={},
            model_usage_path=None,
            model_usage_summary={},
            model_usage_cumulative_summary={},
            analysis_reused=reused,
        )

    def analyse_music(self, audio_path: Path) -> dict[str, object]:
        type(self).music_builds += 1
        return {
            "schema_version": "1.0",
            "audio_path": str(audio_path.resolve()),
            "source_duration_sec": 120.0,
            "beats_sec": [0.0, 1.0],
            "accents_sec": [0.0],
            "energy_curve": [{"time_sec": 0.0, "energy": 0.5}],
            "sections": [{"start_sec": 0.0, "end_sec": 120.0}],
        }


def test_video_stage_stops_after_analysis_boundary_before_result_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Token:
        cancelled = False

        def raise_if_cancelled(self) -> None:
            if self.cancelled:
                raise WorkflowCancelledError("analysis stopped")

    token = _Token()

    class _CancellingMaterialAnalyst(_FakeMaterialAnalyst):
        def analyse(self, *args, cancellation_token, **kwargs):
            artifacts = super().analyse(*args, **kwargs)
            assert cancellation_token is token
            token.cancelled = True
            return artifacts

    monkeypatch.setattr(
        analyser_module,
        "MaterialAnalystAgent",
        _CancellingMaterialAnalyst,
    )
    handle = _handle(tmp_path, MaterialType.VIDEO)
    workspace = (tmp_path / "cancelled-workspace").resolve()
    request = AnalyseVideoRequest(
        material=handle,
        options=VideoAnalysisOptions(video_title="Feature"),
        workspace=AnalysisWorkspace(workspace),
    )

    with pytest.raises(WorkflowCancelledError, match="analysis stopped"):
        Analyser(_config(tmp_path)).analyse(
            request,
            cancellation_token=token,
        )

    assert (handle.memory_root / "video_description.json").is_file()
    assert not (workspace / "analysis_result.json").exists()


def test_video_stage_uses_handle_memory_and_reuses_completed_analysis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakeMaterialAnalyst.video_builds = 0
    monkeypatch.setattr(analyser_module, "MaterialAnalystAgent", _FakeMaterialAnalyst)
    handle = _handle(tmp_path, MaterialType.VIDEO)
    request = AnalyseVideoRequest(
        material=handle,
        options=VideoAnalysisOptions(video_title="Feature"),
        workspace=AnalysisWorkspace((tmp_path / "workspace").resolve()),
    )
    analyser = Analyser(_config(tmp_path))

    first = analyser.analyse(request)
    second = analyser.analyse(request)

    assert not hasattr(analyser, "material_library")
    assert not hasattr(analyser, "resolve_video")
    assert first.video.material is handle
    assert first.analysis_reused is False
    assert second.analysis_reused is True
    assert _FakeMaterialAnalyst.video_builds == 1
    assert first.video.video_description_path.parent == handle.memory_root
    assert (request.workspace.root / "analysis_result.json").is_file()
    assert not (handle.memory_root / "analysis_result.json").exists()
    assert (request.workspace.root / "analysis_result.json").is_file()


def test_music_stage_uses_handle_and_reuses_music_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakeMaterialAnalyst.music_builds = 0
    monkeypatch.setattr(analyser_module, "MaterialAnalystAgent", _FakeMaterialAnalyst)
    handle = _handle(tmp_path, MaterialType.MUSIC)
    request = AnalyseMusicRequest(
        material=handle,
        options=MusicAnalysisOptions(),
        workspace=AnalysisWorkspace((tmp_path / "music-workspace").resolve()),
    )
    analyser = Analyser(_config(tmp_path))

    first = analyser.analyse_music(request)
    second = analyser.analyse_music(request)

    assert first.analysis_reused is False
    assert second.analysis_reused is True
    assert _FakeMaterialAnalyst.music_builds == 1
    payload = json.loads(first.music.music_memory_path.read_text(encoding="utf-8"))
    assert Path(payload["audio_path"]) == handle.source_path
    assert (request.workspace.root / "music_analysis_result.json").is_file()
