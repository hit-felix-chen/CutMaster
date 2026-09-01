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
from cutmaster.infrastructure.models.openai_compatible import empty_usage_summary
from cutmaster.workflow.contracts.checkpoints import (
    PlannersCheckpoint,
    PlannersCheckpointStage,
    PlannersReplanScope,
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
from cutmaster.workflow.planners.tools.errors import (
    GroupNoCandidateError,
    NoFeasiblePathError,
    RetryablePlanningStageError,
)
from cutmaster.workflow.planners.tools.music_analysis import project_music_profile
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
        json.dumps(
            {
                "schema_version": "3.0",
                "source": {
                    "title": "Video",
                    "duration_sec": 1.0,
                    "fps": 30.0,
                    "width": 1920,
                    "height": 1080,
                },
                "scene_detection": {
                    "detector": "AdaptiveDetector",
                    "adaptive_threshold": 2.0,
                    "adaptive_min_content_val": 15.0,
                    "adaptive_min_scene_len_sec": 0.25,
                    "duplicate_frame_threshold": 1.0,
                },
                "segments": [
                    {
                        "segment_id": "segment_0001",
                        "time_range": {"start_sec": 0.0, "end_sec": 1.0},
                        "has_dialogue": False,
                        "speech_mode": "none",
                        "content_type": None,
                        "timeline_role": "opening",
                        "shots": [
                            {
                                "shot_id": "shot_0001",
                                "time_range": {
                                    "start_sec": 0.0,
                                    "end_sec": 1.0,
                                },
                                "segment_time_range": {
                                    "start_sec": 0.0,
                                    "end_sec": 1.0,
                                },
                                "start_boundary": "video_start",
                                "end_boundary": "video_end",
                                "visual_description": None,
                                "dominant_action": None,
                                "content_type": None,
                                "narrative_function": None,
                                "emotional_tone": None,
                                "emotional_intensity": None,
                                "scene": None,
                                "characters": [],
                                "dialogue": [],
                                "shot_scale": None,
                                "camera_angle": None,
                                "camera_movement": None,
                                "composition": None,
                                "sampled_frame_times_sec": [],
                                "visual_evidence": None,
                                "visual_annotation_status": "provider_rejected",
                                "visual_annotation_failure": "data_inspection_failed",
                            }
                        ],
                        "dialogue_items": [],
                        "segment_summary": None,
                        "narrative_function": None,
                        "emotional_tone": None,
                        "emotional_intensity": None,
                        "appearing_characters": [],
                    }
                ],
                "asr_model": "test-asr",
                "scene_boundary_model": "test-vlm",
                "visual_description_model": "test-vlm",
            }
        ),
        encoding="utf-8",
    )
    video_summary.write_text(json.dumps({"summary": "story"}), encoding="utf-8")
    dialogues.write_text("{}", encoding="utf-8")
    music_profile.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "source_duration_sec": 60.0,
                "tempo_bpm": 120.0,
                "beats_sec": [0.0, 0.5, 1.0],
                "accents_sec": [0.0, 1.0],
                "energy_step_sec": 0.5,
                "energy_curve": [
                    {"time_sec": 0.0, "energy": 0.25},
                    {"time_sec": 0.5, "energy": 0.75},
                ],
                "sections": [
                    {
                        "section_id": "music_01",
                        "start_sec": 0.0,
                        "end_sec": 60.0,
                        "role": "build",
                        "mean_energy": 0.5,
                        "energy_trend": "rising",
                        "suggested_clip_duration_sec": [2.0, 4.0],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
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
            memory_schema_version="3.0",
            video_description_path=video_description,
            video_summary_path=video_summary,
            dialogues_path=dialogues,
        ),
        music=AnalysedMusicRuntimeHandle(
            material=music_material,
            memory_schema_version="2.0",
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
        stop_replan_scope: PlannersReplanScope | None = None,
    ) -> None:
        self.token = token
        self.stop_stage = stop_stage
        self.stop_attempt = stop_attempt
        self.stop_replan_scope = stop_replan_scope
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
            and (
                self.stop_replan_scope is None
                or checkpoint.replan_scope is self.stop_replan_scope
            )
        ):
            self.armed = False
            self.token.cancelled = True


