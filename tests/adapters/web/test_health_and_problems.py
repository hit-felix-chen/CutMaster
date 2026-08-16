from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_is_real_secret_free_application_state(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "cutmaster",
        "data_root": {
            "maintenance": False,
            "restart_required": False,
            "migration_id": None,
            "migration_status": None,
        },
        "configured": {"llm": False, "vlm": False, "asr": False},
    }


def test_unknown_api_and_validation_errors_are_problem_details(
    client: TestClient,
) -> None:
    missing = client.get("/api/not-a-route")
    invalid = client.get("/api/activity?limit=0")

    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["code"] == "resource_not_found"
    assert invalid.status_code == 422
    assert invalid.headers["content-type"].startswith("application/problem+json")
    assert invalid.json()["code"] == "request_validation_failed"
    assert invalid.json()["field_errors"]


def test_idempotency_key_is_required_on_mutation(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "Film"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "request_validation_failed"
