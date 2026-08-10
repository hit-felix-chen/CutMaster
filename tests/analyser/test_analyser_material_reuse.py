from __future__ import annotations

import json
from pathlib import Path

import cutmaster.analyser.analyser as analyser_module
import pytest

from cutmaster.analyser.analyser import Analyser
from cutmaster.configuration.schema import (
    ASRConfig,
    AnalyserConfig,
    AppConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    PlannersConfig,
    RendererConfig,
    SceneSegmentationConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.contracts.analyser import AnalysisRequest, MusicAnalysisRequest
from cutmaster.contracts.material import MaterialAnalysisResult


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
                material_cache_dir=tmp_path / "materials"
            ),
            shot_detection=ShotDetectionConfig(),
            asr=ASRConfig(backend="unused", api_key="unused"),
            scene_segmentation=SceneSegmentationConfig(),
            shot_annotation=ShotAnnotationConfig(),
        ),
        planners=PlannersConfig(),
        renderer=RendererConfig(),
    )


class _FakeMaterialAnalyst:
    build_count = 0

    def __init__(self, _config: AppConfig) -> None:
        pass

    def analyse(
        self,
        _video_path: Path,
        _video_title: str,
        _subtitle_path: Path | None,
        *,
        material_directory: Path,
    ) -> MaterialAnalysisResult:
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
            type(self).build_count += 1
            material_directory.mkdir(parents=True, exist_ok=True)
            paths["source_srt"].write_text("", encoding="utf-8")
            paths["processed_subtitle"].write_text("", encoding="utf-8")
            for key in (
                "dialogues_json",
                "video_description",
                "video_summary",
                "analysis_history",
            ):
                paths[key].write_text("{}\n", encoding="utf-8")
        return MaterialAnalysisResult(
            material_directory=material_directory,
            source_srt=paths["source_srt"],
            processed_subtitle=paths["processed_subtitle"],
            dialogues_json=paths["dialogues_json"],
            video_description_path=paths["video_description"],
            video_summary_path=paths["video_summary"],
            analysis_history_path=paths["analysis_history"],
            video_description={},
            video_summary={},
            analysis_reused=reused,
        )


def _video_request(source: Path, output_dir: Path, name: str) -> AnalysisRequest:
    return AnalysisRequest(
        video_path=source,
        output_dir=output_dir,
        material_name=name,
    )


def test_same_named_same_hash_video_reuses_material_and_analysis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakeMaterialAnalyst.build_count = 0
    monkeypatch.setattr(analyser_module, "MaterialAnalystAgent", _FakeMaterialAnalyst)
    first_source = tmp_path / "upload-one.mp4"
    second_source = tmp_path / "upload-two.mp4"
    first_source.write_bytes(b"identical video")
    second_source.write_bytes(b"identical video")
    analyser = Analyser(_config(tmp_path))

    first = analyser.analyse(
        _video_request(first_source, tmp_path / "result-one", "Opening Scene")
    )
    second = analyser.analyse(
        _video_request(second_source, tmp_path / "result-two", "Opening Scene")
    )

    assert first.material_name == second.material_name == "Opening Scene"
    assert first.source_video == second.source_video
    assert first.material_directory == second.material_directory
    assert first.material_reused is False
    assert first.analysis_reused is False
    assert second.material_reused is True
    assert second.analysis_reused is True
    assert _FakeMaterialAnalyst.build_count == 1

    resolved = analyser.resolve_video("  Opening   Scene ")
    assert resolved.material_name == "Opening Scene"
    assert resolved.source_video == first.source_video
    assert resolved.material_directory == first.material_directory
    assert resolved.material_reused is True
    assert resolved.analysis_reused is True

    canonical = Path(first.material_directory) / "analysis_result.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    payload["material_name"] = "Different Material"
    canonical.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="not bound to Material"):
        analyser.resolve_video("Opening Scene")


def test_same_named_different_hash_video_allocates_numeric_suffix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakeMaterialAnalyst.build_count = 0
    monkeypatch.setattr(analyser_module, "MaterialAnalystAgent", _FakeMaterialAnalyst)
    first_source = tmp_path / "first.mp4"
    second_source = tmp_path / "second.mp4"
    first_source.write_bytes(b"first video")
    second_source.write_bytes(b"second video")
    analyser = Analyser(_config(tmp_path))

    first = analyser.analyse(
        _video_request(first_source, tmp_path / "result-one", "Trailer")
    )
    second = analyser.analyse(
        _video_request(second_source, tmp_path / "result-two", "Trailer")
    )

    assert first.material_name == "Trailer"
    assert second.material_name == "Trailer (2)"
    assert first.source_video != second.source_video
    assert first.material_directory != second.material_directory
    assert second.material_reused is False
    assert second.analysis_reused is False
    assert _FakeMaterialAnalyst.build_count == 2
    assert analyser.resolve_video("Trailer (2)").source_video == second.source_video


def test_same_named_same_hash_music_reuses_memory_and_resolves_by_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    analysis_calls: list[Path] = []

    def fake_analyze_music_memory(audio_path: Path) -> dict[str, object]:
        analysis_calls.append(audio_path)
        return {
            "schema_version": "1.0",
            "audio_path": str(audio_path.resolve()),
            "source_duration_sec": 120.0,
            "beats_sec": [0.0, 1.0],
            "accents_sec": [0.0],
            "energy_curve": [{"time_sec": 0.0, "energy": 0.5}],
            "sections": [
                {"id": "section-1", "start_sec": 0.0, "end_sec": 120.0}
            ],
        }

    class FakeMusicAnalyst:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def analyse_music(self, audio_path: Path) -> dict[str, object]:
            return fake_analyze_music_memory(audio_path)

    monkeypatch.setattr(analyser_module, "MaterialAnalystAgent", FakeMusicAnalyst)
    first_source = tmp_path / "score-one.wav"
    second_source = tmp_path / "score-two.wav"
    first_source.write_bytes(b"identical music")
    second_source.write_bytes(b"identical music")
    analyser = Analyser(_config(tmp_path))

    first = analyser.analyse_music(
        MusicAnalysisRequest(
            audio_path=first_source,
            output_dir=tmp_path / "music-result-one",
            material_name="Main Score",
        )
    )
    second = analyser.analyse_music(
        MusicAnalysisRequest(
            audio_path=second_source,
            output_dir=tmp_path / "music-result-two",
            material_name="Main Score",
        )
    )

    assert first.material_name == second.material_name == "Main Score"
    assert first.source_audio == second.source_audio
    assert first.music_memory == second.music_memory
    assert first.material_reused is False
    assert first.analysis_reused is False
    assert second.material_reused is True
    assert second.analysis_reused is True
    assert analysis_calls == [Path(first.source_audio)]

    resolved = analyser.resolve_music(" Main   Score ")
    assert resolved.material_name == "Main Score"
    assert resolved.source_audio == first.source_audio
    assert resolved.music_memory == first.music_memory
    assert resolved.material_reused is True
    assert resolved.analysis_reused is True
    assert json.loads(Path(resolved.music_memory).read_text(encoding="utf-8"))[
        "audio_path"
    ] == first.source_audio

    canonical = Path(first.material_directory) / "music_analysis_result.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    payload["material_name"] = "Different Material"
    canonical.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="not bound to Material"):
        analyser.resolve_music("Main Score")
