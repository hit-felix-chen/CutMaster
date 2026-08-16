from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import cutmaster.workflow.planners.planners as planners_module
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
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint, MaterialType
from cutmaster.workflow.contracts.checkpoints import (
    PlannersCheckpoint,
    PlannersCheckpointStage,
)
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
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.planners import Planners
from cutmaster.workflow.planners.tools.errors import NoFeasiblePathError
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
    video_description.write_text(
        json.dumps({"source": {}, "segments": []}),
        encoding="utf-8",
    )
    video_summary.write_text(json.dumps({"summary": "story"}), encoding="utf-8")
    dialogues.write_text("{}", encoding="utf-8")
    music_profile.write_text("{}", encoding="utf-8")
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


class _BoundaryToken:
    cancelled = False

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise WorkflowCancelledError("checkpoint boundary reached")


class _MemoryCheckpointStore:
    def __init__(
        self,
        token: _BoundaryToken,
        stop_stage: PlannersCheckpointStage,
        *,
        stop_attempt: int = 1,
    ) -> None:
        self.token = token
        self.stop_stage = stop_stage
        self.stop_attempt = stop_attempt
        self.latest: PlannersCheckpoint | None = None
        self.saved: list[PlannersCheckpoint] = []
        self.armed = True

    def load(self) -> PlannersCheckpoint | None:
        return self.latest

    def save(self, checkpoint: PlannersCheckpoint) -> None:
        self.latest = checkpoint
        self.saved.append(checkpoint)
        if (
            self.armed
            and checkpoint.completed_stage is self.stop_stage
            and checkpoint.aster_attempt == self.stop_attempt
        ):
            self.armed = False
            self.token.cancelled = True


def _install_fake_workflow(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    fail_first_composition: bool = False,
) -> None:
    class _Team:
        def __init__(self, _video, _config, context) -> None:
            self.context = context

        def profile_music(self, *_args):
            calls.append("profile_music")
            return {
                "planned_duration_sec": 30.0,
                "tempo_bpm": 120.0,
                "sections": [],
            }

        def arrange(self, *_args):
            calls.append("arrangement_architect")
            return [{"slot_id": "slot_01", "content_description": "setup"}]

        def anchor_story(self, slots):
            calls.append("story_editor")
            self.context.set_artifact("dialogue_anchors", [])
            return slots

        def scout(self, _slots):
            calls.append("timeline_scout")
            return {
                "slot_01": [
                    {
                        "candidate_id": "candidate_01",
                        "timestamp": "00:00:00,000-00:00:01,000",
                    }
                ]
            }

        def validate_composition(self, *_args) -> None:
            calls.append("validate_composition")

        def compose(self, _slots, pool):
            calls.append("edit_composer")
            if fail_first_composition and calls.count("edit_composer") == 1:
                raise NoFeasiblePathError(
                    "slot_01",
                    {"failed_slot_id": "slot_01", "shortages": {}},
                )
            return list(pool["slot_01"]), {"beam_score": 1.0}, {}

        def build_script(self, _slots, selected):
            calls.append("build_script")
            return [
                {
                    **selected[0],
                    "output_frame_range": [0, 30],
                }
            ]

        def revise(self, _slots, _pool, script, _scores):
            calls.append("revision_editor")
            return script, []

        def record_failure(self, *, attempt, **_kwargs) -> None:
            calls.append("record_failure")
            self.context.set_artifact(
                "planners_feedback",
                {
                    "attempt": attempt,
                    "failed_slots": [],
                    "forbidden_segment_ids": [],
                    "instruction": "private retry instruction",
                },
            )

    def compile_plan(*, request, **_kwargs):
        calls.append("compile_render_plan")
        return RenderPlan.create(
            video_material_id=request.video.material.material_id,
            video_expected_fingerprint=request.video.material.expected_fingerprint,
            music_material_id=request.music.material.material_id,
            music_expected_fingerprint=request.music.material.expected_fingerprint,
            fps=30,
            clips=[
                {
                    "timestamp": "00:00:00,000-00:00:01,000",
                    "output_frame_range": [0, 30],
                }
            ],
            planners_metadata={"test": True},
        )

    monkeypatch.setattr(planners_module, "ASTERTeam", _Team)
    monkeypatch.setattr(planners_module, "compile_render_plan", compile_plan)


@pytest.mark.parametrize(
    "stage",
    [
        PlannersCheckpointStage.ARRANGEMENT,
        PlannersCheckpointStage.STORY,
        PlannersCheckpointStage.TIMELINE,
        PlannersCheckpointStage.EDIT,
        PlannersCheckpointStage.REVISION,
    ],
)
def test_resume_skips_every_completed_aster_agent_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: PlannersCheckpointStage,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(monkeypatch, calls)
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(token, stage)

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    calls_before_resume = {
        agent: calls.count(agent)
        for agent in (
            "profile_music",
            "arrangement_architect",
            "story_editor",
            "timeline_scout",
            "edit_composer",
            "revision_editor",
        )
    }
    token.cancelled = False

    result = Planners(_config(tmp_path)).plan(
        request,
        cancellation_token=token,
        checkpoint_store=store,
    )

    assert result.status == "success"
    completed_rank = {
        PlannersCheckpointStage.ARRANGEMENT: 1,
        PlannersCheckpointStage.STORY: 2,
        PlannersCheckpointStage.TIMELINE: 3,
        PlannersCheckpointStage.EDIT: 4,
        PlannersCheckpointStage.REVISION: 5,
    }[stage]
    agents = (
        "arrangement_architect",
        "story_editor",
        "timeline_scout",
        "edit_composer",
        "revision_editor",
    )
    assert calls.count("profile_music") == 1
    for index, agent in enumerate(agents, 1):
        expected = calls_before_resume[agent] if index <= completed_rank else 1
        assert calls.count(agent) == expected
    assert calls.count("compile_render_plan") == 1


def test_resume_after_replan_reuses_second_arrangement_and_sanitizes_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_first_composition=True,
    )
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(
        token,
        PlannersCheckpointStage.REPLAN_PENDING,
        stop_attempt=2,
    )

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    assert store.latest is not None
    assert store.latest.completed_stage is PlannersCheckpointStage.REPLAN_PENDING
    assert store.latest.aster_attempt == 2
    assert store.latest.planners_feedback is not None
    assert "instruction" not in store.latest.planners_feedback
    token.cancelled = False

    result = Planners(_config(tmp_path)).plan(
        request,
        cancellation_token=token,
        checkpoint_store=store,
    )

    assert result.status == "success"
    assert calls.count("arrangement_architect") == 2
    assert calls.count("edit_composer") == 2


def test_checkpoint_contract_rejects_non_string_nested_keys() -> None:
    value: dict[str, Any] = {
        "schema_version": "1.0",
        "completed_stage": "arrangement_architect",
        "aster_attempt": 1,
        "music_profile": {1: "coerced"},
        "slots": [{"slot_id": "slot_01"}],
        "dialogue_anchors": None,
        "candidate_pool": None,
        "beam_path": None,
        "selection": None,
        "pairwise_scores": None,
        "raw_script": None,
        "planners_feedback": None,
        "stage_timings_sec": {},
        "prior_model_usage": {},
        "prior_model_call_count": 0,
    }

    with pytest.raises(TypeError, match="string keys"):
        PlannersCheckpoint.from_dict(value)
