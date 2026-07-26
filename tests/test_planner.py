import json
import re
import threading

import pytest

from cutmaster.configuration.schema import (
    CandidateRetrievalConfig,
    LLMConfig,
    VLMConfig,
)
from cutmaster.planner.candidate_retrieval import (
    _retrieval_segment_context,
    _validate_candidates,
    _validate_visual_grounding,
    _window_capacity,
    retrieve_candidates,
)
from cutmaster.planner.script_review import review_and_patch
from cutmaster.planner.sequence_selection import (
    NoFeasiblePathError,
    _pair_key,
    _unary,
    path_to_script,
    precompute_pairwise_scores,
    select_paths,
    validate_chronological_path,
)
from cutmaster.planner.slot_planning import (
    _globally_align_boundaries,
    _validate_slots,
    align_slots_to_music,
)
from cutmaster.runtime.workflow_context import WorkflowContext


def _slots():
    return [
        {
            "slot_id": "slot_01",
            "content_description": "setup",
            "target_emotional_intensity": 0.2,
            "target_kinetic_energy": 0.2,
            "desired_duration_sec": 5,
        },
        {
            "slot_id": "slot_02",
            "content_description": "climax",
            "target_emotional_intensity": 0.9,
            "target_kinetic_energy": 0.9,
            "desired_duration_sec": 3,
        },
    ]


def _video_description():
    return {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
                "shots": [
                    {
                        "shot_id": f"shot_{index:05d}",
                        "time_range": {
                            "start_sec": float((index - 1) * 10),
                            "end_sec": float(index * 10),
                        },
                    }
                ],
            }
            for index in range(1, 4)
        ]
    }


def test_slot_validation_and_accent_alignment() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "setup",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.2,
                "target_kinetic_energy": 0.2,
                "desired_duration_sec": 5,
                "continuity_from_previous": "opening",
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": [],
            },
            {
                "narrative_role": "climax",
                "content_description": "climax",
                "target_emotion": "excited",
                "target_emotional_intensity": 0.9,
                "target_kinetic_energy": 0.9,
                "desired_duration_sec": 3,
                "continuity_from_previous": "rising action",
                "source_segment_ids": ["segment_0002"],
                "required_visible_subjects": [],
            },
        ]
    }
    slots = _validate_slots(
        raw,
        8.0,
        4.0,
        _video_description(),
        set(),
        set(),
    )
    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [5.0], "beats_sec": [4.0, 5.0]},
        8.0,
        30,
        4.0,
    )
    assert aligned[0]["output_end_sec"] == 5.0
    assert sum(slot["planned_duration_sec"] for slot in aligned) == 8.0


def test_slot_validation_rejects_failed_replan_assignments() -> None:
    raw = {
        "slots": [
            {
                "content_description": "The focal subject starts a journey",
                "narrative_role": "setup",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": 4,
                "continuity_from_previous": "opening",
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }
    with pytest.raises(ValueError, match="visually disproven"):
        _validate_slots(
            raw,
            4.0,
            4.0,
            _video_description(),
            {"segment_0001"},
            set(),
        )
    with pytest.raises(ValueError, match="failed source-Segment assignment"):
        _validate_slots(
            raw,
            4.0,
            4.0,
            _video_description(),
            set(),
            failed_segment_assignments={("segment_0001",)},
        )


def test_slot_validation_accepts_model_selected_slot_count() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "The focal subject begins",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.4,
                "target_kinetic_energy": 0.3,
                "desired_duration_sec": 3.0,
                "continuity_from_previous": "opening",
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": ["focal subject"],
            },
            {
                "narrative_role": "resolution",
                "content_description": "The focal subject succeeds",
                "target_emotion": "fulfilled",
                "target_emotional_intensity": 0.7,
                "target_kinetic_energy": 0.4,
                "desired_duration_sec": 5.0,
                "continuity_from_previous": "resolves the journey",
                "source_segment_ids": ["segment_0002"],
                "required_visible_subjects": ["focal subject"],
            },
        ]
    }

    slots = _validate_slots(
        raw,
        8.0,
        4.0,
        _video_description(),
        set(),
        set(),
    )

    assert len(slots) == 2
    assert sum(slot["desired_duration_sec"] for slot in slots) == 8.0


