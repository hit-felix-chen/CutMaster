from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import ClaimJobCommand
from cutmaster.application.projects import (
    CreateProjectCommand,
    SaveCreativeBriefCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.renders import (
    CompleteRenderVariantCommand,
    CreateRenderVariantCommand,
    VerifyRenderVariantCommand,
)
from cutmaster.application.runs import CompleteRunCommand, CreateRunCommand
from cutmaster.domain.ids import FrozenEditId
from cutmaster.domain.materials import MaterialFingerprint
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.shared.timecode import format_range


def _command_id() -> str:
    return str(uuid4())


@dataclass(frozen=True)
class ReviewFixture:
    application: CutMasterApplication
    client: TestClient
    edit_id: FrozenEditId
    plan: RenderPlan
    plan_path: Path
    candidate_pool: dict[str, list[dict[str, Any]]]


@pytest.fixture
def review_fixture(
    application: CutMasterApplication,
    client: TestClient,
    tmp_path: Path,
) -> ReviewFixture:
    project = application.projects.create(
        CreateProjectCommand(_command_id(), "Review Fixture")
    )
    video_source = tmp_path / "review-source.mp4"
    music_source = tmp_path / "review-music.mp3"
    video_source.write_bytes(b"fixture-video")
    music_source.write_bytes(b"fixture-music")
    video = application.materials.add(video_source, "video", "Review Video")
    music = application.materials.add(music_source, "music", "Review Music")
    application.projects.set_materials(
        SetProjectMaterialsCommand(
            _command_id(),
            project.project_id,
            (video.material_id,),
            (music.material_id,),
        )
    )
    application.projects.save_creative_brief(
        SaveCreativeBriefCommand(
            _command_id(),
            project.project_id,
            "Build a chronological story",
            30.0,
        )
    )
    submission = application.runs.create(
        CreateRunCommand(_command_id(), project.project_id)
    )
    claimed = application.jobs.claim_next(
        ClaimJobCommand(
            "review-fixture-worker",
            4242,
            submission.job.job_id,
        )
    )
    assert claimed is not None

    clips: list[dict[str, Any]] = []
    pool: dict[str, list[dict[str, Any]]] = {}
    for index in range(1, 16):
        slot_id = f"slot_{index:02d}"
        source_start = float((index - 1) * 10)
        output_start = float((index - 1) * 2)
        selected_id = (
            f"{slot_id}_dialogue_anchor"
            if index == 1
            else f"{slot_id}_candidate_01"
        )
        anchor = (
            {
                "anchor_id": "anchor-1",
                "speakers": ["Speaker 1"],
                "text": "The story begins.",
                "output_audio_start_sec": 0.0,
                "output_audio_end_sec": 2.0,
            }
            if index == 1
            else None
        )
        clip: dict[str, Any] = {
            "_id": index,
            "video_id": 1,
            "video_name": "source.mp4",
            "timestamp": format_range(source_start, source_start + 2.0),
            "picture": f"Story beat {index}",
            "narration": f"Play source {index}",
            "OST": 1,
            "slot_id": slot_id,
            "candidate_id": selected_id,
            "output_start_sec": output_start,
            "output_end_sec": output_start + 2.0,
            "planned_duration_sec": 2.0,
            "output_frame_range": [
                int(output_start * 30),
                int((output_start + 2.0) * 30),
            ],
            "selection_scores": {
                "semantic_relevance": 0.9,
                "visual_slot_relevance_likert": 5,
                "protagonist_visibility_likert": 4,
                "emotional_intensity": 0.5,
                "kinetic_energy": 0.5,
                "salience": 0.8,
            },
        }
        if anchor is not None:
            clip["dialogue_anchor"] = anchor
        clips.append(clip)

        def candidate(
            candidate_id: str,
            start_sec: float,
            *,
            description: str,
            dialogue_anchor: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            value: dict[str, Any] = {
                "candidate_id": candidate_id,
                "slot_id": slot_id,
                "timestamp": format_range(start_sec, start_sec + 2.0),
                "description": description,
                "structured_context": description,
                "semantic_relevance": 0.88,
                "visual_slot_relevance_likert": 4,
                "protagonist_visibility_likert": 4,
                "emotional_intensity": 0.55,
                "kinetic_energy": 0.45,
                "salience": 0.82,
                "visual_evidence": f"Visible evidence for {candidate_id}",
            }
            if dialogue_anchor is not None:
                value["dialogue_anchor"] = dialogue_anchor
            return value

        pool[slot_id] = [
            candidate(
                selected_id,
                source_start,
                description=f"Selected scene {index}",
                dialogue_anchor=anchor,
            ),
            candidate(
                f"{slot_id}_candidate_02",
                source_start + 3.0,
                description=f"Alternative scene {index}",
            ),
        ]

    # This validated candidate belongs to slot 02 but makes the whole source
    # sequence overlap slot 03. It exercises the atomic chronology guard.
    pool["slot_02"].append(
        {
            **pool["slot_02"][1],
            "candidate_id": "slot_02_candidate_late",
            "timestamp": format_range(25.0, 27.0),
        }
    )
    plan = RenderPlan.create(
        video_material_id=video.material_id,
        video_expected_fingerprint=MaterialFingerprint("a" * 64),
        music_material_id=music.material_id,
        music_expected_fingerprint=MaterialFingerprint("b" * 64),
        fps=30,
        clips=clips,
        planners_metadata={"prompt": "fixture"},
    )
    relative_plan = (
        f"projects/{project.project_id}/runs/{submission.run.run_id}/plan.json"
    )
    plan_path = application.settings.get().data_root / relative_plan
    plan.write(plan_path)
    completed = application.runs.complete(
        CompleteRunCommand(
            _command_id(),
            submission.run.run_id,
            submission.attempt.attempt_id,
            relative_plan,
        )
    )
    return ReviewFixture(
        application=application,
        client=client,
        edit_id=completed.frozen_edit.edit_id,
        plan=plan,
        plan_path=plan_path,
        candidate_pool=pool,
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish_bundle(fixture: ReviewFixture) -> Path:
    payloads = {
        "candidate_pool": fixture.candidate_pool,
        "edit_plan": [
            {
                "slot_id": clip["slot_id"],
                "planned_duration_sec": clip["planned_duration_sec"],
            }
            for clip in fixture.plan.clips
        ],
        "music_profile": {"schema_version": "1.0", "beats_sec": [0.5, 1.5]},
        "selection_diagnostics": {
            "beam_candidate_ids": [clip["candidate_id"] for clip in fixture.plan.clips]
        },
    }
    entries: dict[str, dict[str, str]] = {}
    for logical_name, payload in payloads.items():
        path = fixture.plan_path.parent / f"{logical_name}.fixture.json"
        _write_json(path, payload)
        entries[logical_name] = {"path": path.name, "sha256": _sha256(path)}
    manifest = fixture.plan_path.parent / "review_bundle.json"
    _write_json(
        manifest,
        {"schema_version": "1.0", "artifacts": entries},
    )
    return manifest


def test_historical_edit_without_bundle_is_complete_read_only_review(
    review_fixture: ReviewFixture,
) -> None:
    response = review_fixture.client.get(
        f"/api/frozen-edits/{review_fixture.edit_id}/review"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["edit"]["edit_id"] == str(review_fixture.edit_id)
    assert payload["candidate_space_available"] is False
    assert payload["candidate_space_unavailable_reason"] == "not_persisted"
    assert len(payload["slots"]) == 15
    assert payload["slots"][0]["is_anchor"] is True
    assert payload["timeline"]["dialogue_cues"] == [
        {
            "slot_id": "slot_01",
            "start_sec": 0.0,
            "end_sec": 2.0,
            "text": "The story begins.",
            "speaker": "Speaker 1",
        }
    ]
    assert all(
        len(items) == 1 and items[0]["selected"] is True
        and items[0]["eligible_for_replacement"] is False
        for items in payload["candidates"].values()
    )
    serialized = json.dumps(payload, sort_keys=True)
    assert "a" * 64 not in serialized
    assert "b" * 64 not in serialized
    assert str(review_fixture.plan_path) not in serialized
    assert "plan_relative_path" not in serialized
    assert "expected_fingerprint" not in serialized
    assert "specification_digest" not in serialized
    assert "master_sha256" not in serialized
    assert "master_relative_path" not in serialized


def test_committed_review_bundle_exposes_only_real_validated_candidates(
    review_fixture: ReviewFixture,
) -> None:
    _publish_bundle(review_fixture)

    response = review_fixture.client.get(
        f"/api/frozen-edits/{review_fixture.edit_id}/review"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate_space_available"] is True
    assert payload["candidate_space_unavailable_reason"] is None
    selected, alternative, late = payload["candidates"]["slot_02"]
    assert selected["candidate_id"] == "slot_02_candidate_01"
    assert selected["selected"] is True
    assert selected["eligible_for_replacement"] is False
    assert alternative["candidate_id"] == "slot_02_candidate_02"
    assert alternative["description"] == "Alternative scene 2"
    assert alternative["visual_score"] == 4.0
    assert alternative["eligible_for_replacement"] is True
    assert alternative["media_url"].endswith("/source")
    assert late["candidate_id"] == "slot_02_candidate_late"
    assert payload["variants"] == []


def test_guided_revision_creates_one_idempotent_child_and_preserves_timing(
    review_fixture: ReviewFixture,
) -> None:
    _publish_bundle(review_fixture)
    command_id = _command_id()
    request = {
        "replacements": [
            {"slot_id": "slot_02", "candidate_id": "slot_02_candidate_02"}
        ]
    }

    first = review_fixture.client.post(
        f"/api/frozen-edits/{review_fixture.edit_id}/revisions",
        headers={"Idempotency-Key": command_id},
        json=request,
    )
    second = review_fixture.client.post(
        f"/api/frozen-edits/{review_fixture.edit_id}/revisions",
        headers={"Idempotency-Key": command_id},
        json=request,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    child_payload = first.json()["frozen_edit"]
    assert child_payload["origin"] == "guided_revision"
    assert child_payload["parent_edit_id"] == str(review_fixture.edit_id)
    assert child_payload["sequence"] == 2
    assert first.json()["review_url"].endswith(child_payload["edit_id"])

    child_id = FrozenEditId.parse(child_payload["edit_id"])
    child = review_fixture.application.runs.get_frozen_edit(child_id)
    child_path = (
        review_fixture.application.settings.get().data_root
        / child.plan.relative_path
    )
    child_plan = RenderPlan.read(child_path)
    original = review_fixture.plan.clips[1]
    revised = child_plan.clips[1]
    assert revised["candidate_id"] == "slot_02_candidate_02"
    assert revised["timestamp"] == format_range(13.0, 15.0)
    assert revised["output_frame_range"] == original["output_frame_range"]
    assert revised["output_start_sec"] == original["output_start_sec"]
    assert revised["output_end_sec"] == original["output_end_sec"]
    assert len(
        review_fixture.application.runs.list_frozen_edits(child.run_id)
    ) == 2
    child_review = review_fixture.client.get(
        f"/api/frozen-edits/{child_id}/review"
    )
    assert child_review.status_code == 200
    assert child_review.json()["candidate_space_available"] is True


@pytest.mark.parametrize(
    ("revision_payload", "expected_code"),
    [
        (
            {
                "replacements": [
                    {"slot_id": "slot_01", "candidate_id": "slot_01_candidate_02"}
                ]
            },
            "anchor_locked",
        ),
        (
            {
                "replacements": [
                    {"slot_id": "slot_02", "candidate_id": "slot_03_candidate_02"}
                ]
            },
            "invalid_candidate_replacement",
        ),
        (
            {
                "replacements": [
                    {"slot_id": "slot_02", "candidate_id": "slot_02_candidate_01"}
                ]
            },
            "invalid_candidate_replacement",
        ),
        (
            {
                "replacements": [
                    {"slot_id": "slot_02", "candidate_id": "slot_02_candidate_late"}
                ]
            },
            "revision_infeasible",
        ),
        (
            {
                "replacements": [
                    {"slot_id": "slot_02", "candidate_id": "slot_02_candidate_02"},
                    {"slot_id": "slot_02", "candidate_id": "slot_02_candidate_late"},
                ]
            },
            "invalid_candidate_replacement",
        ),
    ],
)
def test_invalid_guided_revision_is_atomic(
    review_fixture: ReviewFixture,
    revision_payload: dict[str, Any],
    expected_code: str,
) -> None:
    _publish_bundle(review_fixture)

    response = review_fixture.client.post(
        f"/api/frozen-edits/{review_fixture.edit_id}/revisions",
        headers={"Idempotency-Key": _command_id()},
        json=revision_payload,
    )

    assert response.status_code == 409
    assert response.json()["code"] == expected_code
    source = review_fixture.application.runs.get_frozen_edit(review_fixture.edit_id)
    assert len(
        review_fixture.application.runs.list_frozen_edits(source.run_id)
    ) == 1


def test_empty_guided_revision_is_rejected_before_history_changes(
    review_fixture: ReviewFixture,
) -> None:
    _publish_bundle(review_fixture)

    response = review_fixture.client.post(
        f"/api/frozen-edits/{review_fixture.edit_id}/revisions",
        headers={"Idempotency-Key": _command_id()},
        json={"replacements": []},
    )

    assert response.status_code == 422
    source = review_fixture.application.runs.get_frozen_edit(review_fixture.edit_id)
    assert len(
        review_fixture.application.runs.list_frozen_edits(source.run_id)
    ) == 1


@pytest.mark.parametrize("corrupt", ["hash", "path"])
def test_review_bundle_integrity_or_path_failure_is_a_review_conflict(
    review_fixture: ReviewFixture,
    corrupt: str,
) -> None:
    manifest_path = _publish_bundle(review_fixture)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if corrupt == "hash":
        manifest["artifacts"]["candidate_pool"]["sha256"] = "0" * 64
    else:
        manifest["artifacts"]["candidate_pool"]["path"] = "../outside.json"
    _write_json(manifest_path, manifest)

    response = review_fixture.client.get(
        f"/api/frozen-edits/{review_fixture.edit_id}/review"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "review_artifact_unavailable"


def _create_render_variant(review_fixture: ReviewFixture):
    return review_fixture.application.renders.create(
        CreateRenderVariantCommand(
            _command_id(),
            review_fixture.edit_id,
            {
                "audio_mode": "dialogue",
                "fps": 30,
                "test_case": _command_id(),
            },
        )
    )


def _complete_render_variant(
    review_fixture: ReviewFixture,
    *,
    master_bytes: bytes = b"0123456789abcdef",
):
    submission = _create_render_variant(review_fixture)
    claimed = review_fixture.application.jobs.claim_next(
        ClaimJobCommand(
            "review-render-worker",
            4343,
            submission.job.job_id,
        )
    )
    assert claimed is not None
    edit = review_fixture.application.runs.get_frozen_edit(review_fixture.edit_id)
    run = review_fixture.application.runs.get(edit.run_id)
    relative_master = (
        f"projects/{run.project_id}/renders/"
        f"{submission.render_variant.render_variant_id}/master.mp4"
    )
    master_path = (
        review_fixture.application.settings.get().data_root / relative_master
    )
    master_path.parent.mkdir(parents=True, exist_ok=True)
    master_path.write_bytes(master_bytes)
    completed = review_fixture.application.renders.complete(
        CompleteRenderVariantCommand(
            _command_id(),
            submission.render_variant.render_variant_id,
            submission.attempt.attempt_id,
            relative_master,
            frame_count=16,
            duration_sec=2.0,
        )
    )
    return completed.render_variant, master_path


def test_ready_render_variant_media_supports_http_byte_ranges(
    review_fixture: ReviewFixture,
) -> None:
    variant, _ = _complete_render_variant(review_fixture)

    response = review_fixture.client.get(
        f"/api/render-variants/{variant.render_variant_id}/media",
        headers={"Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/16"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, no-store"


def test_non_ready_and_unavailable_render_media_share_stable_problem_code(
    review_fixture: ReviewFixture,
) -> None:
    queued = _create_render_variant(review_fixture)
    queued_response = review_fixture.client.get(
        f"/api/render-variants/{queued.render_variant.render_variant_id}/media"
    )

    assert queued_response.status_code == 409
    assert queued_response.json()["code"] == "render_media_unavailable"

    ready, master_path = _complete_render_variant(review_fixture)
    master_path.write_bytes(b"tampered-master")
    unavailable = review_fixture.application.renders.verify(
        VerifyRenderVariantCommand(
            _command_id(),
            ready.render_variant_id,
        )
    )
    assert unavailable.status.value == "unavailable"

    unavailable_response = review_fixture.client.get(
        f"/api/render-variants/{ready.render_variant_id}/media"
    )
    assert unavailable_response.status_code == 409
    assert unavailable_response.json()["code"] == "render_media_unavailable"
