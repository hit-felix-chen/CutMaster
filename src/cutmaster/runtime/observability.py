from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from loguru import logger


LOG_FORMAT = (
    "{extra[log_timestamp]} | {level:<8} | {extra[component]} | "
    "{extra[event]}{extra[fields_suffix]} | {message}{exception}"
)
CONSOLE_LOG_FORMAT = (
    "{extra[log_timestamp]} | <level>{level:<8}</level> | {extra[component]} | "
    "{extra[event]}{extra[fields_suffix]} | {message}{exception}"
)
LEVEL_COLORS = {
    "DEBUG": "<cyan>",
    "INFO": "<blue>",
    "SUCCESS": "<green>",
    "WARNING": "<yellow>",
    "ERROR": "<red>",
    "CRITICAL": "<red><bold>",
}
VALID_LEVELS = frozenset(
    {"DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
)
VALID_COMPONENTS = frozenset(
    {
        "orchestrator",
        "analyser",
        "dialogue",
        "music",
        "planner.slot",
        "planner.candidate",
        "planner.sequence",
        "planner.review",
        "script",
        "source_window",
        "renderer",
        "asr",
        "model",
    }
)
VALID_EVENTS = frozenset(
    {
        "workflow.start",
        "workflow.complete",
        "workflow.fail",
        "stage.start",
        "stage.progress",
        "stage.complete",
        "stage.fail",
        "cache.hit",
        "cache.miss",
        "cache.invalid",
        "model.start",
        "model.complete",
        "model.retry",
        "model.fail",
        "checkpoint.write",
        "checkpoint.resume",
        "validation.reject",
        "fallback.apply",
    }
)

_COMPONENT_BY_MODULE = {
    "analyser": "analyser",
    "asr": "asr",
    "beats": "music",
    "candidate_retriever": "planner.candidate",
    "cli": "orchestrator",
    "cuts": "source_window",
    "dialogue": "dialogue",
    "llm": "model",
    "music": "music",
    "orchestrator": "orchestrator",
    "renderer": "renderer",
    "script": "script",
    "script_reviewer": "planner.review",
    "sequence_selector": "planner.sequence",
    "slot_planner": "planner.slot",
    "workflow_context": "model",
}
_INTERNAL_FIELDS = {"component", "event", "fields_suffix", "log_timestamp"}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|authorization)\b(\s*[:=]\s*)([^\s,;]+)"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
)


def _infer_component(module_name: str) -> str:
    leaf = module_name.rsplit(".", 1)[-1]
    return _COMPONENT_BY_MODULE.get(leaf, "orchestrator")


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def error_summary(error: BaseException) -> str:
    """Return a compact, redacted error description suitable for operational logs."""
    text = " ".join(str(error).split())
    return _redact_text(text)[:500]


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        value = _redact_text(value)
        if value and all(character.isalnum() or character in "._:/+-" for character in value):
            return value
        return json.dumps(value, ensure_ascii=False)
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return _redact_text(serialized)


def _patch_record(record: dict[str, Any]) -> None:
    extra = record["extra"]
    extra["log_timestamp"] = record["time"].isoformat(
        timespec="milliseconds"
    ).replace("T", " ", 1)
    extra["component"] = extra.get("component") or _infer_component(record["name"])
    extra["event"] = extra.get("event") or "stage.progress"
    fields = [
        f"{key}={_format_value(extra[key])}"
        for key in sorted(extra)
        if key not in _INTERNAL_FIELDS
    ]
    extra["fields_suffix"] = f" | {' '.join(fields)}" if fields else ""


def configure_logging(
    file_path: Path,
    *,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    console_color: bool | None = None,
) -> None:
    """Configure the process-wide operational log sinks."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(patcher=_patch_record)
    for level, color in LEVEL_COLORS.items():
        logger.level(level, color=color)
    console_colorize = (
        bool(getattr(sys.stderr, "isatty", lambda: False)())
        if console_color is None
        else console_color
    )
    logger.add(
        sys.stderr,
        level=console_level,
        format=CONSOLE_LOG_FORMAT,
        colorize=console_colorize,
    )
    logger.add(
        file_path,
        level=file_level,
        format=LOG_FORMAT,
        colorize=False,
        encoding="utf-8",
    )


def log_event(
    level: str,
    component: str,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    """Write one structured operational event without prompt or response payloads."""
    normalized_level = level.upper()
    if normalized_level not in VALID_LEVELS:
        raise ValueError(f"Unsupported log level: {level}")
    if component not in VALID_COMPONENTS:
        raise ValueError(f"Unsupported log component: {component}")
    if event not in VALID_EVENTS:
        raise ValueError(f"Unsupported log event: {event}")
    logger.bind(component=component, event=event, **fields).log(
        normalized_level,
        message,
    )