def test_slot_validation_rejects_duration_total_far_from_target() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "The focal subject begins",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.4,
                "target_kinetic_energy": 0.3,
                "desired_duration_sec": 4.0,
                "continuity_from_previous": "opening",
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }

    with pytest.raises(ValueError, match="must total"):
        _validate_slots(
            raw,
            8.0,
            4.0,
            _video_description(),
            set(),
            set(),
        )


def test_slot_validation_rejects_long_visual_slot_for_dialogue() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "development",
                "content_description": "A long exchange begins on synchronized lips",
                "target_emotion": "urgent",
                "target_emotional_intensity": 0.7,
                "target_kinetic_energy": 0.4,
                "desired_duration_sec": 8.0,
                "continuity_from_previous": "opening",
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }

    with pytest.raises(ValueError, match="start-aligned L-cut"):
        _validate_slots(
            raw,
            8.0,
            4.0,
            _video_description(),
            set(),
            set(),
        )


def test_slot_validation_rejects_average_far_from_visual_target() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "development",
                "content_description": f"Story beat {index}",
                "target_emotion": "urgent",
                "target_emotional_intensity": 0.7,
                "target_kinetic_energy": 0.4,
                "desired_duration_sec": 6.0,
                "continuity_from_previous": "continues",
                "source_segment_ids": [f"segment_{index:04d}"],
                "required_visible_subjects": [],
            }
            for index in range(1, 3)
        ]
    }

    with pytest.raises(ValueError, match="Average Slot duration"):
        _validate_slots(
            raw,
            12.0,
            4.0,
            _video_description(),
            set(),
            set(),
        )


def test_slot_average_allows_integer_count_quantization_for_short_output() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "development",
                "content_description": f"Short-output beat {index}",
                "target_emotion": "focused",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": 10.0 / 3.0,
                "continuity_from_previous": "continues",
                "source_segment_ids": [f"segment_{index:04d}"],
                "required_visible_subjects": [],
            }
            for index in range(1, 4)
        ]
    }

    slots = _validate_slots(
        raw,
        10.0,
        4.0,
        _video_description(),
        set(),
        set(),
    )

    assert len(slots) == 3


def test_candidate_retrieval_does_not_request_replacements_for_fixed_anchor(
    tmp_path,
    monkeypatch,
) -> None:
    fixed = {
        "candidate_id": "slot_01_dialogue_anchor",
        "slot_id": "slot_01",
        "timestamp": "00:00:01,000-00:00:05,000",
        "semantic_relevance": 0.8,
        "emotional_intensity": 0.5,
        "salience": 1.0,
        "protagonist_visibility_likert": 3,
        "visual_slot_relevance_likert": 4,
        "kinetic_energy": 0.2,
    }
    slots = [
        {
            "slot_id": "slot_01",
            "planned_duration_sec": 4.0,
            "fixed_candidate": fixed,
        }
    ]
    context = WorkflowContext(tmp_path / "planning_history.json")
    context.set_artifact("video_description", _video_description())
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_kinetic_features",
        lambda *args, **kwargs: None,
    )

    pool = retrieve_candidates(
        slots,
        tmp_path / "unused.mp4",
        LLMConfig(model="unused", base_url="", api_key="unused"),
        VLMConfig(model="unused", base_url="", api_key="unused"),
        CandidateRetrievalConfig(candidates_per_slot=3),
        context,
    )

    assert pool == {"slot_01": [fixed]}


def test_global_alignment_does_not_exhaust_later_accents() -> None:
    result = _globally_align_boundaries(
        [4.0, 8.0, 12.0],
        [3.8, 4.2, 7.9, 8.1, 11.9, 12.1],
        16.0,
        30,
    )
    assert len(result) == 3
    assert result == sorted(result)


