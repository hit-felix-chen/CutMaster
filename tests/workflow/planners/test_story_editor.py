import json
from types import SimpleNamespace

import pytest

from cutmaster.configuration.schema import DialogueAnchorConfig, LLMConfig
from cutmaster.infrastructure.models.openai_compatible import ModelResponse
from cutmaster.workflow.planners.story_editor import (
    StoryEditorAgent,
    _apply_planning_partitions,
    _dialogue_constraints_by_slot,
    _dialogues_by_segment,
    _eligible_source_segments,
    _fixed_candidate,
    _validate_selection,
    select_dialogue_anchors,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.workflow.shared.timecode import format_range


def _video_description() -> dict:
    return {
        "segments": [
            {
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 10.0, "end_sec": 20.0},
                "has_dialogue": True,
                "speech_mode": "dialogue",
                "content_type": "narrative",
                "timeline_role": "body",
                "segment_summary": "Mia confronts doubt.",
                "narrative_function": "Tests Mia's resolve.",
                "emotional_tone": "uncertain",
                "emotional_intensity": 0.7,
                "appearing_characters": ["Mia", "Sebastian"],
                "dialogue_items": [],
                "shots": [
                    {
                        "shot_id": "shot_00001",
                        "time_range": {"start_sec": 10.0, "end_sec": 20.0},
                        "visual_description": "Mia and Sebastian speak in close-up.",
                        "dominant_action": "They have an emotional exchange.",
                        "characters": [{"name": "Mia"}, {"name": "Sebastian"}],
                        "dialogue": [
                            {
                                "dialogue_id": 7,
                                "time_range": {
                                    "start_sec": 13.0,
                                    "end_sec": 13.4,
                                },
                                "speaker": "Mia",
                                "text": "Maybe I'm not.",
                            },
                            {
                                "dialogue_id": 8,
                                "time_range": {
                                    "start_sec": 13.6,
                                    "end_sec": 14.1,
                                },
                                "speaker": "Sebastian",
                                "text": "Yes, you are.",
                            },
                            {
                                "dialogue_id": 9,
                                "time_range": {
                                    "start_sec": 14.3,
                                    "end_sec": 15.0,
                                },
                                "speaker": "Mia",
                                "text": "Maybe I'm not.",
                            },
                        ],
                    }
                ],
            }
        ]
    }


def _anchor(
    *,
    slot_id: str = "slot_01",
    source_segment_id: str = "segment_0001",
    first_dialogue_id: str = "7",
    last_dialogue_id: str = "9",
) -> dict:
    return {
        "slot_id": slot_id,
        "source_segment_id": source_segment_id,
        "first_dialogue_id": first_dialogue_id,
        "last_dialogue_id": last_dialogue_id,
        "narrative_significance": "Mia confronts her fear of failure.",
        "request_relevance": "It directly expresses her struggle to keep pursuing acting.",
        "standalone_meaning": "The exchange is understandable without surrounding dialogue.",
        "importance_likert": 5,
        "coherence_likert": 5,
    }


def _config() -> DialogueAnchorConfig:
    return DialogueAnchorConfig(
        max_anchors=4,
        min_anchor_duration_sec=1.5,
    )


def test_story_agent_caps_anchor_to_three_model_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[int] = []

    def select_stub(_slots, model_config, _anchor_config, _context):
        captured.append(model_config.max_retries)
        return []

    monkeypatch.setattr(
        "cutmaster.workflow.planners.story_editor.select_dialogue_anchors",
        select_stub,
    )
    config = SimpleNamespace(
        llm=LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_retries=9,
        ),
        planners=SimpleNamespace(
            dialogue_anchors=DialogueAnchorConfig(max_model_requests=3)
        ),
    )

    StoryEditorAgent(config, SimpleNamespace()).anchor([])

    assert captured == [2]


def _slot() -> dict:
    return {
        "slot_id": "slot_01",
        "group_id": "group_001",
        "content_description": "Mia confronts doubt.",
        "source_segment_id": "segment_0001",
        "required_visible_subjects": ["Mia"],
        "target_emotional_intensity": 0.7,
        "target_kinetic_energy": 0.2,
        "output_start_sec": 4.0,
        "output_end_sec": 8.0,
        "planned_duration_ms": 4000,
        "planned_duration_sec": 4.0,
    }


def _next_slot() -> dict:
    return {
        **_slot(),
        "slot_id": "slot_02",
        "output_start_sec": 8.0,
        "output_end_sec": 12.0,
    }


def _constraints(
    slots: list[dict],
    video_description: dict,
    dialogues: dict[str, list[dict]],
) -> dict[str, dict]:
    return _dialogue_constraints_by_slot(
        slots,
        video_description,
        dialogues,
        _config().min_anchor_duration_sec,
    )


