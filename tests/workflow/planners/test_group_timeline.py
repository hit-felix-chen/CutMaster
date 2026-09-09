from __future__ import annotations

from typing import Any

import pytest

from cutmaster.configuration.schema import (
    CandidateRetrievalConfig,
    LLMConfig,
    VLMConfig,
)
from cutmaster.workflow.planners.timeline_scout import (
    _planning_units,
    _validate_complete_trajectory,
    _validate_trajectory_response,
    add_visual_features,
    retrieve_candidates,
)
from cutmaster.workflow.planners.tools.errors import GroupNoCandidateError


def _slots() -> list[dict[str, Any]]:
    return [
        {
            "slot_id": "slot_01",
            "group_id": "group_01",
            "parent_group_id": "group_01",
            "source_segment_id": "segment_0001",
            "planning_segment_id": "segment_0001_1",
            "planned_duration_ms": 2000,
            "content_description": "First beat",
            "required_visible_subjects": [],
            "target_emotional_intensity": 0.4,
            "target_kinetic_energy": 0.4,
        },
        {
            "slot_id": "slot_02",
            "group_id": "group_01",
            "parent_group_id": "group_01",
            "source_segment_id": "segment_0001",
            "planning_segment_id": "segment_0001_1",
            "planned_duration_ms": 2000,
            "content_description": "Second beat",
            "required_visible_subjects": [],
            "target_emotional_intensity": 0.6,
            "target_kinetic_energy": 0.6,
        },
    ]


def _video_description() -> dict[str, Any]:
    return {
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
    }


def _planning_groups() -> list[dict[str, Any]]:
    return [
        {
            "group_id": "group_01",
            "parent_group_id": "group_01",
            "source_segment_id": "segment_0001",
            "planning_segment_id": "segment_0001_1",
            "slot_ids": ["slot_01", "slot_02"],
        }
    ]


def _planning_segments() -> list[dict[str, Any]]:
    return [
        {
            "planning_segment_id": "segment_0001_1",
            "source_segment_id": "segment_0001",
            "start_ms": 0,
            "end_ms": 20000,
        }
    ]


class _Context:
    def __init__(self) -> None:
        self.artifacts: dict[str, Any] = {
            "video_description": _video_description(),
            "planning_groups": _planning_groups(),
            "planning_segments": _planning_segments(),
        }
        self.calls = 0
        self.packages: list[Any] = []
        self.fail_after: int | None = None

    def get_artifact(self, name: str) -> Any:
        return self.artifacts.get(name)

    def set_artifact(self, name: str, value: Any) -> None:
        self.artifacts[name] = value

    def call_prompt(self, *, validate_business, package, **_kwargs):
        self.calls += 1
        self.packages.append(package)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("no more distinct trajectories")
        requested_count = package.response_contract.schema["properties"][
            "trajectories"
        ]["maxItems"]
        round_offset_ms = (self.calls - 1) * 4000 * requested_count
        return validate_business(
            {
                "trajectories": [
                    {
                        "items": [
                            _raw_item("slot_01", offset_ms),
                            _raw_item("slot_02", offset_ms + 2000),
                        ]
                    }
                    for offset_ms in (
                        round_offset_ms + index * 4000
                        for index in range(requested_count)
                    )
                ]
            }
        )


class _TwoGroupContext(_Context):
    def __init__(self) -> None:
        super().__init__()
        second_segment = {
            **_video_description()["segments"][0],
            "segment_id": "segment_0002",
        }
        self.artifacts["video_description"]["segments"].append(second_segment)
        self.artifacts["planning_groups"] = [
            {
                "group_id": "group_01",
                "parent_group_id": "group_01",
                "source_segment_id": "segment_0001",
                "planning_segment_id": "segment_0001_1",
                "slot_ids": ["slot_01"],
            },
            {
                "group_id": "group_02",
                "parent_group_id": "group_02",
                "source_segment_id": "segment_0002",
                "planning_segment_id": "segment_0002_1",
                "slot_ids": ["slot_02"],
            },
        ]
        self.artifacts["planning_segments"] = [
            {
                "planning_segment_id": "segment_0001_1",
                "source_segment_id": "segment_0001",
                "start_ms": 0,
                "end_ms": 10000,
            },
            {
                "planning_segment_id": "segment_0002_1",
                "source_segment_id": "segment_0002",
                "start_ms": 0,
                "end_ms": 10000,
            },
        ]

    def call_prompt(self, *, validate_business, **_kwargs):
        self.calls += 1
        slot_id, start_ms = {
            1: ("slot_01", 8000),
            2: ("slot_02", 2000),
            3: ("slot_01", 0),
            4: ("slot_02", 4000),
        }[self.calls]
        return validate_business(
            {
                "trajectories": [
                    {"items": [_raw_item(slot_id, start_ms)]}
                ]
            }
        )


