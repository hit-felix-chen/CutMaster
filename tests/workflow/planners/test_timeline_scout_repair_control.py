from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from cutmaster.configuration.schema import (
    CandidateRetrievalConfig,
    LLMConfig,
    VLMConfig,
)
from cutmaster.workflow.planners.timeline_scout import (
    _all_static_batch_source_segment_id,
    _candidate_duplicates_source_evidence,
    retrieve_candidates,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext


def _video_description() -> dict:
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
            for index in range(1, 5)
        ]
    }


def _slot(slot_id: str, segment_id: str) -> dict:
    return {
        "slot_id": slot_id,
        "narrative_role": "development",
        "content_description": f"supported event for {slot_id}",
        "target_emotion": "focused",
        "target_emotional_intensity": 0.6,
        "target_kinetic_energy": 0.6,
        "desired_duration_sec": 4.0,
        "planned_duration_sec": 4.0,
        "continuity_from_previous": "continues",
        "source_segment_ids": [segment_id],
        "required_visible_subjects": ["focal subject"],
    }


def _context(tmp_path) -> WorkflowContext:
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact("video_description", _video_description())
    return context


def _requested_candidate_count(user_prompt: str) -> int:
    match = re.search(r"Retrieve exactly (\d+) source candidates", user_prompt)
    assert match is not None
    return int(match.group(1))


def _prompt_slot(user_prompt: str) -> dict:
    match = re.search(r"<slots>\n(.*?)\n</slots>", user_prompt, re.DOTALL)
    assert match is not None
    return json.loads(match.group(1))[0]


def _candidate_response(slot: dict, count: int) -> dict:
    segment_number = int(slot["source_segment_ids"][0].rsplit("_", 1)[-1])
    start = float((segment_number - 1) * 10)
    return {
        "candidates": [
            {
                "slot_id": slot["slot_id"],
                "items": [
                    {
                        "timestamp": (
                            f"00:00:{int(start + offset):02d},000-"
                            f"00:00:{int(start + offset + 4):02d},000"
                        ),
                        "description": "visible source content",
                        "matched_dialogue": "",
                        "semantic_relevance": 0.8,
                        "emotional_intensity": 0.5,
                        "salience": 0.7,
                    }
                    for offset in range(count)
                ],
            }
        ]
    }


def _install_media_stubs(monkeypatch, *, accepted_starts: set[str]) -> None:
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    def add_visual_features(_media, slots, pool, *_args, **_kwargs):
        slot_id = slots[0]["slot_id"]
        for candidate in pool[slot_id]:
            accepted = candidate["timestamp"].split("-", 1)[0] in accepted_starts
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "the required subject is visible"
                        if accepted
                        else "the required subject is absent"
                    ),
                }
            )

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )


def _configs() -> tuple[LLMConfig, VLMConfig, CandidateRetrievalConfig]:
    return (
        LLMConfig(model="text", base_url="", api_key="test", max_concurrency=1),
        VLMConfig(model="vision", base_url="", api_key="test", max_concurrency=1),
        CandidateRetrievalConfig(
            candidates_per_slot=1,
            retrieval_max_rounds=0,
        ),
    )