def test_anchor_selection_accepts_arrangement_with_no_eligible_dialogue(
    tmp_path,
) -> None:
    video_description = _video_description()
    silent_segment = {
        **video_description["segments"][0],
        "segment_id": "segment_0002",
        "has_dialogue": False,
        "speech_mode": "none",
        "dialogue_items": [],
        "shots": [
            {
                **video_description["segments"][0]["shots"][0],
                "shot_id": "shot_00002",
                "dialogue": [],
            }
        ],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("video_description", {"segments": [silent_segment]})
    stale_slot = {
        **_slot(),
        "source_segment_id": "segment_0002",
        "dialogue_anchor": {"source_segment_id": "segment_0001"},
        "fixed_candidate": {"candidate_id": "slot_01_dialogue_anchor"},
    }

    result = select_dialogue_anchors(
        [stale_slot],
        LLMConfig(model="test", base_url="", api_key="test"),
        _config(),
        context,
    )
    assert "dialogue_anchor" not in result[0]


def test_disabled_anchor_skips_model_and_clears_anchor_partition(tmp_path, monkeypatch):
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("anchor_enabled", False)
    context.set_artifact("video_description", _video_description())
    def unexpected(**kwargs):
        pytest.fail("No-anchor mode must not call the model")
    monkeypatch.setattr(context, "call_prompt", unexpected)
    slot = {**_slot(), "dialogue_anchor": {"stale": True},
            "fixed_candidate": {"candidate_id": "stale"}}
    result = select_dialogue_anchors(
        [slot], LLMConfig(model="test", base_url="", api_key="test"),
        DialogueAnchorConfig(), context,
    )
    assert "dialogue_anchor" not in result[0]
    assert "fixed_candidate" not in result[0]
    assert context.get_artifact("dialogue_anchors") == []
    assert context.get_artifact("planning_segments")
    assert context.get_artifact("planning_groups")


def test_short_dialogue_items_remain_available_for_contiguous_selection() -> None:
    dialogues = _dialogues_by_segment(_video_description())
    source_segments = _eligible_source_segments(
        [_slot()],
        _video_description(),
        dialogues,
    )

    assert [item["dialogue_id"] for item in source_segments[0]["dialogue_items"]] == [
        "7",
        "8",
        "9",
    ]


def test_eligible_segment_includes_story_description_and_exact_dialogue() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)
    segment = _eligible_source_segments(
        [_slot()],
        video_description,
        dialogues,
    )[0]

    assert segment["segment_summary"] == "Mia confronts doubt."
    assert segment["narrative_function"] == "Tests Mia's resolve."
    assert [item["dialogue_id"] for item in segment["dialogue_items"]] == [
        "7",
        "8",
        "9",
    ]


def test_selection_expands_first_and_last_ids_to_every_intervening_line() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)
    selected = _validate_selection(
        {"anchors": [_anchor()]},
        [_slot()],
        video_description,
        dialogues,
        _constraints([_slot()], video_description, dialogues),
        _config(),
    )

    assert selected[0]["dialogue_ids"] == ["7", "8", "9"]
    assert selected[0]["source_window"] == "00:00:13,000-00:00:17,000"
    assert selected[0]["dialogue_start_sec"] == 13.0
    assert selected[0]["dialogue_end_sec"] == 15.0
    assert selected[0]["speakers"] == ["Mia", "Sebastian"]


def test_fixed_candidate_stores_structured_dialogue_item_list() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)
    selection = _validate_selection(
        {"anchors": [_anchor()]},
        [_slot()],
        video_description,
        dialogues,
        _constraints([_slot()], video_description, dialogues),
        _config(),
    )[0]

    anchor, candidate = _fixed_candidate(_slot(), selection)

    assert anchor["anchor_id"] == "7-9"
    assert len(anchor["dialogue_items"]) == 3
    assert anchor["output_audio_start_sec"] == 4.0
    assert anchor["output_audio_end_sec"] == 6.0
    assert anchor["audio_cut_style"] == "l_cut"
    assert anchor["audio_overlap_before_sec"] == 0.0
    assert anchor["audio_overlap_after_sec"] == 0.0
    assert candidate["dialogue_anchor"] == anchor


def test_dialogue_may_extend_across_slot_boundary_as_start_aligned_l_cut() -> None:
    video_description = _video_description()
    video_description["segments"][0]["time_range"]["end_sec"] = 21.0
    video_description["segments"][0]["shots"][0]["time_range"]["end_sec"] = 21.0
    video_description["segments"][0]["shots"][0]["dialogue"][-1][
        "time_range"
    ]["end_sec"] = 19.0
    dialogues = _dialogues_by_segment(video_description)
    slots = [_slot(), _next_slot()]
    selection = _validate_selection(
        {"anchors": [_anchor()]},
        slots,
        video_description,
        dialogues,
        _constraints(slots, video_description, dialogues),
        _config(),
    )[0]
    anchor, _ = _fixed_candidate(_slot(), selection)

    assert anchor["output_audio_start_sec"] == 4.0
    assert anchor["output_audio_end_sec"] == 10.0
    assert anchor["audio_cut_style"] == "l_cut"
    assert anchor["audio_overlap_before_sec"] == 0.0
    assert anchor["audio_overlap_after_sec"] == 2.0