def _two_group_slots() -> list[dict[str, Any]]:
    first, second = _slots()
    first.update(
        group_id="group_01",
        parent_group_id="group_01",
        planning_segment_id="segment_0001_1",
    )
    second.update(
        group_id="group_02",
        parent_group_id="group_02",
        source_segment_id="segment_0002",
        planning_segment_id="segment_0002_1",
    )
    return [first, second]


def _raw_item(slot_id: str, start_ms: int) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "source_start_ms": start_ms,
        "description": f"Visible content for {slot_id}",
        "semantic_relevance": 0.8,
        "emotional_intensity": 0.5,
        "salience": 0.7,
    }


def test_planning_units_require_exact_order_and_integer_durations() -> None:
    context = _Context()

    units = _planning_units(_slots(), context)

    assert [slot["slot_id"] for slot in units[0]["slots"]] == [
        "slot_01",
        "slot_02",
    ]
    invalid = _slots()
    invalid[0]["planned_duration_ms"] = 2000.5
    with pytest.raises(ValueError, match="planned_duration_ms"):
        _planning_units(invalid, context)

    duplicate = _slots()
    duplicate[1]["slot_id"] = "slot_01"
    with pytest.raises(ValueError, match="Slot IDs must be unique"):
        _planning_units(duplicate, context)


def test_planning_group_cannot_cross_an_anchor_slot() -> None:
    context = _Context()
    slots = _slots()
    anchor = {
        "slot_id": "slot_anchor",
        "group_id": "group_01_anchor_01",
        "parent_group_id": "group_01",
        "source_segment_id": "segment_0001",
        "planned_duration_ms": 1000,
        "fixed_candidate": {"candidate_id": "anchor_01"},
    }

    with pytest.raises(ValueError, match="crosses a Slot boundary"):
        _planning_units([slots[0], anchor, slots[1]], context)


def test_planning_segments_must_respect_anchor_window() -> None:
    context = _Context()
    before, after = _slots()
    before.update(group_id="group_01_01", planning_segment_id="segment_0001_1")
    after.update(group_id="group_01_02", planning_segment_id="segment_0001_2")
    anchor = {
        "slot_id": "slot_anchor",
        "group_id": "group_01_anchor_01",
        "parent_group_id": "group_01",
        "source_segment_id": "segment_0001",
        "planned_duration_ms": 1000,
        "fixed_candidate": {
            "candidate_id": "anchor_01",
            "timestamp": "00:00:04,000-00:00:05,000",
        },
    }
    context.artifacts["planning_groups"] = [
        {
            **_planning_groups()[0],
            "group_id": "group_01_01",
            "planning_segment_id": "segment_0001_1",
            "slot_ids": ["slot_01"],
        },
        {
            **_planning_groups()[0],
            "group_id": "group_01_02",
            "planning_segment_id": "segment_0001_2",
            "slot_ids": ["slot_02"],
        },
    ]
    context.artifacts["planning_segments"] = [
        {**_planning_segments()[0], "planning_segment_id": "segment_0001_1"},
        {**_planning_segments()[0], "planning_segment_id": "segment_0001_2"},
    ]

    with pytest.raises(ValueError, match="crosses its next Anchor"):
        _planning_units([before, anchor, after], context)


def test_adjacent_slots_in_one_source_segment_must_share_one_group() -> None:
    context = _Context()
    first, second = _slots()
    first.update(group_id="group_01_01", planning_segment_id="segment_0001_1")
    second.update(group_id="group_01_02", planning_segment_id="segment_0001_2")
    context.artifacts["planning_groups"] = [
        {
            **_planning_groups()[0],
            "group_id": "group_01_01",
            "planning_segment_id": "segment_0001_1",
            "slot_ids": ["slot_01"],
        },
        {
            **_planning_groups()[0],
            "group_id": "group_01_02",
            "planning_segment_id": "segment_0001_2",
            "slot_ids": ["slot_02"],
        },
    ]
    context.artifacts["planning_segments"] = [
        {
            **_planning_segments()[0],
            "planning_segment_id": "segment_0001_1",
            "end_ms": 10000,
        },
        {
            **_planning_segments()[0],
            "planning_segment_id": "segment_0001_2",
            "start_ms": 10000,
        },
    ]

    with pytest.raises(ValueError, match="share a source Segment exactly"):
        _planning_units([first, second], context)