def test_task006_near_identical_windows_share_one_shot_and_trigger_next_round(
    tmp_path,
    monkeypatch,
) -> None:
    video_description = {
        "segments": [
            {
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 1320.0, "end_sec": 1359.3},
                "shots": [
                    {
                        "shot_id": f"shot_before_{index}",
                        "time_range": {
                            "start_sec": start,
                            "end_sec": end,
                        },
                    }
                    for index, (start, end) in enumerate(
                        (
                            (1320.0, 1325.0),
                            (1325.0, 1330.0),
                            (1330.0, 1335.0),
                        ),
                        1,
                    )
                ],
            },
            {
                "segment_id": "segment_0002",
                "time_range": {"start_sec": 1359.3, "end_sec": 1363.916667},
                "shots": [
                    {
                        "shot_id": "shot_task006_slot02_logo",
                        "time_range": {
                            "start_sec": 1359.3,
                            "end_sec": 1363.916667,
                        },
                    }
                ],
            },
            {
                "segment_id": "segment_0003",
                "time_range": {"start_sec": 1363.916667, "end_sec": 1400.0},
                "shots": [
                    {
                        "shot_id": f"shot_after_{index}",
                        "time_range": {
                            "start_sec": start,
                            "end_sec": end,
                        },
                    }
                    for index, (start, end) in enumerate(
                        (
                            (1363.916667, 1369.0),
                            (1369.0, 1374.0),
                            (1374.0, 1379.0),
                        ),
                        1,
                    )
                ],
            },
        ]
    }
    slot = {
        **_slot("slot_02", "segment_0002"),
        "desired_duration_sec": 4.5,
        "planned_duration_sec": 4.5,
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact("video_description", video_description)
    requested_counts: list[int] = []

    def response(items: list[str]) -> dict:
        return {
            "candidates": [
                {
                    "slot_id": "slot_02",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in items
                    ],
                }
            ]
        }

    task006_windows = [
        "00:22:39,300-00:22:43,800",
        "00:22:39,350-00:22:43,850",
        "00:22:39,416-00:22:43,916",
    ]
    adjacent_windows = [
        "00:22:39,400-00:22:43,900",
        "00:22:05,000-00:22:09,500",
        "00:22:10,000-00:22:14,500",
        "00:22:44,000-00:22:48,500",
        "00:22:49,000-00:22:53,500",
        "00:22:54,000-00:22:58,500",
    ]

    def call_prompt(**kwargs):
        count = _requested_candidate_count(kwargs["package"].user_prompt)
        requested_counts.append(count)
        windows = task006_windows if len(requested_counts) == 1 else adjacent_windows
        assert len(windows) == count
        return kwargs["validate_business"](response(windows))

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    visual_batch_sizes: list[int] = []

    def add_visual_features(_media, slots, pool, *_args, **_kwargs):
        slot_id = slots[0]["slot_id"]
        visual_batch_sizes.append(len(pool[slot_id]))
        for candidate in pool[slot_id]:
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"],
                    "protagonist_visibility_likert": 5,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": "the required subject is visible",
                }
            )

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    text_config, visual_config, _retrieval_config = _configs()
    retrieval_config = CandidateRetrievalConfig(
        candidates_per_slot=3,
        retrieval_max_rounds=0,
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        retrieval_config,
        context,
    )

    assert requested_counts == [3, 6]
    assert visual_batch_sizes == [1, 5]
    assert len(pool["slot_02"]) == 3
    shot_sets = [
        set(candidate["source_shot_ids"])
        for candidate in pool["slot_02"]
    ]
    assert all(
        first.isdisjoint(second)
        for index, first in enumerate(shot_sets)
        for second in shot_sets[index + 1 :]
    )
    duplicate_rejections = [
        rejection
        for rejection in context.get_artifact("candidate_rejection_history")
        if rejection["reason_code"] == "duplicate_candidate_range"
    ]
    assert [item["timestamp"] for item in duplicate_rejections] == [
        "00:22:39,350-00:22:43,850",
        "00:22:39,416-00:22:43,916",
        "00:22:39,400-00:22:43,900",
    ]
    assert {
        item["conflicting_candidate_id"]
        for item in duplicate_rejections
    } == {"slot_02_round_01_candidate_01"}
    assert {
        tuple(item["source_shot_ids"])
        for item in duplicate_rejections
    } == {("shot_task006_slot02_logo",)}


def test_all_static_candidate_batch_blacklists_source_segment(
    tmp_path,
    monkeypatch,
) -> None:
    video_description = {
        "segments": [
            {
                "segment_id": "segment_0099",
                "time_range": {"start_sec": 0.0, "end_sec": 10.0},
                "shots": [
                    {
                        "shot_id": "shot_static_tail",
                        "time_range": {"start_sec": 0.0, "end_sec": 10.0},
                    }
                ],
            }
        ]
    }
    slot = _slot("slot_01", "segment_0099")
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact("video_description", video_description)
    retrieval_calls = 0

    def call_prompt(**kwargs):
        nonlocal retrieval_calls
        retrieval_calls += 1
        return kwargs["validate_business"](
            {
                "candidates": [
                    {
                        "slot_id": "slot_01",
                        "items": [
                            {
                                "timestamp": (
                                    f"00:00:0{offset},000-"
                                    f"00:00:0{offset + 4},000"
                                ),
                                "description": "static end slate",
                                "matched_dialogue": "",
                                "semantic_relevance": 0.8,
                                "emotional_intensity": 0.2,
                                "salience": 0.4,
                            }
                            for offset in range(3)
                        ],
                    }
                ]
            }
        )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.0})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        lambda *_args, **_kwargs: pytest.fail(
            "static candidates must be rejected before VLM scoring"
        ),
    )
    text_config, visual_config, _retrieval_config = _configs()

    with pytest.raises(
        ValueError,
        match="No usable candidate remains after visual diagnostics",
    ):
        retrieve_candidates(
            [slot],
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            CandidateRetrievalConfig(
                candidates_per_slot=3,
                retrieval_max_rounds=0,
            ),
            context,
        )

    assert retrieval_calls == 1
    assert context.get_artifact("unavailable_source_segment_ids") == [
        "segment_0099"
    ]
    failure = context.get_artifact("retrieval_failure")
    assert failure["unavailable_source_segment_ids"] == ["segment_0099"]


def test_all_static_batch_requires_one_unambiguous_source_segment() -> None:
    static_candidates = [
        {"source_segment_ids": ["segment_0099"]},
        {"source_segment_ids": ["segment_0099"]},
    ]

    assert (
        _all_static_batch_source_segment_id(
            static_candidates,
            static_candidates,
        )
        == "segment_0099"
    )
    assert (
        _all_static_batch_source_segment_id(
            static_candidates,
            static_candidates[:1],
        )
        is None
    )
    assert (
        _all_static_batch_source_segment_id(
            [
                {"source_segment_ids": ["segment_0098"]},
                {"source_segment_ids": ["segment_0099"]},
            ],
            [
                {"source_segment_ids": ["segment_0098"]},
                {"source_segment_ids": ["segment_0099"]},
            ],
        )
        is None
    )