def test_selection_rejects_dialogue_from_disallowed_segment() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)
    raw = {
        "anchors": [
            _anchor(
                source_segment_id="segment_9999",
            )
        ]
    }

    with pytest.raises(ValueError, match="uses disallowed Segment"):
        _validate_selection(
            raw,
            [_slot()],
            video_description,
            dialogues,
            _constraints([_slot()], video_description, dialogues),
            _config(),
        )


def test_dialogue_constraints_allow_consecutive_short_lines_as_one_passage() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)
    constraint = _constraints(
        [_slot()],
        video_description,
        dialogues,
    )["slot_01"]

    assert constraint["allowed_segment_ids"] == ["segment_0001"]
    assert constraint["min_audio_duration_sec"] == 1.5
    assert "allowed_last_dialogue_ids_by_segment_and_first" not in constraint
    assert "output_audio_end_sec_by_segment_and_first_and_last" not in constraint
    assert constraint["output_audio_start_sec"] == 4.0
    assert "allowed_passages" not in constraint


def test_story_contract_excludes_dialogue_starts_whose_picture_exceeds_segment() -> None:
    video_description = _video_description()
    video_description["segments"][0]["shots"][0]["dialogue"].extend(
        [
            {
                "dialogue_id": 10,
                "time_range": {"start_sec": 17.5, "end_sec": 18.3},
                "speaker": "Mia",
                "text": "This passage is long enough with the next line.",
            },
            {
                "dialogue_id": 11,
                "time_range": {"start_sec": 18.4, "end_sec": 19.2},
                "speaker": "Sebastian",
                "text": "But its four-second picture cannot fit this Segment.",
            },
        ]
    )

    class StubContext:
        def __init__(self) -> None:
            self.artifacts = {
                "video_description": video_description,
                "video_summary": {"story": "Mia confronts doubt."},
            }

        def get_artifact(self, key, default=None):
            return self.artifacts.get(key, default)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

        def call_prompt(self, *, package, validate_business, **_kwargs):
            anchor_schema = package.response_contract.schema["properties"][
                "anchors"
            ]["items"]["oneOf"][0]
            assert "10" in anchor_schema["properties"][
                "first_dialogue_id"
            ]["enum"]
            package.response_contract.validate_structure(
                {
                    "anchors": [
                        _anchor(
                            first_dialogue_id="7",
                            last_dialogue_id="10",
                        )
                    ]
                }
            )
            with pytest.raises(ValueError):
                validate_business(
                    {
                        "anchors": [
                            _anchor(
                                first_dialogue_id="7",
                                last_dialogue_id="10",
                            )
                        ]
                    }
                )
            return validate_business({"anchors": [_anchor()]})

    select_dialogue_anchors(
        [_slot()],
        LLMConfig(model="test", base_url="", api_key="test"),
        _config(),
        StubContext(),  # type: ignore[arg-type]
    )


def test_dialogue_constraints_leave_group_partition_choice_to_backend() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)
    slots = [_slot(), _next_slot()]

    constraints = _constraints(slots, video_description, dialogues)

    # The only possible picture starts at 13s. In the 10s-20s Segment it
    # leaves just 3s before/after a 4s sibling Slot, so neither Slot can be an
    # Anchor without making the remaining group impossible.
    assert constraints["slot_01"]["required_after_picture_ms"] == 4000
    assert constraints["slot_02"]["required_before_picture_ms"] == 4000
    assert constraints["slot_01"]["allowed_segment_ids"] == ["segment_0001"]
    assert constraints["slot_02"]["allowed_segment_ids"] == ["segment_0001"]


def test_selection_rejects_endpoints_below_minimum_duration() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)

    with pytest.raises(ValueError, match="must last at least"):
        _validate_selection(
            {
                "anchors": [
                    _anchor(
                        first_dialogue_id="7",
                        last_dialogue_id="7",
                    )
                ]
            },
            [_slot()],
            video_description,
            dialogues,
            _constraints([_slot()], video_description, dialogues),
            _config(),
        )


def test_dialogue_constraints_fit_remaining_output_timeline() -> None:
    video_description = _video_description()
    video_description["segments"][0]["time_range"]["end_sec"] = 21.0
    video_description["segments"][0]["shots"][0]["time_range"]["end_sec"] = 21.0
    video_description["segments"][0]["shots"][0]["dialogue"][-1][
        "time_range"
    ]["end_sec"] = 19.0
    dialogues = _dialogues_by_segment(video_description)
    slots = [_slot(), _next_slot()]

    constraints = _constraints(slots, video_description, dialogues)

    assert constraints["slot_01"]["allowed_segment_ids"] == ["segment_0001"]
    assert constraints["slot_02"]["allowed_segment_ids"] == []


