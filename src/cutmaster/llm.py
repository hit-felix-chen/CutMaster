from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from openai import OpenAI

from cutmaster.json_utils import parse_json_object
from cutmaster.models import ModelConfig
from cutmaster.observability import error_summary, log_event


T = TypeVar("T")


def generate_text(
    prompt: str,
    config: ModelConfig,
    system_prompt: str,
    enable_thinking: bool | None = None,
    image_data_urls: list[str] | None = None,
) -> str:
    client = OpenAI(
        api_key=config.api_key,
        base_url=config.base_url or None,
        timeout=config.timeout_sec,
        # Full request/JSON/schema retries are handled by request_json_with_retries.
        # Keeping SDK retries enabled here would multiply the configured attempts.
        max_retries=0,
    )
    thinking = config.enable_thinking if enable_thinking is None else enable_thinking
    extra_body = {"enable_thinking": thinking}
    user_content: str | list[dict[str, Any]] = prompt
    if image_data_urls:
        user_content = [{"type": "text", "text": prompt}]
        user_content.extend(
            {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
            for data_url in image_data_urls
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
    return response.choices[0].message.content


def request_json_with_retries(
    request: Callable[[], str],
    config: ModelConfig,
    *,
    operation: str,
    validate: Callable[[dict], T] | None = None,
) -> T | dict:
    """Retry the complete request/parse/validation transaction."""
    attempts = max(1, config.max_retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            parsed = parse_json_object(request())
            return validate(parsed) if validate is not None else parsed
        except Exception as exc:
            if "data_inspection_failed" in str(exc).lower():
                # Repeating the same rejected image payload cannot make it pass provider-side
                # inspection. Let the visual caller split or resample the payload instead.
                log_event(
                    "ERROR",
                    "model",
                    "model.fail",
                    "Model request rejected by provider inspection",
                    operation=operation,
                    attempt=attempt,
                    max_attempts=attempts,
                    error_type=type(exc).__name__,
                    reason=error_summary(exc),
                )
                raise
            if attempt >= attempts:
                log_event(
                    "ERROR",
                    "model",
                    "model.fail",
                    "Model transaction exhausted its retries",
                    operation=operation,
                    attempt=attempt,
                    max_attempts=attempts,
                    error_type=type(exc).__name__,
                    reason=error_summary(exc),
                )
                raise RuntimeError(
                    f"{operation} failed after {attempts} attempts: {exc}"
                ) from exc
            delay = min(2 ** (attempt - 1), 8)
            log_event(
                "WARNING",
                "model",
                "model.retry",
                "Model transaction failed; retrying",
                operation=operation,
                attempt=attempt,
                max_attempts=attempts,
                backoff_sec=delay,
                error_type=type(exc).__name__,
                reason=error_summary(exc),
            )
            time.sleep(delay)
    raise AssertionError("unreachable")
