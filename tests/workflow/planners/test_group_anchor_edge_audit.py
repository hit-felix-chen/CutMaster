"""Deterministic, offline audit of whole-group repair feasibility."""

from __future__ import annotations

from itertools import combinations_with_replacement
from random import Random
from types import SimpleNamespace

import pytest

from cutmaster.configuration.schema import LLMConfig
from cutmaster.workflow.planners.arrangement_architect import (
    _repair_window_slot_ids,
    _targeted_slot_constraints,
    _validate_targeted_slots,
    redesign_edit_slots,
)
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.story_editor import StoryEditorAgent
from cutmaster.workflow.shared.timecode import format_range


def _video(capacities: tuple[int, ...]) -> dict:
    return {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float(index * 20),
                    "end_sec": float(index * 20) + duration / 1000.0,
                },
            }
            for index, duration in enumerate(capacities)
        ]
    }


def _slots(original: tuple[int, ...], durations: tuple[int, ...]) -> list[dict]:
    elapsed_ms = 0
    result = []
    for index, (segment, duration) in enumerate(zip(original, durations, strict=True)):
        result.append(
            {
                "slot_id": f"slot_{index:02d}",
                "group_id": f"group_{index:03d}",
                "source_segment_id": f"segment_{segment:04d}",
                "planned_duration_ms": duration,
                "planned_duration_sec": duration / 1000.0,
                "desired_duration_sec": duration / 1000.0,
                "output_start_sec": elapsed_ms / 1000.0,
                "output_end_sec": (elapsed_ms + duration) / 1000.0,
                "narrative_role": "development",
                "content_description": f"Visible action {index}",
                "target_emotion": "focused",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "continuity_from_previous": "continues",
                "required_visible_subjects": [],
            }
        )
        elapsed_ms += duration
    return result


def _witnesses(
    original: tuple[int, ...],
    durations: tuple[int, ...],
    capacities: tuple[int, ...],
    unavailable: set[int],
    mutable: set[int] | None = None,
    anchored: set[int] | None = None,
    anchor_ranges: dict[int, tuple[int, int]] | None = None,
) -> list[tuple[int, ...]]:
    """Exhaust all chronological assignments, without reproducing the DP."""
    witnesses = []
    for assignment in combinations_with_replacement(range(len(capacities)), len(original)):
        if any(
            original[index] == original[index + 1]
            and assignment[index] != assignment[index + 1]
            for index in range(len(original) - 1)
        ):
            continue
        anchor_indices = set(anchored or set()) | set(anchor_ranges or {})
        if any(assignment[index] != original[index] for index in anchor_indices):
            continue
        if mutable is not None:
            if any(
                assignment[index] != original[index]
                for index in range(len(original))
                if index not in mutable
            ):
                continue
            # The prompt fixes the chronological interval outside each repair run.
            if any(
                assignment[index] == assignment[index + 1]
                and ((index in mutable) != (index + 1 in mutable))
                for index in range(len(original) - 1)
            ):
                continue
        if any(segment in unavailable for segment in assignment):
            continue
        if any(
            sum(duration for value, duration in zip(assignment, durations) if value == segment)
            > capacity
            for segment, capacity in enumerate(capacities)
        ):
            continue
        picture_end_ms = 0
        for index, segment in enumerate(assignment):
            segment_start_ms = segment * 20000
            picture_start_ms = max(picture_end_ms, segment_start_ms)
            if index in (anchor_ranges or {}):
                anchor_start, anchor_end = (anchor_ranges or {})[index]
                if picture_start_ms > segment_start_ms + anchor_start:
                    break
                picture_end_ms = segment_start_ms + anchor_end
            else:
                picture_end_ms = picture_start_ms + durations[index]
            if picture_end_ms > segment_start_ms + capacities[segment]:
                break
        else:
            witnesses.append(assignment)
    return witnesses