def test_selection_keeps_required_anchor_when_returned_anchors_overlap() -> None:
    video_description = _partition_video_description(end_sec=40.0)
    video_description["segments"][0]["shots"][0]["time_range"]["end_sec"] = 40.0
    video_description["segments"][0]["shots"][0]["dialogue"] = [
        {
            "dialogue_id": 20,
            "time_range": {"start_sec": 11.0, "end_sec": 17.0},
            "speaker": "Mia",
            "text": "The first meaningful passage lasts six seconds.",
        },
        {
            "dialogue_id": 21,
            "time_range": {"start_sec": 21.0, "end_sec": 27.0},
            "speaker": "Sebastian",
            "text": "The second meaningful passage also lasts six seconds.",
        },
    ]
    slots = [
        _partition_slot(index, duration_ms=4000)
        for index in range(1, 4)
    ]
    dialogues = _dialogues_by_segment(video_description)
    first = _anchor(
        slot_id="slot_01",
        first_dialogue_id="20",
        last_dialogue_id="20",
    )
    first["importance_likert"] = 4
    first["coherence_likert"] = 4
    second = _anchor(
        slot_id="slot_02",
        first_dialogue_id="21",
        last_dialogue_id="21",
    )
    selected = _validate_selection(
        {"anchors": [first, second]},
        slots,
        video_description,
        dialogues,
        _constraints(slots, video_description, dialogues),
        _config(),
        required_anchor_slot_ids={"slot_01"},
    )

    assert [item["slot_id"] for item in selected] == ["slot_01"]


def test_story_selection_resolves_anchor_conflict_without_another_model_call(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_description = _partition_video_description(end_sec=40.0)
    video_description["segments"][0]["shots"][0]["time_range"]["end_sec"] = 40.0
    video_description["segments"][0]["shots"][0]["dialogue"] = [
        {
            "dialogue_id": 20,
            "time_range": {"start_sec": 11.0, "end_sec": 17.0},
            "speaker": "Mia",
            "text": "The first meaningful passage lasts six seconds.",
        },
        {
            "dialogue_id": 21,
            "time_range": {"start_sec": 21.0, "end_sec": 27.0},
            "speaker": "Sebastian",
            "text": "The second meaningful passage also lasts six seconds.",
        },
    ]
    slots = [
        _partition_slot(index, duration_ms=4000)
        for index in range(1, 4)
    ]
    first = _anchor(
        slot_id="slot_01",
        first_dialogue_id="20",
        last_dialogue_id="20",
    )
    first["importance_likert"] = 4
    first["coherence_likert"] = 4
    second = _anchor(
        slot_id="slot_02",
        first_dialogue_id="21",
        last_dialogue_id="21",
    )

    responses = iter([{"anchors": [first, second]}])
    prompts: list[str] = []

    def generate(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return ModelResponse(
            content=json.dumps(next(responses)),
            usage=None,
        )

    monkeypatch.setattr(
        "cutmaster.workflow.shared.execution_context.generate_text",
        generate,
    )
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("video_description", video_description)
    context.set_artifact(
        "video_summary",
        {"story": "Mia chooses the stronger line."},
    )
    result = select_dialogue_anchors(
        slots,
        LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_retries=1,
        ),
        _config(),
        context,
    )

    assert len(prompts) == 1
    assert [
        slot["slot_id"] for slot in result if slot.get("dialogue_anchor") is not None
    ] == ["slot_02"]


def test_story_selection_retries_whole_response_when_one_anchor_is_malformed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_description = _partition_video_description(end_sec=40.0)
    video_description["segments"][0]["shots"][0]["time_range"]["end_sec"] = 40.0
    video_description["segments"][0]["shots"][0]["dialogue"] = [
        {
            "dialogue_id": 20,
            "time_range": {"start_sec": 11.0, "end_sec": 17.0},
            "speaker": "Mia",
            "text": "The first meaningful passage lasts six seconds.",
        },
        {
            "dialogue_id": 21,
            "time_range": {"start_sec": 21.0, "end_sec": 27.0},
            "speaker": "Sebastian",
            "text": "The second meaningful passage also lasts six seconds.",
        },
    ]
    slots = [_partition_slot(index, duration_ms=4000) for index in range(1, 4)]
    malformed = _anchor(
        slot_id="slot_01",
        first_dialogue_id="20",
        last_dialogue_id="20",
    )
    malformed.pop("standalone_meaning")
    valid = _anchor(
        slot_id="slot_02",
        first_dialogue_id="21",
        last_dialogue_id="21",
    )
    responses = iter(
        [
            {"anchors": [malformed, valid]},
            {"anchors": [valid]},
        ]
    )
    requests = 0

    def generate(_prompt, *_args, **_kwargs):
        nonlocal requests
        requests += 1
        return ModelResponse(
            content=json.dumps(next(responses)),
            usage=None,
        )

    monkeypatch.setattr(
        "cutmaster.workflow.shared.execution_context.generate_text",
        generate,
    )
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("video_description", video_description)
    context.set_artifact("video_summary", {"story": "Mia chooses the stronger line."})

    result = select_dialogue_anchors(
        slots,
        LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_retries=1,
        ),
        _config(),
        context,
    )

    assert requests == 2
    assert [
        slot["slot_id"] for slot in result if slot.get("dialogue_anchor") is not None
    ] == ["slot_02"]


