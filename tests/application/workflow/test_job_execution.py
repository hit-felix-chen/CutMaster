from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from cutmaster.application.workflow.job_execution import ManagedJobExecutor
from cutmaster.domain.ids import JobId
from cutmaster.infrastructure.observability.job_logs import JobLogReader
from cutmaster.infrastructure.observability.logging import log_event


def _application(data_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            effective_configuration=SimpleNamespace(data_root=data_root)
        )
    )


@pytest.mark.parametrize(
    ("method_name", "module_name", "function_name", "component"),
    (
        (
            "execute_material_job",
            "cutmaster.application.workflow.material_job",
            "execute_material_job",
            "analyser",
        ),
        (
            "execute_run_job",
            "cutmaster.application.workflow.run_job",
            "execute_run_job",
            "aster.arrangement",
        ),
        (
            "execute_render_job",
            "cutmaster.application.workflow.render_job",
            "execute_render_job",
            "renderer",
        ),
    ),
)
def test_synchronous_managed_job_writes_its_canonical_job_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    module_name: str,
    function_name: str,
    component: str,
) -> None:
    data_root = tmp_path / ".cutmaster"
    application = _application(data_root)
    executor = ManagedJobExecutor(application)
    job_id = JobId.new()
    console = io.StringIO()
    console_sink = logger.add(console, format="{message}")

    def execute(app, exact_job_id, **kwargs):
        assert app is application
        assert exact_job_id == job_id
        assert kwargs == {"worker_id": "benchmark"}
        log_event(
            "INFO",
            component,
            "stage.start",
            "Managed job stage started",
            stage="fixture",
        )
        return "complete"

    monkeypatch.setattr(f"{module_name}.{function_name}", execute)
    try:
        assert getattr(executor, method_name)(
            job_id,
            worker_id="benchmark",
        ) == "complete"
    finally:
        logger.remove(console_sink)

    assert "Managed job stage started" in console.getvalue()
    page = JobLogReader(data_root).tail(job_id, limit=5)
    assert page.exists is True
    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.level == "INFO"
    assert entry.component == component
    assert entry.event == "stage.start"
    assert entry.fields == "stage=fixture"
    assert entry.message == "Managed job stage started"


def test_synchronous_job_log_sink_is_removed_when_execution_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / ".cutmaster"
    executor = ManagedJobExecutor(_application(data_root))
    job_id = JobId.new()

    def execute(*_args, **_kwargs):
        log_event(
            "ERROR",
            "renderer",
            "stage.fail",
            "Render failed inside the managed Job",
        )
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(
        "cutmaster.application.workflow.render_job.execute_render_job",
        execute,
    )

    with pytest.raises(RuntimeError, match="fixture failure"):
        executor.execute_render_job(job_id, worker_id="benchmark")

    path = JobLogReader(data_root).path(job_id)
    before = path.read_text(encoding="utf-8")
    log_event(
        "INFO",
        "renderer",
        "stage.progress",
        "This later event must not leak into the completed Job log",
    )
    assert path.read_text(encoding="utf-8") == before