def _validate_witness(
    slots: list[dict],
    selected: set[str],
    witness: tuple[int, ...],
    capacities: tuple[int, ...],
    kwargs: dict,
) -> None:
    """Check a brute-force witness through the actual prompt-domain validator."""
    video = _video(capacities)
    constraints = _targeted_slot_constraints(slots, selected, video, **kwargs)
    response = {
        "slots": [
            {**slot, "source_segment_id": f"segment_{segment:04d}"}
            for slot, segment in zip(slots, witness, strict=True)
            if slot["slot_id"] in selected
        ]
    }
    repaired = _validate_targeted_slots(response, slots, constraints, video, **kwargs)
    assert tuple(int(slot["source_segment_id"].rsplit("_", 1)[1]) for slot in repaired) == witness


def _with_anchor(slot: dict, offset_ms: int) -> dict:
    segment_start_sec = int(str(slot["source_segment_id"]).rsplit("_", 1)[1]) * 20.0
    return {
        **slot,
        "dialogue_anchor": {
            "anchor_id": f"dialogue_{slot['slot_id']}",
            "source_segment_id": slot["source_segment_id"],
            "source_video_timestamp": format_range(
                segment_start_sec + offset_ms / 1000.0,
                segment_start_sec + (offset_ms + slot["planned_duration_ms"]) / 1000.0,
            ),
        },
    }


@pytest.mark.parametrize("seed", [210, 617, 903])
@pytest.mark.parametrize("unavailable_mode", ["mixed", "failed_original"])
def test_repair_window_matches_exhaustive_feasibility_with_static_segments(
    seed: int, unavailable_mode: str,
) -> None:
    random = Random(seed)
    outcomes = {"feasible": 0, "unrepairable": 0, "expanded": 0, "multi_target": 0}
    for _ in range(250):
        segment_count = random.randint(2, 6)
        group_count = random.randint(1, min(segment_count, 5))
        original = tuple(sorted(random.sample(range(segment_count), group_count)))
        durations = tuple(random.choice((1500, 2000, 2500)) for _ in original)
        capacities = tuple(random.choice((2500, 4000, 6000)) for _ in range(segment_count))
        targets = {index for index in range(group_count) if random.random() < 0.55}
        targets.add(random.randrange(group_count))
        unavailable = {
            segment for segment in range(segment_count)
            if segment not in original and random.random() < 0.25
        }
        unavailable.update(original[index] for index in targets if random.random() < 0.5)
        if unavailable_mode == "failed_original":
            unavailable.add(original[random.choice(sorted(targets))])
        slots = _slots(original, durations)
        kwargs = {
            "unavailable_source_segment_ids": {
                f"segment_{segment:04d}" for segment in unavailable
            },
        }
        expected = _witnesses(original, durations, capacities, unavailable)
        case = (original, durations, capacities, targets, unavailable)
        outcomes["multi_target"] += len(targets) > 1
        try:
            selected = _repair_window_slot_ids(
                slots, {f"slot_{index:02d}" for index in targets}, _video(capacities), **kwargs
            )
        except ValueError:
            assert not expected, case
            outcomes["unrepairable"] += 1
            continue
        mutable = {index for index in range(group_count) if f"slot_{index:02d}" in selected}
        assert targets <= mutable, case
        witnesses = _witnesses(original, durations, capacities, unavailable, mutable)
        assert witnesses, (case, selected, expected)
        _validate_witness(slots, selected, witnesses[0], capacities, kwargs)
        outcomes["feasible"] += 1
        outcomes["expanded"] += mutable != targets
        if not unavailable.intersection(original):
            assert mutable == targets, case
            assert original in witnesses, case
    # These checks prevent same-Segment repair from turning the oracle into
    # nothing but already-valid original assignments.
    assert all(count > 0 for count in outcomes.values()), outcomes