def _install_fake_workflow(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    fail_first_composition: bool = False,
    fail_first_global_composition: bool = False,
    fail_anchor_attempts: int = 0,
    fail_candidate_attempts: int = 0,
    fail_local_repair_attempts: int = 0,
    candidate_failure_groups: tuple[tuple[str, ...], ...] = (),
    repair_diagnostics: list[dict[str, Any]] | None = None,
    fail_first_reuse_with_retryable_error: bool = False,
    revise_to_alternative: bool = False,
    checkpoint_validation_error: str | None = None,
) -> None:
    class _Team:
        def __init__(
            self,
            _video,
            _segment_cache_directory,
            _config,
            context,
        ) -> None:
            self.context = context

        def profile_music(self, music_memory, target_duration_sec, output_path):
            calls.append("profile_music")
            profile = project_music_profile(music_memory, target_duration_sec)
            output_path.write_text(json.dumps(profile), encoding="utf-8")
            return profile

        def arrange(self, *_args):
            calls.append("arrangement_architect")
            slots = [
                {
                    "slot_id": "slot_01",
                    "group_id": "group_001",
                    "parent_group_id": "group_001",
                    "source_segment_id": "segment_0001",
                    "content_description": "setup",
                    "required_visible_subjects": [],
                    "planned_duration_ms": 1000,
                    "planned_duration_sec": 1.0,
                }
            ]
            self.context.set_artifact(
                "arrangement_groups",
                [
                    {
                        "group_id": "group_001",
                        "source_segment_id": "segment_0001",
                        "slot_ids": ["slot_01"],
                        "total_planned_duration_ms": 1000,
                    }
                ],
            )
            return slots

        def anchor_story(self, slots):
            calls.append("story_editor")
            if calls.count("story_editor") <= fail_anchor_attempts:
                try:
                    raise ValueError(
                        "Anchor partition leaves an infeasible child group: "
                        "failed_group_id=group_001"
                    )
                except ValueError as exc:
                    raise RuntimeError("Anchor model requests exhausted") from exc
            self.context.set_artifact("dialogue_anchors", [])
            self.context.set_artifact(
                "planning_segments",
                [
                    {
                        "planning_segment_id": "planning_segment_001",
                        "source_segment_id": "segment_0001",
                        "start_ms": 0,
                        "end_ms": 1000,
                    }
                ],
            )
            self.context.set_artifact(
                "planning_groups",
                [
                    {
                        "group_id": "group_001",
                        "parent_group_id": "group_001",
                        "source_segment_id": "segment_0001",
                        "planning_segment_id": "planning_segment_001",
                        "slot_ids": ["slot_01"],
                    }
                ],
            )
            return slots

        def scout(self, _slots, _cancellation_token=None):
            calls.append("timeline_scout")
            scout_index = calls.count("timeline_scout") - 1
            scheduled_groups = (
                candidate_failure_groups[scout_index]
                if scout_index < len(candidate_failure_groups)
                else ()
            )
            if scheduled_groups or scout_index < fail_candidate_attempts:
                failed_groups = list(scheduled_groups or ("group_001",))
                self.context.set_artifact("candidate_pool", {"group_001": []})
                raise GroupNoCandidateError(
                    {
                        "failed_group_ids": failed_groups,
                        "failed_parent_group_ids": failed_groups,
                        "valid_trajectory_counts": {
                            group_id: 0 for group_id in failed_groups
                        },
                        "rounds_completed": 1,
                    }
                )
            return {
                "group_001": [
                    {
                        "trajectory_id": "group_001_trajectory_001",
                        "group_id": "group_001",
                        "planning_segment_id": "planning_segment_001",
                        "items": [
                            {
                                "slot_id": "slot_01",
                                "candidate_id": "candidate_01",
                                "timestamp": "00:00:00,000-00:00:01,000",
                            }
                        ],
                    },
                    {
                        "trajectory_id": "group_001_trajectory_002",
                        "group_id": "group_001",
                        "planning_segment_id": "planning_segment_001",
                        "items": [
                            {
                                "slot_id": "slot_01",
                                "candidate_id": "candidate_02",
                                "timestamp": "00:00:00,000-00:00:01,000",
                            }
                        ],
                    },
                ]
            }

        def validate_planning(self, _slots) -> None:
            calls.append("validate_planning")
            if checkpoint_validation_error == "planning":
                raise ValueError("restored planning contract is invalid")

        def validate_composition(self, *_args) -> None:
            calls.append("validate_composition")
            if (
                checkpoint_validation_error == "composition"
                and self.context.get_artifact("candidate_pool") is not None
            ):
                raise ValueError("restored candidate contract is invalid")

        def validate_edit_checkpoint(
            self, slots, pool, _beam, _selection, _pairwise
        ) -> None:
            calls.append("validate_edit_checkpoint")
            self.validate_composition(slots, pool)

        def validate_revision_checkpoint(
            self,
            slots,
            pool,
            _beam,
            _selection,
            _script,
            _pairwise,
        ) -> None:
            calls.append("validate_revision_checkpoint")
            self.validate_composition(slots, pool)

        def compose(self, _slots, pool):
            calls.append("edit_composer")
            if (
                fail_first_global_composition
                and calls.count("edit_composer") == 1
            ):
                raise ValueError("global composition contract failed")
            if fail_first_composition and calls.count("edit_composer") == 1:
                raise NoFeasiblePathError(
                    "group_001",
                    {
                        "failed_group_id": "group_001",
                        "valid_trajectory_counts": {"group_001": 1},
                    },
                )
            trajectory = pool["group_001"][0]
            return (
                [trajectory],
                {
                    "beam_score": 1.0,
                    "selected_trajectory_ids": {
                        "group_001": trajectory["trajectory_id"],
                    },
                },
                {},
            )

        def build_script(self, _slots, selected):
            calls.append("build_script")
            item = selected[0]["items"][0]
            return [
                {
                    **item,
                    "group_id": selected[0]["group_id"],
                    "trajectory_id": selected[0]["trajectory_id"],
                    "output_frame_range": [0, 30],
                }
            ]

        def revise(self, _slots, _pool, script, _scores):
            calls.append("revision_editor")
            if revise_to_alternative:
                return (
                    [
                        {
                            **script[0],
                            "candidate_id": "candidate_02",
                            "trajectory_id": "group_001_trajectory_002",
                        }
                    ],
                    [
                        {
                            "operation": "replace",
                            "group_id": "group_001",
                            "trajectory_id": "group_001_trajectory_002",
                            "reason": "stronger visual",
                        }
                    ],
                )
            return script, []

        def repair_groups(self, slots, diagnostics):
            calls.append("repair_groups")
            if repair_diagnostics is not None:
                repair_diagnostics.append(dict(diagnostics))
            if calls.count("repair_groups") <= fail_local_repair_attempts:
                try:
                    raise ValueError("failed_group_id=group_001")
                except ValueError as exc:
                    raise RuntimeError("Local group repair failed") from exc
            self.context.set_artifact(
                "arrangement_groups",
                [
                    {
                        "group_id": "group_001",
                        "source_segment_id": "segment_0001",
                        "slot_ids": ["slot_01"],
                        "total_planned_duration_ms": 1000,
                    }
                ],
            )
            return slots, {"slot_01"}

        def refresh_story_groups(
            self,
            slots,
            *,
            previous_slots,
            replanned_slot_ids,
        ):
            calls.append("refresh_story_groups")
            assert previous_slots
            refreshed = self.anchor_story(slots)
            return refreshed, replanned_slot_ids, {"group_001"}

        def scout_with_reuse(
            self,
            slots,
            *,
            cancellation_token=None,
            previous_candidate_pool,
            previous_slots,
            **_reuse,
        ):
            calls.append("scout_with_reuse")
            assert previous_slots
            assert "group_001" in previous_candidate_pool
            if (
                fail_first_reuse_with_retryable_error
                and calls.count("scout_with_reuse") == 1
            ):
                raise RetryablePlanningStageError(
                    "retrieval",
                    {
                        "diagnosis": "retrieval response contract failed",
                        "failed_group_ids": [],
                        "failed_parent_group_ids": [],
                    },
                )
            return self.scout(slots, cancellation_token)

        def record_failure(self, *, attempt, diagnostics, **_kwargs) -> None:
            calls.append("record_failure")
            self.context.set_artifact(
                "planners_feedback",
                {
                    "attempt": attempt,
                    "diagnostics": diagnostics,
                    "failed_slots": [
                        {
                            "slot_id": "slot_01",
                            "group_id": "group_001",
                            "source_segment_id": "segment_0001",
                            "valid_trajectory_count": 1,
                        }
                    ],
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


def test_resume_story_checkpoint_revalidates_planning_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        checkpoint_validation_error="planning",
    )
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(token, PlannersCheckpointStage.STORY)

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    token.cancelled = False

    with pytest.raises(ValueError, match="restored planning contract is invalid"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    assert calls.count("timeline_scout") == 0


@pytest.mark.parametrize(
    "stage",
    [PlannersCheckpointStage.EDIT, PlannersCheckpointStage.REVISION],
)
def test_late_checkpoint_resume_revalidates_candidate_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: PlannersCheckpointStage,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        checkpoint_validation_error="composition",
    )
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(token, stage)

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    token.cancelled = False

    with pytest.raises(ValueError, match="restored candidate contract is invalid"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    assert calls.count("compile_render_plan") == 0


def test_resume_rejects_disagreeing_checkpoint_selection_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(monkeypatch, calls)
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(token, PlannersCheckpointStage.EDIT)

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    assert store.latest is not None
    changed = store.latest.to_dict()
    changed["selected_trajectory_ids"] = {"group_001": "trajectory_changed"}
    store.latest = PlannersCheckpoint.from_dict(changed)
    token.cancelled = False

    with pytest.raises(ValueError, match="disagree with selection"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )


def test_timeline_resume_promotes_no_path_to_full_global_retry(
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
    store = _MemoryCheckpointStore(token, PlannersCheckpointStage.TIMELINE)

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    token.cancelled = False

    result = Planners(_config(tmp_path)).plan(
        request,
        cancellation_token=token,
        checkpoint_store=store,
    )

    assert result.status == "success"
    assert calls.count("arrangement_architect") == 2
    assert calls.count("repair_groups") == 0
    assert calls.count("timeline_scout") == 2
    assert calls.count("edit_composer") == 2


def test_anchor_failure_returns_to_arrangement_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(monkeypatch, calls, fail_anchor_attempts=1)

    result = Planners(_config(tmp_path)).plan(request)

    assert result.status == "success"
    assert calls.count("arrangement_architect") == 2
    assert calls.count("repair_groups") == 0
    assert calls.count("story_editor") == 2
    assert calls.count("timeline_scout") == 1
    assert calls.index("arrangement_architect", 1) < calls.index(
        "story_editor",
        calls.index("story_editor") + 1,
    )


def test_candidate_empty_local_replan_keeps_current_aster_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_anchor_attempts=1,
        fail_candidate_attempts=1,
    )

    result = Planners(_config(tmp_path)).plan(request)
    diagnostics = json.loads(
        result.selection_diagnostics_path.read_text(encoding="utf-8")
    )

    assert result.status == "success"
    assert diagnostics["aster_attempt"] == 2
    assert calls.count("timeline_scout") == 2
    assert calls.count("scout_with_reuse") == 1


def test_candidate_local_replan_drops_groups_recovered_in_latest_retrieval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    repairs: list[dict[str, Any]] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        candidate_failure_groups=(("group_001",), ("group_002",)),
        repair_diagnostics=repairs,
    )

    result = Planners(_config(tmp_path)).plan(request)

    assert result.status == "success"
    assert [item["failed_parent_group_ids"] for item in repairs] == [
        ["group_001"],
        ["group_002"],
    ]


def test_candidate_local_budget_exhaustion_starts_full_next_aster_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_candidate_attempts=3,
    )

    result = Planners(_config(tmp_path)).plan(request)
    diagnostics = json.loads(
        result.selection_diagnostics_path.read_text(encoding="utf-8")
    )

    assert result.status == "success"
    assert diagnostics["aster_attempt"] == 2
    assert calls.count("arrangement_architect") == 2
    assert calls.count("repair_groups") == 2
    assert calls.count("timeline_scout") == 4
    assert calls.count("scout_with_reuse") == 2