def _static_followup_video_description() -> dict:
    return {
        "segments": [
            {
                "segment_id": "segment_0099",
                "time_range": {"start_sec": 0.0, "end_sec": 60.0},
                "segment_summary": "Post-match source content.",
                "shots": [
                    {
                        "shot_id": f"shot_0099_{index}",
                        "time_range": {
                            "start_sec": float((index - 1) * 20),
                            "end_sec": float(index * 20),
                        },
                        "camera_movement": "tracking",
                    }
                    for index in range(1, 4)
                ],
            }
        ]
    }


def _install_static_followup_run(
    context: WorkflowContext,
    monkeypatch,
) -> tuple[list[int], list[list[str]]]:
    requested_counts: list[int] = []
    visual_timestamps: list[list[str]] = []
    timestamps_by_round = [
        [
            "00:00:00,000-00:00:04,000",
            "00:00:05,000-00:00:09,000",
            "00:00:10,000-00:00:14,000",
        ],
        [
            "00:00:15,000-00:00:19,000",
            "00:00:20,000-00:00:24,000",
            "00:00:25,000-00:00:29,000",
            "00:00:30,000-00:00:34,000",
            "00:00:40,000-00:00:44,000",
            "00:00:45,000-00:00:49,000",
        ],
    ]

    def call_prompt(**kwargs):
        requested_count = _requested_candidate_count(
            kwargs["package"].user_prompt
        )
        requested_counts.append(requested_count)
        timestamps = timestamps_by_round[len(requested_counts) - 1]
        assert len(timestamps) == requested_count
        return kwargs["validate_business"](
            {
                "candidates": [
                    {
                        "slot_id": "slot_01",
                        "items": [
                            {
                                "timestamp": timestamp,
                                "description": "post-match source content",
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
        )

    def add_kinetic_features(_media, pool, *_args):
        for candidates in pool.values():
            for candidate in candidates:
                candidate["kinetic_energy"] = (
                    0.0145
                    if candidate["timestamp"].startswith("00:00:00,000")
                    else 0.0
                )

    def add_visual_features(_media, slots, pool, *_args, **_kwargs):
        slot_id = slots[0]["slot_id"]
        visual_timestamps.append(
            [candidate["timestamp"] for candidate in pool[slot_id]]
        )
        for candidate in pool[slot_id]:
            candidate.update(
                {
                    "description": "the focal subject is visible",
                    "visible_subjects": ["focal subject"],
                    "protagonist_visibility_likert": 5,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": "the required subject is visible",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        add_kinetic_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    return requested_counts, visual_timestamps


def test_later_all_static_batch_blacklists_segment_and_purges_earlier_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact(
        "video_description",
        _static_followup_video_description(),
    )
    requested_counts, visual_timestamps = _install_static_followup_run(
        context,
        monkeypatch,
    )
    text_config, visual_config, _retrieval_config = _configs()

    with pytest.raises(
        ValueError,
        match="No usable candidate remains after visual diagnostics",
    ):
        retrieve_candidates(
            [_slot("slot_01", "segment_0099")],
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            CandidateRetrievalConfig(
                candidates_per_slot=3,
                retrieval_max_rounds=0,
                static_kinetic_energy_threshold=0.01,
            ),
            context,
        )

    assert requested_counts == [3, 6]
    assert visual_timestamps == [["00:00:00,000-00:00:04,000"]]
    assert context.get_artifact("unavailable_source_segment_ids") == [
        "segment_0099"
    ]
    assert context.get_artifact("retrieval_failure")["failed_slot_ids"] == [
        "slot_01"
    ]


def test_fixed_candidate_cannot_bypass_unavailable_segment_exclusion(
    tmp_path,
    monkeypatch,
) -> None:
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact(
        "video_description",
        _static_followup_video_description(),
    )
    context.set_artifact("unavailable_source_segment_ids", ["segment_0099"])
    slot = _slot("slot_01", "segment_0099")
    slot["fixed_candidate"] = {
        "candidate_id": "fixed-static-anchor",
        "timestamp": "00:00:00,000-00:00:04,000",
        "source_segment_ids": ["segment_0099"],
    }
    monkeypatch.setattr(
        context,
        "call_prompt",
        lambda **_kwargs: pytest.fail("unavailable fixed media must fail locally"),
    )
    text_config, visual_config, retrieval_config = _configs()

    with pytest.raises(ValueError, match="Fixed candidates use unavailable"):
        retrieve_candidates(
            [slot],
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            retrieval_config,
            context,
        )

    assert context.get_artifact("retrieval_failure") == {
        "reason_code": "fixed_candidate_uses_unavailable_source_segment",
        "failed_slot_ids": ["slot_01"],
        "unavailable_source_segment_ids": ["segment_0099"],
    }


def test_candidate_diversity_without_shots_requires_non_overlapping_windows() -> None:
    accepted = {"timestamp": "00:00:00,000-00:00:04,000"}

    assert _candidate_duplicates_source_evidence(
        {"timestamp": "00:00:00,050-00:00:04,050"},
        accepted,
    )
    assert not _candidate_duplicates_source_evidence(
        {"timestamp": "00:00:05,000-00:00:09,000"},
        accepted,
    )


def test_candidate_diversity_allows_non_overlapping_windows_from_same_shot() -> None:
    accepted = {
        "timestamp": "00:00:00,000-00:00:04,000",
        "source_shot_ids": ["shot_00001"],
    }

    assert not _candidate_duplicates_source_evidence(
        {
            "timestamp": "00:00:05,000-00:00:09,000",
            "source_shot_ids": ["shot_00001"],
        },
        accepted,
    )
    assert _candidate_duplicates_source_evidence(
        {
            "timestamp": "00:00:03,000-00:00:07,000",
            "source_shot_ids": ["shot_00001"],
        },
        accepted,
    )
    assert not _candidate_duplicates_source_evidence(
        {
            "timestamp": "00:00:03,000-00:00:07,000",
            "source_shot_ids": ["shot_00002"],
        },
        accepted,
    )


def test_same_shot_non_overlapping_windows_both_reach_vlm(
    tmp_path,
    monkeypatch,
) -> None:
    slot = _slot("slot_01", "segment_0001")
    context = _context(tmp_path)
    prompt_calls = 0
    visual_batch_sizes: list[int] = []

    def response(timestamps: list[str]) -> dict:
        return {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "distinct visible source content",
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

    def call_prompt(**kwargs):
        nonlocal prompt_calls
        prompt_calls += 1
        timestamps = (
            [
                "00:00:00,000-00:00:04,000",
                "00:00:05,000-00:00:09,000",
            ]
            if prompt_calls == 1
            else [
                "00:00:10,000-00:00:14,000",
                "00:00:14,500-00:00:18,500",
                "00:00:16,000-00:00:20,000",
            ]
        )
        assert len(timestamps) == _requested_candidate_count(
            kwargs["package"].user_prompt
        )
        return kwargs["validate_business"](response(timestamps))

    def add_visual_features(_media, slots, pool, *_args, **_kwargs):
        slot_id = slots[0]["slot_id"]
        visual_batch_sizes.append(len(pool[slot_id]))
        for candidate in pool[slot_id]:
            candidate.update(
                {
                    "description": "the focal subject is visible",
                    "visible_subjects": ["focal subject"],
                    "protagonist_visibility_likert": 5,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": "distinct non-overlapping evidence",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    text_config, visual_config, _retrieval_config = _configs()

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        CandidateRetrievalConfig(
            candidates_per_slot=2,
            retrieval_max_rounds=0,
        ),
        context,
    )

    assert prompt_calls == 1
    assert visual_batch_sizes == [2]
    assert [candidate["timestamp"] for candidate in pool["slot_01"]] == [
        "00:00:00,000-00:00:04,000",
        "00:00:05,000-00:00:09,000",
    ]


def test_same_shot_non_overlapping_window_survives_cross_round_pool_check(
    tmp_path,
    monkeypatch,
) -> None:
    slot = _slot("slot_01", "segment_0001")
    context = _context(tmp_path)
    prompt_calls = 0
    visual_timestamps: list[list[str]] = []

    def response(timestamps: list[str]) -> dict:
        return {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "distinct visible source content",
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

    def call_prompt(**kwargs):
        nonlocal prompt_calls
        prompt_calls += 1
        timestamps = (
            [
                "00:00:00,000-00:00:04,000",
                "00:00:00,500-00:00:04,500",
            ]
            if prompt_calls == 1
            else ["00:00:05,000-00:00:09,000"]
        )
        assert len(timestamps) == _requested_candidate_count(
            kwargs["package"].user_prompt
        )
        return kwargs["validate_business"](response(timestamps))

    def add_visual_features(_media, slots, pool, *_args, **_kwargs):
        slot_id = slots[0]["slot_id"]
        visual_timestamps.append(
            [candidate["timestamp"] for candidate in pool[slot_id]]
        )
        for candidate in pool[slot_id]:
            candidate.update(
                {
                    "description": "the focal subject is visible",
                    "visible_subjects": ["focal subject"],
                    "protagonist_visibility_likert": 5,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": "distinct non-overlapping evidence",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    text_config, visual_config, _retrieval_config = _configs()

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        CandidateRetrievalConfig(
            candidates_per_slot=2,
            retrieval_max_rounds=1,
        ),
        context,
    )

    assert prompt_calls == 2
    assert visual_timestamps == [
        ["00:00:00,000-00:00:04,000"],
        ["00:00:05,000-00:00:09,000"],
    ]
    assert [candidate["timestamp"] for candidate in pool["slot_01"]] == [
        "00:00:00,000-00:00:04,000",
        "00:00:05,000-00:00:09,000",
    ]


def test_fixed_anchor_candidate_bypasses_diversity_retrieval(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slot("slot_01", "segment_0001"),
        "fixed_candidate": {
            "candidate_id": "slot_01_dialogue_anchor",
            "slot_id": "slot_01",
            "timestamp": "00:00:00,000-00:00:04,000",
        },
    }
    context = _context(tmp_path)
    monkeypatch.setattr(
        context,
        "call_prompt",
        lambda **_kwargs: pytest.fail("fixed anchors must not be retrieved again"),
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda *_args, **_kwargs: None,
    )
    text_config, visual_config, retrieval_config = _configs()

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        retrieval_config,
        context,
    )

    assert pool["slot_01"] == [slot["fixed_candidate"]]


def test_visual_slot_relevance_is_an_independent_hard_gate(
    tmp_path,
    monkeypatch,
) -> None:
    slot = _slot("slot_01", "segment_0001")
    context = _context(tmp_path)

    def call_prompt(**kwargs):
        package = kwargs["package"]
        prompt_slot = _prompt_slot(package.user_prompt)
        return kwargs["validate_business"](
            _candidate_response(
                prompt_slot,
                _requested_candidate_count(package.user_prompt),
            )
        )

    def add_visual_features(_media, visual_slots, pool, *_args, **_kwargs):
        for candidate in pool[visual_slots[0]["slot_id"]]:
            candidate.update(
                {
                    "description": "The focal subject is visible but idle.",
                    "visible_subjects": ["focal subject"],
                    "protagonist_visibility_likert": 5,
                    "visual_slot_relevance_likert": 2,
                    "visual_evidence": "The intended action is absent.",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    text_config, visual_config, retrieval_config = _configs()

    with pytest.raises(ValueError, match="No usable candidate remains"):
        retrieve_candidates(
            [slot],
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            retrieval_config,
            context,
        )

    rejection_history = context.get_artifact("candidate_rejection_history")
    assert rejection_history
    assert {
        rejection["reason_code"]
        for rejection in rejection_history
    } == {"visual_slot_not_relevant"}


def test_zero_first_round_uses_remaining_scope_and_accepts_partial_pool(
    tmp_path,
    monkeypatch,
) -> None:
    slot = _slot("slot_01", "segment_0002")
    context = _context(tmp_path)
    requested_counts: list[int] = []
    visual_round = 0

    def response(timestamps: list[str]) -> dict:
        return {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "candidate source evidence",
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

    planned_windows = [
        "00:00:10,000-00:00:14,000",
        "00:00:10,100-00:00:14,100",
        "00:00:10,200-00:00:14,200",
    ]
    adjacent_windows = [
        "00:00:00,000-00:00:04,000",
        "00:00:01,000-00:00:05,000",
        "00:00:02,000-00:00:06,000",
        "00:00:05,000-00:00:09,000",
        "00:00:13,000-00:00:17,000",
        "00:00:14,000-00:00:18,000",
        "00:00:20,000-00:00:24,000",
        "00:00:21,000-00:00:25,000",
        "00:00:22,000-00:00:26,000",
    ]

    def call_prompt(**kwargs):
        count = _requested_candidate_count(kwargs["package"].user_prompt)
        requested_counts.append(count)
        timestamps = planned_windows if len(requested_counts) == 1 else adjacent_windows
        assert len(timestamps) == count
        return kwargs["validate_business"](response(timestamps))

    def add_visual_features(_media, slots, pool, *_args, **_kwargs):
        nonlocal visual_round
        visual_round += 1
        for candidate in pool[slots[0]["slot_id"]]:
            accepted = (
                visual_round == 2
                and candidate["timestamp"].startswith("00:00:20,000")
            )
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "the subject is visible"
                        if accepted
                        else "the subject is absent"
                    ),
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    text_config, visual_config, _retrieval_config = _configs()

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=0,
        ),
        context,
        replan_slots=lambda *_args: pytest.fail(
            "a Slot with later retrieval scope remaining must not be replanned"
        ),
    )

    assert requested_counts == [3, 9]
    assert len(pool["slot_01"]) == 1
    assert pool["slot_01"][0]["timestamp"] == (
        "00:00:20,000-00:00:24,000"
    )
    assert context.get_artifact("retrieval_failure") is None
    assert context.get_artifact("candidate_shortages") == {"slot_01": 2}


def test_zero_pool_triggers_targeted_repair_only_after_all_base_rounds(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [_slot("slot_01", "segment_0001")]
    context = _context(tmp_path)
    prompt_calls = 0
    repair_calls = 0

    def response(timestamp: str) -> dict:
        return {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "candidate source evidence",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                    ],
                }
            ]
        }

    def call_prompt(**kwargs):
        nonlocal prompt_calls
        prompt_calls += 1
        timestamp = {
            1: "00:00:00,000-00:00:04,000",
            2: "00:00:10,000-00:00:14,000",
            3: "00:00:20,000-00:00:24,000",
        }[prompt_calls]
        return kwargs["validate_business"](response(timestamp))

    def add_visual_features(_media, visual_slots, pool, *_args, **_kwargs):
        for candidate in pool[visual_slots[0]["slot_id"]]:
            accepted = candidate["timestamp"].startswith("00:00:20,000")
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "the subject is visible"
                        if accepted
                        else "the subject is absent"
                    ),
                }
            )

    def replan_slots(current_slots, _failures):
        nonlocal repair_calls
        repair_calls += 1
        assert prompt_calls == 2, "repair must wait for planned and adjacent scopes"
        return [
            {
                **current_slots[0],
                "source_segment_ids": ["segment_0003"],
            }
        ], {"slot_01"}

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    text_config, visual_config, retrieval_config = _configs()

    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        retrieval_config,
        context,
        replan_slots=replan_slots,
    )

    assert prompt_calls == 3
    assert repair_calls == 1
    assert pool["slot_01"][0]["timestamp"] == (
        "00:00:20,000-00:00:24,000"
    )


def test_partial_repair_validation_continues_remaining_planned_rounds(
    tmp_path,
    monkeypatch,
) -> None:
    video_description = {
        "segments": [
            {
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 0.0, "end_sec": 4.0},
                "shots": [
                    {
                        "shot_id": "shot_too_short",
                        "time_range": {"start_sec": 0.0, "end_sec": 4.0},
                    }
                ],
            },
            {
                "segment_id": "segment_0002",
                "time_range": {"start_sec": 10.0, "end_sec": 30.0},
                "shots": [
                    {
                        "shot_id": "shot_repair_a",
                        "time_range": {"start_sec": 10.0, "end_sec": 16.0},
                    },
                    {
                        "shot_id": "shot_repair_b",
                        "time_range": {"start_sec": 16.0, "end_sec": 22.0},
                    },
                    {
                        "shot_id": "shot_repair_c",
                        "time_range": {"start_sec": 22.0, "end_sec": 30.0},
                    },
                ],
            },
            {
                "segment_id": "segment_0003",
                "time_range": {"start_sec": 30.0, "end_sec": 40.0},
                "shots": [
                    {
                        "shot_id": "shot_after",
                        "time_range": {"start_sec": 30.0, "end_sec": 40.0},
                    }
                ],
            },
        ]
    }
    slots = [_slot("slot_01", "segment_0001")]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact("video_description", video_description)
    prompt_calls = 0
    repair_calls = 0
    operations: list[str] = []

    def response(timestamps: list[str]) -> dict:
        return {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "candidate source evidence",
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

    def call_prompt(**kwargs):
        nonlocal prompt_calls
        prompt_calls += 1
        operations.append(kwargs["package"].operation)
        timestamps = (
            [
                "00:00:10,000-00:00:14,000",
                "00:00:16,000-00:00:20,000",
                "00:00:22,000-00:00:26,000",
            ]
            if prompt_calls == 1
            else [
                "00:00:17,000-00:00:21,000",
                "00:00:23,000-00:00:27,000",
            ]
        )
        assert len(timestamps) == _requested_candidate_count(
            kwargs["package"].user_prompt
        )
        return kwargs["validate_business"](response(timestamps))

    def add_visual_features(_media, visual_slots, pool, *_args, **_kwargs):
        for candidate in pool[visual_slots[0]["slot_id"]]:
            accepted = prompt_calls > 1 or candidate["timestamp"].startswith(
                "00:00:10,000"
            )
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "the subject is visible"
                        if accepted
                        else "the subject is absent"
                    ),
                }
            )

    def replan_slots(current_slots, _failures):
        nonlocal repair_calls
        repair_calls += 1
        return [
            {
                **current_slots[0],
                "source_segment_ids": ["segment_0002"],
            }
        ], {"slot_01"}

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    text_config, visual_config, _retrieval_config = _configs()

    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=1,
        ),
        context,
        replan_slots=replan_slots,
    )

    assert repair_calls == 1
    assert prompt_calls == 2
    assert operations == [
        "Candidate retrieval round 2 slot slot_01",
        "Candidate retrieval round 3 slot slot_01",
    ]
    assert len(pool["slot_01"]) == 3