def _partition_video_description(*, end_sec: float = 30.0) -> dict:
    value = _video_description()
    value["segments"][0]["time_range"] = {
        "start_sec": 10.0,
        "end_sec": end_sec,
    }
    value["segments"][0]["shots"][0]["time_range"] = {
        "start_sec": 10.0,
        "end_sec": end_sec,
    }
    return value


def _partition_slot(index: int, *, duration_ms: int = 2000) -> dict:
    start_sec = float((index - 1) * duration_ms) / 1000.0
    return {
        **_slot(),
        "slot_id": f"slot_{index:02d}",
        "group_id": "group_001",
        "source_segment_id": "segment_0001",
        "output_start_sec": start_sec,
        "output_end_sec": start_sec + duration_ms / 1000.0,
        "planned_duration_ms": duration_ms,
        "planned_duration_sec": duration_ms / 1000.0,
    }


def test_anchor_guidance_ranges_follow_slot_counts_not_slot_durations() -> None:
    video_description = _partition_video_description()
    slots = [
        _partition_slot(1, duration_ms=1000),
        _partition_slot(2, duration_ms=3000),
        _partition_slot(3, duration_ms=2000),
        _partition_slot(4, duration_ms=4000),
        _partition_slot(5, duration_ms=1000),
    ]

    constraints = _constraints(
        slots,
        video_description,
        _dialogues_by_segment(video_description),
    )

    assert constraints["slot_01"]["preferred_anchor_picture_range"] == {
        "source_segment_id": "segment_0001",
        "start_sec": 10.0,
        "end_sec": 14.0,
        "left_slot_count": 0,
        "right_slot_count": 4,
    }
    assert constraints["slot_03"]["preferred_anchor_picture_range"] == {
        "source_segment_id": "segment_0001",
        "start_sec": 18.0,
        "end_sec": 22.0,
        "left_slot_count": 2,
        "right_slot_count": 2,
    }
    assert constraints["slot_05"]["preferred_anchor_picture_range"] == {
        "source_segment_id": "segment_0001",
        "start_sec": 26.0,
        "end_sec": 30.0,
        "left_slot_count": 4,
        "right_slot_count": 0,
    }

    single_slot_constraints = _constraints(
        [_partition_slot(1, duration_ms=1000)],
        video_description,
        _dialogues_by_segment(video_description),
    )
    assert single_slot_constraints["slot_01"][
        "preferred_anchor_picture_range"
    ] == {
        "source_segment_id": "segment_0001",
        "start_sec": 10.0,
        "end_sec": 30.0,
        "left_slot_count": 0,
        "right_slot_count": 0,
    }


def _with_picture_anchor(slot: dict, start_sec: float) -> dict:
    item = dict(slot)
    end_sec = start_sec + int(slot["planned_duration_ms"]) / 1000.0
    item["dialogue_anchor"] = {
        "anchor_id": str(slot["slot_id"]),
        "source_video_timestamp": format_range(start_sec, end_sec),
        "source_audio_start_sec": start_sec,
        "source_audio_end_sec": end_sec,
    }
    item["fixed_candidate"] = {
        "candidate_id": f"{slot['slot_id']}_dialogue_anchor"
    }
    return item


def test_selection_keeps_the_largest_legal_subset_of_conflicting_anchors() -> None:
    video_description = _partition_video_description()
    video_description["segments"][0]["shots"][0]["dialogue"] = [
        {
            "dialogue_id": 20,
            "time_range": {"start_sec": 14.0, "end_sec": 18.0},
            "speaker": "Mia",
            "text": "A meaningful passage near the Segment edge.",
        },
        {
            "dialogue_id": 21,
            "time_range": {"start_sec": 19.0, "end_sec": 23.0},
            "speaker": "Sebastian",
            "text": "An equally meaningful passage near the Segment center.",
        },
    ]
    slots = [_partition_slot(index) for index in range(1, 6)]
    dialogues = _dialogues_by_segment(video_description)
    constraints = _constraints(slots, video_description, dialogues)

    selected = _validate_selection(
        {
            "anchors": [
                _anchor(
                    slot_id="slot_02",
                    first_dialogue_id="20",
                    last_dialogue_id="20",
                ),
                _anchor(
                    slot_id="slot_03",
                    first_dialogue_id="21",
                    last_dialogue_id="21",
                ),
            ]
        },
        slots,
        video_description,
        dialogues,
        constraints,
        _config(),
    )

    assert len(selected) == 1
    assert selected[0]["slot_id"] in {"slot_02", "slot_03"}


def test_single_anchor_splits_one_group_into_before_and_after_regions() -> None:
    slots = [_partition_slot(index) for index in range(1, 6)]
    slots[2] = _with_picture_anchor(slots[2], 16.0)

    result, planning_segments, planning_groups = _apply_planning_partitions(
        slots,
        _partition_video_description(),
    )

    assert [group["slot_ids"] for group in planning_groups] == [
        ["slot_01", "slot_02"],
        ["slot_04", "slot_05"],
    ]
    assert [group["group_id"] for group in planning_groups] == [
        "group_001_01",
        "group_001_02",
    ]
    assert planning_segments == [
        {
            "planning_segment_id": "segment_0001_01",
            "source_segment_id": "segment_0001",
            "start_ms": 10000,
            "end_ms": 16000,
        },
        {
            "planning_segment_id": "segment_0001_02",
            "source_segment_id": "segment_0001",
            "start_ms": 18000,
            "end_ms": 30000,
        },
    ]
    assert result[2]["group_id"] == "group_001_anchor_01"
    assert result[2]["parent_group_id"] == "group_001"
    assert "planning_segment_id" not in result[2]