def test_source_segments_cannot_reverse_across_an_anchor() -> None:
    context = _Context()
    later = _slots()[0]
    later.update(
        group_id="group_02",
        parent_group_id="group_02",
        source_segment_id="segment_0002",
        planning_segment_id="segment_0002_1",
    )
    anchor = {
        "slot_id": "slot_anchor",
        "group_id": "group_01_anchor_01",
        "parent_group_id": "group_01",
        "source_segment_id": "segment_0001",
        "planned_duration_ms": 1000,
            "fixed_candidate": {
                "candidate_id": "anchor_01",
                "slot_id": "slot_anchor",
                "source_segment_id": "segment_0001",
                "timestamp": "00:00:01,000-00:00:02,000",
            },
            "dialogue_anchor": {
                "source_segment_id": "segment_0001",
                "source_video_timestamp": "00:00:01,000-00:00:02,000",
            },
    }
    second_segment = {
        **_video_description()["segments"][0],
        "segment_id": "segment_0002",
        "time_range": {"start_sec": 20.0, "end_sec": 40.0},
    }
    context.artifacts["video_description"]["segments"].append(second_segment)
    context.artifacts["planning_groups"] = [
        {
            "group_id": "group_02",
            "parent_group_id": "group_02",
            "source_segment_id": "segment_0002",
            "planning_segment_id": "segment_0002_1",
            "slot_ids": ["slot_01"],
        }
    ]
    context.artifacts["planning_segments"] = [
        {
            "planning_segment_id": "segment_0002_1",
            "source_segment_id": "segment_0002",
            "start_ms": 20000,
            "end_ms": 40000,
        }
    ]

    with pytest.raises(ValueError, match="monotonically nondecreasing"):
        _planning_units([later, anchor], context)


def test_trajectory_response_is_complete_and_app_identified() -> None:
    slots = _slots()
    slots[1]["planned_duration_ms"] = 3000
    parsed = {
        "trajectories": [
            {
                "items": [
                    _raw_item("slot_01", 1000),
                    _raw_item("slot_02", 3000),
                ]
            }
        ]
    }

    result = _validate_trajectory_response(
        parsed,
        group=_planning_groups()[0],
        slots=slots,
        planning_segment=_planning_segments()[0],
        source_segment=_video_description()["segments"][0],
        requested_count=1,
    )

    trajectory = result["trajectories"][0]
    assert trajectory["trajectory_id"] == "group_01_trajectory_01"
    assert [item["slot_id"] for item in trajectory["items"]] == [
        "slot_01",
        "slot_02",
    ]
    assert all(
        item["trajectory_id"] == trajectory["trajectory_id"]
        for item in trajectory["items"]
    )
    assert all(item["source_segment_id"] == "segment_0001" for item in trajectory["items"])
    assert [item["timestamp"] for item in trajectory["items"]] == [
        "00:00:01,000-00:00:03,000",
        "00:00:03,000-00:00:06,000",
    ]


def test_trajectory_response_rejects_broken_group_path() -> None:
    with pytest.raises(ValueError, match="exact group order"):
        _validate_trajectory_response(
            {
                "trajectories": [
                    {
                        "items": [
                            _raw_item("slot_02", 1000),
                            _raw_item("slot_01", 3000),
                        ]
                    }
                ]
            },
            group=_planning_groups()[0],
            slots=_slots(),
            planning_segment=_planning_segments()[0],
            source_segment=_video_description()["segments"][0],
            requested_count=1,
        )


def test_trajectory_response_deterministically_repairs_invalid_starts() -> None:
    result = _validate_trajectory_response(
        {
            "trajectories": [
                {
                    "items": [
                        _raw_item("slot_01", 19000),
                        _raw_item("slot_02", 19000),
                    ]
                }
            ]
        },
        group=_planning_groups()[0],
        slots=_slots(),
        planning_segment=_planning_segments()[0],
        source_segment=_video_description()["segments"][0],
        requested_count=1,
    )

    assert [
        item["timestamp"] for item in result["trajectories"][0]["items"]
    ] == [
        "00:00:16,000-00:00:18,000",
        "00:00:18,000-00:00:20,000",
    ]


