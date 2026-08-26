"""Bounded, append-only readers for managed Job logs."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from cutmaster.application.jobs.views import AttemptLogEntryView, AttemptLogPageView
from cutmaster.domain.ids import JobId
from cutmaster.infrastructure.observability.logging import redact_log_text

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_TAIL_BYTES = 2 * 1024 * 1024
_MAX_LINE_BYTES = 64 * 1024
_LEVELS = frozenset({"DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})


class JobLogReader:
    """Read only the exact log owned by one durable Job identifier."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.absolute()

    def path(self, job_id: JobId) -> Path:
        if not isinstance(job_id, JobId):
            raise TypeError("job_id must be a JobId")
        return self._data_root / "logs" / "jobs" / f"{job_id}.log"

    def info(self, job_id: JobId) -> tuple[Path, bool]:
        path = self.path(job_id)
        opened = _safe_regular_file(path, self._data_root)
        if opened is None:
            return path, False
        descriptor, _size = opened
        os.close(descriptor)
        return path, True

    def tail(self, job_id: JobId, *, limit: int = 50) -> AttemptLogPageView:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Job log tail limit must be between 1 and 500")
        path = self.path(job_id)
        opened = _safe_regular_file(path, self._data_root)
        if opened is None:
            return AttemptLogPageView(
                path=str(path),
                exists=False,
                entries=(),
                start_cursor=0,
                end_cursor=0,
                has_more_before=False,
            )
        descriptor, size = opened
        try:
            start = max(0, size - _MAX_TAIL_BYTES)
            chunk = os.pread(descriptor, size - start, start)
        finally:
            os.close(descriptor)
        lines = _complete_lines(
            chunk,
            absolute_start=start,
            discard_initial_partial=start > 0,
        )
        selected = lines[-limit:]
        return AttemptLogPageView(
            path=str(path),
            exists=True,
            entries=tuple(_entry(content, end) for content, _start, end in selected),
            start_cursor=(selected[0][1] if selected else 0),
            end_cursor=(lines[-1][2] if lines else 0),
            has_more_before=len(lines) > len(selected) or start > 0,
        )

    def full(self, job_id: JobId) -> AttemptLogPageView:
        """Read every complete line present in one Job log snapshot."""

        path = self.path(job_id)
        opened = _safe_regular_file(path, self._data_root)
        if opened is None:
            return AttemptLogPageView(
                path=str(path),
                exists=False,
                entries=(),
                start_cursor=0,
                end_cursor=0,
                has_more_before=False,
            )
        descriptor, size = opened
        try:
            chunk = os.pread(descriptor, size, 0)
        finally:
            os.close(descriptor)
        lines = _complete_lines(chunk, absolute_start=0)
        return AttemptLogPageView(
            path=str(path),
            exists=True,
            entries=tuple(_entry(content, end) for content, _start, end in lines),
            start_cursor=0,
            end_cursor=(lines[-1][2] if lines else 0),
            has_more_before=False,
        )

    def after(
        self,
        job_id: JobId,
        *,
        cursor: int,
        limit: int = 200,
    ) -> AttemptLogPageView:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("Job log cursor must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Job log page limit must be between 1 and 500")
        path = self.path(job_id)
        opened = _safe_regular_file(path, self._data_root)
        if opened is None:
            return AttemptLogPageView(
                path=str(path),
                exists=False,
                entries=(),
                start_cursor=0,
                end_cursor=0,
                has_more_before=False,
            )
        descriptor, size = opened
        try:
            if cursor > size:
                cursor = 0
            maximum = min(_MAX_TAIL_BYTES, size - cursor)
            chunk = os.pread(descriptor, maximum, cursor)
        finally:
            os.close(descriptor)
        lines = _complete_lines(chunk, absolute_start=cursor)[:limit]
        return AttemptLogPageView(
            path=str(path),
            exists=True,
            entries=tuple(_entry(content, end) for content, _start, end in lines),
            start_cursor=cursor,
            end_cursor=(lines[-1][2] if lines else cursor),
            has_more_before=False,
        )


def _safe_regular_file(path: Path, data_root: Path) -> tuple[int, int] | None:
    logs_path = data_root / "logs"
    jobs = logs_path / "jobs"
    try:
        root = data_root.resolve(strict=True)
        logs_metadata = logs_path.lstat()
        jobs_metadata = jobs.lstat()
        logs = logs_path.resolve(strict=True)
        parent = jobs.resolve(strict=True)
        metadata = path.lstat()
    except (FileNotFoundError, OSError, RuntimeError):
        return None
    if (
        stat.S_ISLNK(logs_metadata.st_mode)
        or not stat.S_ISDIR(logs_metadata.st_mode)
        or stat.S_ISLNK(jobs_metadata.st_mode)
        or not stat.S_ISDIR(jobs_metadata.st_mode)
        or logs.parent != root
        or parent.parent != logs
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or path.resolve(strict=True).parent != parent
    ):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    observed = os.fstat(descriptor)
    if not stat.S_ISREG(observed.st_mode) or observed.st_ino != metadata.st_ino:
        os.close(descriptor)
        return None
    return descriptor, observed.st_size


def _complete_lines(
    chunk: bytes,
    *,
    absolute_start: int,
    discard_initial_partial: bool = False,
) -> list[tuple[bytes, int, int]]:
    if discard_initial_partial:
        boundary = chunk.find(b"\n")
        if boundary < 0:
            return []
        absolute_start += boundary + 1
        chunk = chunk[boundary + 1 :]
    if not chunk.endswith(b"\n"):
        boundary = chunk.rfind(b"\n")
        if boundary < 0:
            return []
        chunk = chunk[: boundary + 1]
    result: list[tuple[bytes, int, int]] = []
    cursor = absolute_start
    for raw in chunk.splitlines(keepends=True):
        start = cursor
        cursor += len(raw)
        result.append((raw[:_MAX_LINE_BYTES], start, cursor))
    return result


def _entry(raw: bytes, cursor: int) -> AttemptLogEntryView:
    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    text = redact_log_text(_ANSI_ESCAPE.sub("", text))
    parts = text.split(" | ", 5)
    if len(parts) == 6 and parts[1].strip().upper() in _LEVELS:
        timestamp, level, component, event, fields, message = parts
        return AttemptLogEntryView(
            cursor=cursor,
            timestamp=timestamp.strip(),
            level=level.strip().upper(),
            component=component.strip(),
            event=event.strip(),
            fields=fields.strip(),
            message=message,
        )
    if len(parts) == 5 and parts[1].strip().upper() in _LEVELS:
        timestamp, level, component, event, message = parts
        return AttemptLogEntryView(
            cursor=cursor,
            timestamp=timestamp.strip(),
            level=level.strip().upper(),
            component=component.strip(),
            event=event.strip(),
            fields="",
            message=message,
        )
    return AttemptLogEntryView(
        cursor=cursor,
        timestamp=None,
        level="RAW",
        component=None,
        event=None,
        fields="",
        message=text,
    )


__all__ = ["JobLogReader"]