def test_multiple_anchors_create_before_middle_and_after_regions() -> None:
    slots = [_partition_slot(index) for index in range(1, 6)]
    slots[1] = _with_picture_anchor(slots[1], 14.0)
    slots[3] = _with_picture_anchor(slots[3], 20.0)

    result, planning_segments, planning_groups = _apply_planning_partitions(
        slots,
        _partition_video_description(),
    )

    assert [group["slot_ids"] for group in planning_groups] == [
        ["slot_01"],
        ["slot_03"],
        ["slot_05"],
    ]
    assert [(item["start_ms"], item["end_ms"]) for item in planning_segments] == [
        (10000, 14000),
        (16000, 20000),
        (22000, 30000),
    ]
    assert result[1]["group_id"] == "group_001_anchor_01"
    assert result[3]["group_id"] == "group_001_anchor_02"


def test_consecutive_anchors_do_not_create_an_empty_middle_group() -> None:
    slots = [_partition_slot(index) for index in range(1, 5)]
    slots[1] = _with_picture_anchor(slots[1], 14.0)
    slots[2] = _with_picture_anchor(slots[2], 16.0)

    _, planning_segments, planning_groups = _apply_planning_partitions(
        slots,
        _partition_video_description(),
    )

    assert [group["slot_ids"] for group in planning_groups] == [
        ["slot_01"],
        ["slot_04"],
    ]
    assert [(item["start_ms"], item["end_ms"]) for item in planning_segments] == [
        (10000, 14000),
        (18000, 30000),
    ]


def test_first_and_last_anchors_omit_empty_edge_groups() -> None:
    slots = [_partition_slot(index) for index in range(1, 4)]
    slots[0] = _with_picture_anchor(slots[0], 10.0)
    slots[2] = _with_picture_anchor(slots[2], 18.0)

    _, planning_segments, planning_groups = _apply_planning_partitions(
        slots,
        _partition_video_description(end_sec=20.0),
    )

    assert [group["slot_ids"] for group in planning_groups] == [["slot_02"]]
    assert planning_segments[0]["start_ms"] == 12000
    assert planning_segments[0]["end_ms"] == 18000


def test_l_cut_audio_does_not_consume_the_after_picture_region() -> None:
    slots = [_partition_slot(index) for index in range(1, 4)]
    slots[1] = _with_picture_anchor(slots[1], 14.0)
    slots[1]["dialogue_anchor"]["source_audio_end_sec"] = 25.0

    _, planning_segments, _ = _apply_planning_partitions(
        slots,
        _partition_video_description(),
    )

    assert planning_segments[-1]["start_ms"] == 16000


def test_anchor_partition_capacity_failure_reports_group_and_milliseconds() -> None:
    slots = [
        _partition_slot(1, duration_ms=5000),
        _with_picture_anchor(_partition_slot(2), 14.0),
    ]

    with pytest.raises(
        ValueError,
        match=(
            r"failed_group_id=group_001_01 "
            r"available_ms=4000 required_ms=5000"
        ),
    ):
        _apply_planning_partitions(
            slots,
            _partition_video_description(end_sec=20.0),
        )


def test_selection_keeps_legal_subset_when_anchor_pair_starves_middle_group() -> None:
    video_description = _partition_video_description()
    video_description["segments"][0]["shots"][0]["dialogue"] = [
        {
            "dialogue_id": 20,
            "time_range": {"start_sec": 14.0, "end_sec": 15.5},
            "speaker": "Mia",
            "text": "First meaningful line.",
        },
        {
            "dialogue_id": 21,
            "time_range": {"start_sec": 17.0, "end_sec": 18.5},
            "speaker": "Sebastian",
            "text": "Second meaningful line.",
        },
    ]
    slots = [_partition_slot(index) for index in range(1, 6)]
    dialogues = _dialogues_by_segment(video_description)
    constraints = _constraints(slots, video_description, dialogues)
    anchors = [
        {
            **_anchor(
                slot_id="slot_02",
                first_dialogue_id="20",
                last_dialogue_id="20",
            ),
        },
        {
            **_anchor(
                slot_id="slot_04",
                first_dialogue_id="21",
                last_dialogue_id="21",
            ),
        },
    ]

    selected = _validate_selection(
        {"anchors": anchors},
        slots,
        video_description,
        dialogues,
        constraints,
        _config(),
    )
    assert len(selected) == 1


