from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
import json
from math import inf, nan
from types import MappingProxyType
from uuid import uuid1, uuid4

import pytest

from cutmaster.domain import (
    CreativeBrief,
    DomainEvent,
    ManagedArtifactReference,
    Material,
    MaterialCondition,
    MaterialFingerprint,
    MaterialId,
    MaterialType,
)
from cutmaster.domain.edits import FrozenEdit, FrozenEditOrigin
from cutmaster.domain.ids import FrozenEditId, RunId
from cutmaster.domain.runs import RunStatus


def test_typed_ids_round_trip_without_filesystem_state() -> None:
    material_id = MaterialId.new()

    assert MaterialId.parse(str(material_id)) == material_id
    assert str(material_id).startswith("mat_")


@pytest.mark.parametrize(
    "value",
    [
        f"mat_{str(uuid4()).upper()}",
        f"mat_{uuid1()}",
        "mat_00000000000040008000000000000000",
    ],
)
def test_typed_ids_require_canonical_lowercase_uuidv4(value: str) -> None:
    with pytest.raises(ValueError):
        MaterialId(value)


def test_material_is_an_immutable_identity_snapshot() -> None:
    material = Material(
        material_id=MaterialId.new(),
        material_type=MaterialType.VIDEO,
        name="source-video",
        fingerprint=MaterialFingerprint("a" * 64),
        condition=MaterialCondition.READY,
    )

    with pytest.raises(FrozenInstanceError):
        material.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        "/absolute/file.json",
        "folder\\file.json",
        "folder/../file.json",
        "folder/./file.json",
        "folder//file.json",
        "folder/",
    ],
)
def test_managed_artifact_reference_rejects_non_portable_paths(
    relative_path: str,
) -> None:
    with pytest.raises(ValueError):
        ManagedArtifactReference("owner-1", relative_path)


def test_creative_brief_requires_user_facing_inputs() -> None:
    with pytest.raises(ValueError, match="Editing Intent"):
        CreativeBrief(" ", 30.0)
    with pytest.raises(ValueError, match="Target Duration"):
        CreativeBrief("Make a trailer", 0.0)
    for invalid in (nan, inf, -inf):
        with pytest.raises(ValueError, match="finite"):
            CreativeBrief("Make a trailer", invalid)


def test_run_success_is_complete_and_not_preview_state() -> None:
    assert RunStatus.COMPLETE.value == "complete"
    assert "ready_for_review" not in {status.value for status in RunStatus}


def test_frozen_edit_enforces_initial_and_revision_lineage() -> None:
    run_id = RunId.new()
    initial_id = FrozenEditId.new()
    initial = FrozenEdit(initial_id, run_id, FrozenEditOrigin.INITIAL)
    assert initial.parent_edit_id is None

    child = FrozenEdit(
        FrozenEditId.new(),
        run_id,
        FrozenEditOrigin.GUIDED_REVISION,
        initial_id,
    )
    assert child.parent_edit_id == initial_id

    with pytest.raises(ValueError, match="initial.*parent"):
        FrozenEdit(
            FrozenEditId.new(),
            run_id,
            FrozenEditOrigin.INITIAL,
            initial_id,
        )
    with pytest.raises(ValueError, match="requires a parent"):
        FrozenEdit(
            FrozenEditId.new(),
            run_id,
            FrozenEditOrigin.GUIDED_REVISION,
        )
    with pytest.raises(ValueError, match="own parent"):
        edit_id = FrozenEditId.new()
        FrozenEdit(
            edit_id,
            run_id,
            FrozenEditOrigin.GUIDED_REVISION,
            edit_id,
        )


def test_domain_event_defensively_freezes_json_payload() -> None:
    original = {"nested": {"values": [1, 2]}}
    event = DomainEvent(
        event_type="attempt.completed",
        occurred_at=datetime(2026, 8, 12, tzinfo=UTC),
        object_type="attempt",
        object_id="attempt-1",
        payload=original,
    )

    original["nested"]["values"].append(3)
    assert isinstance(event.payload, MappingProxyType)
    assert event.payload["nested"]["values"] == (1, 2)
    assert json.dumps(event.to_dict())
    with pytest.raises(TypeError):
        event.payload["changed"] = True  # type: ignore[index]


def test_domain_event_rejects_naive_time_and_non_json_payload() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DomainEvent("attempt.started", datetime.now(), "attempt", "attempt-1")
    with pytest.raises(TypeError, match="JSON-compatible"):
        DomainEvent(
            "attempt.started",
            datetime.now(tz=UTC),
            "attempt",
            "attempt-1",
            {"bad": object()},
        )
