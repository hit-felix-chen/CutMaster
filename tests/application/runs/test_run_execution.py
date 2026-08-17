"""Tests for managed ASTER Run planning execution."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from cutmaster.application.materials import MaterialsService
from cutmaster.application.ports.material_catalog import MaterialBinding
from cutmaster.application.runs import (
    ExecuteRunPlanningCommand,
    RunPlanningExecutor,
    RunView,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.ids import MaterialId, ProjectId, RunId
from cutmaster.domain.materials import (
    Material,
    MaterialCondition,
    MaterialFingerprint,
    MaterialType,
)
from cutmaster.domain.projects import CreativeBrief
from cutmaster.domain.runs import RunStatus
from cutmaster.workflow.contracts.planners import PlannersResult
from cutmaster.workflow.contracts.render_plan import RenderPlan


class LeaseCatalog:
    def __init__(self, bindings: tuple[MaterialBinding, ...]) -> None:
        self.bindings = {
            binding.material.material_id: binding for binding in bindings
        }
        self.active: set[MaterialId] = set()
        self.acquired: list[MaterialId] = []

    @contextmanager
    def consume_lease(self, material_id: MaterialId):
        binding = self.bindings[material_id]
        self.acquired.append(material_id)
        self.active.add(material_id)
        try:
            yield binding
        finally:
            self.active.remove(material_id)


class Reporter:
    def report(self, _update: object) -> None:
        pass


class Token:
    def __init__(self) -> None:
        self.polls = 0

    def raise_if_cancelled(self) -> None:
        self.polls += 1


def _binding(root: Path, material_type: MaterialType, name: str) -> MaterialBinding:
    source = root / f"source-{material_type.value}"
    source.write_bytes(material_type.value.encode())
    fingerprint = MaterialFingerprint(hashlib.sha256(source.read_bytes()).hexdigest())
    material = Material(
        material_id=MaterialId.new(),
        material_type=material_type,
        name=name,
        fingerprint=fingerprint,
        condition=MaterialCondition.READY,
    )
    memory = root / f"memory-{material_type.value}"
    memory.mkdir()
    manifest = memory / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    if material_type is MaterialType.VIDEO:
        for filename in (
            "video_description.json",
            "video_summary.json",
            "dialogues.json",
        ):
            (memory / filename).write_text("{}\n", encoding="utf-8")
        result_name = "analysis_result.json"
        memory_schema_version = "3.0"
        extra = {
            "model_usage_summary": {},
            "model_usage_cumulative_summary": {},
        }
    else:
        (memory / "music_memory.json").write_text("{}\n", encoding="utf-8")
        result_name = "music_analysis_result.json"
        memory_schema_version = "2.0"
        extra = {}
    (memory / result_name).write_text(
        json.dumps(
            {
                "schema_version": "3.0",
                "status": "success",
                "material_id": str(material.material_id),
                "material_type": material.material_type.value,
                "material_name": material.name,
                "material_fingerprint": str(material.fingerprint),
                "memory_schema_version": memory_schema_version,
                "elapsed_sec": 0.0,
                "material_reused": False,
                "analysis_reused": False,
                **extra,
            }
        ),
        encoding="utf-8",
    )
    return MaterialBinding(
        material,
        source.resolve(),
        memory.resolve(),
        manifest.resolve(),
    )


def _run(
    configuration: EffectiveConfiguration,
    video_id: MaterialId,
    music_id: MaterialId,
    *,
    video_ids: tuple[MaterialId, ...] | None = None,
) -> RunView:
    values = configuration.to_dict()
    values["llm"]["model"] = "snapshotted-llm"
    values["vlm"]["model"] = "snapshotted-vlm"
    now = datetime.now(UTC)
    return RunView(
        run_id=RunId.new(),
        project_id=ProjectId.new(),
        sequence=1,
        status=RunStatus.PLANNERS,
        creative_brief=CreativeBrief("Build the immutable story", 42.5),
        video_material_ids=(video_id,) if video_ids is None else video_ids,
        music_material_ids=(music_id,),
        configuration=MappingProxyType(values),
        planning_options=MappingProxyType(
            {
                "target_shot_length_sec": 3.25,
                "prompt_type": "character",
                "video_title": "Snapshot Title",
                "max_clip_duration_sec": 7.5,
            }
        ),
        failure_message=None,
        created_at=now,
        updated_at=now,
    )


def _planner_result(request, workspace: Path) -> PlannersResult:
    plan = RenderPlan.create(
        video_material_id=request.video.material.material_id,
        video_expected_fingerprint=request.video.material.expected_fingerprint,
        music_material_id=request.music.material.material_id,
        music_expected_fingerprint=request.music.material.expected_fingerprint,
        fps=30,
        clips=[
            {
                "slot_id": "slot_01",
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            }
        ],
        planners_metadata={},
    )
    render_plan = plan.write(workspace / "render_plan.json")
    paths = {
        name: workspace / f"{name}.json"
        for name in (
            "music_profile",
            "edit_plan",
            "dialogue_anchors",
            "candidate_pool",
            "raw_script",
            "selection_diagnostics",
            "planners_history",
            "planners_calls",
        )
    }
    for path in paths.values():
        path.write_text("{}\n", encoding="utf-8")
    return PlannersResult(
        status="success",
        render_plan=plan,
        render_plan_path=render_plan,
        music_profile_path=paths["music_profile"],
        edit_plan_path=paths["edit_plan"],
        dialogue_anchors_path=paths["dialogue_anchors"],
        candidate_pool_path=paths["candidate_pool"],
        raw_script_path=paths["raw_script"],
        selection_diagnostics_path=paths["selection_diagnostics"],
        planners_history_path=paths["planners_history"],
        planners_calls_path=paths["planners_calls"],
        target_output_length_sec=request.brief.target_duration_sec,
        planned_output_length_sec=1.0,
        num_raw_clips=1,
        num_planned_clips=1,
        stage_timings_sec={},
        wall_clock_sec=0.1,
        model_usage_summary={"total_tokens": 123},
    )


def test_executor_uses_run_snapshot_ids_options_and_runtime_capabilities(
    managed_configuration: EffectiveConfiguration,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    video = _binding(tmp_path, MaterialType.VIDEO, "Managed Video")
    music = _binding(tmp_path, MaterialType.MUSIC, "Managed Music")
    catalog = LeaseCatalog((video, music))
    materials = MaterialsService(managed_configuration, catalog=catalog)
    run = _run(
        managed_configuration,
        video.material.material_id,
        music.material.material_id,
    )
    workspace = (tmp_path / "planning-workspace").resolve()
    workspace.mkdir()
    reporter = Reporter()
    token = Token()
    checkpoint = object()
    captured: dict[str, Any] = {}

    class Planner:
        def __init__(self, config) -> None:
            captured["config"] = config

        def plan(self, request, **kwargs):
            captured["request"] = request
            captured["kwargs"] = kwargs
            assert catalog.active == {
                video.material.material_id,
                music.material.material_id,
            }
            return _planner_result(request, workspace)

    result = RunPlanningExecutor(managed_configuration, materials).execute(
        ExecuteRunPlanningCommand(
            run=run,
            workspace=workspace,
            overwrite=True,
            progress_reporter=reporter,
            cancellation_token=token,
            checkpoint_store=checkpoint,
        ),
        planner_factory=Planner,
    )

    request = captured["request"]
    runtime = captured["config"]
    assert runtime.llm.model == "snapshotted-llm"
    assert runtime.vlm.model == "snapshotted-vlm"
    assert runtime.llm.api_key == runtime.vlm.api_key == "test-secret"
    assert request.video.material.material_id == video.material.material_id
    assert request.music.material.material_id == music.material.material_id
    assert request.brief.editing_intent == "Build the immutable story"
    assert request.brief.target_duration_sec == 42.5
    assert request.options.target_shot_length_sec == 3.25
    assert request.options.prompt_type == "character"
    assert request.options.video_title == "Snapshot Title"
    assert request.options.max_clip_duration_sec == 7.5
    assert request.workspace.root == workspace
    assert captured["kwargs"] == {
        "overwrite": True,
        "progress_reporter": reporter,
        "cancellation_token": token,
        "checkpoint_store": checkpoint,
    }
    assert catalog.acquired == [
        video.material.material_id,
        music.material.material_id,
    ]
    assert catalog.active == set()
    assert token.polls == 3
    assert result.render_plan == (workspace / "render_plan.json").resolve()
    assert result.model_usage_summary == {"total_tokens": 123}
    with pytest.raises(TypeError):
        result.model_usage_summary["total_tokens"] = 456

    serialized_plan = result.render_plan.read_text(encoding="utf-8")
    assert str(video.material.material_id) in serialized_plan
    assert str(music.material.material_id) in serialized_plan
    assert str(video.source_path) not in serialized_plan
    assert str(music.source_path) not in serialized_plan


def test_executor_reads_only_canonical_material_memory_results(
    managed_configuration: EffectiveConfiguration,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    video = _binding(tmp_path, MaterialType.VIDEO, "Managed Video")
    music = _binding(tmp_path, MaterialType.MUSIC, "Managed Music")
    result_path = video.memory_root / "analysis_result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["material_id"] = str(MaterialId.new())
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    materials = MaterialsService(
        managed_configuration,
        catalog=LeaseCatalog((video, music)),
    )
    run = _run(
        managed_configuration,
        video.material.material_id,
        music.material.material_id,
    )

    with pytest.raises(ValueError, match="active Material"):
        RunPlanningExecutor(managed_configuration, materials).execute(
            ExecuteRunPlanningCommand(
                run=run,
                workspace=tmp_path.resolve(),
            ),
            planner_factory=lambda _config: pytest.fail("planner must not run"),
        )


def test_executor_rejects_artifacts_outside_its_workspace(
    managed_configuration: EffectiveConfiguration,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    video = _binding(tmp_path, MaterialType.VIDEO, "Managed Video")
    music = _binding(tmp_path, MaterialType.MUSIC, "Managed Music")
    materials = MaterialsService(
        managed_configuration,
        catalog=LeaseCatalog((video, music)),
    )
    run = _run(
        managed_configuration,
        video.material.material_id,
        music.material.material_id,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    class EscapingPlanner:
        def __init__(self, _config) -> None:
            pass

        def plan(self, request, **_kwargs):
            result = _planner_result(request, workspace)
            escaped = tmp_path / "escaped.json"
            escaped.write_text("{}\n", encoding="utf-8")
            object.__setattr__(result, "candidate_pool_path", escaped)
            return result

    with pytest.raises(ValueError, match="escaped the planning workspace"):
        RunPlanningExecutor(managed_configuration, materials).execute(
            ExecuteRunPlanningCommand(run=run, workspace=workspace),
            planner_factory=EscapingPlanner,
        )


def test_command_and_snapshot_cardinality_are_strict(
    managed_configuration: EffectiveConfiguration,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ExecuteRunPlanningCommand(
            run=_run(
                managed_configuration,
                MaterialId.new(),
                MaterialId.new(),
            ),
            workspace=Path("relative"),
        )

    video = _binding(tmp_path, MaterialType.VIDEO, "Managed Video")
    music = _binding(tmp_path, MaterialType.MUSIC, "Managed Music")
    catalog = LeaseCatalog((video, music))
    materials = MaterialsService(managed_configuration, catalog=catalog)
    invalid = _run(
        managed_configuration,
        video.material.material_id,
        music.material.material_id,
        video_ids=(),
    )
    with pytest.raises(ValueError, match="exactly one video and one music"):
        RunPlanningExecutor(managed_configuration, materials).execute(
            ExecuteRunPlanningCommand(run=invalid, workspace=tmp_path.resolve()),
            planner_factory=lambda _config: pytest.fail("planner must not run"),
        )
    assert catalog.acquired == []