def test_business_validation_keeps_one_anchor_for_one_slot() -> None:
    video_description = _video_description()
    dialogues = _dialogues_by_segment(video_description)
    slots = [_slot()]

    selected = _validate_selection(
        {"anchors": [_anchor(), _anchor()]},
        slots,
        video_description,
        dialogues,
        _constraints(slots, video_description, dialogues),
        _config(),
    )
    assert len(selected) == 1


def test_story_selection_writes_anchor_and_planning_artifacts() -> None:
    class StubContext:
        def __init__(self) -> None:
            self.artifacts = {
                "video_description": _partition_video_description(),
                "video_summary": {"story": "Mia confronts doubt."},
            }

        def get_artifact(self, key, default=None):
            return self.artifacts.get(key, default)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

        def call_prompt(self, *, validate_business, **_kwargs):
            return validate_business(
                {
                    "anchors": [
                        _anchor(
                            slot_id="slot_02",
                            first_dialogue_id="7",
                            last_dialogue_id="9",
                        )
                    ]
                }
            )

    context = StubContext()
    slots = [_partition_slot(index) for index in range(1, 6)]

    result = select_dialogue_anchors(
        slots,
        LLMConfig(model="test", base_url="", api_key="test"),
        _config(),
        context,  # type: ignore[arg-type]
    )

    assert result[1]["group_id"] == "group_001_anchor_01"
    assert [group["slot_ids"] for group in context.artifacts["planning_groups"]] == [
        ["slot_01"],
        ["slot_03", "slot_04", "slot_05"],
    ]
    assert context.artifacts["planning_segments"] == [
        {
            "planning_segment_id": "segment_0001_01",
            "source_segment_id": "segment_0001",
            "start_ms": 10000,
            "end_ms": 13000,
        },
        {
            "planning_segment_id": "segment_0001_02",
            "source_segment_id": "segment_0001",
            "start_ms": 15000,
            "end_ms": 30000,
        },
    ]
    assert context.artifacts["dialogue_anchors"][0]["slot_id"] == "slot_02"


def test_anchor_outside_guidance_range_warns_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str, str, str, dict]] = []

    def capture_event(level, component, event, message, **fields) -> None:
        events.append((level, component, event, message, fields))

    monkeypatch.setattr(
        "cutmaster.workflow.planners.story_editor.log_event",
        capture_event,
    )

    class StubContext:
        def __init__(self) -> None:
            self.calls = 0
            self.artifacts = {
                "video_description": _partition_video_description(),
                "video_summary": {"story": "Mia confronts doubt."},
            }

        def get_artifact(self, key, default=None):
            return self.artifacts.get(key, default)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

        def call_prompt(self, *, validate_business, **_kwargs):
            self.calls += 1
            return validate_business(
                {
                    "anchors": [
                        _anchor(
                            slot_id="slot_02",
                            first_dialogue_id="7",
                            last_dialogue_id="9",
                        )
                    ]
                }
            )

    context = StubContext()
    result = select_dialogue_anchors(
        [_partition_slot(index) for index in range(1, 6)],
        LLMConfig(model="test", base_url="", api_key="test"),
        _config(),
        context,  # type: ignore[arg-type]
    )

    assert context.calls == 1
    assert result[1]["dialogue_anchor"]["source_video_timestamp"] == (
        "00:00:13,000-00:00:15,000"
    )
    warnings = [item for item in events if item[0] == "WARNING"]
    assert len(warnings) == 1
    level, component, event, _message, fields = warnings[0]
    assert (level, component, event) == (
        "WARNING",
        "aster.story",
        "stage.progress",
    )
    assert fields["reason_code"] == "anchor_outside_preferred_picture_range"
    assert fields["slot_id"] == "slot_02"
    assert fields["source_segment_id"] == "segment_0001"
    assert fields["actual_picture_range"] == {
        "start_sec": 13.0,
        "end_sec": 15.0,
    }
    assert fields["preferred_picture_range"] == {
        "start_sec": 14.0,
        "end_sec": 18.0,
    }


def test_story_selection_accepts_empty_anchor_response() -> None:
    class StubContext:
        def __init__(self) -> None:
            self.artifacts = {
                "video_description": _partition_video_description(),
                "video_summary": {"story": "Mia confronts doubt."},
            }

        def get_artifact(self, key, default=None):
            return self.artifacts.get(key, default)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

        def call_prompt(self, *, validate_business, **_kwargs):
            return validate_business({"anchors": []})

    context = StubContext()
    slots = [_partition_slot(index) for index in range(1, 4)]

    result = select_dialogue_anchors(
        slots,
        LLMConfig(model="test", base_url="", api_key="test"),
        _config(),
        context,  # type: ignore[arg-type]
    )

    assert all("dialogue_anchor" not in slot for slot in result)
    assert context.artifacts["dialogue_anchors"] == []


