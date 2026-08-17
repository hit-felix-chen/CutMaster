"""Tests for managed Material Analysis execution."""

from __future__ import annotations

from pathlib import Path

import pytest

import cutmaster.application.materials.execution as execution_module
from cutmaster.application.materials import (
    ExecuteMaterialAnalysisCommand,
    ManagedMaterialAnalysisExecutor,
    MaterialsService,
)
from cutmaster.configuration.effective import load_effective_configuration
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.workflow.contracts import (
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
    AnalysisWorkspace,
    MusicAnalysisResult,
    VideoAnalysisResult,
)
from cutmaster.workflow.ports import WorkflowCancelledError

MINIMAL_CONFIG = """
[llm]
model = "test-llm"
api_key_env = "CUTMASTER_TEST_LLM_KEY"

[vlm]
model = "test-vlm"
api_key_env = "CUTMASTER_TEST_VLM_KEY"

[analyser.asr]
api_key_env = "CUTMASTER_TEST_ASR_KEY"
""".strip()


def _effective(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(MINIMAL_CONFIG + "\n", encoding="utf-8")
    return load_effective_configuration(config_path)


class _FakeAnalyser:
    def __init__(self, captured: list[object]) -> None:
        self._captured = captured

    def analyse(self, request, *, cancellation_token=None):
        del cancellation_token
        self._captured.append(request)
        memory = request.material.memory_root
        source_srt = _write(memory / "source.srt", "subtitle\n")
        processed = _write(memory / "dialogue_merged.srt", "subtitle\n")
        dialogues = _write(memory / "dialogues.json", "{}\n")
        description = _write(memory / "video_description.json", "{}\n")
        summary = _write(memory / "video_summary.json", "{}\n")
        history = _write(memory / "analysis_history.json", "{}\n")
        return VideoAnalysisResult(
            status="success",
            video=AnalysedVideoRuntimeHandle(
                material=request.material,
                memory_schema_version="test-video-memory",
                video_description_path=description,
                video_summary_path=summary,
                dialogues_path=dialogues,
            ),
            source_srt_path=source_srt,
            processed_subtitle_path=processed,
            analysis_history_path=history,
            elapsed_sec=1.25,
            model_usage_summary={"total_tokens": 12},
            model_usage_cumulative_summary={"total_tokens": 34},
        )

    def analyse_music(self, request, *, cancellation_token=None):
        del cancellation_token
        self._captured.append(request)
        memory_path = _write(
            request.material.memory_root / "music_memory.json",
            "{}\n",
        )
        return MusicAnalysisResult(
            status="success",
            music=AnalysedMusicRuntimeHandle(
                material=request.material,
                memory_schema_version="test-music-memory",
                music_memory_path=memory_path,
            ),
            elapsed_sec=0.5,
        )


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path.resolve()


def _executor(tmp_path: Path):
    effective = _effective(tmp_path)
    materials = MaterialsService(effective)
    return materials, ManagedMaterialAnalysisExecutor(effective, materials)


def _command(material_id, root: Path, **values):
    return ExecuteMaterialAnalysisCommand(
        material_id=material_id,
        workspace=AnalysisWorkspace(root.resolve()),
        **values,
    )


def test_video_execution_uses_only_leased_paths_and_publishes_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    materials, executor = _executor(tmp_path)
    source = _write(tmp_path / "film.mp4", "video")
    subtitle = _write(tmp_path / "film.srt", "managed subtitle")
    material = materials.add(source, MaterialType.VIDEO, "Film")
    with materials.lease(material.material_id) as binding:
        managed_subtitle = materials.ensure_subtitle(binding, subtitle)
    required_sets: list[frozenset[str]] = []
    captured: list[object] = []

    def resolve(_configuration, *, required=frozenset()):
        required_sets.append(frozenset(required))
        return object()

    monkeypatch.setattr(execution_module, "resolve_runtime_config", resolve)
    result = executor.execute(
        _command(
            material.material_id,
            tmp_path / "video-workspace",
            video_title="Feature Film",
            material_reused=True,
        ),
        analyser_factory=lambda _config: _FakeAnalyser(captured),
    )

    request = captured[0]
    assert request.material.source_path != source
    assert request.material.source_path.is_relative_to(
        (tmp_path / ".cutmaster").resolve()
    )
    assert request.material.subtitle_path == managed_subtitle
    assert request.material.subtitle_path != subtitle
    assert request.options.video_title == "Feature Film"
    assert required_sets == [frozenset({"llm", "vlm"})]
    assert result.material_reused is True
    assert materials.get(material.material_id).condition is MaterialCondition.READY
    assert (request.material.memory_root / "analysis_result.json").is_file()


def test_video_without_managed_subtitle_requires_asr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    materials, executor = _executor(tmp_path)
    source = _write(tmp_path / "source.mp4", "video")
    material = materials.add(source, "video", "Real Film")
    required_sets: list[frozenset[str]] = []
    captured: list[object] = []

    def resolve(_configuration, *, required=frozenset()):
        required_sets.append(frozenset(required))
        return object()

    monkeypatch.setattr(execution_module, "resolve_runtime_config", resolve)
    executor.execute(
        _command(material.material_id, tmp_path / "asr-workspace"),
        analyser_factory=lambda _config: _FakeAnalyser(captured),
    )

    assert captured[0].material.subtitle_path is None
    assert required_sets == [frozenset({"llm", "vlm", "asr"})]


def test_music_execution_dispatches_and_publishes_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    materials, executor = _executor(tmp_path)
    material = materials.add(
        _write(tmp_path / "score.mp3", "music"),
        "music",
        "Score",
    )
    required_sets: list[frozenset[str]] = []
    captured: list[object] = []

    def resolve(_configuration, *, required=frozenset()):
        required_sets.append(frozenset(required))
        return object()

    monkeypatch.setattr(execution_module, "resolve_runtime_config", resolve)
    result = executor.execute(
        _command(material.material_id, tmp_path / "music-workspace"),
        analyser_factory=lambda _config: _FakeAnalyser(captured),
    )

    assert result.music.material.material_type is MaterialType.MUSIC
    assert required_sets == [frozenset()]
    assert len(captured) == 1
    assert materials.get(material.material_id).condition is MaterialCondition.READY
    assert (tmp_path / "music-workspace" / "music_analysis_result.json").is_file()


@pytest.mark.parametrize("material_type", [MaterialType.VIDEO, MaterialType.MUSIC])
def test_ready_material_loads_canonical_result_without_analyser(
    tmp_path: Path,
    monkeypatch,
    material_type: MaterialType,
) -> None:
    materials, executor = _executor(tmp_path)
    suffix = ".mp4" if material_type is MaterialType.VIDEO else ".mp3"
    material = materials.add(
        _write(tmp_path / f"source{suffix}", material_type.value),
        material_type,
        "Reusable",
    )
    monkeypatch.setattr(
        execution_module,
        "resolve_runtime_config",
        lambda *_args, **_kwargs: object(),
    )
    executor.execute(
        _command(material.material_id, tmp_path / "first"),
        analyser_factory=lambda _config: _FakeAnalyser([]),
    )

    def forbidden(_config):
        raise AssertionError("READY Material must not construct Analyser")

    result = executor.execute(
        _command(material.material_id, tmp_path / "cached"),
        analyser_factory=forbidden,
    )

    assert result.analysis_reused is True
    assert result.material_reused is True
    assert result.elapsed_sec == 0.0
    result_name = (
        "analysis_result.json"
        if material_type is MaterialType.VIDEO
        else "music_analysis_result.json"
    )
    assert (tmp_path / "cached" / result_name).is_file()
    if isinstance(result, VideoAnalysisResult):
        assert result.model_usage_summary == {}
        assert result.model_usage_cumulative_summary == {"total_tokens": 34}


def test_cancellation_after_analysis_does_not_publish_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class CancelBeforePublish:
        calls = 0

        def raise_if_cancelled(self):
            self.calls += 1
            if self.calls == 2:
                raise WorkflowCancelledError("stop")

    materials, executor = _executor(tmp_path)
    material = materials.add(
        _write(tmp_path / "score.mp3", "music"),
        "music",
        "Score",
    )
    monkeypatch.setattr(
        execution_module,
        "resolve_runtime_config",
        lambda *_args, **_kwargs: object(),
    )

    with pytest.raises(WorkflowCancelledError, match="stop"):
        executor.execute(
            _command(material.material_id, tmp_path / "cancelled"),
            cancellation_token=CancelBeforePublish(),
            analyser_factory=lambda _config: _FakeAnalyser([]),
        )

    assert materials.get(material.material_id).condition is MaterialCondition.QUEUED
    with materials.lease(material.material_id) as binding:
        assert not (binding.memory_root / "music_analysis_result.json").exists()


def test_execution_command_has_no_raw_source_or_subtitle_path(tmp_path: Path) -> None:
    materials, _executor_instance = _executor(tmp_path)
    material = materials.add(
        _write(tmp_path / "film.mp4", "video"),
        "video",
        "Film",
    )
    command = _command(material.material_id, tmp_path / "workspace")

    assert not hasattr(command, "source_path")
    assert not hasattr(command, "video_path")
    assert not hasattr(command, "audio_path")
    assert not hasattr(command, "subtitle_path")
