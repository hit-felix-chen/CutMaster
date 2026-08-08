from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest
from loguru import logger

from cutmaster.runtime.observability import (
    VALID_COMPONENTS,
    configure_logging,
    error_summary,
    log_event,
)


def test_dialogue_anchor_stage_is_part_of_log_taxonomy() -> None:
    assert "orchestrator" in VALID_COMPONENTS
    assert "planner" in VALID_COMPONENTS
    assert "aster.story" in VALID_COMPONENTS
    assert "dialogue_audio" in VALID_COMPONENTS
    log_event(
        "INFO",
        "aster.story",
        "stage.start",
        "Dialogue anchor selection started",
    )


def test_structured_log_format_and_field_normalization(tmp_path: Path) -> None:
    log_path = tmp_path / "cutmaster.log"
    try:
        configure_logging(log_path)
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Shot detection completed",
            stage="shot_detection",
            elapsed_sec=1.23456,
            thinking=False,
            shots=42,
        )
        line = log_path.read_text(encoding="utf-8").strip()
    finally:
        logger.remove()
        logger.configure(patcher=None)
        logger.add(sys.stderr)

    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} "
        r"\| INFO\s+ \| analyser \| stage\.complete \| ",
        line,
    )
    assert (
        "elapsed_sec=1.235 shots=42 stage=shot_detection thinking=false"
        in line
    )
    assert line.endswith("| Shot detection completed")
    assert "\n" not in line


def test_error_summary_redacts_credentials_and_is_bounded() -> None:
    error = RuntimeError(
        "api_key=secret-value Authorization: bearer-value "
        "Bearer token-value sk-abcdefghijklmnopqrstuvwxyz " + "x" * 600
    )

    summary = error_summary(error)

    assert "secret-value" not in summary
    assert "bearer-value" not in summary
    assert "token-value" not in summary
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in summary
    assert len(summary) <= 500


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("level", "ansi_code"),
    [
        ("DEBUG", "\x1b[36m"),
        ("INFO", "\x1b[34m"),
        ("SUCCESS", "\x1b[32m"),
        ("WARNING", "\x1b[33m"),
        ("ERROR", "\x1b[31m"),
        ("CRITICAL", "\x1b[31m"),
    ],
)
def test_console_colors_log_levels_but_file_stays_plain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    ansi_code: str,
) -> None:
    log_path = tmp_path / "cutmaster.log"
    console = _TTYBuffer()
    monkeypatch.setattr(sys, "stderr", console)
    try:
        configure_logging(log_path, console_level="DEBUG")
        log_event(level, "cutmaster", "stage.progress", "Colored event")
        console_line = console.getvalue()
        file_line = log_path.read_text(encoding="utf-8")
    finally:
        logger.remove()
        logger.configure(patcher=None)
        logger.add(sys.__stderr__)

    assert ansi_code in console_line
    if level == "CRITICAL":
        assert "\x1b[1m" in console_line
    assert f"{level}" in console_line
    assert "\x1b[" not in file_line
    assert f"| {level}" in file_line


def test_non_tty_console_disables_colors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "cutmaster.log"
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    try:
        configure_logging(log_path)
        log_event("WARNING", "cutmaster", "stage.progress", "Plain event")
        console_line = console.getvalue()
    finally:
        logger.remove()
        logger.configure(patcher=None)
        logger.add(sys.__stderr__)

    assert "\x1b[" not in console_line


def test_explicit_console_color_survives_a_forwarding_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "cutmaster.log"
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    try:
        configure_logging(log_path, console_color=True)
        log_event("INFO", "cutmaster", "stage.progress", "Forwarded event")
        console_line = console.getvalue()
        file_line = log_path.read_text(encoding="utf-8")
    finally:
        logger.remove()
        logger.configure(patcher=None)
        logger.add(sys.__stderr__)

    assert "\x1b[34m" in console_line
    assert "\x1b[" not in file_line


@pytest.mark.parametrize(
    ("level", "component", "event"),
    [
        ("TRACE", "analyser", "stage.start"),
        ("INFO", "unknown", "stage.start"),
        ("INFO", "analyser", "unknown.event"),
    ],
)
def test_log_event_rejects_nonstandard_taxonomy(
    level: str,
    component: str,
    event: str,
) -> None:
    with pytest.raises(ValueError):
        log_event(level, component, event, "Invalid event")