def test_candidate_local_budget_is_nested_inside_each_global_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_candidate_attempts=9,
    )

    with pytest.raises(GroupNoCandidateError):
        Planners(_config(tmp_path)).plan(request)

    assert calls.count("arrangement_architect") == 3
    assert calls.count("repair_groups") == 6
    assert calls.count("timeline_scout") == 9
    assert calls.count("scout_with_reuse") == 6
    assert calls.count("record_failure") == 9


def test_last_global_attempt_can_use_candidate_local_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_anchor_attempts=2,
        fail_candidate_attempts=1,
    )

    result = Planners(_config(tmp_path)).plan(request)
    diagnostics = json.loads(
        result.selection_diagnostics_path.read_text(encoding="utf-8")
    )

    assert result.status == "success"
    assert diagnostics["aster_attempt"] == 3
    assert calls.count("arrangement_architect") == 3
    assert calls.count("repair_groups") == 1
    assert calls.count("timeline_scout") == 2


def test_candidate_local_repair_failure_uses_local_budget_before_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_candidate_attempts=1,
        fail_local_repair_attempts=1,
    )

    result = Planners(_config(tmp_path)).plan(request)
    diagnostics = json.loads(
        result.selection_diagnostics_path.read_text(encoding="utf-8")
    )

    assert result.status == "success"
    assert diagnostics["aster_attempt"] == 1
    assert calls.count("arrangement_architect") == 1
    assert calls.count("repair_groups") == 2
    assert calls.count("timeline_scout") == 2