def test_final_round_repair_gets_one_validation_retrieval_before_failure_check(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [_slot("slot_01", "segment_0002")]
    context = _context(tmp_path)
    operations: list[str] = []
    call_count = 0

    def call_prompt(**kwargs):
        nonlocal call_count
        call_count += 1
        package = kwargs["package"]
        operations.append(package.operation)
        if call_count == 1:
            raise RuntimeError("force the initial planned-scope call to fail")
        slot = _prompt_slot(package.user_prompt)
        response = _candidate_response(
            slot,
            _requested_candidate_count(package.user_prompt),
        )
        return kwargs["validate_business"](response)

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    _install_media_stubs(
        monkeypatch,
        accepted_starts={"00:00:20,000"},
    )
    repair_calls: list[list[dict]] = []

    def replan_slots(current_slots, failures):
        repair_calls.append(failures)
        return [
            {
                **current_slots[0],
                "source_segment_ids": ["segment_0003"],
            }
        ], {"slot_01"}

    text_config, visual_config, retrieval_config = _configs()
    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        retrieval_config,
        context,
        replan_slots=replan_slots,
    )

    assert len(repair_calls) == 1
    assert len(operations) == 3
    assert [candidate["timestamp"] for candidate in pool["slot_01"]] == [
        "00:00:20,000-00:00:24,000"
    ]


