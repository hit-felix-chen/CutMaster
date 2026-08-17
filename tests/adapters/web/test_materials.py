from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
import anyio
from fastapi.testclient import TestClient

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import EnqueueMaterialAnalysisCommand
from cutmaster.application.projects import (
    CreateProjectCommand,
    SaveCreativeBriefCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.runs import CreateRunCommand
from cutmaster.adapters.web.routes.materials import _PinnedFileResponse
from cutmaster.domain.ids import MaterialId, ProjectId
from cutmaster.infrastructure.storage.local import (
    material_catalog as material_catalog_module,
)

THUMBNAIL_BYTES = b"\xff\xd8\xffreal-middle-frame\xff\xd9"


def source(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def publish_ready_video(application: CutMasterApplication, tmp_path: Path) -> str:
    material = application.materials.add(
        source(tmp_path / "film.mp4", b"video"),
        "video",
        "Feature Film",
    )
    with application.materials.lease(material.material_id) as binding:
        description = {
            "schema_version": "3.0",
            "source": {
                "title": "Film",
                "duration_sec": 120.5,
                "fps": 24.0,
                "width": 1920,
                "height": 1080,
            },
            "scene_detection": {"detector": "AdaptiveDetector"},
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": 4.0},
                    "segment_summary": "Opening",
                    "shots": [
                        {
                            "shot_id": "shot_0001",
                            "visual_description": "A city skyline",
                            "visual_annotation_status": "complete",
                        }
                    ],
                },
                {
                    "segment_id": "segment_0002",
                    "time_range": {"start_sec": 4.0, "end_sec": 8.0},
                    "segment_summary": "Meeting",
                    "shots": [],
                },
            ],
            "asr_model": "test-asr",
            "scene_boundary_model": "test-vlm",
            "visual_description_model": "test-vlm",
        }
        story = {
            "schema_version": "1.0",
            "title": "Film",
            "logline": "Two people meet.",
            "synopsis": "A complete synopsis.",
            "chronological_story_beats": [],
            "character_arcs": [],
            "themes": ["connection"],
            "ending": "They part.",
        }
        dialogue = {
            "schema_version": "2.0",
            "postprocessor": {"prompt": "private"},
            "statistics": {"sentence_count": 2},
            "sentences": [
                {"sentence_id": 1, "speaker": "A", "text": "Hello"},
                {"sentence_id": 2, "speaker": "B", "text": "Goodbye"},
            ],
            "merge_operations": [{"provider_response": "private"}],
        }
        (binding.memory_root / "video_description.json").write_text(
            json.dumps(description), encoding="utf-8"
        )
        (binding.memory_root / "video_summary.json").write_text(
            json.dumps(story), encoding="utf-8"
        )
        (binding.memory_root / "dialogues.json").write_text(
            json.dumps(dialogue), encoding="utf-8"
        )
        frames = binding.memory_root / "scene_frames"
        frames.mkdir()
        (frames / "shot_00007_01.jpg").write_bytes(
            b"\xff\xd8\xffreal-first-frame\xff\xd9"
        )
        (frames / "shot_00007_02.jpg").write_bytes(THUMBNAIL_BYTES)
        (frames / "shot_00042_02.jpg").write_bytes(
            b"\xff\xd8\xffreal-later-shot\xff\xd9"
        )
        (binding.memory_root / "cover.jpg").write_bytes(THUMBNAIL_BYTES)
        staged = tmp_path / "analysis-result.json"
        staged.write_text(
            json.dumps(
                {
                    "schema_version": "3.0",
                    "status": "success",
                    "material_id": str(binding.material.material_id),
                    "material_type": "video",
                    "material_name": binding.material.name,
                    "material_fingerprint": str(binding.material.fingerprint),
                    "memory_schema_version": "3.0",
                    "elapsed_sec": 0.0,
                    "material_reused": False,
                    "analysis_reused": False,
                    "model_usage_summary": {},
                    "model_usage_cumulative_summary": {
                        "total_cost_yuan": 0.123456,
                    },
                }
            ),
            encoding="utf-8",
        )
        application.materials.publish_analysis_result(binding, staged)
    return str(material.material_id)