def test_trajectory_response_rejects_nonfinite_scores() -> None:
    items = [_raw_item("slot_01", 1000), _raw_item("slot_02", 3000)]
    items[0]["semantic_relevance"] = float("nan")

    with pytest.raises(ValueError, match="semantic_relevance must be finite"):
        _validate_trajectory_response(
            {"trajectories": [{"items": items}]},
            group=_planning_groups()[0],
            slots=_slots(),
            planning_segment=_planning_segments()[0],
            source_segment=_video_description()["segments"][0],
            requested_count=1,
        )


def test_motion_execution_error_is_not_a_semantic_candidate_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trajectory = _validate_trajectory_response(
        {
            "trajectories": [
                {
                    "items": [
                        _raw_item("slot_01", 1000),
                        _raw_item("slot_02", 3000),
                    ]
                }
            ]
        },
        group=_planning_groups()[0],
        slots=_slots(),
        planning_segment=_planning_segments()[0],
        source_segment=_video_description()["segments"][0],
        requested_count=1,
    )["trajectories"][0]

    def fail_motion(*_args, **_kwargs):
        raise RuntimeError("motion decoder unavailable")

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        fail_motion,
    )

    with pytest.raises(RuntimeError, match="motion decoder unavailable"):
        _validate_complete_trajectory(
            trajectory,
            _slots(),
            object(),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(),
            _Context(),
        )


def test_visual_execution_error_is_not_a_semantic_candidate_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trajectory = _validate_trajectory_response(
        {
            "trajectories": [
                {
                    "items": [
                        _raw_item("slot_01", 1000),
                        _raw_item("slot_02", 3000),
                    ]
                }
            ]
        },
        group=_planning_groups()[0],
        slots=_slots(),
        planning_segment=_planning_segments()[0],
        source_segment=_video_description()["segments"][0],
        requested_count=1,
    )["trajectories"][0]

    def mark_moving(_media, candidates, *_args, **_kwargs):
        for candidate in candidates:
            candidate["kinetic_energy"] = 1.0

    def fail_visual(*_args, **_kwargs):
        raise RuntimeError("visual provider unavailable")

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        mark_moving,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        fail_visual,
    )

    with pytest.raises(RuntimeError, match="visual provider unavailable"):
        _validate_complete_trajectory(
            trajectory,
            _slots(),
            object(),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(),
            _Context(),
        )


def _retrieval_config(*, target: int = 3) -> CandidateRetrievalConfig:
    return CandidateRetrievalConfig(
        target_trajectories_per_group=target,
        static_kinetic_energy_threshold=0.0,
    )


def _accept_trajectory(trajectory, *_args, **_kwargs):
    accepted = dict(trajectory)
    accepted["selection_score"] = 0.8
    accepted["items"] = [
        {
            **item,
            "selection_score": 0.8,
            "visual_evidence": "Visible in sampled frames.",
        }
        for item in trajectory["items"]
    ]
    return accepted, []


def test_semantic_item_rejection_preserves_precise_slot_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trajectory = _validate_trajectory_response(
        {
            "trajectories": [
                {
                    "items": [
                        _raw_item("slot_01", 1000),
                        _raw_item("slot_02", 3000),
                    ]
                }
            ]
        },
        group=_planning_groups()[0],
        slots=_slots(),
        planning_segment=_planning_segments()[0],
        source_segment=_video_description()["segments"][0],
        requested_count=1,
    )["trajectories"][0]

    def mark_one_static(_media, candidates, *_args, **_kwargs):
        candidates[0]["kinetic_energy"] = 0.0
        candidates[1]["kinetic_energy"] = 1.0

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        mark_one_static,
    )

    accepted, failures = _validate_complete_trajectory(
        trajectory,
        _slots(),
        object(),
        VLMConfig(model="test", base_url="", api_key="test"),
        CandidateRetrievalConfig(static_kinetic_energy_threshold=0.1),
        _Context(),
    )

    assert accepted is None
    assert [failure["slot_id"] for failure in failures] == ["slot_01"]
    assert failures[0]["planned_content_description"] == "First beat"
    assert failures[0]["diagnostic_source"] == "local_motion"