def test_targeted_repair_preserves_unchanged_neighbor_pool_and_rejection_memory(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        _slot("slot_01", "segment_0001"),
        _slot("slot_02", "segment_0002"),
    ]
    context = _context(tmp_path)
    calls_by_slot: Counter[str] = Counter()

    def call_prompt(**kwargs):
        package = kwargs["package"]
        slot = _prompt_slot(package.user_prompt)
        calls_by_slot[slot["slot_id"]] += 1
        response = _candidate_response(
            slot,
            _requested_candidate_count(package.user_prompt),
        )
        return kwargs["validate_business"](response)

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    _install_media_stubs(
        monkeypatch,
        accepted_starts={"00:00:00,000", "00:00:20,000"},
    )
    repair_calls: list[list[dict]] = []

    def replan_slots(current_slots, failures):
        repair_calls.append(failures)
        return [
            current_slots[0],
            {
                **current_slots[1],
                "source_segment_ids": ["segment_0003"],
            },
        ], {"slot_01", "slot_02"}

    text_config, visual_config, retrieval_config = _configs()
    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        retrieval_config,
        context,
        replan_slots=replan_slots,
    )

    assert len(repair_calls) == 1
    assert calls_by_slot == Counter({"slot_02": 3, "slot_01": 1})
    assert [candidate["timestamp"] for candidate in pool["slot_01"]] == [
        "00:00:00,000-00:00:04,000"
    ]
    assert [candidate["timestamp"] for candidate in pool["slot_02"]] == [
        "00:00:20,000-00:00:24,000"
    ]
    rejections = context.get_artifact("candidate_rejection_history")
    assert len(rejections) == 1
    assert rejections[0]["slot_id"] == "slot_02"
    assert rejections[0]["reason_code"] == "required_subject_not_visually_confirmed"
    assert repair_calls[0][0]["failed_source_segment_ids_by_slot"] == {
        "slot_02": ["segment_0002"]
    }