def test_music_alignment_keeps_each_boundary_adjustment_local() -> None:
    slots = [
        {
            "slot_id": "slot_01",
            "desired_duration_sec": 4.0,
        },
        {
            "slot_id": "slot_02",
            "desired_duration_sec": 4.0,
        },
    ]

    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [1.0, 7.0], "beats_sec": [1.0, 7.0]},
        8.0,
        30,
        4.0,
    )

    assert aligned[0]["output_end_sec"] == 4.0
    assert aligned[1]["output_end_sec"] == 8.0


def test_chronology_preflight_accepts_a_complete_path() -> None:
    slots = _slots()
    for slot in slots:
        slot["planned_duration_sec"] = slot["desired_duration_sec"]
    pool = {
        "slot_01": [
            {"candidate_id": "a", "timestamp": "00:00:01,000-00:00:07,000", "description": "subject starts", "semantic_relevance": 1.0, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
            {"candidate_id": "b", "timestamp": "00:00:20,000-00:00:26,000", "description": "subject starts", "semantic_relevance": 0.95, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
        ],
        "slot_02": [
            {"candidate_id": "c", "timestamp": "00:00:10,000-00:00:14,000", "description": "subject climax", "semantic_relevance": 1.0, "emotional_intensity": 0.9, "kinetic_energy": 0.9, "salience": 1.0},
        ],
    }
    validate_chronological_path(slots, pool)


def test_beam_search_rejects_source_time_reversal() -> None:
    slots = _slots()
    for slot in slots:
        slot["planned_duration_sec"] = slot["desired_duration_sec"]
    pool = {
        "slot_01": [
            {"candidate_id": "a", "timestamp": "00:00:20,000-00:00:26,000", "description": "first", "semantic_relevance": 1.0, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
        ],
        "slot_02": [
            {"candidate_id": "b", "timestamp": "00:00:10,000-00:00:14,000", "description": "second", "semantic_relevance": 1.0, "emotional_intensity": 0.9, "kinetic_energy": 0.9, "salience": 1.0},
        ],
    }
    with pytest.raises(NoFeasiblePathError, match="chronological"):
        validate_chronological_path(slots, pool)


def test_beam_search_uses_precomputed_vlm_pairwise_scores() -> None:
    slots = _slots()
    for slot in slots:
        slot["planned_duration_sec"] = slot["desired_duration_sec"]
    shared = {
        "description": "focal subject",
        "semantic_relevance": 1.0,
        "visual_slot_relevance_likert": 5,
        "protagonist_visibility_likert": 5,
        "salience": 1.0,
    }
    pool = {
        "slot_01": [
            shared
            | {
                "candidate_id": "a",
                "slot_id": "slot_01",
                "timestamp": "00:00:01,000-00:00:06,000",
                "emotional_intensity": 0.2,
                "kinetic_energy": 0.2,
            },
            shared
            | {
                "candidate_id": "b",
                "slot_id": "slot_01",
                "timestamp": "00:00:07,000-00:00:12,000",
                "emotional_intensity": 0.2,
                "kinetic_energy": 0.2,
            },
        ],
        "slot_02": [
            shared
            | {
                "candidate_id": "c",
                "slot_id": "slot_02",
                "timestamp": "00:00:13,000-00:00:16,000",
                "emotional_intensity": 0.9,
                "kinetic_energy": 0.9,
            },
            shared
            | {
                "candidate_id": "d",
                "slot_id": "slot_02",
                "timestamp": "00:00:17,000-00:00:20,000",
                "emotional_intensity": 0.9,
                "kinetic_energy": 0.9,
            },
        ],
    }
    pairwise_scores = {
        _pair_key("a", "c"): {"pairwise_score": 0.1},
        _pair_key("a", "d"): {"pairwise_score": 0.2},
        _pair_key("b", "c"): {"pairwise_score": 0.3},
        _pair_key("b", "d"): {"pairwise_score": 0.9},
    }
    beam, diagnostics = select_paths(
        slots,
        pool,
        beam_width=4,
        pairwise_scores=pairwise_scores,
    )
    assert [item["candidate_id"] for item in beam] == ["b", "d"]
    assert diagnostics["pairwise_scoring"] == "precomputed_vlm_hard_cut"
    assert diagnostics["pairwise_score_count"] == 4
    assert diagnostics["beam_score"] == pytest.approx(1.56)


def test_pairwise_vlm_precompute_runs_boundaries_in_parallel(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"scene {index}",
            "continuity_from_previous": "direct progression",
            "target_kinetic_energy": 0.5,
        }
        for index in range(1, 4)
    ]
    pool = {
        slot["slot_id"]: [
            {
                "candidate_id": f"{slot['slot_id']}_candidate_01",
                "slot_id": slot["slot_id"],
                "timestamp": f"00:00:{index * 10:02d},000-00:00:{index * 10 + 4:02d},000",
                "description": f"visible scene {index}",
                "kinetic_energy": 0.5,
            }
        ]
        for index, slot in enumerate(slots, 1)
    }
    monkeypatch.setattr(
        "cutmaster.planner.sequence_selection._edge_contact_sheet_data_url",
        lambda *args, **kwargs: "data:image/jpeg;base64,stub",
    )
    barrier = threading.Barrier(2)
    worker_ids: set[int] = set()
    worker_lock = threading.Lock()

    class Context:
        def __init__(self) -> None:
            self.artifacts = {}

        def call_prompt(self, **kwargs):
            with worker_lock:
                worker_ids.add(threading.get_ident())
            barrier.wait(timeout=2)
            previous_id = kwargs["image_labels"][0].split()[0]
            current_id = kwargs["image_labels"][1].split()[0]
            parsed = {
                "items": [
                    {
                        "previous_candidate_id": previous_id,
                        "current_candidate_id": current_id,
                        "visual_continuity": 0.8,
                        "emotional_continuity": 0.7,
                        "narrative_bridge": 0.6,
                        "evidence": "coherent hard cut",
                    }
                ]
            }
            kwargs["package"].response_contract.validate_structure(parsed)
            return kwargs["validate_business"](parsed)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

    context = Context()
    scores = precompute_pairwise_scores(
        tmp_path / "video.mp4",
        slots,
        pool,
        VLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        context,
        sample_frames=2,
    )
    assert len(worker_ids) == 2
    assert len(scores) == 2
    assert all(item["pairwise_score"] == pytest.approx(0.765) for item in scores.values())
    assert context.artifacts["pairwise_scores"] == scores


def test_candidate_validation_requires_exact_duration_and_nonoverlap() -> None:
    slots = [_slots()[0] | {"planned_duration_sec": 5.0}]
    source_segments = {
        "slot_01": [
            {
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 8.0, "end_sec": 20.0},
                "shots": [
                    {
                        "shot_id": "shot_00001",
                        "time_range": {"start_sec": 8.0, "end_sec": 14.0},
                    },
                    {
                        "shot_id": "shot_00002",
                        "time_range": {"start_sec": 14.0, "end_sec": 20.0},
                    }
                ],
            }
        ]
    }
    raw_item = {
        "timestamp": "00:00:08,000-00:00:13,000",
        "description": "structured visual context",
        "matched_dialogue": "hello",
        "semantic_relevance": 0.8,
        "emotional_intensity": 0.3,
        "salience": 0.7,
    }
    with pytest.raises(ValueError, match="exactly 2"):
        _validate_candidates(
            {"candidates": [{"slot_id": "slot_01", "items": [raw_item]}]},
            slots,
            source_segments,
            2,
        )
    result = _validate_candidates(
        {"candidates": [{"slot_id": "slot_01", "items": [raw_item]}]},
        slots,
        source_segments,
        1,
    )
    assert result["slot_01"][0]["timestamp"] == "00:00:08,000-00:00:13,000"
    assert result["slot_01"][0]["source_shot_ids"] == ["shot_00001"]
    assert result["slot_01"][0]["source_segment_ids"] == ["segment_0001"]

    overlapping_item = {
        **raw_item,
        "timestamp": "00:00:12,000-00:00:17,000",
    }
    with pytest.raises(ValueError, match="overlap"):
        _validate_candidates(
            {
                "candidates": [
                    {
                        "slot_id": "slot_01",
                        "items": [raw_item, overlapping_item],
                    }
                ]
            },
            slots,
            source_segments,
            2,
        )

    with pytest.raises(ValueError, match="duration must equal"):
        _validate_candidates(
            {
                "candidates": [
                    {
                        "slot_id": "slot_01",
                        "items": [
                            {
                                **raw_item,
                                "timestamp": "00:00:08,000-00:00:14,000",
                            }
                        ],
                    }
                ]
            },
            slots,
            source_segments,
            1,
        )


def test_fixed_duration_window_capacity_accounts_for_exclusions() -> None:
    segments = [
        {
            "segment_id": "segment_0001",
            "time_range": {"start_sec": 0.0, "end_sec": 20.0},
            "shots": [],
        }
    ]

    assert _window_capacity(segments, 5.0, []) == 4
    assert _window_capacity(
        segments,
        5.0,
        ["00:00:05,000-00:00:10,000"],
    ) == 3


def test_visual_grounding_requires_integer_likert_scores() -> None:
    candidates = [{"candidate_id": "candidate_01"}]
    response = {
        "items": [
            {
                "candidate_id": "candidate_01",
                "visible_description": "the focal subject is visible",
                "visible_subjects": ["focal subject"],
                "required_subject_visibility": 4,
                "visual_slot_relevance": 4,
                "visual_evidence": "clear face in multiple frames",
            }
        ]
    }
    result = _validate_visual_grounding(response, candidates)
    assert result["candidate_01"]["protagonist_visibility_likert"] == 4
    assert result["candidate_01"]["visual_slot_relevance_likert"] == 4

    response["items"][0]["required_subject_visibility"] = 0.75
    with pytest.raises(ValueError, match="integer from 1 to 5"):
        _validate_visual_grounding(response, candidates)
    response["items"][0]["required_subject_visibility"] = 4
    response["items"][0]["visual_slot_relevance"] = 0.8
    with pytest.raises(ValueError, match="visual_slot_relevance.*integer from 1 to 5"):
        _validate_visual_grounding(response, candidates)


def test_retrieval_context_expands_only_when_adjacent_scope_is_requested() -> None:
    slots = [{"slot_id": "slot_01", "source_segment_ids": ["segment_0002"]}]
    planned_scope = _retrieval_segment_context(
        _video_description(),
        slots,
        include_adjacent=False,
    )
    adjacent_scope = _retrieval_segment_context(
        _video_description(),
        slots,
        include_adjacent=True,
    )
    assert [
        segment["segment_id"] for segment in planned_scope["slot_01"]
    ] == ["segment_0002"]
    assert [
        segment["segment_id"] for segment in adjacent_scope["slot_01"]
    ] == ["segment_0001", "segment_0002", "segment_0003"]


def test_candidate_retrieval_expands_without_calling_infeasible_round(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": [],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", _video_description())
    operations = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        operations.append(package.operation)
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in (
                            "00:00:00,000-00:00:04,000",
                            "00:00:10,000-00:00:14,000",
                            "00:00:20,000-00:00:24,000",
                        )
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": [],
                    "protagonist_visibility_likert": 2,
                    "visual_slot_relevance_likert": 4,
                    "visual_evidence": "sampled frames match",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=2,
        ),
        context,
    )

    assert operations == ["Candidate retrieval round 3 slot slot_01"]
    assert len(pool["slot_01"]) == 3