def test_one_retrieval_requests_target_batch_and_validates_each_trajectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_Context):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            return validate_business(
                {
                    "trajectories": [
                        {
                            "items": [
                                _raw_item("slot_01", offset_ms),
                                _raw_item("slot_02", offset_ms + 2000),
                            ]
                        }
                        for offset_ms in (0, 4000, 8000)
                    ]
                }
            )

    validated_ids: list[str] = []

    def validate(trajectory, *_args, **_kwargs):
        validated_ids.append(trajectory["trajectory_id"])
        if trajectory["trajectory_id"].endswith("trajectory_02"):
            return None, [
                {
                    "candidate_id": trajectory["items"][0]["candidate_id"],
                    "reason_code": "test_rejection",
                }
            ]
        return _accept_trajectory(trajectory)

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        validate,
    )

    pool = retrieve_candidates(
        _slots(),
        object(),
        LLMConfig(model="test", base_url="", api_key="test"),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=3),
        context,
    )

    assert context.calls == 1
    trajectories_schema = context.packages[0].response_contract.schema[
        "properties"
    ]["trajectories"]
    assert trajectories_schema["minItems"] == 0
    assert trajectories_schema["maxItems"] == 3
    assert sorted(validated_ids) == [
        "group_01_trajectory_01",
        "group_01_trajectory_02",
        "group_01_trajectory_03",
    ]
    assert [trajectory["trajectory_id"] for trajectory in pool["group_01"]] == [
        "group_01_trajectory_01",
        "group_01_trajectory_03",
    ]
    rejections = context.artifacts["candidate_rejections"]
    assert len(rejections) == 1
    assert rejections[0]["trajectory_id"] == (
        "group_01_trajectory_02"
    )
    assert rejections[0]["candidate_rejections"][0]["reason_code"] == (
        "test_rejection"
    )


def test_overlapping_same_shot_trajectory_is_rejected_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_Context):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            return validate_business(
                {
                    "trajectories": [
                        {
                            "items": [
                                _raw_item("slot_01", offset_ms),
                                _raw_item("slot_02", offset_ms + 2000),
                            ]
                        }
                        for offset_ms in (0, 1000, 4000)
                    ]
                }
            )

    visually_validated_ids: list[str] = []

    def validate(trajectory, *_args, **_kwargs):
        visually_validated_ids.append(trajectory["trajectory_id"])
        return _accept_trajectory(trajectory)

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        validate,
    )

    pool = retrieve_candidates(
        _slots(),
        object(),
        LLMConfig(model="test", base_url="", api_key="test"),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=3),
        context,
    )

    assert visually_validated_ids == [
        "group_01_trajectory_01",
        "group_01_trajectory_03",
    ]
    assert [trajectory["trajectory_id"] for trajectory in pool["group_01"]] == [
        "group_01_trajectory_01",
        "group_01_trajectory_03",
    ]
    rejection = context.artifacts["candidate_rejections"][0]
    assert rejection["trajectory_id"] == "group_01_trajectory_02"
    assert {
        failure["reason_code"]
        for failure in rejection["candidate_rejections"]
    } == {"duplicate_candidate_range"}


def test_vlm_rejected_trajectory_does_not_claim_diversity_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_Context):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            return validate_business(
                {
                    "trajectories": [
                        {
                            "items": [
                                _raw_item("slot_01", offset_ms),
                                _raw_item("slot_02", offset_ms + 2000),
                            ]
                        }
                        for offset_ms in (0, 1000, 4000)
                    ]
                }
            )

    visually_validated_ids: list[str] = []

    def reject_first(trajectory, *_args, **_kwargs):
        visually_validated_ids.append(trajectory["trajectory_id"])
        if trajectory["trajectory_id"].endswith("trajectory_01"):
            return None, [{"reason_code": "test_vlm_rejection"}]
        return _accept_trajectory(trajectory)

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        reject_first,
    )

    pool = retrieve_candidates(
        _slots(),
        object(),
        LLMConfig(model="test", base_url="", api_key="test"),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=3),
        context,
    )

    assert visually_validated_ids == [
        "group_01_trajectory_01",
        "group_01_trajectory_02",
        "group_01_trajectory_03",
    ]
    assert [trajectory["trajectory_id"] for trajectory in pool["group_01"]] == [
        "group_01_trajectory_02",
        "group_01_trajectory_03",
    ]
    rejections = context.artifacts["candidate_rejections"]
    assert len(rejections) == 1
    assert rejections[0]["trajectory_id"].endswith("trajectory_01")


