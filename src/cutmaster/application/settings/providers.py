"""Canonical provider presets and secret-free editable projections."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlparse

ProviderProfile = Literal["cost_saving", "simple", "custom"]
ProviderCapability = Literal["llm", "vlm", "asr"]

MODEL_PROVIDER_FIELDS = frozenset(
    {
        "model",
        "base_url",
        "api_key_env",
        "enable_thinking",
        "temperature",
        "max_tokens",
        "timeout_sec",
        "max_retries",
        "max_concurrency",
        "input_price_yuan_per_million_tokens",
        "cached_input_price_yuan_per_million_tokens",
        "output_price_yuan_per_million_tokens",
    }
)
ASR_PROVIDER_FIELDS = frozenset(
    {
        "backend",
        "api_key_env",
        "reuse",
        "timeout_sec",
        "poll_interval_sec",
        "max_chars",
        "max_subtitle_duration_sec",
    }
)

_COMMON_MODEL = {
    "enable_thinking": True,
    "temperature": 0.1,
    "max_tokens": 40000,
    "timeout_sec": 600.0,
    "max_retries": 3,
    "max_concurrency": 10,
}
_DEEPSEEK_LLM = {
    **_COMMON_MODEL,
    "model": "deepseek-v4-flash",
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
    "input_price_yuan_per_million_tokens": 1.0,
    "cached_input_price_yuan_per_million_tokens": 0.02,
    "output_price_yuan_per_million_tokens": 2.0,
}
_DASHSCOPE_LLM = {
    **_COMMON_MODEL,
    "model": "qwen3.7-max",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key_env": "DASHSCOPE_API_KEY",
    "input_price_yuan_per_million_tokens": 6.0,
    "cached_input_price_yuan_per_million_tokens": 1.2,
    "output_price_yuan_per_million_tokens": 18.0,
}
_DASHSCOPE_VLM = {
    **_COMMON_MODEL,
    "model": "qwen3.7-plus",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key_env": "DASHSCOPE_API_KEY",
    "input_price_yuan_per_million_tokens": 2.0,
    "cached_input_price_yuan_per_million_tokens": 0.4,
    "output_price_yuan_per_million_tokens": 8.0,
}
_DASHSCOPE_ASR = {
    "backend": "bailian",
    "api_key_env": "DASHSCOPE_API_KEY",
    "reuse": True,
    "timeout_sec": 600.0,
    "poll_interval_sec": 2.0,
    "max_chars": 20,
    "max_subtitle_duration_sec": 3.5,
}

COST_SAVING_PROVIDERS: dict[str, dict[str, Any]] = {
    "llm": _DEEPSEEK_LLM,
    "vlm": _DASHSCOPE_VLM,
    "asr": _DASHSCOPE_ASR,
}
SIMPLE_PROVIDERS: dict[str, dict[str, Any]] = {
    "llm": _DASHSCOPE_LLM,
    "vlm": _DASHSCOPE_VLM,
    "asr": _DASHSCOPE_ASR,
}


def provider_presets() -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "cost_saving": copy.deepcopy(COST_SAVING_PROVIDERS),
        "simple": copy.deepcopy(SIMPLE_PROVIDERS),
    }


def provider_projection(values: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    llm = _projection_section(values.get("llm"), MODEL_PROVIDER_FIELDS, "llm")
    vlm = _projection_section(values.get("vlm"), MODEL_PROVIDER_FIELDS, "vlm")
    analyser = values.get("analyser")
    if not isinstance(analyser, Mapping):
        raise TypeError("analyser must be a mapping")
    asr = _projection_section(analyser.get("asr"), ASR_PROVIDER_FIELDS, "analyser.asr")
    return {"llm": llm, "vlm": vlm, "asr": asr}


def canonical_providers(profile: ProviderProfile) -> dict[str, dict[str, Any]]:
    if profile == "cost_saving":
        return copy.deepcopy(COST_SAVING_PROVIDERS)
    if profile == "simple":
        return copy.deepcopy(SIMPLE_PROVIDERS)
    raise ValueError("Custom provider settings require explicit providers")


def validate_custom_providers(
    providers: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if set(providers) != {"llm", "vlm", "asr"}:
        raise ValueError("Custom providers must contain exactly llm, vlm, and asr")
    normalized = {
        "llm": _exact_section(providers["llm"], MODEL_PROVIDER_FIELDS, "llm"),
        "vlm": _exact_section(providers["vlm"], MODEL_PROVIDER_FIELDS, "vlm"),
        "asr": _exact_section(providers["asr"], ASR_PROVIDER_FIELDS, "asr"),
    }
    _validate_model(normalized["llm"], "llm")
    _validate_model(normalized["vlm"], "vlm")
    _validate_asr(normalized["asr"])
    return normalized


def derive_profile(providers: Mapping[str, Any]) -> ProviderProfile:
    if set(providers) != {"llm", "vlm", "asr"}:
        return "custom"
    normalized = {
        "llm": _exact_section(providers["llm"], MODEL_PROVIDER_FIELDS, "llm"),
        "vlm": _exact_section(providers["vlm"], MODEL_PROVIDER_FIELDS, "vlm"),
        "asr": _exact_section(providers["asr"], ASR_PROVIDER_FIELDS, "asr"),
    }
    if normalized == COST_SAVING_PROVIDERS:
        return "cost_saving"
    if normalized == SIMPLE_PROVIDERS:
        return "simple"
    return "custom"


def _exact_section(
    value: Any,
    fields: frozenset[str],
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if set(value) != fields:
        missing = sorted(fields - set(value))
        unknown = sorted(set(value) - fields)
        raise ValueError(
            f"{name} has an invalid editable field set; missing={missing}, unknown={unknown}"
        )
    return {field: copy.deepcopy(value[field]) for field in sorted(fields)}


def _projection_section(
    value: Any,
    fields: frozenset[str],
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    projected = dict(value)
    projected.setdefault("api_key_env", "")
    return _exact_section(projected, fields, name)


def _valid_url(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}.base_url must not be empty")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name}.base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError(
            f"{name}.base_url cannot contain credentials, a query, or a fragment"
        )
    return value.strip().rstrip("/")


def _finite_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return normalized


def _integer(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _environment_name(value: Any, name: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", value.strip()
    ):
        raise ValueError(f"{name}.api_key_env must be an environment variable name")
    return value.strip()


def _validate_model(value: dict[str, Any], name: str) -> None:
    if not isinstance(value["model"], str) or not value["model"].strip():
        raise ValueError(f"{name}.model must not be empty")
    value["model"] = value["model"].strip()
    value["base_url"] = _valid_url(value["base_url"], name)
    value["api_key_env"] = _environment_name(value["api_key_env"], name)
    if not isinstance(value["enable_thinking"], bool):
        raise TypeError(f"{name}.enable_thinking must be a boolean")
    value["temperature"] = _finite_number(value["temperature"], f"{name}.temperature")
    value["timeout_sec"] = _finite_number(
        value["timeout_sec"], f"{name}.timeout_sec", minimum=0.001
    )
    value["input_price_yuan_per_million_tokens"] = _finite_number(
        value["input_price_yuan_per_million_tokens"],
        f"{name}.input_price_yuan_per_million_tokens",
    )
    value["cached_input_price_yuan_per_million_tokens"] = _finite_number(
        value["cached_input_price_yuan_per_million_tokens"],
        f"{name}.cached_input_price_yuan_per_million_tokens",
    )
    value["output_price_yuan_per_million_tokens"] = _finite_number(
        value["output_price_yuan_per_million_tokens"],
        f"{name}.output_price_yuan_per_million_tokens",
    )
    value["max_tokens"] = _integer(value["max_tokens"], f"{name}.max_tokens", minimum=1)
    value["max_retries"] = _integer(
        value["max_retries"], f"{name}.max_retries", minimum=0
    )
    value["max_concurrency"] = _integer(
        value["max_concurrency"], f"{name}.max_concurrency", minimum=1
    )


def _validate_asr(value: dict[str, Any]) -> None:
    if value["backend"] != "bailian":
        raise ValueError("asr.backend must be bailian")
    value["api_key_env"] = _environment_name(value["api_key_env"], "asr")
    if not isinstance(value["reuse"], bool):
        raise TypeError("asr.reuse must be a boolean")
    value["timeout_sec"] = _finite_number(
        value["timeout_sec"], "asr.timeout_sec", minimum=0.001
    )
    value["poll_interval_sec"] = _finite_number(
        value["poll_interval_sec"], "asr.poll_interval_sec", minimum=0.001
    )
    value["max_chars"] = _integer(value["max_chars"], "asr.max_chars", minimum=1)
    value["max_subtitle_duration_sec"] = _finite_number(
        value["max_subtitle_duration_sec"],
        "asr.max_subtitle_duration_sec",
        minimum=0.001,
    )


__all__ = [
    "ASR_PROVIDER_FIELDS",
    "MODEL_PROVIDER_FIELDS",
    "ProviderCapability",
    "ProviderProfile",
    "canonical_providers",
    "derive_profile",
    "provider_presets",
    "provider_projection",
    "validate_custom_providers",
]