def test_vlm_rejection_retries_planned_segment_and_preserves_confirmed_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": ["focal subject"],
    }
    video_description = _video_description()
    segment = video_description["segments"][1]
    segment["time_range"]["end_sec"] = 30.0
    segment["shots"][0]["time_range"]["end_sec"] = 30.0
    video_description["segments"][2]["time_range"] = {
        "start_sec": 30.0,
        "end_sec": 40.0,
    }
    video_description["segments"][2]["shots"][0]["time_range"] = {
        "start_sec": 30.0,
        "end_sec": 40.0,
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", video_description)
    calls: list[dict[str, object]] = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        segment_match = re.search(
            r"<available_source_segments_by_slot>\n(.*?)\n"
            r"</available_source_segments_by_slot>",
            package.user_prompt,
            re.DOTALL,
        )
        confirmed_match = re.search(
            r"<confirmed_candidates>\n(.*?)\n</confirmed_candidates>",
            package.user_prompt,
            re.DOTALL,
        )
        assert segment_match is not None
        assert confirmed_match is not None
        supplied_segments = json.loads(segment_match.group(1))["slot_01"]
        confirmed = json.loads(confirmed_match.group(1))["slot_01"]
        calls.append(
            {
                "operation": package.operation,
                "segment_ids": [
                    item["segment_id"] for item in supplied_segments
                ],
                "confirmed": confirmed,
            }
        )
        timestamps = (
            [
                "00:00:10,000-00:00:14,000",
                "00:00:14,000-00:00:18,000",
            ]
            if len(calls) == 1
            else ["00:00:18,000-00:00:22,000"]
        )
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in timestamps
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            accepted = candidate["timestamp"] != "00:00:14,000-00:00:18,000"
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "clear identity" if accepted else "different identity"
                    ),
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=2,
            retrieval_max_rounds=2,
        ),
        context,
    )

    assert [call["segment_ids"] for call in calls] == [
        ["segment_0002"],
        ["segment_0002"],
    ]
    assert calls[0]["confirmed"] == []
    assert calls[1]["confirmed"] == [
        {
            "candidate_id": "slot_01_round_01_candidate_01",
            "timestamp": "00:00:10,000-00:00:14,000",
            "visible_description": "visible source content",
            "visual_evidence": "clear identity",
        }
    ]
    assert [candidate["timestamp"] for candidate in pool["slot_01"]] == [
        "00:00:10,000-00:00:14,000",
        "00:00:18,000-00:00:22,000",
    ]