def test_invalid_trajectory_does_not_discard_valid_batch_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_Context):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            return validate_business(
                {
                    "trajectories": [
                        {
                            "items": [
                                _raw_item("slot_01", 0),
                                _raw_item("slot_02", 2000),
                            ]
                        },
                        {"broken": "trajectory"},
                        {
                            "items": [
                                _raw_item("slot_01", 4000),
                                _raw_item("slot_02", 6000),
                            ]
                        },
                    ]
                }
            )

    visually_validated_ids: list[str] = []

    def validate(trajectory, *_args, **_kwargs):
        visually_validated_ids.append(trajectory["trajectory_id"])
        return _accept_trajectory(trajectory)

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        validate,
    )

    pool = retrieve_candidates(
        _slots(),
        object(),
        LLMConfig(model="test", base_url="", api_key="test"),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=3),
        context,
    )

    assert visually_validated_ids == [
        "group_01_trajectory_01",
        "group_01_trajectory_03",
    ]
    assert [trajectory["trajectory_id"] for trajectory in pool["group_01"]] == [
        "group_01_trajectory_01",
        "group_01_trajectory_03",
    ]
    response_rejection = context.artifacts["candidate_rejections"][0]
    assert response_rejection["trajectory_id"].endswith("trajectory_02")
    assert response_rejection["reason_code"] == "response_validation_failed"
    assert "Trajectory must contain an items array" in response_rejection[
        "diagnosis"
    ]


def test_entire_malformed_batch_is_response_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_Context):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            return validate_business(
                {
                    "trajectories": [
                        {"broken": "trajectory"},
                        {"broken": "trajectory"},
                        {"broken": "trajectory"},
                    ]
                }
            )

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept_trajectory,
    )

    with pytest.raises(
        ValueError,
        match="Every trajectory failed response validation",
    ):
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(target=3),
            context,
        )

    assert context.calls == 1
    assert "retrieval_failure" not in context.artifacts


def test_exact_duplicate_batch_trajectories_skip_redundant_visual_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_Context):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            trajectory = {
                "items": [
                    _raw_item("slot_01", 0),
                    _raw_item("slot_02", 2000),
                ]
            }
            return validate_business(
                {"trajectories": [trajectory, trajectory, trajectory]}
            )

    visually_validated_ids: list[str] = []

    def validate(trajectory, *_args, **_kwargs):
        visually_validated_ids.append(trajectory["trajectory_id"])
        return _accept_trajectory(trajectory)

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        validate,
    )

    pool = retrieve_candidates(
        _slots(),
        object(),
        LLMConfig(model="test", base_url="", api_key="test"),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=3),
        context,
    )

    assert visually_validated_ids == [
        "group_01_trajectory_01"
    ]
    assert len(pool["group_01"]) == 1
    response_rejections = context.artifacts["candidate_rejections"]
    assert [
        rejection["trajectory_id"] for rejection in response_rejections
    ] == [
        "group_01_trajectory_02",
        "group_01_trajectory_03",
    ]
    assert all(
        rejection["reason_code"]
        == "no_candidate_passed_visual_diagnostics"
        for rejection in response_rejections
    )
    assert all(
        {
            failure["reason_code"]
            for failure in rejection["candidate_rejections"]
        }
        == {"duplicate_candidate_range"}
        for rejection in response_rejections
    )


def test_zero_slack_group_requests_its_single_distinct_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()
    context.artifacts["planning_segments"][0]["end_ms"] = 4000
    context.artifacts["video_description"]["segments"][0]["time_range"][
        "end_sec"
    ] = 4.0
    context.artifacts["video_description"]["segments"][0]["shots"][0][
        "time_range"
    ]["end_sec"] = 4.0
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept_trajectory,
    )

    pool = retrieve_candidates(
        _slots(),
        object(),
        LLMConfig(model="test", base_url="", api_key="test"),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=3),
        context,
    )

    trajectories_schema = context.packages[0].response_contract.schema[
        "properties"
    ]["trajectories"]
    assert trajectories_schema["minItems"] == 0
    assert trajectories_schema["maxItems"] == 1
    assert len(pool["group_01"]) == 1


def test_underfilled_nonempty_group_stops_after_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()

    def accept_only_first(trajectory, *_args, **_kwargs):
        if trajectory["trajectory_id"].endswith("trajectory_01"):
            return _accept_trajectory(trajectory)
        return None, [{"reason_code": "test_rejection"}]

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        accept_only_first,
    )

    pool = retrieve_candidates(
        _slots(),
        object(),
        LLMConfig(model="test", base_url="", api_key="test"),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(),
        context,
    )

    assert len(pool["group_01"]) == 1
    assert context.calls == 1
    assert context.artifacts["retrieval_summary"]["underfilled_group_ids"] == [
        "group_01"
    ]
    assert context.artifacts["retrieval_summary"][
        "retrieval_batches_completed"
    ] == 1