def publish_ready_music(application: CutMasterApplication, tmp_path: Path) -> str:
    material = application.materials.add(
        source(tmp_path / "score.mp3", b"music"),
        "music",
        "Main Score",
    )
    with application.materials.lease(material.material_id) as binding:
        memory = {
            "schema_version": "2.0",
            "source_duration_sec": 95.0,
            "tempo_bpm": 120.0,
            "beats_sec": [0.5, 1.0],
            "accents_sec": [1.0],
            "energy_step_sec": 0.5,
            "energy_curve": [
                {"time_sec": 0.0, "energy": 0.2},
                {"time_sec": 0.5, "energy": 0.8},
            ],
            "sections": [
                {
                    "section_id": "music_01",
                    "start_sec": 0.0,
                    "end_sec": 95.0,
                    "role": "intro",
                    "mean_energy": 0.5,
                    "energy_trend": "stable",
                    "suggested_clip_duration_sec": [2.0, 4.0],
                }
            ],
        }
        (binding.memory_root / "music_memory.json").write_text(
            json.dumps(memory), encoding="utf-8"
        )
        staged = tmp_path / "music-analysis-result.json"
        staged.write_text(
            json.dumps(
                {
                    "schema_version": "3.0",
                    "status": "success",
                    "material_id": str(binding.material.material_id),
                    "material_type": "music",
                    "material_name": binding.material.name,
                    "material_fingerprint": str(binding.material.fingerprint),
                    "memory_schema_version": "2.0",
                    "elapsed_sec": 0.0,
                    "material_reused": False,
                    "analysis_reused": False,
                }
            ),
            encoding="utf-8",
        )
        application.materials.publish_analysis_result(binding, staged)
    return str(material.material_id)