def test_noop_targeted_repair_stops_without_repeating_same_retrieval(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [_slot("slot_01", "segment_0001")]
    context = _context(tmp_path)
    prompt_calls = 0
    repair_calls = 0

    def call_prompt(**kwargs):
        nonlocal prompt_calls
        prompt_calls += 1
        package = kwargs["package"]
        slot = _prompt_slot(package.user_prompt)
        response = _candidate_response(
            slot,
            _requested_candidate_count(package.user_prompt),
        )
        return kwargs["validate_business"](response)

    def replan_slots(current_slots, _failures):
        nonlocal repair_calls
        repair_calls += 1
        return [dict(current_slots[0])], {"slot_01"}

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    _install_media_stubs(monkeypatch, accepted_starts=set())
    text_config, visual_config, retrieval_config = _configs()

    with pytest.raises(
        ValueError,
        match="No usable candidate remains after visual diagnostics",
    ):
        retrieve_candidates(
            slots,
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            retrieval_config,
            context,
            replan_slots=replan_slots,
        )

    assert repair_calls == 1
    assert prompt_calls == 2
    assert context.get_artifact("candidate_rejections")


def test_failed_repair_validation_does_not_trigger_a_second_targeted_repair(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [_slot("slot_01", "segment_0001")]
    context = _context(tmp_path)
    prompt_calls = 0
    repair_calls = 0

    def call_prompt(**kwargs):
        nonlocal prompt_calls
        prompt_calls += 1
        package = kwargs["package"]
        slot = _prompt_slot(package.user_prompt)
        response = _candidate_response(
            slot,
            _requested_candidate_count(package.user_prompt),
        )
        return kwargs["validate_business"](response)

    def replan_slots(current_slots, _failures):
        nonlocal repair_calls
        repair_calls += 1
        assert repair_calls == 1, "the same Slot must not be repaired twice"
        return [
            {
                **current_slots[0],
                "source_segment_ids": ["segment_0002"],
            }
        ], {"slot_01"}

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    _install_media_stubs(monkeypatch, accepted_starts=set())
    text_config, visual_config, retrieval_config = _configs()

    with pytest.raises(
        ValueError,
        match="No usable candidate remains after visual diagnostics",
    ):
        retrieve_candidates(
            slots,
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            retrieval_config,
            context,
            replan_slots=replan_slots,
        )

    assert repair_calls == 1
    assert prompt_calls == 3
    assert len(context.get_artifact("candidate_rejection_history")) == 2
    assert len(context.get_artifact("candidate_rejections")) == 1


def test_previously_repaired_slot_can_move_later_as_a_required_blocker(
    tmp_path,
    monkeypatch,
) -> None:
    video_description = _video_description()
    video_description["segments"][0]["time_range"]["end_sec"] = 3.0
    video_description["segments"][0]["shots"][0]["time_range"][
        "end_sec"
    ] = 3.0
    slots = [
        _slot("slot_01", "segment_0001"),
        _slot("slot_02", "segment_0003"),
    ]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact("video_description", video_description)
    repair_calls: list[list[dict]] = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        slot = _prompt_slot(package.user_prompt)
        return kwargs["validate_business"](
            _candidate_response(
                slot,
                _requested_candidate_count(package.user_prompt),
            )
        )

    def add_visual_features(_media, visual_slots, pool, *_args, **_kwargs):
        slot = visual_slots[0]
        accepted = str(slot["content_description"]).startswith(
            ("first repair", "joint repair")
        )
        for candidate in pool[slot["slot_id"]]:
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "the required subject is visible"
                        if accepted
                        else "the required subject is absent"
                    ),
                }
            )

    def replan_slots(current_slots, failures):
        repair_calls.append(failures)
        if len(repair_calls) == 1:
            assert {failure["slot_id"] for failure in failures} == {"slot_01"}
            return [
                {
                    **current_slots[0],
                    "content_description": "first repair slot_01",
                    "source_segment_ids": ["segment_0002"],
                },
                current_slots[1],
            ], {"slot_01"}
        assert len(repair_calls) == 2
        assert {failure["slot_id"] for failure in failures} == {"slot_02"}
        return [
            {
                **current_slots[0],
                "content_description": "joint repair slot_01",
                "source_segment_ids": ["segment_0003"],
            },
            {
                **current_slots[1],
                "content_description": "joint repair slot_02",
                "source_segment_ids": ["segment_0004"],
            },
        ], {"slot_01", "slot_02"}

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _media, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )
    text_config, visual_config, retrieval_config = _configs()

    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        text_config,
        visual_config,
        retrieval_config,
        context,
        replan_slots=replan_slots,
    )

    assert len(repair_calls) == 2
    assert [slot["source_segment_ids"] for slot in slots] == [
        ["segment_0003"],
        ["segment_0004"],
    ]
    assert all(len(candidates) == 1 for candidates in pool.values())