def test_all_trajectories_semantically_rejected_fails_after_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()
    context.artifacts["planning_segments"][0]["end_ms"] = 40000
    context.artifacts["video_description"]["segments"][0]["time_range"][
        "end_sec"
    ] = 40.0
    context.artifacts["video_description"]["segments"][0]["shots"][0][
        "time_range"
    ]["end_sec"] = 40.0

    def reject(trajectory, *_args, **_kwargs):
        return None, [
            {
                "slot_id": trajectory["items"][0]["slot_id"],
                "candidate_id": trajectory["items"][0]["candidate_id"],
                "reason_code": "test_rejection",
            }
        ]

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        reject,
    )

    with pytest.raises(GroupNoCandidateError) as captured:
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(target=3),
            context,
        )

    assert context.calls == 1
    assert captured.value.diagnostics[
        "semantic_zero_candidate_group_ids"
    ] == ["group_01"]
    assert captured.value.diagnostics["retrieval_batches_completed"] == 1
    assert len(context.artifacts["candidate_rejections"]) == 3


def test_all_static_batch_marks_source_segment_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()

    def reject_every_item_as_static(trajectory, *_args, **_kwargs):
        return None, [
            {
                "slot_id": item["slot_id"],
                "candidate_id": item["candidate_id"],
                "reason_code": "visually_static",
            }
            for item in trajectory["items"]
        ]

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        reject_every_item_as_static,
    )

    with pytest.raises(GroupNoCandidateError) as captured:
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(target=3),
            context,
        )

    assert captured.value.diagnostics["unavailable_source_segment_ids"] == [
        "segment_0001"
    ]


def test_partially_static_trajectory_does_not_mark_segment_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()

    def reject_only_one_item_as_static(trajectory, *_args, **_kwargs):
        item = trajectory["items"][0]
        return None, [
            {
                "slot_id": item["slot_id"],
                "candidate_id": item["candidate_id"],
                "reason_code": "visually_static",
            }
        ]

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        reject_only_one_item_as_static,
    )

    with pytest.raises(GroupNoCandidateError) as captured:
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(target=3),
            context,
        )

    assert captured.value.diagnostics["unavailable_source_segment_ids"] == []


def test_malformed_peer_prevents_static_segment_blacklist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_Context):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            return validate_business(
                {
                    "trajectories": [
                        {
                            "items": [
                                _raw_item("slot_01", 0),
                                _raw_item("slot_02", 2000),
                            ]
                        },
                        {"broken": "trajectory"},
                        {"broken": "trajectory"},
                    ]
                }
            )

    def reject_every_item_as_static(trajectory, *_args, **_kwargs):
        return None, [
            {
                "slot_id": item["slot_id"],
                "candidate_id": item["candidate_id"],
                "reason_code": "visually_static",
            }
            for item in trajectory["items"]
        ]

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        reject_every_item_as_static,
    )

    with pytest.raises(GroupNoCandidateError) as captured:
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(target=3),
            context,
        )

    assert captured.value.diagnostics["unavailable_source_segment_ids"] == []


def test_nonempty_group_does_not_retry_for_global_viability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _TwoGroupContext()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept_trajectory,
    )

    pool = retrieve_candidates(
        _two_group_slots(),
        object(),
        LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=1,
        ),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=1),
        context,
    )

    assert context.calls == 2
    assert all(len(trajectories) == 1 for trajectories in pool.values())
    assert context.artifacts["retrieval_summary"][
        "retrieval_batches_completed"
    ] == 1


def test_targeted_retrieval_stops_after_one_local_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_TwoGroupContext):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            start_ms = 8000 if self.calls == 1 else 0
            return validate_business(
                {
                    "trajectories": [
                        {"items": [_raw_item("slot_01", start_ms)]}
                    ]
                }
            )

    context = Context()
    seeded_trajectory = {
        "trajectory_id": "group_02_seed_trajectory_01",
        "group_id": "group_02",
        "planning_segment_id": "segment_0002_1",
        "items": [
            {
                "slot_id": "slot_02",
                "candidate_id": "slot_02_seed_candidate_01",
                "source_segment_id": "segment_0002",
                "timestamp": "00:00:04,000-00:00:06,000",
                "description": "Seeded reusable candidate",
                "selection_score": 0.8,
                "visual_evidence": "Visible in sampled frames.",
            }
        ],
    }
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept_trajectory,
    )

    pool = retrieve_candidates(
        _two_group_slots(),
        object(),
        LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=1,
        ),
        VLMConfig(model="test", base_url="", api_key="test"),
        _retrieval_config(target=1),
        context,
        target_group_ids={"group_01"},
        seed_candidate_pool={"group_02": [seeded_trajectory]},
    )

    assert context.calls == 1
    assert len(pool["group_01"]) == 1
    assert pool["group_01"][0]["items"][0]["timestamp"] == (
        "00:00:08,000-00:00:10,000"
    )
    assert pool["group_02"] == [seeded_trajectory]


