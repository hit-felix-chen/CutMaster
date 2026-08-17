from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import cutmaster.adapters.worker.main as worker_main
from cutmaster.domain.ids import JobId


@pytest.fixture(autouse=True)
def _isolate_process_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_main,
        "configure_console_logging",
        lambda **_kwargs: None,
    )


class _RootCoordinator:
    def __init__(self) -> None:
        self.allow_maintenance: list[bool] = []

    @contextmanager
    def shared(self, *, allow_maintenance: bool = False):
        self.allow_maintenance.append(allow_maintenance)
        yield


def _application(operation_type: str):
    attempt = SimpleNamespace(operation_type=operation_type)
    jobs = SimpleNamespace(
        get_job=lambda _job_id: SimpleNamespace(attempt_id="attempt-token"),
        get_attempt=lambda _attempt_id: attempt,
    )
    return SimpleNamespace(jobs=jobs, data_root_coordinator=_RootCoordinator())


def _argv(job_id: JobId) -> list[str]:
    return ["--config", "config.toml", "--job-id", str(job_id)]


def test_worker_configures_console_before_opening_application_or_executing_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application("material_analysis")
    events: list[object] = []

    def configure(**kwargs: object) -> None:
        events.append(("configure", kwargs))

    def open_application(path) -> object:
        events.append(("open", path))
        return application

    def execute(*_args, **_kwargs) -> object:
        events.append("execute")
        return SimpleNamespace(value="complete")

    monkeypatch.setattr(worker_main, "configure_console_logging", configure)
    monkeypatch.setattr(
        worker_main,
        "CutMasterApplication",
        SimpleNamespace(open=open_application),
    )
    monkeypatch.setattr(worker_main, "adopt_supervisor_lease", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "cutmaster.application.workflow.material_job.execute_material_job",
        execute,
    )

    assert worker_main.main(_argv(JobId.new())) == 0
    assert events == [
        ("configure", {"console_color": False}),
        ("open", Path("config.toml")),
        "execute",
    ]


@pytest.mark.parametrize(
    ("operation_type", "module_name", "function_name"),
    (
        (
            "material_analysis",
            "cutmaster.application.workflow.material_job",
            "execute_material_job",
        ),
        (
            "aster_planning",
            "cutmaster.application.workflow.run_job",
            "execute_run_job",
        ),
        (
            "rendering",
            "cutmaster.application.workflow.render_job",
            "execute_render_job",
        ),
    ),
)
def test_worker_routes_each_operation_to_its_application_job_executor(
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    module_name: str,
    function_name: str,
) -> None:
    application = _application(operation_type)
    job_id = JobId.new()
    calls: list[tuple[object, JobId, dict[str, object]]] = []

    def execute(app, exact_job_id, **kwargs):
        calls.append((app, exact_job_id, kwargs))
        return SimpleNamespace(value="complete")

    monkeypatch.setattr(
        worker_main,
        "CutMasterApplication",
        SimpleNamespace(open=lambda _path: application),
    )
    monkeypatch.setattr(
        worker_main,
        "adopt_supervisor_lease",
        lambda *_args, **_kwargs: "claimed-submission",
    )
    monkeypatch.setattr(f"{module_name}.{function_name}", execute)

    assert worker_main.main(_argv(job_id)) == 0
    assert len(calls) == 1
    called_application, called_job_id, kwargs = calls[0]
    assert called_application is application
    assert called_job_id == job_id
    assert kwargs["claimed_submission"] == "claimed-submission"
    if operation_type == "aster_planning":
        assert callable(kwargs["preview_dispatcher"])
    else:
        assert "preview_dispatcher" not in kwargs
    assert application.data_root_coordinator.allow_maintenance == [False]


def test_worker_maps_failed_application_status_to_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application("material_analysis")
    monkeypatch.setattr(
        worker_main,
        "CutMasterApplication",
        SimpleNamespace(open=lambda _path: application),
    )
    monkeypatch.setattr(
        worker_main,
        "adopt_supervisor_lease",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "cutmaster.application.workflow.material_job.execute_material_job",
        lambda *_args, **_kwargs: SimpleNamespace(value="failed"),
    )

    assert worker_main.main(_argv(JobId.new())) == 1


def test_exact_supervisor_grant_may_enter_root_during_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application("rendering")
    monkeypatch.setattr(
        worker_main,
        "CutMasterApplication",
        SimpleNamespace(open=lambda _path: application),
    )
    monkeypatch.setattr(
        worker_main,
        "adopt_supervisor_lease",
        lambda *_args, **_kwargs: "claimed-submission",
    )
    monkeypatch.setattr(
        "cutmaster.application.workflow.render_job.execute_render_job",
        lambda *_args, **_kwargs: SimpleNamespace(value="interrupted"),
    )
    argv = [
        *_argv(JobId.new()),
        "--attempt-id",
        "attempt_00000000-0000-4000-8000-000000000000",
        "--lease-worker-id",
        "supervisor",
        "--lease-process-id",
        "42",
    ]

    assert worker_main.main(argv) == 0
    assert application.data_root_coordinator.allow_maintenance == [True]


def test_worker_rejects_unknown_durable_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application("unknown")
    monkeypatch.setattr(
        worker_main,
        "CutMasterApplication",
        SimpleNamespace(open=lambda _path: application),
    )
    monkeypatch.setattr(
        worker_main,
        "adopt_supervisor_lease",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="Unsupported managed Job operation"):
        worker_main.main(_argv(JobId.new()))