def test_vlm_rejection_retries_adjacent_segment_scope(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": ["focal subject"],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", _video_description())
    operations: list[str] = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        operations.append(package.operation)
        timestamps = (
            [
                "00:00:00,000-00:00:04,000",
                "00:00:10,000-00:00:14,000",
                "00:00:20,000-00:00:24,000",
            ]
            if len(operations) == 1
            else [
                "00:00:04,000-00:00:08,000",
                "00:00:14,000-00:00:18,000",
            ]
        )
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in timestamps
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            accepted = (
                len(operations) > 1
                or candidate["timestamp"] == "00:00:10,000-00:00:14,000"
            )
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "clear identity" if accepted else "different identity"
                    ),
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=2,
        ),
        context,
    )

    assert operations == [
        "Candidate retrieval round 3 slot slot_01",
        "Candidate retrieval round 4 slot slot_01",
    ]
    assert len(pool["slot_01"]) == 3


def test_underfilled_nonempty_candidate_pool_continues_after_all_scopes(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": ["focal subject"],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", _video_description())

    def call_prompt(**kwargs):
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in (
                            "00:00:00,000-00:00:04,000",
                            "00:00:10,000-00:00:14,000",
                            "00:00:20,000-00:00:24,000",
                        )
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            accepted = candidate["timestamp"] == "00:00:10,000-00:00:14,000"
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "clear identity" if accepted else "different identity"
                    ),
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=1,
        ),
        context,
    )

    assert len(pool["slot_01"]) == 1
    assert context.get_artifact("retrieval_failure") == {
        "reason": "insufficient_visually_grounded_candidates",
        "shortages": {"slot_01": 2},
        "planned_segment_rounds": 1,
        "adjacent_expansion_rounds": 1,
    }