def test_retrieval_provider_error_propagates_without_semantic_group_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()
    context.fail_after = 0
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept_trajectory,
    )

    with pytest.raises(RuntimeError, match="no more distinct trajectories"):
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(),
            context,
        )

    assert context.calls == 1
    assert "retrieval_failure" not in context.artifacts


def test_one_group_provider_error_propagates_without_discarding_peer_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Context(_TwoGroupContext):
        def call_prompt(self, *, validate_business, package, **_kwargs):
            self.calls += 1
            self.packages.append(package)
            if self.calls >= 2:
                raise RuntimeError("group_02 has no supported trajectory")
            return validate_business(
                {"trajectories": [{"items": [_raw_item("slot_01", 0)]}]}
            )

    context = Context()
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        _accept_trajectory,
    )

    with pytest.raises(
        RuntimeError,
        match="group_02 has no supported trajectory",
    ):
        retrieve_candidates(
            _two_group_slots(),
            object(),
            LLMConfig(
                model="test",
                base_url="",
                api_key="test",
                max_concurrency=1,
            ),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(target=1),
            context,
        )

    assert context.calls == 2
    assert "retrieval_failure" not in context.artifacts


def test_timeline_cancellation_is_not_treated_as_retrieval_failure() -> None:
    context = _Context()

    class CancelledToken:
        def raise_if_cancelled(self) -> None:
            raise RuntimeError("cancelled by user")

    with pytest.raises(RuntimeError, match="cancelled by user"):
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(),
            context,
            CancelledToken(),
        )

    assert context.calls == 0


def test_visual_rejection_immediately_returns_semantic_group_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _Context()

    def reject(trajectory, *_args, **_kwargs):
        return None, [{"reason_code": "test_rejection"}]

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._validate_complete_trajectory",
        reject,
    )

    with pytest.raises(GroupNoCandidateError) as captured:
        retrieve_candidates(
            _slots(),
            object(),
            LLMConfig(model="test", base_url="", api_key="test"),
            VLMConfig(model="test", base_url="", api_key="test"),
            _retrieval_config(target=1),
            context,
        )

    assert context.calls == 1
    assert captured.value.diagnostics["failed_group_ids"] == ["group_01"]
    assert captured.value.diagnostics[
        "semantic_zero_candidate_group_ids"
    ] == ["group_01"]
    assert captured.value.diagnostics["retrieval_batches_completed"] == 1
    assert len(context.artifacts["candidate_rejections"]) == 1
    assert all(
        rejection["group_id"] == "group_01"
        for rejection in context.artifacts["candidate_rejections"]
    )


def test_visual_validation_splits_provider_payload_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"Beat {index}",
            "required_visible_subjects": [],
        }
        for index in range(1, 5)
    ]
    candidates = [
        {
            "candidate_id": f"candidate_{index:02d}",
            "slot_id": f"slot_{index:02d}",
            "source_segment_id": "segment_0001",
            "timestamp": "00:00:01,000-00:00:03,000",
        }
        for index in range(1, 5)
    ]
    request_sizes: list[int] = []

    class Context:
        def get_artifact(self, name: str) -> Any:
            assert name == "video_description"
            return _video_description()

        def call_prompt(self, *, validate_business, image_labels, **_kwargs):
            request_sizes.append(len(image_labels))
            if len(image_labels) > 2:
                raise RuntimeError(
                    "Exceeded limit on max data-uri per request: 2"
                )
            return validate_business(
                {
                    "items": [
                        {
                            "candidate_id": candidate_id,
                            "visible_description": "Visible beat",
                            "visible_subjects": [],
                            "required_subject_visibility": 4,
                            "visual_slot_relevance": 4,
                            "visual_evidence": "Visible in frames",
                        }
                        for candidate_id in image_labels
                    ]
                }
            )

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._contact_sheet_data_url",
        lambda *_args, **_kwargs: "data:image/jpeg;base64,test",
    )

    add_visual_features(
        object(),
        slots,
        candidates,
        VLMConfig(model="test", base_url="", api_key="test"),
        Context(),
        sample_frames=4,
        operation="test trajectory",
    )

    assert request_sizes == [4, 2, 2]
    assert all(candidate["visual_evidence"] == "Visible in frames" for candidate in candidates)