def test_generic_retrieval_failure_after_local_repair_promotes_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_candidate_attempts=1,
        fail_first_reuse_with_retryable_error=True,
    )

    result = Planners(_config(tmp_path)).plan(request)
    diagnostics = json.loads(
        result.selection_diagnostics_path.read_text(encoding="utf-8")
    )

    assert result.status == "success"
    assert diagnostics["aster_attempt"] == 2
    assert calls.count("arrangement_architect") == 2
    assert calls.count("repair_groups") == 1
    assert calls.count("scout_with_reuse") == 1


def test_anchor_failure_stops_after_three_aster_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(monkeypatch, calls, fail_anchor_attempts=3)

    with pytest.raises(RetryablePlanningStageError):
        Planners(_config(tmp_path)).plan(request)

    assert calls.count("arrangement_architect") == 3
    assert calls.count("repair_groups") == 0
    assert calls.count("story_editor") == 3
    assert calls.count("timeline_scout") == 0
    assert calls.count("record_failure") == 3


def test_resume_after_candidate_local_replan_reuses_group_and_sanitizes_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_candidate_attempts=1,
    )
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(
        token,
        PlannersCheckpointStage.REPLAN_PENDING,
        stop_attempt=1,
        stop_replan_scope=PlannersReplanScope.CANDIDATE_LOCAL,
    )

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    assert store.latest is not None
    assert store.latest.completed_stage is PlannersCheckpointStage.REPLAN_PENDING
    assert store.latest.aster_attempt == 1
    assert store.latest.replan_scope is PlannersReplanScope.CANDIDATE_LOCAL
    assert store.latest.local_replan_attempt == 1
    assert store.latest.planners_feedback is not None
    assert "instruction" not in store.latest.planners_feedback
    assert store.latest.replan_reuse is not None
    assert store.latest.replan_reuse["previous_slots"][0]["slot_id"] == "slot_01"
    assert "group_001" in store.latest.replan_reuse["previous_candidate_pool"]
    store.latest = PlannersCheckpoint.from_dict(store.latest.to_dict())
    token.cancelled = False

    result = Planners(_config(tmp_path)).plan(
        request,
        cancellation_token=token,
        checkpoint_store=store,
    )

    assert result.status == "success"
    assert calls.count("arrangement_architect") == 1
    assert calls.count("repair_groups") == 1
    assert calls.count("refresh_story_groups") == 1
    assert calls.count("scout_with_reuse") == 1
    assert calls.count("edit_composer") == 1