def _two_group_story_fixture() -> tuple[dict, list[dict], list[dict]]:
    first = _partition_video_description()
    first_segment = first["segments"][0]
    first_segment["shots"][0]["dialogue"] = [
        {
            "dialogue_id": 20,
            "time_range": {"start_sec": 14.0, "end_sec": 15.6},
            "speaker": "Mia",
            "text": "I will keep going.",
        }
    ]
    second_segment = {
        **first_segment,
        "segment_id": "segment_0002",
        "time_range": {"start_sec": 40.0, "end_sec": 60.0},
        "shots": [
            {
                **first_segment["shots"][0],
                "shot_id": "shot_00002",
                "time_range": {"start_sec": 40.0, "end_sec": 60.0},
                "dialogue": [
                    {
                        "dialogue_id": 30,
                        "time_range": {"start_sec": 44.0, "end_sec": 45.6},
                        "speaker": "Sebastian",
                        "text": "Then take the chance.",
                    }
                ],
            }
        ],
    }
    video_description = {"segments": [first_segment, second_segment]}
    slots = [_partition_slot(index) for index in range(1, 7)]
    for index, slot in enumerate(slots):
        slot["output_start_sec"] = index * 2.0
        slot["output_end_sec"] = (index + 1) * 2.0
        if index >= 3:
            slot["group_id"] = "group_002"
            slot["source_segment_id"] = "segment_0002"
    dialogues = _dialogues_by_segment(video_description)
    selections = _validate_selection(
        {
            "anchors": [
                _anchor(
                    slot_id="slot_02",
                    first_dialogue_id="20",
                    last_dialogue_id="20",
                ),
                _anchor(
                    slot_id="slot_05",
                    source_segment_id="segment_0002",
                    first_dialogue_id="30",
                    last_dialogue_id="30",
                ),
            ]
        },
        slots,
        video_description,
        dialogues,
        _constraints(slots, video_description, dialogues),
        _config(),
    )
    selected_by_slot = {item["slot_id"]: item for item in selections}
    story_slots: list[dict] = []
    for slot in slots:
        item = dict(slot)
        selection = selected_by_slot.get(str(slot["slot_id"]))
        if selection is not None:
            anchor, candidate = _fixed_candidate(item, selection)
            item["dialogue_anchor"] = anchor
            item["fixed_candidate"] = candidate
        story_slots.append(item)
    story_slots, _, _ = _apply_planning_partitions(
        story_slots,
        video_description,
    )
    return video_description, slots, story_slots


def test_incremental_story_preserves_all_still_legal_anchors_without_a_model_call() -> None:
    video_description, arrangement_slots, previous_slots = _two_group_story_fixture()
    repaired_slots = [dict(slot) for slot in arrangement_slots]
    repaired_slots[4]["content_description"] = "A newly planned visible beat."

    class StubContext:
        def __init__(self) -> None:
            self.artifacts = {
                "video_description": video_description,
                "video_summary": {"story": "Mia decides to continue."},
            }

        def get_artifact(self, key, default=None):
            return self.artifacts.get(key, default)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

        def call_prompt(self, **_kwargs):
            raise AssertionError("local Story refresh must not call a model")

    context = StubContext()
    config = SimpleNamespace(
        llm=LLMConfig(model="test", base_url="", api_key="test"),
        planners=SimpleNamespace(dialogue_anchors=_config()),
    )
    result = StoryEditorAgent(config, context).anchor_groups(
        repaired_slots,
        previous_slots=previous_slots,
        replanned_slot_ids={"slot_04", "slot_05", "slot_06"},
    )

    previous_anchor = next(
        slot["dialogue_anchor"]
        for slot in previous_slots
        if slot["slot_id"] == "slot_02"
    )
    kept = next(slot for slot in result if slot["slot_id"] == "slot_02")
    assert kept["dialogue_anchor"] == previous_anchor
    assert [item["slot_id"] for item in context.artifacts["dialogue_anchors"]] == [
        "slot_02",
        "slot_05",
    ]
    assert {
        slot_id
        for group in context.artifacts["planning_groups"]
        for slot_id in group["slot_ids"]
    } == {"slot_01", "slot_03", "slot_04", "slot_06"}


def test_incremental_story_rejects_an_anchor_invalidated_by_repair() -> None:
    video_description, arrangement_slots, previous_slots = _two_group_story_fixture()
    repaired_slots = [dict(slot) for slot in arrangement_slots]
    for slot in repaired_slots[3:]:
        slot["source_segment_id"] = "segment_0001"

    class StubContext:
        def __init__(self) -> None:
            self.artifacts = {
                "video_description": video_description,
                "video_summary": {"story": "Mia decides to continue."},
            }

        def get_artifact(self, key, default=None):
            return self.artifacts.get(key, default)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

        def call_prompt(self, **_kwargs):
            raise AssertionError("local Story refresh must not call a model")

    context = StubContext()
    config = SimpleNamespace(
        llm=LLMConfig(model="test", base_url="", api_key="test"),
        planners=SimpleNamespace(dialogue_anchors=_config()),
    )
    with pytest.raises(ValueError, match="invalidated a preserved dialogue Anchor"):
        StoryEditorAgent(config, context).anchor_groups(
            repaired_slots,
            previous_slots=previous_slots,
            replanned_slot_ids={"slot_04", "slot_05", "slot_06"},
        )