def test_neighbor_anchor_cannot_move_in_targeted_arrangement_response() -> None:
    """The failed group is unanchored; the expanded left neighbor is anchored."""
    original = (0, 1, 2)
    durations = (2000, 2000, 2000)
    capacities = (6000, 6000, 6000)
    runtime_slots = _slots(original, durations)
    runtime_slots[0].update(
        group_id="group_000_anchor_01",
        parent_group_id="group_000",
        dialogue_anchor={
            "anchor_id": "dialogue_00",
            "source_segment_id": "segment_0000",
            "source_video_timestamp": "00:00:00.000-00:00:02.000",
        },
        fixed_candidate={"candidate_id": "slot_00_dialogue_anchor"},
    )
    assert (0, 2, 2) in _witnesses(
        original, durations, capacities, {1}, anchored={0}
    )
    artifacts = {
        "video_description": _video(capacities),
        "planners_feedback": {"unavailable_source_segment_ids": ["segment_0001"]},
    }

    class Context:
        def get_artifact(self, name, default=None):
            return artifacts.get(name, default)

        def set_artifact(self, name, value):
            artifacts[name] = value

        def call_prompt(self, *, validate_business, **_kwargs):
            # The unavailable failed Segment forces real expansion, but this
            # proposal impermissibly moves the preserved neighboring Anchor.
            replacements = [
                {**slot, "source_segment_id": f"segment_{segment:04d}"}
                for slot, segment in zip(_slots(original, durations), (1, 2, 2), strict=True)
            ]
            return validate_business({"slots": replacements})

    context = Context()

    class Arrangement:
        def repair(self, slots, failures):
            return redesign_edit_slots(
                slots, failures, LLMConfig(model="test", base_url="", api_key="test"), context
            )

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.story_editor = StoryEditorAgent(SimpleNamespace(), context)
    team.arrangement_architect = Arrangement()
    with pytest.raises(ValueError, match="[Aa]nchor"):
        team.repair_groups(runtime_slots, {"failed_group_ids": ["group_001"]})


def test_unavailable_original_segments_force_complete_groups_to_merge() -> None:
    original = (0, 2)
    durations = (2000, 2000)
    capacities = (4000, 4000, 4000)
    assert _witnesses(original, durations, capacities, {0, 2}) == [(1, 1)]
    slots = _slots(original, durations)
    kwargs = {"unavailable_source_segment_ids": {"segment_0000", "segment_0002"}}
    selected = _repair_window_slot_ids(slots, {"slot_00", "slot_01"}, _video(capacities), **kwargs)
    assert selected == {"slot_00", "slot_01"}
    _validate_witness(slots, selected, (1, 1), capacities, kwargs)


def test_fixed_pictures_with_zero_facing_capacity_skip_targeted_model_call() -> None:
    slots = _slots((0, 1, 2), (2000, 2000, 2000))
    slots[0] = _with_anchor(slots[0], 4000)
    slots[2] = _with_anchor(slots[2], 0)
    artifacts = {
        "video_description": _video((6000, 6000, 6000)),
        "planners_feedback": {"unavailable_source_segment_ids": ["segment_0001"]},
    }

    class Context:
        def get_artifact(self, name, default=None):
            return artifacts.get(name, default)

        def call_prompt(self, **_kwargs):
            pytest.fail("No preserved-Anchor placement is feasible; skip the model")

    with pytest.raises(ValueError, match="No Arrangement group repair window"):
        redesign_edit_slots(
            slots,
            [{"slot_id": "slot_01"}],
            LLMConfig(model="test", base_url="", api_key="test"),
            Context(),
        )


@pytest.mark.parametrize("target_duration_ms", [2000, 2001])
def test_merged_original_group_respects_ordinary_prefix_before_its_fixed_anchor(
    target_duration_ms: int,
) -> None:
    slots = _slots((0, 2, 2, 2), (target_duration_ms, 2000, 2000, 2000))
    for slot in slots[1:]:
        slot["group_id"] = "group_001"
    slots[2] = _with_anchor(slots[2], 4000)
    video = _video((10000, 10000, 10000))
    kwargs = {"unavailable_source_segment_ids": {"segment_0000", "segment_0001"}}
    all_slot_ids = {str(slot["slot_id"]) for slot in slots}
    if target_duration_ms == 2000:
        assert _repair_window_slot_ids(slots, {"slot_00"}, video, **kwargs) == all_slot_ids
    else:
        with pytest.raises(ValueError, match="No Arrangement group repair window"):
            _repair_window_slot_ids(slots, {"slot_00"}, video, **kwargs)

    constraints = _targeted_slot_constraints(
        slots, all_slot_ids, video, **kwargs
    )
    assert constraints["slot_02"]["allowed_segment_ids"] == ["segment_0002"]
    response = {"slots": [{**slot, "source_segment_id": "segment_0002"} for slot in slots]}
    if target_duration_ms == 2000:
        repaired = _validate_targeted_slots(response, slots, constraints, video, **kwargs)
        assert repaired[2]["dialogue_anchor"] == slots[2]["dialogue_anchor"]
        assert len({slot["group_id"] for slot in repaired}) == 1
    else:
        with pytest.raises(ValueError, match="Anchor.*infeasible picture capacity"):
            _validate_targeted_slots(response, slots, constraints, video, **kwargs)


