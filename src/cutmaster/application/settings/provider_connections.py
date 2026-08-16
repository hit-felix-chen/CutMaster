"""Bounded, response-free provider credential probes."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from cutmaster.application.settings.providers import ProviderCapability

_TEST_TIMEOUT_SEC = 10.0
_MAX_ASR_RESPONSE_BYTES = 128 * 1024
_DASHSCOPE_UPLOAD_POLICY_URL = "https://dashscope.aliyuncs.com/api/v1/uploads"


class ProviderConnectionFailure(RuntimeError):
    """A bounded provider probe failed without retaining provider content."""


@dataclass(frozen=True)
class ProviderProbe:
    capability: ProviderCapability
    configuration: dict[str, object]
    api_key: str


class ProviderConnectionTester(Protocol):
    def __call__(self, probe: ProviderProbe) -> float:
        """Return measured milliseconds or raise ProviderConnectionFailure."""


def test_provider_connection(probe: ProviderProbe) -> float:
    """Perform a real, bounded authentication probe and discard its response."""

    started = time.monotonic()
    try:
        if probe.capability in {"llm", "vlm"}:
            _test_openai_compatible(probe)
        else:
            _test_dashscope_asr(probe)
    except (
        httpx.HTTPError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise ProviderConnectionFailure(
            f"{probe.capability.upper()} connection test failed"
        ) from exc
    return max(0.0, (time.monotonic() - started) * 1000.0)


def _test_openai_compatible(probe: ProviderProbe) -> None:
    base_url = str(probe.configuration["base_url"]).rstrip("/")
    # A streamed status probe avoids retaining or logging a provider model list.
    with (
        httpx.Client(
            timeout=httpx.Timeout(_TEST_TIMEOUT_SEC),
            follow_redirects=False,
        ) as client,
        client.stream(
            "GET",
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {probe.api_key}"},
        ) as response,
    ):
        response.raise_for_status()


def _test_dashscope_asr(probe: ProviderProbe) -> None:
    if probe.configuration.get("backend") != "bailian":
        raise ValueError("Unsupported ASR backend")
    with (
        httpx.Client(
            timeout=httpx.Timeout(_TEST_TIMEOUT_SEC),
            follow_redirects=False,
        ) as client,
        client.stream(
            "GET",
            _DASHSCOPE_UPLOAD_POLICY_URL,
            params={"action": "getPolicy", "model": "fun-asr"},
            headers={
                "Authorization": f"Bearer {probe.api_key}",
                "Content-Type": "application/json",
            },
        ) as response,
    ):
        response.raise_for_status()
        body = _bounded_body(response, _MAX_ASR_RESPONSE_BYTES)
    payload = json.loads(body)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not all(
        data.get(field)
        for field in (
            "upload_host",
            "upload_dir",
            "policy",
            "signature",
            "oss_access_key_id",
        )
    ):
        raise ProviderConnectionFailure("ASR connection test failed")


def _bounded_body(response: httpx.Response, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > maximum:
            raise ProviderConnectionFailure("Provider response exceeded the safe limit")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = [
    "ProviderConnectionFailure",
    "ProviderConnectionTester",
    "ProviderProbe",
    "test_provider_connection",
]