def test_candidate_retrieval_runs_one_slot_per_concurrent_model_request(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            **slot,
            "planned_duration_sec": 5.0,
            "source_segment_ids": [f"segment_{index:04d}"],
            "required_visible_subjects": [],
        }
        for index, slot in enumerate(
            [
                _slots()[0],
                _slots()[1],
                {
                    **_slots()[0],
                    "slot_id": "slot_03",
                    "content_description": "resolution",
                },
            ],
            1,
        )
    ]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", _video_description())
    barrier = threading.Barrier(len(slots))
    prompt_slot_ids = []
    visual_slot_ids = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        match = re.search(r"<slots>\n(.*?)\n</slots>", package.user_prompt, re.DOTALL)
        assert match is not None
        prompt_slots = json.loads(match.group(1))
        assert len(prompt_slots) == 1
        slot = prompt_slots[0]
        slot_id = slot["slot_id"]
        prompt_slot_ids.append(slot_id)
        barrier.wait(timeout=2)
        segment_index = int(slot["source_segment_ids"][0].split("_")[-1])
        start = (segment_index - 1) * 10
        response = {
            "candidates": [
                {
                    "slot_id": slot_id,
                    "items": [
                        {
                            "timestamp": (
                                f"00:00:{start:02d},000-00:00:{start + 5:02d},000"
                            ),
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, visual_slots, visual_pool, *_args, **_kwargs):
        assert len(visual_slots) == 1
        slot_id = visual_slots[0]["slot_id"]
        visual_slot_ids.append(slot_id)
        for candidate in visual_pool[slot_id]:
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": [],
                    "protagonist_visibility_likert": 2,
                    "visual_slot_relevance_likert": 4,
                    "visual_evidence": "sampled frames match",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.planner.candidate_retrieval.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        LLMConfig(
            model="text",
            base_url="",
            api_key="test",
            max_concurrency=3,
        ),
        VLMConfig(
            model="vision",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        CandidateRetrievalConfig(
            candidates_per_slot=1,
            retrieval_max_rounds=1,
        ),
        context,
    )

    assert set(prompt_slot_ids) == {"slot_01", "slot_02", "slot_03"}
    assert set(visual_slot_ids) == {"slot_01", "slot_02", "slot_03"}
    assert all(len(candidates) == 1 for candidates in pool.values())


def test_visual_likert_scores_are_normalized_for_unary() -> None:
    slot = _slots()[0] | {
        "planned_duration_sec": 5.0,
        "required_visible_subjects": ["focal subject"],
    }
    base = {
        "timestamp": "00:00:01,000-00:00:06,000",
        "description": "woman at a cafe",
        "semantic_relevance": 0.9,
        "visual_slot_relevance_likert": 5,
        "emotional_intensity": 0.2,
        "kinetic_energy": 0.2,
        "salience": 0.8,
    }
    assert _unary(slot, base | {"protagonist_visibility_likert": 5}) > _unary(
        slot, base | {"protagonist_visibility_likert": 1}
    )
    assert _unary(slot, base | {"protagonist_visibility_likert": 5}) > _unary(
        slot,
        base
        | {
            "protagonist_visibility_likert": 5,
            "visual_slot_relevance_likert": 1,
        },
    )


def test_unary_uses_requested_quality_weights() -> None:
    slot = _slots()[0] | {
        "planned_duration_sec": 5.0,
        "required_visible_subjects": ["focal subject"],
    }
    candidate = {
        "timestamp": "00:00:01,000-00:00:05,000",
        "semantic_relevance": 0.8,
        "visual_slot_relevance_likert": 4,
        "protagonist_visibility_likert": 3,
        "emotional_intensity": 0.4,
        "kinetic_energy": 0.5,
        "salience": 0.9,
    }
    assert _unary(slot, candidate) == pytest.approx(0.73)


def test_review_accepts_maximal_feasible_patch_subset(tmp_path, monkeypatch) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"scene {index}",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "planned_duration_sec": 5.0,
            "output_start_sec": float((index - 1) * 5),
            "output_end_sec": float(index * 5),
        }
        for index in range(1, 4)
    ]

    def candidate(slot_id, candidate_id, timestamp):
        return {
            "slot_id": slot_id,
            "candidate_id": candidate_id,
            "timestamp": timestamp,
            "description": "visible scene",
            "semantic_relevance": 0.9,
            "visual_slot_relevance_likert": 5,
            "protagonist_visibility_likert": 5,
            "emotional_intensity": 0.5,
            "kinetic_energy": 0.5,
            "salience": 0.9,
        }

    pool = {
        "slot_01": [
            candidate("slot_01", "slot_01_candidate_01", "00:00:00,000-00:00:05,000"),
            candidate("slot_01", "slot_01_candidate_02", "00:00:08,000-00:00:13,000"),
        ],
        "slot_02": [
            candidate("slot_02", "slot_02_candidate_01", "00:00:10,000-00:00:15,000"),
        ],
        "slot_03": [
            candidate("slot_03", "slot_03_candidate_01", "00:00:20,000-00:00:25,000"),
            candidate("slot_03", "slot_03_candidate_02", "00:00:30,000-00:00:35,000"),
        ],
    }
    original = path_to_script(
        slots,
        [pool["slot_01"][0], pool["slot_02"][0], pool["slot_03"][0]],
        __import__("pathlib").Path("video.mp4"),
    )
    context = WorkflowContext(tmp_path / "history.json")
    proposed = [
        {
            "operation": "replace",
            "slot_id": "slot_01",
            "candidate_id": "slot_01_candidate_02",
            "reason": "conflicts with slot 2",
        },
        {
            "operation": "replace",
            "slot_id": "slot_03",
            "candidate_id": "slot_03_candidate_02",
            "reason": "safe improvement",
        },
    ]
    monkeypatch.setattr(context, "call_prompt", lambda **_kwargs: proposed)
    pairwise_scores = {
        _pair_key("slot_01_candidate_01", "slot_02_candidate_01"): {
            "pairwise_score": 0.5
        },
        _pair_key("slot_02_candidate_01", "slot_03_candidate_01"): {
            "pairwise_score": 0.5
        },
        _pair_key("slot_02_candidate_01", "slot_03_candidate_02"): {
            "pairwise_score": 0.9
        },
    }
    patched, accepted = review_and_patch(
        slots,
        pool,
        original,
        __import__("cutmaster.configuration.schema", fromlist=["LLMConfig"]).LLMConfig(
            model="test", base_url="", api_key="test"
        ),
        context,
        pairwise_scores,
    )
    assert [patch["slot_id"] for patch in accepted] == ["slot_03"]
    assert patched[2]["candidate_id"] == "slot_03_candidate_02"
    assert context.data["script_versions"][-1]["rejected_patches"][0]["slot_id"] == "slot_01"


def test_review_rejects_patch_that_degrades_precomputed_hard_cut(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"scene {index}",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "planned_duration_sec": 4.0,
            "output_start_sec": float((index - 1) * 4),
            "output_end_sec": float(index * 4),
        }
        for index in range(1, 3)
    ]

    def candidate(slot_id, candidate_id, timestamp):
        return {
            "slot_id": slot_id,
            "candidate_id": candidate_id,
            "timestamp": timestamp,
            "description": "focal subject visible",
            "semantic_relevance": 1.0,
            "visual_slot_relevance_likert": 5,
            "protagonist_visibility_likert": 5,
            "emotional_intensity": 0.5,
            "kinetic_energy": 0.5,
            "salience": 1.0,
        }

    first = candidate("slot_01", "a", "00:00:01,000-00:00:05,000")
    original_second = candidate("slot_02", "b", "00:00:06,000-00:00:10,000")
    replacement_second = candidate("slot_02", "c", "00:00:11,000-00:00:15,000")
    pool = {
        "slot_01": [first],
        "slot_02": [original_second, replacement_second],
    }
    original = path_to_script(
        slots,
        [first, original_second],
        __import__("pathlib").Path("video.mp4"),
    )
    context = WorkflowContext(tmp_path / "history.json")
    monkeypatch.setattr(
        context,
        "call_prompt",
        lambda **_kwargs: [
            {
                "operation": "replace",
                "slot_id": "slot_02",
                "candidate_id": "c",
                "reason": "text-only preference",
            }
        ],
    )
    patched, accepted = review_and_patch(
        slots,
        pool,
        original,
        LLMConfig(model="test", base_url="", api_key="test"),
        context,
        {
            _pair_key("a", "b"): {"pairwise_score": 1.0},
            _pair_key("a", "c"): {"pairwise_score": 0.0},
        },
    )
    assert accepted == []
    assert patched[1]["candidate_id"] == "b"
    rejected = context.data["script_versions"][-1]["rejected_patches"]
    assert rejected[0]["rejection_reason"] == (
        "degrades_precomputed_hard_cut_path_score"
    )
