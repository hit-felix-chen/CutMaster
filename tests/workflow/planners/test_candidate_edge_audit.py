"""Offline regressions for Candidate batch and motion-evidence edge cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from cutmaster.configuration.schema import (
    CandidateRetrievalConfig,
    LLMConfig,
    VLMConfig,
)
from cutmaster.infrastructure.models.openai_compatible import ModelResponse
from cutmaster.workflow.planners.timeline_scout import retrieve_candidates
from cutmaster.workflow.planners.tools.errors import GroupNoCandidateError
from cutmaster.workflow.shared.execution_context import WorkflowContext


def _setup(tmp_path: Path, *, duration_ms: int = 2000):
    slot = {
        "slot_id": "slot_01",
        "group_id": "group_01",
        "parent_group_id": "group_01",
        "source_segment_id": "segment_0001",
        "planning_segment_id": "segment_0001_01",
        "planned_duration_ms": duration_ms,
        "content_description": "A local subject moves through the frame",
        "required_visible_subjects": [],
        "target_emotional_intensity": 0.5,
        "target_kinetic_energy": 0.5,
    }
    group = {
        "group_id": "group_01",
        "parent_group_id": "group_01",
        "source_segment_id": "segment_0001",
        "planning_segment_id": "segment_0001_01",
        "slot_ids": ["slot_01"],
    }
    segment = {
        "planning_segment_id": "segment_0001_01",
        "source_segment_id": "segment_0001",
        "start_ms": 0,
        "end_ms": 20000,
    }
    context = WorkflowContext(tmp_path / "workflow.json")
    context.set_artifact("planning_groups", [group])
    context.set_artifact("planning_segments", [segment])
    context.set_artifact(
        "video_description",
        {
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": 20.0},
                    "shots": [
                        {
                            "shot_id": "shot_00001",
                            "time_range": {"start_sec": 0.0, "end_sec": 20.0},
                        }
                    ],
                }
            ]
        },
    )
    return [slot], context


def _batch(count: int) -> dict[str, Any]:
    return {
        "trajectories": [
            {
                "items": [
                    {
                        "slot_id": "slot_01",
                        "source_start_ms": index * 4000,
                        "description": "The locally requested subject is moving",
                        "semantic_relevance": 0.9,
                        "emotional_intensity": 0.5,
                        "salience": 0.8,
                    }
                ]
            }
            for index in range(count)
        ]
    }


def _model_reply(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]):
    """Keep real call_prompt parsing, schema validation, and retry behavior."""
    requests: list[str] = []

    def generate(prompt: str, *_args, **_kwargs) -> ModelResponse:
        requests.append(prompt)
        return ModelResponse(content=json.dumps(payload), usage=None)

    monkeypatch.setattr(
        "cutmaster.workflow.shared.execution_context.generate_text", generate
    )
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )
    return requests


def _accept(trajectory, *_args, **_kwargs):
    return {
        **trajectory,
        "selection_score": 0.8,
        "items": [
            {**item, "selection_score": 0.8, "visual_evidence": "visible"}
            for item in trajectory["items"]
        ],
    }, []


def _retrieve(slots, context, *, media=None, config=None):
    return retrieve_candidates(
        slots,
        media if media is not None else object(),
        LLMConfig(model="offline", base_url="", api_key="offline", max_retries=2),
        VLMConfig(model="offline", base_url="", api_key="offline", max_retries=0),
        config or CandidateRetrievalConfig(),
        context,
    )


@pytest.mark.parametrize("returned_count", [1, 2, 3])
def test_underreturned_complete_batch_uses_real_prompt_contract_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returned_count: int
) -> None:
    slots, context = _setup(tmp_path)
    requests = _model_reply(monkeypatch, _batch(returned_count))
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept,
    )

    try:
        pool = _retrieve(slots, context)
    except RuntimeError as exc:
        pytest.fail(
            f"{returned_count} structurally complete trajectories were retried "
            f"{len(requests)} times and discarded: {exc}"
        )

    assert len(requests) == 1
    assert len(pool["group_01"]) == returned_count
    assert context.get_artifact("retrieval_failure") is None


@pytest.mark.parametrize("current_segment", ["segment_0001", "segment_0002"])
def test_replanned_slot_receives_scoped_history_without_forbidding_old_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current_segment: str
) -> None:
    slots, context = _setup(tmp_path)
    slots[0].update(
        group_id="group_replanned",
        source_segment_id=current_segment,
        planning_segment_id=f"{current_segment}_01",
        content_description="Spectators clap after the goal",
        required_visible_subjects=["spectators"],
    )
    group = dict(context.get_artifact("planning_groups")[0])
    group.update(
        group_id="group_replanned",
        source_segment_id=current_segment,
        planning_segment_id=f"{current_segment}_01",
    )
    segment = dict(context.get_artifact("planning_segments")[0])
    segment.update(
        source_segment_id=current_segment,
        planning_segment_id=f"{current_segment}_01",
    )
    context.set_artifact("planning_groups", [group])
    context.set_artifact("planning_segments", [segment])
    source = dict(context.get_artifact("video_description")["segments"][0])
    source["segment_id"] = current_segment
    context.set_artifact("video_description", {"segments": [source]})
    context.set_artifact(
        "planners_feedback",
        {
            "candidate_failure_evidence": [
                {
                    "slot_ids": ["slot_01"],
                    "source_segment_id": "segment_0001",
                    "reason_code": "required_subject_not_visually_confirmed",
                    "planned_content_description": "The goalkeeper catches the ball",
                    "required_visible_subjects": ["goalkeeper"],
                    "timestamp": "00:00:00,000-00:00:02,000",
                    "visible_description": "Spectators clap without the goalkeeper",
                    "visible_subjects": ["spectators"],
                    "visual_evidence": "Clapping fans fill the sampled frames",
                },
                {
                    "slot_ids": ["slot_99"],
                    "source_segment_id": current_segment,
                    "reason_code": "visually_static",
                    "diagnosis": "Unrelated other Slot failure must stay out",
                },
            ]
        },
    )
    requests = _model_reply(monkeypatch, _batch(1))
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept,
    )

    pool = _retrieve(slots, context)

    assert len(requests) == 1
    assert "The goalkeeper catches the ball" in requests[0]
    assert "Spectators clap without the goalkeeper" in requests[0]
    assert "Clapping fans fill the sampled frames" in requests[0]
    assert "required_subject_not_visually_confirmed" in requests[0]
    assert "Unrelated other Slot failure must stay out" not in requests[0]
    assert pool["group_replanned"][0]["items"][0]["timestamp"] == (
        "00:00:00,000-00:00:02,000"
    )


def test_visual_rejection_records_required_subjects_and_original_candidate_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots, context = _setup(tmp_path)
    slots[0]["required_visible_subjects"] = ["goalkeeper"]
    _model_reply(monkeypatch, _batch(1))

    def motion(_media, candidates, *_args, **_kwargs):
        for candidate in candidates:
            candidate["kinetic_energy"] = 0.6

    def visual(_media, _slots, candidates, *_args, **_kwargs):
        for candidate in candidates:
            candidate.update(
                description="Spectators clap without the goalkeeper",
                visible_subjects=["spectators"],
                visual_evidence="Clapping fans fill the sampled frames",
                protagonist_visibility_likert=1,
                visual_slot_relevance_likert=1,
            )

    monkeypatch.setattr("cutmaster.workflow.planners.timeline_scout.add_kinetic_features", motion)
    monkeypatch.setattr("cutmaster.workflow.planners.timeline_scout.add_visual_features", visual)

    with pytest.raises(GroupNoCandidateError) as captured:
        _retrieve(slots, context)

    rejection = captured.value.diagnostics["candidate_rejections"][0]["candidate_rejections"][0]
    assert rejection["required_visible_subjects"] == ["goalkeeper"]
    assert rejection["source_segment_id"] == "segment_0001"
    assert rejection["candidate_description"] == "The locally requested subject is moving"
    assert rejection["visible_description"] == "Spectators clap without the goalkeeper"
    assert captured.value.diagnostics["unavailable_source_segment_ids"] == []


def test_motion_decoder_exception_remains_execution_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots, context = _setup(tmp_path)
    requests = _model_reply(monkeypatch, _batch(3))

    class FailingMedia:
        def sample_frames(self, _times):
            raise RuntimeError("decode failed before motion was measured")

    with pytest.raises(RuntimeError, match="decode failed"):
        _retrieve(slots, context, media=FailingMedia())

    assert len(requests) == 1
    assert context.get_artifact("retrieval_failure") is None


def test_normally_returned_empty_batch_enters_semantic_zero_after_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots, context = _setup(tmp_path)
    requests = _model_reply(monkeypatch, _batch(0))

    try:
        with pytest.raises(GroupNoCandidateError) as captured:
            _retrieve(slots, context)
    except RuntimeError as exc:
        pytest.fail(
            f"A normally returned empty batch caused {len(requests)} model "
            f"requests instead of local repair: {exc}"
        )

    assert len(requests) == 1
    assert captured.value.diagnostics["semantic_zero_candidate_group_ids"] == [
        "group_01"
    ]
    assert captured.value.diagnostics["unavailable_source_segment_ids"] == []


@pytest.mark.parametrize("malformed", [None, [], 17, "broken"])
def test_nonobject_malformed_peer_does_not_retry_or_discard_complete_trajectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: Any
) -> None:
    slots, context = _setup(tmp_path)
    payload = _batch(2)
    payload["trajectories"].insert(1, malformed)
    requests = _model_reply(monkeypatch, payload)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept,
    )

    pool = _retrieve(slots, context)

    assert len(requests) == 1
    assert [trajectory["trajectory_id"] for trajectory in pool["group_01"]] == [
        "group_01_trajectory_01",
        "group_01_trajectory_03",
    ]
    rejections = context.get_artifact("candidate_rejections")
    assert len(rejections) == 1
    assert rejections[0]["reason_code"] == "response_validation_failed"
    assert rejections[0]["trajectory_id"] == "group_01_trajectory_02"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", {"invented": "object"}),
        ("description", ["not", "text"]),
        ("description", 123),
        ("description", True),
        ("description", None),
        ("description", ""),
        ("description", "   "),
        ("semantic_relevance", True),
        ("semantic_relevance", "0.5"),
        ("semantic_relevance", 100),
        ("semantic_relevance", -0.1),
        ("emotional_intensity", False),
        ("emotional_intensity", "0.5"),
        ("emotional_intensity", 1.1),
        ("salience", True),
        ("salience", "0.5"),
        ("salience", -1),
        ("source_start_ms", True),
        ("source_start_ms", "4000"),
        ("source_start_ms", 4000.5),
        ("slot_id", 1),
    ],
)
def test_wrongly_typed_or_out_of_range_fields_reject_only_the_bad_trajectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    slots, context = _setup(tmp_path)
    payload = _batch(2)
    payload["trajectories"][1]["items"][0][field] = value
    requests = _model_reply(monkeypatch, payload)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept,
    )

    pool = _retrieve(slots, context)

    assert len(requests) == 1
    assert [trajectory["trajectory_id"] for trajectory in pool["group_01"]] == [
        "group_01_trajectory_01"
    ]
    rejections = context.get_artifact("candidate_rejections")
    assert len(rejections) == 1
    assert rejections[0]["reason_code"] == "response_validation_failed"
    assert rejections[0]["trajectory_id"] == "group_01_trajectory_02"


@pytest.mark.parametrize("scope", ["trajectory", "item"])
def test_extra_fields_reject_only_the_bad_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    slots, context = _setup(tmp_path)
    payload = _batch(2)
    bad_record = payload["trajectories"][1]
    if scope == "item":
        bad_record = bad_record["items"][0]
    bad_record["unexpected_field"] = "not permitted by the complete contract"
    requests = _model_reply(monkeypatch, payload)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept,
    )

    pool = _retrieve(slots, context)

    assert len(requests) == 1
    assert len(pool["group_01"]) == 1
    assert pool["group_01"][0]["trajectory_id"] == "group_01_trajectory_01"
    assert len(context.get_artifact("candidate_rejections")) == 1


@pytest.mark.parametrize(
    "missing_field",
    [
        "slot_id",
        "source_start_ms",
        "description",
        "semantic_relevance",
        "emotional_intensity",
        "salience",
    ],
)
def test_missing_required_field_rejects_only_the_bad_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_field: str
) -> None:
    slots, context = _setup(tmp_path)
    payload = _batch(2)
    del payload["trajectories"][1]["items"][0][missing_field]
    requests = _model_reply(monkeypatch, payload)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept,
    )

    pool = _retrieve(slots, context)

    assert len(requests) == 1
    assert len(pool["group_01"]) == 1
    assert pool["group_01"][0]["trajectory_id"] == "group_01_trajectory_01"
    assert len(context.get_artifact("candidate_rejections")) == 1


def test_nonempty_all_malformed_batch_remains_response_failure_not_semantic_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots, context = _setup(tmp_path)
    requests = _model_reply(monkeypatch, {"trajectories": [None, [], 17]})

    with pytest.raises(RuntimeError, match="Every trajectory failed response validation"):
        _retrieve(slots, context)

    assert len(requests) == 3
    assert context.get_artifact("retrieval_failure") is None


@pytest.mark.parametrize("malformed_peer", [False, True])
def test_real_static_evidence_bans_segment_only_when_every_peer_is_well_formed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed_peer: bool
) -> None:
    slots, context = _setup(tmp_path)
    payload = _batch(3)
    if malformed_peer:
        payload["trajectories"][1] = None
    requests = _model_reply(monkeypatch, payload)

    class StaticMedia:
        def sample_frames(self, times):
            return [np.zeros((4, 4, 3), dtype=np.uint8) for _ in times]

    with pytest.raises(GroupNoCandidateError) as captured:
        _retrieve(slots, context, media=StaticMedia())

    assert len(requests) == 1
    assert captured.value.diagnostics["semantic_zero_candidate_group_ids"] == [
        "group_01"
    ]
    assert captured.value.diagnostics["unavailable_source_segment_ids"] == (
        [] if malformed_peer else ["segment_0001"]
    )


@pytest.mark.parametrize("returned_frames", [0, 1])
def test_missing_motion_samples_are_execution_failure_not_static_segment_ban(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returned_frames: int
) -> None:
    slots, context = _setup(tmp_path)
    _model_reply(monkeypatch, _batch(3))

    class MissingFrameMedia:
        def sample_frames(self, times):
            assert len(times) >= 2
            return [np.zeros((4, 4, 3), dtype=np.uint8)] * returned_frames

    with pytest.raises(RuntimeError):
        _retrieve(slots, context, media=MissingFrameMedia())

    assert context.get_artifact("retrieval_failure") is None


@pytest.mark.parametrize(
    ("duration_ms", "motion_fps"), [(500, 2.0), (2000, 0.1)]
)
def test_legal_clip_sampling_observes_motion_instead_of_treating_one_frame_as_static(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duration_ms: int,
    motion_fps: float,
) -> None:
    slots, context = _setup(tmp_path, duration_ms=duration_ms)
    _model_reply(monkeypatch, _batch(3))

    class MovingMedia:
        def sample_frames(self, times):
            return [
                np.full((4, 4, 3), int(t * 100) % 256, dtype=np.uint8)
                for t in times
            ]

    def accept_visual(_media, _slots, candidates, *_args, **_kwargs):
        for item in candidates:
            item.update(
                protagonist_visibility_likert=5,
                visual_slot_relevance_likert=5,
                visual_evidence="Real sampled movement is visible",
            )

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        accept_visual,
    )
    pool = _retrieve(
        slots,
        context,
        media=MovingMedia(),
        config=CandidateRetrievalConfig(motion_sample_fps=motion_fps),
    )

    assert len(pool["group_01"]) == 3
    assert context.get_artifact("retrieval_failure") is None
