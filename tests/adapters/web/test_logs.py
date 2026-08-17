from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    EnqueueMaterialAnalysisCommand,
    FailAttemptCommand,
)


def _submission(application: CutMasterApplication, tmp_path: Path):
    source = tmp_path / "log-source.mp4"
    source.write_bytes(b"video")
    material = application.materials.add(source, "video", "Logged material")
    submission = application.jobs.enqueue_material_analysis(
        EnqueueMaterialAnalysisCommand(str(uuid4()), material.material_id)
    )
    return material, submission


def _line(index: int) -> str:
    return (
        f"2026-08-17 10:00:{index:02d}.000+08:00 | "
        "\x1b[34mINFO    \x1b[0m | analyser | stage.progress | "
        f"completed={index} stage=analysis | Completed line {index}\n"
    )


def test_attempt_logs_return_absolute_path_last_fifty_lines_and_safe_execution_projection(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    material, submission = _submission(application, tmp_path)
    path = (
        application.settings.get().data_root
        / "logs"
        / "jobs"
        / f"{submission.job.job_id}.log"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_line(index) for index in range(60)), encoding="utf-8")

    response = client.get(f"/api/attempts/{submission.attempt.attempt_id}/logs")
    activity = client.get(
        "/api/activity",
        params={"owner_type": "material", "owner_id": str(material.material_id)},
    ).json()

    assert response.status_code == 200
    payload = response.json()
    assert payload["attempt_id"] == str(submission.attempt.attempt_id)
    assert payload["log"] == {"path": str(path.absolute()), "exists": True}
    assert len(payload["entries"]) == 50
    assert payload["entries"][0]["message"] == "Completed line 10"
    assert payload["entries"][-1]["message"] == "Completed line 59"
    assert payload["entries"][0]["level"] == "INFO"
    assert "\x1b" not in str(payload)
    assert payload["has_more_before"] is True
    assert activity["items"][0]["log"] == payload["log"]


def test_attempt_log_stream_replays_after_cursor_and_finishes_for_terminal_attempt(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    _material, submission = _submission(application, tmp_path)
    path = (
        application.settings.get().data_root
        / "logs"
        / "jobs"
        / f"{submission.job.job_id}.log"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_line(1), encoding="utf-8")
    initial = client.get(
        f"/api/attempts/{submission.attempt.attempt_id}/logs"
    ).json()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(_line(2))
    assert application.jobs.claim_next(
        ClaimJobCommand("log-test-worker", 1234, submission.job.job_id)
    ) is not None
    application.jobs.mark_failed(
        FailAttemptCommand(submission.attempt.attempt_id, "expected test failure")
    )

    with client.stream(
        "GET",
        f"/api/attempts/{submission.attempt.attempt_id}/logs/stream",
        params={"after_cursor": initial["end_cursor"]},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: log_entry" in body
    assert "Completed line 2" in body
    assert "event: log_end" in body
    assert '"status":"failed"' in body
