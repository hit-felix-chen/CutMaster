from __future__ import annotations

import io

import pytest

from cutmaster.infrastructure.observability import progress as progress_module


class _InteractiveBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_non_interactive_stdout_does_not_emit_progress_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(progress_module.sys, "stdout", stream)

    observed = list(
        progress_module.progress_iter(
            range(3),
            total=3,
            description="Managed worker progress",
            unit="item",
        )
    )

    assert observed == [0, 1, 2]
    assert stream.getvalue() == ""


def test_interactive_stdout_keeps_terminal_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _InteractiveBuffer()
    monkeypatch.setattr(progress_module.sys, "stdout", stream)

    observed = list(
        progress_module.progress_iter(
            range(1),
            total=1,
            description="Interactive progress",
            unit="item",
        )
    )

    assert observed == [0]
    assert "Interactive progress" in stream.getvalue()