def test_material_cards_are_searchable_sorted_and_contain_real_metadata(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    video_id = publish_ready_video(application, tmp_path)
    publish_ready_music(application, tmp_path)

    listing = client.get("/api/materials?type=video&search=feature&sort=name_desc")
    detail = client.get(f"/api/materials/{video_id}")

    assert listing.status_code == detail.status_code == 200
    assert listing.json()["total"] == 1
    detail_payload = detail.json()
    assert detail_payload.pop("attempts") == []
    assert listing.json()["items"][0] == detail_payload
    assert detail.json()["duration_sec"] == 120.5
    assert detail.json()["reference_count"] == 0
    assert detail.json()["analysis_available"] is True
    assert detail.json()["analysis_cost_yuan"] == 0.123456
    assert detail.json()["source"] == {
        "filename": "Feature Film.mp4",
        "size_bytes": 5,
        "media_type": "video/mp4",
        "width": 1920,
        "height": 1080,
        "frame_rate": 24.0,
    }
    assert detail.json()["memory_summary"] == {
        "segment_count": 2,
        "shot_count": 1,
        "dialogue_count": 2,
    }
    assert detail.json()["thumbnail_url"] == (
        f"/api/materials/{video_id}/thumbnail"
    )
    assert detail.json()["waveform_url"] is None
    assert "fingerprint" not in detail.text
    assert str(tmp_path) not in detail.text


def test_material_preview_endpoints_are_real_bounded_and_never_open_source_media(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    video_id = publish_ready_video(application, tmp_path)
    music_id = publish_ready_music(application, tmp_path)
    for material_id in (video_id, music_id):
        with application.materials.read_lease(MaterialId.parse(material_id)) as binding:
            binding.source_path.chmod(0)

    video_detail = client.get(f"/api/materials/{video_id}").json()
    music_detail = client.get(f"/api/materials/{music_id}").json()
    thumbnail = client.get(
        video_detail["thumbnail_url"],
        headers={"Range": "bytes=0-3"},
    )
    waveform = client.get(
        music_detail["waveform_url"],
        headers={"Range": "bytes=0-12"},
    )

    assert thumbnail.status_code == waveform.status_code == 200
    assert thumbnail.content == THUMBNAIL_BYTES
    assert thumbnail.headers["content-type"] == "image/jpeg"
    assert len(thumbnail.content) < 2 * 1024 * 1024
    assert waveform.headers["content-type"] == "image/svg+xml"
    assert len(waveform.content) < 64 * 1024
    assert b"<rect" in waveform.content
    assert b"<polygon" not in waveform.content
    assert waveform.content.count(b"<rect") == 2
    assert b"<script" not in waveform.content
    assert b"Main Score" not in waveform.content
    assert str(tmp_path).encode() not in waveform.content
    for response in (thumbnail, waveform):
        assert response.headers["accept-ranges"] == "none"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in response.headers["content-security-policy"]

    wrong_thumbnail = client.get(f"/api/materials/{music_id}/thumbnail")
    wrong_waveform = client.get(f"/api/materials/{video_id}/waveform")
    assert wrong_thumbnail.status_code == wrong_waveform.status_code == 404
    assert wrong_thumbnail.json()["code"] == "material_preview_unavailable"
    assert wrong_waveform.json()["code"] == "material_preview_unavailable"


def test_video_preview_reads_only_dedicated_cover_and_rejects_unsafe_files(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    material_id = publish_ready_video(application, tmp_path)
    with application.materials.read_lease(MaterialId.parse(material_id)) as binding:
        frame_directory = binding.memory_root / "scene_frames"
        for frame in frame_directory.iterdir():
            frame.unlink()
        frame_directory.rmdir()

    independent = client.get(f"/api/materials/{material_id}/thumbnail")
    assert independent.status_code == 200
    assert independent.content == THUMBNAIL_BYTES

    with application.materials.read_lease(MaterialId.parse(material_id)) as binding:
        cover = binding.memory_root / "cover.jpg"
        cover.unlink()
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(b"\xff\xd8\xffprivate\xff\xd9")
        cover.symlink_to(outside)

    unavailable = client.get(f"/api/materials/{material_id}/thumbnail")
    detail = client.get(f"/api/materials/{material_id}")
    assert unavailable.status_code == 404
    assert unavailable.json()["code"] == "material_preview_unavailable"
    assert detail.json()["thumbnail_url"] is None
    assert outside.read_bytes() not in unavailable.content

    with application.materials.read_lease(MaterialId.parse(material_id)) as binding:
        cover = binding.memory_root / "cover.jpg"
        cover.unlink()
        cover.write_bytes(
            b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024) + b"\xff\xd9"
        )

    oversized = client.get(f"/api/materials/{material_id}/thumbnail")
    assert oversized.status_code == 404
    assert len(oversized.content) < 2 * 1024 * 1024


def test_material_detail_includes_real_analysis_attempt_history(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    material_id = publish_ready_video(application, tmp_path)
    application.jobs.enqueue_material_analysis(
        EnqueueMaterialAnalysisCommand(
            str(uuid4()),
            MaterialId.parse(material_id),
        )
    )

    response = client.get(f"/api/materials/{material_id}")

    assert response.status_code == 200
    assert len(response.json()["attempts"]) == 1
    assert response.json()["attempts"][0]["operation_type"] == "material_analysis"


def test_material_references_are_human_link_metadata_without_raw_tokens(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    video_id = publish_ready_video(application, tmp_path)
    music_id = publish_ready_music(application, tmp_path)
    project = application.projects.create(
        CreateProjectCommand(str(uuid4()), "lalaland")
    )
    application.projects.set_materials(
        SetProjectMaterialsCommand(
            str(uuid4()),
            project.project_id,
            (MaterialId.parse(video_id),),
            (MaterialId.parse(music_id),),
        )
    )
    application.projects.save_creative_brief(
        SaveCreativeBriefCommand(
            str(uuid4()),
            project.project_id,
            "Keep the musical story moving",
            30.0,
        )
    )
    submission = application.runs.create(
        CreateRunCommand(str(uuid4()), project.project_id)
    )

    detail = client.get(f"/api/materials/{video_id}")
    listing = client.get("/api/materials?type=video")

    expected = [
        {
            "kind": "project_current",
            "project_name": "lalaland",
            "navigation": {
                "kind": "project",
                "project_id": str(project.project_id),
            },
        },
        {
            "kind": "run_snapshot",
            "project_name": "lalaland",
            "run_sequence": 1,
            "navigation": {
                "kind": "run",
                "project_id": str(project.project_id),
                "run_id": str(submission.run.run_id),
            },
        },
    ]
    assert detail.status_code == listing.status_code == 200
    assert detail.json()["reference_count"] == 2
    assert detail.json()["references"] == expected
    assert listing.json()["items"][0]["references"] == expected
    for response in (detail, listing):
        assert f"project:{project.project_id}:current:video" not in response.text
        assert f"run:{submission.run.run_id}:snapshot:video" not in response.text
        assert '"reference"' not in response.text
        assert '"url"' not in response.text
        assert "/projects/" not in response.text


def test_unknown_material_reference_is_safe_and_does_not_expose_owner_identity(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_id = publish_ready_video(application, tmp_path)
    missing_project_id = ProjectId.new()
    raw_reference = f"project:{missing_project_id}:current:video"
    monkeypatch.setattr(
        type(application.projects),
        "references",
        lambda _service, _material_id: (raw_reference,),
    )

    response = client.get(f"/api/materials/{video_id}")

    assert response.status_code == 200
    assert response.json()["reference_count"] == 1
    assert response.json()["references"] == [
        {"kind": "unknown", "navigation": None}
    ]
    assert str(missing_project_id) not in response.text
    assert raw_reference not in response.text


def test_video_memory_tabs_are_real_paginated_and_sanitized(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    material_id = publish_ready_video(application, tmp_path)
    timeline = client.get(
        f"/api/materials/{material_id}/memory/timeline?limit=1&offset=1"
    )
    dialogue = client.get(
        f"/api/materials/{material_id}/memory/dialogue?limit=1&offset=0"
    )
    story = client.get(f"/api/materials/{material_id}/memory/story")
    technical = client.get(f"/api/materials/{material_id}/memory/technical")

    assert all(item.status_code == 200 for item in (timeline, dialogue, story, technical))
    segments = timeline.json()["payload"]["segments"]
    assert segments["total"] == 2
    assert [item["segment_id"] for item in segments["items"]] == ["segment_0002"]
    sentences = dialogue.json()["payload"]["sentences"]
    assert sentences["total"] == 2
    assert sentences["items"][0]["text"] == "Hello"
    assert story.json()["payload"]["logline"] == "Two people meet."
    assert technical.json()["payload"]["shot_count"] == 1

    combined = "".join(item.text for item in (timeline, dialogue, story, technical))
    for forbidden in (
        "material_fingerprint",
        "clip_path",
        "source_srt",
        "postprocessor",
        "merge_operations",
        "provider_response",
        str(tmp_path),
    ):
        assert forbidden not in combined


def test_music_memory_has_real_sections_and_rejects_wrong_tab(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    material_id = publish_ready_music(application, tmp_path)
    structure = client.get(
        f"/api/materials/{material_id}/memory/structure?limit=1"
    )
    technical = client.get(f"/api/materials/{material_id}/memory/technical")
    wrong = client.get(f"/api/materials/{material_id}/memory/story")

    assert structure.status_code == technical.status_code == 200
    assert structure.json()["payload"]["sections"]["items"][0]["role"] == "intro"
    assert technical.json()["payload"]["beat_count"] == 2
    assert "audio_path" not in structure.text
    assert str(tmp_path) not in structure.text
    assert wrong.status_code == 404
    assert wrong.json()["code"] == "material_memory_tab_not_found"


def test_queued_material_memory_returns_explicit_unavailable(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    material = application.materials.add(
        source(tmp_path / "queued.mp4", b"queued"),
        "video",
        "Queued Film",
    )

    response = client.get(
        f"/api/materials/{material.material_id}/memory/timeline"
    )

    assert response.status_code == 409
    assert response.json()["code"] == "material_memory_unavailable"


def test_video_and_music_source_streams_support_ranges_without_identity_leaks(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    video = application.materials.add(
        source(tmp_path / "movie.mp4", b"0123456789"),
        "video",
        'Movie / "Private"',
    )
    music = application.materials.add(
        source(tmp_path / "score.mp3", b"abcdefghij"),
        "music",
        "Main Score",
    )

    video_range = client.get(
        f"/api/materials/{video.material_id}/source",
        headers={"Range": "bytes=2-5"},
    )
    music_range = client.get(
        f"/api/materials/{music.material_id}/source",
        headers={"Range": "bytes=1-3"},
    )

    assert video_range.status_code == music_range.status_code == 206
    assert video_range.content == b"2345"
    assert music_range.content == b"bcd"
    assert video_range.headers["content-range"] == "bytes 2-5/10"
    assert music_range.headers["content-range"] == "bytes 1-3/10"
    assert video_range.headers["content-type"] == "video/mp4"
    assert music_range.headers["content-type"] == "audio/mpeg"
    assert video_range.headers["content-disposition"].startswith("inline;")
    assert "/" not in video_range.headers["content-disposition"].split("filename", 1)[-1]
    combined_headers = str(dict(video_range.headers)) + str(dict(music_range.headers))
    assert str(tmp_path) not in combined_headers
    assert "fingerprint" not in combined_headers
    application.materials.delete(video.material_id)
    application.materials.delete(music.material_id)
    assert application.materials.get(video.material_id) is None
    assert application.materials.get(music.material_id) is None


def test_web_material_reads_never_rehash_the_managed_source(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch,
) -> None:
    material_id = publish_ready_video(application, tmp_path)

    def unexpected_fingerprint(*_args, **_kwargs) -> str:
        raise AssertionError("Web browsing must not recompute the source SHA-256")

    monkeypatch.setattr(
        material_catalog_module,
        "fingerprint_file",
        unexpected_fingerprint,
    )

    listing = client.get("/api/materials?type=video")
    detail = client.get(f"/api/materials/{material_id}")
    memory = client.get(f"/api/materials/{material_id}/memory/technical")
    source_range = client.get(
        f"/api/materials/{material_id}/source",
        headers={"Range": "bytes=0-1"},
    )

    assert listing.status_code == detail.status_code == memory.status_code == 200
    assert source_range.status_code == 206
    assert source_range.content == b"vi"


def test_material_delete_rejects_active_consumer_without_waiting_and_replays(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    material = application.materials.add(
        source(tmp_path / "consumed.mp4", b"video"),
        "video",
        "Consumed",
    )
    command_id = str(uuid4())

    with application.materials.consume_lease(material.material_id):
        started = time.monotonic()
        blocked = client.delete(
            f"/api/materials/{material.material_id}",
            headers={"Idempotency-Key": command_id},
        )
        elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "material_has_active_consumers"
    assert blocked.json()["blockers"] == [{"kind": "active_consumer"}]

    replay = client.delete(
        f"/api/materials/{material.material_id}",
        headers={"Idempotency-Key": command_id},
    )
    assert replay.status_code == 204
    assert application.materials.get(material.material_id) is None


def test_pinned_source_response_survives_path_unlink(tmp_path: Path) -> None:
    path = source(tmp_path / "source.mp4", b"0123456789")
    stream = path.open("rb")
    response = _PinnedFileResponse(
        path,
        stream=stream,
        stat_result=os.fstat(stream.fileno()),
        media_type="video/mp4",
    )
    path.unlink()
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def invoke() -> None:
        await response(
            {
                "type": "http",
                "method": "GET",
                "headers": [],
                "extensions": {},
            },
            receive,
            send,
        )

    anyio.run(invoke)

    assert b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    ) == b"0123456789"


def test_missing_material_source_is_problem_details(client: TestClient) -> None:
    response = client.get(
        "/api/materials/mat_00000000-0000-4000-8000-000000000001/source"
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "resource_not_found"
