from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest
from loguru import logger

from cutmaster.domain.ids import JobId
from cutmaster.infrastructure.observability.job_logs import JobLogReader
from cutmaster.infrastructure.observability.logging import (
    VALID_COMPONENTS,
    configure_console_logging,
    configure_logging,
    error_summary,
    log_event,
)


def test_dialogue_anchor_stage_is_part_of_log_taxonomy() -> None:
    assert "orchestrator" not in VALID_COMPONENTS
    assert "planners" in VALID_COMPONENTS
    assert "aster.story" in VALID_COMPONENTS
    assert "dialogue_audio" in VALID_COMPONENTS
    log_event(
        "INFO",
        "aster.story",
        "stage.start",
        "Dialogue anchor selection started",
    )


@pytest.mark.parametrize(
    "component",
    (
        "application.workflow",
        "application.materials",
        "application.runs",
        "application.renders",
        "application.jobs",
        "worker",
        "web",
    ),
)
def test_adapter_and_application_components_are_part_of_log_taxonomy(
    component: str,
) -> None:
    assert component in VALID_COMPONENTS
    log_event("INFO", component, "stage.progress", "Boundary event")


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


def test_console_only_log_is_single_and_parseable_as_a_job_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    try:
        configure_console_logging(console_color=False)
        log_event(
            "WARNING",
            "model",
            "model.retry",
            "Model request will retry",
            attempt=2,
            operation="slot_retrieval",
        )
        lines = console.getvalue().splitlines(keepends=True)
    finally:
        logger.remove()
        logger.configure(patcher=None)
        logger.add(sys.__stderr__)

    assert len(lines) == 1
    assert "\x1b[" not in lines[0]

    data_root = tmp_path / "data-root"
    job_id = JobId.new()
    path = data_root / "logs" / "jobs" / f"{job_id}.log"
    path.parent.mkdir(parents=True)
    path.write_text(lines[0], encoding="utf-8")

    page = JobLogReader(data_root).tail(job_id, limit=1)

    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.level == "WARNING"
    assert entry.component == "model"
    assert entry.event == "model.retry"
    assert entry.fields == "attempt=2 operation=slot_retrieval"
    assert entry.message == "Model request will retry"


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