def test_empty_repair_validation_stops_unrelated_remaining_rounds(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        _slot("slot_01", "segment_0001"),
        _slot("slot_02", "segment_0003"),
    ]
    context = _context(tmp_path)
    calls_by_slot: Counter[str] = Counter()
    repair_calls = 0
    repair_failures: list[list[dict]] = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        slot = _prompt_slot(package.user_prompt)
        slot_id = slot["slot_id"]
        calls_by_slot[slot_id] += 1
        if slot_id == "slot_02":
            raise RuntimeError("simulate a transient retrieval failure")
        response = _candidate_response(
            slot,
            _requested_candidate_count(package.user_prompt),
        )
        return kwargs["validate_business"](response)

    def replan_slots(current_slots, failures):
        nonlocal repair_calls
        repair_calls += 1
        repair_failures.append(failures)
        return [
            {
                **current_slots[0],
                "source_segment_ids": ["segment_0002"],
            },
            current_slots[1],
        ], {"slot_01"}

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    _install_media_stubs(monkeypatch, accepted_starts=set())
    text_config, visual_config, retrieval_config = _configs()

    with pytest.raises(
        ValueError,
        match="No usable candidate remains after visual diagnostics",
    ):
        retrieve_candidates(
            slots,
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            retrieval_config,
            context,
            replan_slots=replan_slots,
        )

    assert repair_calls == 2
    assert repair_failures[1][0]["blocker_slot_ids_by_slot"] == {
        "slot_01": ["slot_02"]
    }
    assert repair_failures[1][0]["failed_source_segment_ids"] == [
        "segment_0001",
        "segment_0002",
    ]
    assert repair_failures[1][0]["failed_source_segment_ids_by_slot"] == {
        "slot_01": ["segment_0001", "segment_0002"],
        "slot_02": ["segment_0003"],
    }
    assert calls_by_slot == Counter({"slot_01": 3, "slot_02": 2})
    failure = context.get_artifact("retrieval_failure")
    assert failure["unrepairable"]["reason_code"] == (
        "targeted_repair_domain_unrepairable"
    )


