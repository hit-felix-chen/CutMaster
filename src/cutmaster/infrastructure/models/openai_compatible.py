from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from openai import OpenAI

from cutmaster.infrastructure.models.json_codec import parse_json_object
from cutmaster.configuration.schema import ModelConfig
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.infrastructure.observability.logging import error_summary, log_event


T = TypeVar("T")
TOKEN_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_prompt_tokens",
    "uncached_prompt_tokens",
    "reasoning_tokens",
)
COST_USAGE_FIELDS = (
    "uncached_input_cost_yuan",
    "cached_input_cost_yuan",
    "output_cost_yuan",
    "total_cost_yuan",
)
USAGE_COUNT_FIELDS = (
    "request_count",
    "reported_usage_count",
    "unreported_usage_count",
    "priced_usage_count",
    "unpriced_usage_count",
)


def _empty_usage_bucket() -> dict[str, Any]:
    return {
        **{field_name: 0 for field_name in USAGE_COUNT_FIELDS},
        **{field_name: 0 for field_name in TOKEN_USAGE_FIELDS},
        **{field_name: 0.0 for field_name in COST_USAGE_FIELDS},
    }


@dataclass(frozen=True)
class ModelUsage:
    """Provider-reported token usage normalized across compatible APIs."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: int = 0
    uncached_prompt_tokens: int = 0
    reasoning_tokens: int = 0
    provider_usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelResponse:
    content: str
    usage: ModelUsage | None
    response_id: str | None = None
    response_model: str | None = None


def empty_usage_summary() -> dict[str, Any]:
    return {
        **_empty_usage_bucket(),
        "currency": "CNY",
        "price_unit": "yuan_per_million_tokens",
        "by_model": {},
        "by_task": {},
    }


def merge_usage_summaries(
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = empty_usage_summary()
    for summary in summaries:
        for field_name in (*USAGE_COUNT_FIELDS, *TOKEN_USAGE_FIELDS):
            merged[field_name] += int(summary.get(field_name) or 0)
        for field_name in COST_USAGE_FIELDS:
            merged[field_name] += float(summary.get(field_name) or 0.0)
        for group_name in ("by_model", "by_task"):
            for key, values in (summary.get(group_name) or {}).items():
                target = merged[group_name].setdefault(
                    str(key),
                    _empty_usage_bucket(),
                )
                for field_name in (*USAGE_COUNT_FIELDS, *TOKEN_USAGE_FIELDS):
                    target[field_name] += int(values.get(field_name) or 0)
                for field_name in COST_USAGE_FIELDS:
                    target[field_name] += float(values.get(field_name) or 0.0)
    for field_name in COST_USAGE_FIELDS:
        merged[field_name] = round(merged[field_name], 12)
    for group_name in ("by_model", "by_task"):
        for values in merged[group_name].values():
            for field_name in COST_USAGE_FIELDS:
                values[field_name] = round(values[field_name], 12)
    return merged


def load_usage_summary(
    path: Path,
    *,
    cumulative: bool = True,
) -> dict[str, Any]:
    """Read a usage artifact while remaining compatible with schema 1.0."""

    if not path.is_file():
        return empty_usage_summary()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_usage_summary()
    if not isinstance(payload, dict):
        return empty_usage_summary()
    scope = "cumulative" if cumulative else "current_run"
    scoped = payload.get(scope)
    if isinstance(scoped, dict) and isinstance(scoped.get("summary"), dict):
        return dict(scoped["summary"])
    if cumulative and isinstance(payload.get("summary"), dict):
        return dict(payload["summary"])
    return empty_usage_summary()


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json", exclude_none=True))
    return {
        key: value
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_tokens_details",
            "completion_tokens_details",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "reasoning_tokens",
        )
        if (value := getattr(usage, key, None)) is not None
    }


def _detail_value(value: Any, key: str) -> int:
    if isinstance(value, dict):
        return int(value.get(key) or 0)
    return int(getattr(value, key, 0) or 0)


def normalize_model_usage(usage: Any) -> ModelUsage | None:
    raw = _usage_dict(usage)
    if not raw:
        return None
    prompt_tokens = int(raw.get("prompt_tokens") or 0)
    completion_tokens = int(raw.get("completion_tokens") or 0)
    total_tokens = int(
        raw.get("total_tokens") or prompt_tokens + completion_tokens
    )
    cached_prompt_tokens = max(
        _detail_value(raw.get("prompt_tokens_details"), "cached_tokens"),
        int(raw.get("prompt_cache_hit_tokens") or 0),
    )
    uncached_prompt_tokens = int(
        raw.get("prompt_cache_miss_tokens")
        if raw.get("prompt_cache_miss_tokens") is not None
        else max(0, prompt_tokens - cached_prompt_tokens)
    )
    reasoning_tokens = max(
        _detail_value(
            raw.get("completion_tokens_details"),
            "reasoning_tokens",
        ),
        int(raw.get("reasoning_tokens") or 0),
    )
    return ModelUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_prompt_tokens=cached_prompt_tokens,
        uncached_prompt_tokens=uncached_prompt_tokens,
        reasoning_tokens=reasoning_tokens,
        provider_usage=raw,
    )


def _thinking_request_body(
    config: ModelConfig,
    enabled: bool,
) -> dict[str, Any]:
    hostname = (urlparse(config.base_url).hostname or "").lower()
    if hostname == "api.deepseek.com":
        return {
            "thinking": {
                "type": "enabled" if enabled else "disabled",
            }
        }
    return {"enable_thinking": enabled}


def generate_text(
    prompt: str,
    config: ModelConfig,
    system_prompt: str,
    enable_thinking: bool | None = None,
    image_data_urls: list[str] | None = None,
    image_labels: list[str] | None = None,
) -> ModelResponse:
    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url or None,
        timeout=config.timeout_sec,
        # Full request/JSON/schema retries are handled by request_json_with_retries.
        # Keeping SDK retries enabled here would multiply the configured attempts.
        max_retries=0,
    )
    thinking = config.enable_thinking if enable_thinking is None else enable_thinking
    extra_body = _thinking_request_body(config, thinking)
    user_content: str | list[dict[str, Any]] = prompt
    if image_data_urls:
        if image_labels is not None and len(image_labels) != len(image_data_urls):
            raise ValueError("image_labels must match image_data_urls")
        user_content = [{"type": "text", "text": prompt}]
        for index, data_url in enumerate(image_data_urls):
            if image_labels is not None:
                user_content.append(
                    {"type": "text", "text": image_labels[index]}
                )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                }
            )
    response = client.chat.completions.create(
        model=config.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        extra_body=extra_body,
    )
    if not response.choices or not response.choices[0].message.content:
        raise RuntimeError("LLM returned an empty response")
    return ModelResponse(
        content=response.choices[0].message.content,
        usage=normalize_model_usage(getattr(response, "usage", None)),
        response_id=getattr(response, "id", None),
        response_model=getattr(response, "model", None),
    )


def request_json_with_retries(
    request: Callable[[], str],
    config: ModelConfig,
    *,
    operation: str,
    validate: Callable[[dict], T] | None = None,
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> T | dict:
    """Retry the complete request/parse/validation transaction."""
    attempts = max(1, config.max_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            parsed = parse_json_object(request())
            return validate(parsed) if validate is not None else parsed
        except Exception as exc:
            error_text = str(exc).lower()
            if "exceeded limit on max data-uri per request" in error_text:
                failure = build_prompt_failure(
                    PromptFailureCode.PROVIDER_REQUEST_LIMIT_EXCEEDED,
                    operation=operation,
                    error_message=error_summary(exc),
                )
                log_event(
                    "ERROR",
                    "model",
                    "model.fail",
                    "Model request exceeded a provider payload limit",
                    attempt=attempt,
                    max_attempts=attempts,
                    error_type=type(exc).__name__,
                    **failure,
                )
                raise
            if "data_inspection_failed" in error_text:
                # Repeating the same rejected image payload cannot make it pass provider-side
                # inspection. Let the visual caller split or resample the payload instead.
                failure = build_prompt_failure(
                    PromptFailureCode.PROVIDER_IMAGE_INSPECTION_FAILED,
                    operation=operation,
                    error_message=error_summary(exc),
                )
                log_event(
                    "ERROR",
                    "model",
                    "model.fail",
                    "Model request rejected by provider inspection",
                    attempt=attempt,
                    max_attempts=attempts,
                    error_type=type(exc).__name__,
                    **failure,
                )
                raise
            if attempt >= attempts:
                failure = build_prompt_failure(
                    PromptFailureCode.MODEL_RETRY_EXHAUSTED,
                    operation=operation,
                    max_attempts=attempts,
                    error_type=type(exc).__name__,
                    error_message=error_summary(exc),
                )
                log_event(
                    "ERROR",
                    "model",
                    "model.fail",
                    "Model transaction exhausted its retries",
                    attempt=attempt,
                    **failure,
                )
                raise RuntimeError(
                    f"{operation} failed after {attempts} attempts: {exc}"
                ) from exc
            delay = min(2 ** (attempt - 1), 8)
            if on_retry is not None:
                on_retry(exc, attempt)
            failure = build_prompt_failure(
                PromptFailureCode.MODEL_RETRY_SCHEDULED,
                operation=operation,
                attempt=attempt,
                max_attempts=attempts,
                error_type=type(exc).__name__,
                error_message=error_summary(exc),
            )
            log_event(
                "WARNING",
                "model",
                "model.retry",
                "Model transaction failed; retrying",
                backoff_sec=delay,
                **failure,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")
