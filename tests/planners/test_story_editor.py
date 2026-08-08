import pytest

from cutmaster.configuration.schema import DialogueAnchorConfig, LLMConfig
from cutmaster.planners.story_editor import (
    _dialogue_constraints_by_slot,
    _dialogues_by_segment,
    _eligible_source_segments,
    _fixed_candidate,
    _optimal_non_overlapping_anchors,
    _validate_selection,
    select_dialogue_anchors,
)
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.timecode import format_range


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


def _slot() -> dict:
    return {
        "slot_id": "slot_01",
        "content_description": "Mia confronts doubt.",
        "source_segment_ids": ["segment_0001"],
        "required_visible_subjects": ["Mia"],
        "target_emotional_intensity": 0.7,
        "target_kinetic_energy": 0.2,
        "output_start_sec": 4.0,
        "output_end_sec": 8.0,
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


def test_anchor_rerun_removes_stale_binding_when_new_segment_has_no_dialogue(
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
        "source_segment_ids": ["segment_0002"],
        "dialogue_anchor": {"source_segment_id": "segment_0001"},
        "fixed_candidate": {"candidate_id": "slot_01_dialogue_anchor"},
    }

    refreshed = select_dialogue_anchors(
        [stale_slot],
        LLMConfig(model="test", base_url="", api_key="test"),
        _config(),
        context,
    )

    assert "dialogue_anchor" not in refreshed[0]
    assert "fixed_candidate" not in refreshed[0]
    assert context.get_artifact("dialogue_anchors") == []


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
    video_description["segments"][0]["shots"][0]["dialogue"][-1][
        "time_range"
    ]["end_sec"] = 19.0
    dialogues = _dialogues_by_segment(video_description)
    slots = [_slot(), _next_slot()]

    constraints = _constraints(slots, video_description, dialogues)

    assert constraints["slot_01"]["allowed_segment_ids"] == ["segment_0001"]
    assert constraints["slot_02"]["allowed_segment_ids"] == []


def _interval(
    anchor_id: str,
    start: float,
    end: float,
) -> dict:
    return {
        "anchor_id": anchor_id,
        "slot_id": f"slot_{anchor_id}",
        "dialogue_ids": [anchor_id],
        "source_window": format_range(start, end),
        "output_audio_start_sec": start,
        "output_audio_end_sec": end,
    }


def test_optimal_anchor_subset_prioritizes_number_of_passages() -> None:
    selected = _optimal_non_overlapping_anchors(
        [
            _interval("long", 0.0, 10.0),
            _interval("short_1", 0.0, 4.0),
            _interval("short_2", 4.0, 8.0),
        ]
    )

    assert [anchor["anchor_id"] for anchor in selected] == [
        "short_1",
        "short_2",
    ]


def test_optimal_anchor_subset_uses_total_duration_as_tiebreaker() -> None:
    selected = _optimal_non_overlapping_anchors(
        [
            _interval("short", 0.0, 4.0),
            _interval("long", 0.0, 5.0),
            _interval("ending", 8.0, 10.0),
        ]
    )

    assert [anchor["anchor_id"] for anchor in selected] == [
        "long",
        "ending",
    ]
