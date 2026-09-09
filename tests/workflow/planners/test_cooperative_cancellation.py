from __future__ import annotations

import json
from pathlib import Path

import pytest

import cutmaster.workflow.planners.planners as planners_module
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
from cutmaster.workflow.contracts.material import (
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
    MaterialRuntimeHandle,
)
from cutmaster.workflow.contracts.planners import (
    PlannersBrief,
    PlannersOptions,
    PlannersRequest,
    PlannersWorkspace,
)
from cutmaster.workflow.planners import Planners
from cutmaster.workflow.ports import WorkflowCancelledError


class _SwitchToken:
    cancelled = False

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise WorkflowCancelledError("stopped in test")


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
            material_analysis=MaterialAnalysisConfig(tmp_path / "catalog"),
            shot_detection=ShotDetectionConfig(),
            asr=ASRConfig(backend="unused", api_key="unused"),
            scene_segmentation=SceneSegmentationConfig(),
            shot_annotation=ShotAnnotationConfig(),
        ),
        planners=PlannersConfig(),
        renderer=RendererConfig(),
    )


def _request(tmp_path: Path) -> PlannersRequest:
    video_source = (tmp_path / "video.mp4").resolve()
    music_source = (tmp_path / "music.wav").resolve()
    video_source.write_bytes(b"video")
    music_source.write_bytes(b"music")
    video_memory = (tmp_path / "video-memory").resolve()
    music_memory = (tmp_path / "music-memory").resolve()
    video_memory.mkdir()
    music_memory.mkdir()
    video_description = video_memory / "video_description.json"
    video_summary = video_memory / "video_summary.json"
    dialogues = video_memory / "dialogues.json"
    music_profile = music_memory / "music_memory.json"
    for path in (video_description, video_summary, dialogues, music_profile):
        path.write_text(json.dumps({}), encoding="utf-8")
    video_material = MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=MaterialType.VIDEO,
        material_name="Video",
        expected_fingerprint=MaterialFingerprint("a" * 64),
        source_path=video_source,
        memory_root=video_memory,
    )
    music_material = MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=MaterialType.MUSIC,
        material_name="Music",
        expected_fingerprint=MaterialFingerprint("b" * 64),
        source_path=music_source,
        memory_root=music_memory,
    )
    return PlannersRequest(
        video=AnalysedVideoRuntimeHandle(
            material=video_material,
            memory_schema_version="2.0",
            video_description_path=video_description,
            video_summary_path=video_summary,
            dialogues_path=dialogues,
        ),
        music=AnalysedMusicRuntimeHandle(
            material=music_material,
            memory_schema_version="1.0",
            music_memory_path=music_profile,
        ),
        brief=PlannersBrief("Create a coherent montage", 30.0),
        options=PlannersOptions(),
        workspace=PlannersWorkspace((tmp_path / "planners").resolve()),
    )


@pytest.mark.parametrize(
    "cancel_stage",
    ["arrange", "anchor_story", "scout", "compose", "build_script"],
)
def test_aster_checks_cancellation_after_each_agent_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_stage: str,
) -> None:
    token = _SwitchToken()
    calls: list[str] = []

    class _Team:
        def __init__(self, *_args) -> None:
            pass

        def _stage(self, name: str) -> None:
            calls.append(name)
            if name == cancel_stage:
                token.cancelled = True

        def profile_music(self, *_args):
            calls.append("profile_music")
            return {}

        def arrange(self, *_args):
            self._stage("arrange")
            return [{"slot_id": "slot_01", "content_description": "setup"}]

        def anchor_story(self, slots):
            self._stage("anchor_story")
            return slots

        def scout(self, _slots, _cancellation_token=None):
            self._stage("scout")
            return {}

        def validate_composition(self, *_args) -> None:
            calls.append("validate_composition")

        def compose(self, *_args):
            self._stage("compose")
            return [], {}, {}

        def build_script(self, *_args):
            self._stage("build_script")
            return []

        def revise(self, _slots, _pool, script, _scores):
            self._stage("revise")
            return script, {}

    monkeypatch.setattr(planners_module, "ASTERTeam", _Team)
    # This test isolates cancellation checkpoints; persisted Video Memory
    # schema validation is covered by the contract suite.
    monkeypatch.setattr(
        planners_module,
        "validate_video_description_document",
        lambda _value: None,
    )

    with pytest.raises(WorkflowCancelledError, match="stopped in test"):
        Planners(_config(tmp_path)).plan(
            _request(tmp_path),
            cancellation_token=token,
        )

    assert calls[-1] == cancel_stage
    assert not (tmp_path / "planners" / "render_plan.json").exists()
