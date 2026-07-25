from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from loguru import logger

from cutmaster.runtime.observability import (
    configure_logging,
    error_summary,
    log_event,
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
