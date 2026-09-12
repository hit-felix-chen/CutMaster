from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.adapters.web.access import can_write
from cutmaster.bootstrap import local_web


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("192.168.1.10", False),
        ("10.0.0.1", False),
        ("2001:db8::1", False),
        ("testclient", False),
        (None, False),
    ],
)
def test_direct_peer_policy(host, expected):
    request = Request({"type": "http", "client": (host, 1234) if host else None})
    assert can_write(request) is expected


@pytest.mark.parametrize(
    "host,expected",
    [("127.0.0.1", True), ("::1", True), ("192.168.1.20", False)],
)
def test_access_and_project_creation(application, host, expected):
    with TestClient(create_app(application=application), client=(host, 1234)) as client:
        access = client.get("/api/access")
        assert access.json() == {"can_write": expected}
        assert access.headers["cache-control"] == "no-store"
        result = client.post(
            "/api/projects",
            json={"name": "Access test"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == (201 if expected else 403), result.text
        assert client.get("/api/projects").status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/projects"),
        ("PUT", "/api/settings"),
        ("DELETE", "/api/materials/mat_fake"),
        ("PATCH", "/api/projects/fake"),
        ("POST", "/api/settings/providers/llm/test"),
        ("POST", "/api/attempts/fake/stop"),
        ("POST", "/api/runs/fake/retry"),
        ("POST", "/api/settings/storage/migrations/preflight"),
        ("POST", "/api/edits/fake/render-variants"),
    ],
)
def test_remote_writes_blocked_before_route_validation(application, method, path):
    with TestClient(
        create_app(application=application), client=("192.168.1.20", 1234)
    ) as client:
        response = client.request(
            method,
            path,
            headers={"X-Forwarded-For": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "remote_read_only"


def test_remote_read_methods_are_not_blocked(application):
    with TestClient(
        create_app(application=application), client=("192.168.1.20", 1234)
    ) as client:
        for method in ("GET", "HEAD", "OPTIONS"):
            assert client.request(method, "/api/health").status_code != 403


def test_serve_disables_proxy_headers(monkeypatch, tmp_path):
    monkeypatch.setattr(local_web, "_editable_web_root", lambda: None)
    monkeypatch.setattr(local_web, "_resolve_spa_directory", lambda _: tmp_path)
    app = object()
    monkeypatch.setattr(local_web, "create_local_web_app", lambda *args, **kwargs: app)
    run = Mock()
    monkeypatch.setattr(local_web.uvicorn, "run", run)
    local_web.serve("config.toml", host="0.0.0.0", open_browser=False)
    assert run.call_args.kwargs["proxy_headers"] is False