def test_global_retry_checkpoint_discards_candidate_reuse_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_first_global_composition=True,
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
    assert store.latest.replan_scope is PlannersReplanScope.GLOBAL
    assert store.latest.local_replan_attempt == 0
    assert store.latest.replan_reuse is None
    token.cancelled = False

    result = Planners(_config(tmp_path)).plan(
        request,
        cancellation_token=token,
        checkpoint_store=store,
    )

    assert result.status == "success"
    assert calls.count("arrangement_architect") == 2
    assert calls.count("timeline_scout") == 2
    assert calls.count("scout_with_reuse") == 0


def test_candidate_local_story_checkpoint_keeps_reuse_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        fail_candidate_attempts=1,
    )
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(
        token,
        PlannersCheckpointStage.STORY,
        stop_attempt=1,
        stop_replan_scope=PlannersReplanScope.CANDIDATE_LOCAL,
    )

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )
    assert store.latest is not None
    assert store.latest.completed_stage is PlannersCheckpointStage.STORY
    assert store.latest.aster_attempt == 1
    assert store.latest.replan_scope is PlannersReplanScope.CANDIDATE_LOCAL
    assert store.latest.local_replan_attempt == 1
    assert store.latest.replan_reuse is not None
    assert store.latest.replan_reuse["affected_parent_group_ids"] == [
        "group_001"
    ]
    token.cancelled = False

    result = Planners(_config(tmp_path)).plan(
        request,
        cancellation_token=token,
        checkpoint_store=store,
    )

    assert result.status == "success"
    assert calls.count("repair_groups") == 1
    assert calls.count("refresh_story_groups") == 1
    assert calls.count("scout_with_reuse") == 1


