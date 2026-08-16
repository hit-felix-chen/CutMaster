from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from cutmaster.infrastructure.storage.local import (
    material_catalog as material_catalog_module,
)
from fastapi.testclient import TestClient

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import EnqueueMaterialAnalysisCommand
from cutmaster.domain.ids import MaterialId


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
            "schema_version": "2.0",
            "source": {
                "path": str(binding.source_path),
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
                    "clip_path": str(binding.memory_root / "private.mp4"),
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
                    "clip_path": str(binding.memory_root / "private-2.mp4"),
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
            "schema_version": "1.0",
            "source_srt": str(binding.memory_root / "private.srt"),
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
        staged = tmp_path / "analysis-result.json"
        staged.write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "status": "success",
                    "material_id": str(binding.material.material_id),
                    "material_type": "video",
                    "material_name": binding.material.name,
                    "material_fingerprint": str(binding.material.fingerprint),
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
            "schema_version": "1.0",
            "audio_path": str(binding.source_path),
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
                    "schema_version": "2.0",
                    "status": "success",
                    "material_id": str(binding.material.material_id),
                    "material_type": "music",
                    "material_name": binding.material.name,
                    "material_fingerprint": str(binding.material.fingerprint),
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
    assert "fingerprint" not in detail.text
    assert str(tmp_path) not in detail.text


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


def test_missing_material_source_is_problem_details(client: TestClient) -> None:
    response = client.get(
        "/api/materials/mat_00000000-0000-4000-8000-000000000001/source"
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "resource_not_found"