@pytest.mark.parametrize("seed", [124, 813, 995])
def test_anchor_constrained_repair_window_matches_exhaustive_picture_placement(seed: int) -> None:
    random = Random(seed)
    outcomes = {"feasible": 0, "unrepairable": 0, "expanded": 0, "multi_target": 0}
    for _ in range(400):
        segment_count = random.randint(2, 6)
        group_count = random.randint(2, min(segment_count, 5))
        original = tuple(sorted(random.sample(range(segment_count), group_count)))
        durations = tuple(random.choice((1500, 2000, 2500)) for _ in original)
        capacities = tuple(random.choice((2500, 4000, 6000)) for _ in range(segment_count))
        anchor_index = random.randrange(group_count)
        targets = {
            index for index in range(group_count)
            if index != anchor_index and random.random() < 0.65
        }
        targets.add((anchor_index + 1) % group_count)
        anchor_ranges = {}
        for index in range(group_count):
            if index in targets or (index != anchor_index and random.random() < 0.5):
                continue
            offset = random.choice((0, capacities[original[index]] - durations[index]))
            anchor_ranges[index] = (offset, offset + durations[index])
        unavailable = {
            segment for segment in range(segment_count)
            if segment not in original and random.random() < 0.2
        }
        unavailable.update(original[index] for index in targets if random.random() < 0.7)
        slots = _slots(original, durations)
        for index, (offset, _end) in anchor_ranges.items():
            slots[index] = _with_anchor(slots[index], offset)
        kwargs = {
            "unavailable_source_segment_ids": {f"segment_{segment:04d}" for segment in unavailable},
        }
        expected = _witnesses(
            original, durations, capacities, unavailable,
            anchor_ranges=anchor_ranges,
        )
        case = (original, durations, capacities, targets, unavailable, anchor_ranges)
        outcomes["multi_target"] += len(targets) > 1
        try:
            selected = _repair_window_slot_ids(
                slots, {f"slot_{index:02d}" for index in targets}, _video(capacities), **kwargs
            )
        except ValueError:
            assert not expected, case
            outcomes["unrepairable"] += 1
            continue
        mutable = {index for index in range(group_count) if f"slot_{index:02d}" in selected}
        assert targets <= mutable, case
        witnesses = _witnesses(
            original, durations, capacities, unavailable, mutable,
            anchor_ranges=anchor_ranges,
        )
        assert witnesses, (case, selected, expected)
        _validate_witness(slots, selected, witnesses[0], capacities, kwargs)
        outcomes["feasible"] += 1
        outcomes["expanded"] += mutable != targets
        if not unavailable.intersection(original):
            assert mutable == targets, case
            assert original in witnesses, case
    assert all(count > 0 for count in outcomes.values()), outcomes


