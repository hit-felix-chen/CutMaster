from __future__ import annotations

from pathlib import Path

import pytest

import cutmaster.application.direct.service as direct_service_module
import cutmaster.workflow.analyser as analyser_module
from cutmaster.application.direct import AnalyseVideoCommand, DirectService
from cutmaster.application.materials import MaterialsService
from cutmaster.configuration.effective import load_effective_configuration
from cutmaster.contracts.workflow import ExecuteWorkflowCommand
from cutmaster.infrastructure.storage.local.material_catalog import (
    SubtitleSidecarConflictError,
)
from cutmaster.workflow.contracts import (
    AnalysedVideoRuntimeHandle,
    VideoAnalysisResult,
)


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


def _install_fake_analyser(
    monkeypatch,
    captured_requests: list,
) -> list[frozenset[str]]:
    required_sets: list[frozenset[str]] = []

    def fake_resolve_runtime_config(_effective, *, required=frozenset()):
        required_sets.append(frozenset(required))
        return object()

    class FakeAnalyser:
        def __init__(self, _config) -> None:
            pass

        def analyse(self, request):
            captured_requests.append(request)
            memory_root = request.material.memory_root
            source_srt = memory_root / "source.srt"
            processed = memory_root / "dialogue_merged.srt"
            dialogues = memory_root / "dialogues.json"
            description = memory_root / "video_description.json"
            summary = memory_root / "video_summary.json"
            history = memory_root / "analysis_history.json"
            source_srt.write_text("subtitle\n", encoding="utf-8")
            processed.write_text("subtitle\n", encoding="utf-8")
            dialogues.write_text("[]\n", encoding="utf-8")
            description.write_text("{}\n", encoding="utf-8")
            summary.write_text("{}\n", encoding="utf-8")
            history.write_text("{}\n", encoding="utf-8")
            return VideoAnalysisResult(
                status="success",
                video=AnalysedVideoRuntimeHandle(
                    material=request.material,
                    memory_schema_version="2.0",
                    video_description_path=description,
                    video_summary_path=summary,
                    dialogues_path=dialogues,
                ),
                source_srt_path=source_srt,
                processed_subtitle_path=processed,
                analysis_history_path=history,
                elapsed_sec=0.01,
            )

    monkeypatch.setattr(
        direct_service_module,
        "resolve_runtime_config",
        fake_resolve_runtime_config,
    )
    monkeypatch.setattr(analyser_module, "Analyser", FakeAnalyser)
    return required_sets


def test_direct_reuses_material_owned_subtitle_without_requesting_asr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    effective = _effective(tmp_path)
    materials = MaterialsService(effective)
    direct = DirectService(effective, materials)
    video = tmp_path / "film.mp4"
    video.write_bytes(b"video")
    external_subtitle = tmp_path / "film.srt"
    external_subtitle.write_text("external subtitle", encoding="utf-8")
    captured_requests: list = []
    required_sets = _install_fake_analyser(monkeypatch, captured_requests)

    first = direct.analyse_video(
        AnalyseVideoCommand(
            video_path=video,
            subtitle_path=external_subtitle,
            material_name="Film",
            output_dir=tmp_path / "first-run",
        )
    )
    second = direct.analyse_video(
        AnalyseVideoCommand(
            video_path=video,
            material_name="Film",
            output_dir=tmp_path / "second-run",
        )
    )

    assert first.material_reused is False
    assert second.material_reused is True
    assert len(captured_requests) == 2
    first_sidecar = captured_requests[0].material.subtitle_path
    second_sidecar = captured_requests[1].material.subtitle_path
    assert first_sidecar == second_sidecar
    assert first_sidecar != external_subtitle
    assert first_sidecar.read_text(encoding="utf-8") == "external subtitle"
    assert first_sidecar.is_relative_to(
        captured_requests[0].material.source_path.parent
    )
    assert required_sets == [
        frozenset({"llm", "vlm"}),
        frozenset({"llm", "vlm"}),
    ]


def test_direct_rejects_first_subtitle_after_asr_analysis_is_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    effective = _effective(tmp_path)
    direct = DirectService(effective, MaterialsService(effective))
    video = tmp_path / "film.mp4"
    video.write_bytes(b"video")
    subtitle = tmp_path / "film.srt"
    subtitle.write_text("late subtitle", encoding="utf-8")
    captured_requests: list = []
    required_sets = _install_fake_analyser(monkeypatch, captured_requests)

    direct.analyse_video(
        AnalyseVideoCommand(
            video_path=video,
            material_name="Film",
            output_dir=tmp_path / "asr-run",
        )
    )
    with pytest.raises(
        SubtitleSidecarConflictError,
        match="after video analysis is complete",
    ):
        direct.analyse_video(
            AnalyseVideoCommand(
                video_path=video,
                subtitle_path=subtitle,
                material_name="Film",
                output_dir=tmp_path / "late-run",
            )
        )

    assert captured_requests[0].material.subtitle_path is None
    assert required_sets == [frozenset({"llm", "vlm", "asr"})]


def test_complete_workflow_validates_subtitle_before_ready_cache_hit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    effective = _effective(tmp_path)
    direct = DirectService(effective, MaterialsService(effective))
    video = tmp_path / "film.mp4"
    video.write_bytes(b"video")
    audio = tmp_path / "score.mp3"
    audio.write_bytes(b"audio")
    subtitle = tmp_path / "film.srt"
    subtitle.write_text("late subtitle", encoding="utf-8")
    captured_requests: list = []
    _install_fake_analyser(monkeypatch, captured_requests)

    direct.analyse_video(
        AnalyseVideoCommand(
            video_path=video,
            material_name="Film",
            output_dir=tmp_path / "asr-run",
        )
    )

    with pytest.raises(
        SubtitleSidecarConflictError,
        match="after video analysis is complete",
    ):
        direct.execute_workflow(
            ExecuteWorkflowCommand(
                video_path=video,
                audio_path=audio,
                subtitle_path=subtitle,
                video_material_name="Film",
                music_material_name="Score",
                prompt="Make a short edit",
                output_dir=tmp_path / "complete-run",
            )
        )

    assert len(captured_requests) == 1