def test_revision_checkpoint_and_final_diagnostics_store_revised_trajectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    calls: list[str] = []
    _install_fake_workflow(
        monkeypatch,
        calls,
        revise_to_alternative=True,
    )
    token = _BoundaryToken()
    store = _MemoryCheckpointStore(token, PlannersCheckpointStage.REVISION)

    with pytest.raises(WorkflowCancelledError, match="checkpoint boundary"):
        Planners(_config(tmp_path)).plan(
            request,
            cancellation_token=token,
            checkpoint_store=store,
        )

    assert store.latest is not None
    assert store.latest.selected_trajectory_ids == {
        "group_001": "group_001_trajectory_002"
    }
    assert store.latest.selection is not None
    assert store.latest.selection["composer_selected_trajectory_ids"] == {
        "group_001": "group_001_trajectory_001"
    }
    assert store.latest.selection["selected_trajectory_ids"] == {
        "group_001": "group_001_trajectory_002"
    }
    token.cancelled = False

    result = Planners(_config(tmp_path)).plan(
        request,
        cancellation_token=token,
        checkpoint_store=store,
    )
    diagnostics = json.loads(
        result.selection_diagnostics_path.read_text(encoding="utf-8")
    )

    assert diagnostics["selected_trajectory_ids"] == {
        "group_001": "group_001_trajectory_002"
    }
    assert diagnostics["final_candidate_ids"] == ["candidate_02"]