def test_targeted_repair_model_failure_is_outer_attempt_retriable(
    tmp_path,
    monkeypatch,
) -> None:
    slot = _slot("slot_01", "segment_0001")
    context = _context(tmp_path)

    def call_prompt(**kwargs):
        package = kwargs["package"]
        prompt_slot = _prompt_slot(package.user_prompt)
        return kwargs["validate_business"](
            _candidate_response(
                prompt_slot,
                _requested_candidate_count(package.user_prompt),
            )
        )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    _install_media_stubs(monkeypatch, accepted_starts=set())
    text_config, visual_config, retrieval_config = _configs()

    with pytest.raises(ValueError, match="Targeted Slot redesign failed"):
        retrieve_candidates(
            [slot],
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            retrieval_config,
            context,
            replan_slots=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("model retry budget exhausted")
            ),
        )

    failure = context.get_artifact("retrieval_failure")
    assert failure["reason_code"] == "targeted_slot_redesign_failed"
    assert failure["error_type"] == "RuntimeError"


def test_repair_validation_retrieval_failure_is_outer_attempt_retriable(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        _slot("slot_01", "segment_0001"),
        _slot("slot_02", "segment_0003"),
    ]
    context = _context(tmp_path)
    slot_calls: Counter[str] = Counter()

    def call_prompt(**kwargs):
        prompt_slot = _prompt_slot(kwargs["package"].user_prompt)
        slot_id = prompt_slot["slot_id"]
        slot_calls[slot_id] += 1
        if slot_id == "slot_01" and slot_calls[slot_id] == 3:
            raise RuntimeError("repair validation transport failed")
        return kwargs["validate_business"](
            _candidate_response(
                prompt_slot,
                _requested_candidate_count(kwargs["package"].user_prompt),
            )
        )

    def replan_slots(current_slots, _failures):
        return [
            {**current_slots[0], "source_segment_ids": ["segment_0002"]},
            current_slots[1],
        ], {"slot_01"}

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    _install_media_stubs(
        monkeypatch,
        accepted_starts={"00:00:20,000"},
    )
    text_config, visual_config, retrieval_config = _configs()

    with pytest.raises(
        ValueError,
        match="Candidate retrieval failed while validating a targeted repair",
    ):
        retrieve_candidates(
            slots,
            tmp_path / "video.mp4",
            text_config,
            visual_config,
            retrieval_config,
            context,
            replan_slots=replan_slots,
        )

    failure = context.get_artifact("retrieval_failure")
    assert failure["reason_code"] == "candidate_retrieval_failed"
    assert failure["failed_slot_ids"] == ["slot_01"]