@pytest.mark.parametrize("alternative_capacity_ms", [3000, 4000])
def test_same_segment_parent_must_not_split_to_fit_available_segments(
    alternative_capacity_ms: int,
) -> None:
    original = (0, 0, 2)
    durations = (2000, 2000, 2000)
    capacities = (4000, alternative_capacity_ms, 4000)
    slots = _slots(original, durations)
    slots[1]["group_id"] = slots[0]["group_id"]
    kwargs = {"unavailable_source_segment_ids": {"segment_0000"}}
    witnesses = _witnesses(original, durations, capacities, {0})
    if alternative_capacity_ms == 3000:
        # Splitting the original parent across [S1, S2] would fit, but is not
        # a legal repair of its two indivisible same-Segment Slots.
        assert not witnesses
        with pytest.raises(ValueError, match="No Arrangement group repair window"):
            _repair_window_slot_ids(slots, {"slot_00"}, _video(capacities), **kwargs)
    else:
        selected = _repair_window_slot_ids(slots, {"slot_00"}, _video(capacities), **kwargs)
        assert selected == {"slot_00", "slot_01"}
        _validate_witness(slots, selected, (1, 1, 2), capacities, kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("narrative_role", "climax"),
        ("content_description", "A different event not established by the fixed picture"),
        ("target_emotion", "excited"),
        ("target_emotional_intensity", 0.9),
        ("target_kinetic_energy", 0.9),
        ("continuity_from_previous", "A different transition"),
        ("required_visible_subjects", ["newly required person"]),
    ],
)
def test_preserved_anchor_editorial_fields_cannot_change(field: str, value: object) -> None:
    slots = _slots((0, 0, 1), (2000, 2000, 2000))
    slots[0] = _with_anchor(slots[0], 0)
    slots[1]["group_id"] = slots[0]["group_id"]
    video = _video((6000, 4000))
    constraints = _targeted_slot_constraints(slots, {"slot_01"}, video)

    with pytest.raises(ValueError, match="changed a preserved dialogue Anchor Slot"):
        _validate_targeted_slots(
            {"slots": [{**slots[0], field: value}, slots[1]]},
            slots,
            constraints,
            video,
        )


def test_failed_ordinary_slot_sharing_anchor_parent_can_repair_on_same_segment() -> None:
    runtime_slots = _slots((0, 0, 1), (2000, 2000, 2000))
    runtime_slots[0] = _with_anchor(runtime_slots[0], 0)
    runtime_slots[0].update(
        group_id="group_000_anchor_01",
        parent_group_id="group_000",
        fixed_candidate={"candidate_id": "slot_00_dialogue_anchor"},
    )
    runtime_slots[1].update(group_id="group_000_plain_01", parent_group_id="group_000")
    runtime_slots[2]["group_id"] = "group_001"
    artifacts = {"video_description": _video((6000, 4000))}
    requests = []
    arrangement_inputs = []

    class Context:
        def get_artifact(self, name, default=None):
            return artifacts.get(name, default)

        def set_artifact(self, name, value):
            artifacts[name] = value

        def call_prompt(self, *, package, validate_business, **_kwargs):
            requests.append(package)
            slots = arrangement_inputs[0]
            return validate_business({
                "slots": [
                    slots[0],
                    {
                        **slots[1],
                        "content_description": "Visible teammates celebrate the goal.",
                        "required_visible_subjects": ["teammates"],
                        "target_emotion": "joyful",
                    },
                ],
            })

    context = Context()

    class Arrangement:
        def repair(self, slots, failures):
            arrangement_inputs.append(slots)
            return redesign_edit_slots(
                slots, failures, LLMConfig(model="test", base_url="", api_key="test"), context
            )

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.story_editor = StoryEditorAgent(SimpleNamespace(), context)
    team.arrangement_architect = Arrangement()
    repaired, selected = team.repair_groups(
        runtime_slots,
        {"failed_group_ids": ["group_000_plain_01"], "failed_slot_ids": ["slot_01"]},
    )

    assert len(requests) == 1
    assert selected == {"slot_00", "slot_01"}
    assert repaired[0]["dialogue_anchor"] == runtime_slots[0]["dialogue_anchor"]
    assert repaired[0]["content_description"] == runtime_slots[0]["content_description"]
    assert repaired[0]["required_visible_subjects"] == runtime_slots[0]["required_visible_subjects"]
    assert repaired[1]["source_segment_id"] == runtime_slots[1]["source_segment_id"]
    assert repaired[1]["content_description"] == "Visible teammates celebrate the goal."
    assert repaired[1]["required_visible_subjects"] == ["teammates"]
    assert repaired[1]["target_emotion"] == "joyful"
    assert repaired[2]["content_description"] == runtime_slots[2]["content_description"]