def _arrangement_checkpoint_document() -> dict[str, Any]:
    return {
        "schema_version": "4.0",
        "completed_stage": "arrangement_architect",
        "aster_attempt": 1,
        "replan_scope": None,
        "local_replan_attempt": 0,
        "music_profile": {"source_duration_sec": 30.0},
        "slots": [
            {
                "slot_id": "slot_01",
                "group_id": "group_001",
                "source_segment_id": "segment_0001",
                "planned_duration_ms": 1000,
            }
        ],
        "arrangement_groups": [
            {
                "group_id": "group_001",
                "source_segment_id": "segment_0001",
                "slot_ids": ["slot_01"],
                "total_planned_duration_ms": 1000,
            }
        ],
        "planning_segments": None,
        "planning_groups": None,
        "dialogue_anchors": None,
        "candidate_pool": None,
        "replan_reuse": None,
        "beam_path": None,
        "selected_trajectory_ids": None,
        "selection": None,
        "pairwise_scores": None,
        "raw_script": None,
        "planners_feedback": None,
        "stage_timings_sec": {},
        "prior_model_usage": empty_usage_summary(),
        "prior_model_call_count": 0,
    }


def _candidate_local_pending_checkpoint_document() -> dict[str, Any]:
    value = _arrangement_checkpoint_document()
    value.update(
        {
            "completed_stage": "replan_pending",
            "replan_scope": "candidate_local",
            "local_replan_attempt": 1,
            "planners_feedback": {
                "diagnostics": {
                    "failed_parent_group_ids": ["group_001"],
                }
            },
            "replan_reuse": {
                "previous_slots": list(value["slots"]),
                "previous_planning_segments": [],
                "previous_planning_groups": [],
                "previous_dialogue_anchors": [],
                "previous_candidate_pool": {"group_001": []},
                "affected_parent_group_ids": [],
            },
        }
    )
    return value


def test_candidate_local_pending_checkpoint_round_trip() -> None:
    value = _candidate_local_pending_checkpoint_document()

    restored = PlannersCheckpoint.from_dict(value)

    assert restored.to_dict() == value


def test_candidate_local_pending_checkpoint_requires_failed_parent_groups() -> None:
    value = _candidate_local_pending_checkpoint_document()
    value["planners_feedback"]["diagnostics"]["failed_parent_group_ids"] = []

    with pytest.raises(ValueError, match="failed_parent_group_ids"):
        PlannersCheckpoint.from_dict(value)


def test_checkpoint_contract_rejects_schema_1_without_compatibility() -> None:
    value = _arrangement_checkpoint_document()
    value["schema_version"] = "1.0"

    with pytest.raises(ValueError, match=r"Unsupported.*'1\.0'"):
        PlannersCheckpoint.from_dict(value)


def test_checkpoint_contract_rejects_schema_2_without_compatibility() -> None:
    value = _arrangement_checkpoint_document()
    value["schema_version"] = "2.0"

    with pytest.raises(ValueError, match=r"Unsupported.*'2\.0'"):
        PlannersCheckpoint.from_dict(value)


def test_checkpoint_contract_rejects_schema_3_without_compatibility() -> None:
    value = _arrangement_checkpoint_document()
    value["schema_version"] = "3.0"

    with pytest.raises(ValueError, match=r"Unsupported.*'3\.0'"):
        PlannersCheckpoint.from_dict(value)


def test_checkpoint_contract_rejects_non_string_nested_keys() -> None:
    value = _arrangement_checkpoint_document()
    value["music_profile"] = {1: "coerced"}

    with pytest.raises(TypeError, match="string keys"):
        PlannersCheckpoint.from_dict(value)
