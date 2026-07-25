from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from loguru import logger
from openai import OpenAI

from cutmaster.json_utils import parse_json_object
from cutmaster.models import LLMConfig


T = TypeVar("T")


SYSTEM_PROMPT = (
    "You are a professional long-video montage editor. Return strict JSON only. "
    "Select only source segments that really exist in the supplied timestamped subtitles. "
    "Do not invent dialogue or timestamps."
)


def generate_text(
    prompt: str,
    config: LLMConfig,
    system_prompt: str = SYSTEM_PROMPT,
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
    extra_body = {"enable_thinking": enable_thinking} if enable_thinking is not None else None
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
    config: LLMConfig,
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
                raise
            if attempt >= attempts:
                raise RuntimeError(
                    f"{operation} failed after {attempts} attempts: {exc}"
                ) from exc
            delay = min(2 ** (attempt - 1), 8)
            logger.warning(
                "{} attempt {}/{} failed ({}: {}); retrying in {}s",
                operation,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")
