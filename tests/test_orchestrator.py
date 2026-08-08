import json
from types import SimpleNamespace

from cutmaster.contracts.analyser import AnalysisResult
from cutmaster.contracts.planning import PlanningResult
from cutmaster.contracts.renderer import RenderResult
from cutmaster.contracts.workflow import WorkflowRequest
from cutmaster.orchestrator import Orchestrator


def test_orchestrator_only_composes_three_stage_services(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.mp4"
    bgm = tmp_path / "bgm.wav"
    source.write_bytes(b"video")
    bgm.write_bytes(b"audio")
    root = tmp_path / "artifacts" / "cutmaster"
    calls = []

    class FakeAnalyser:
        def __init__(self, _config) -> None:
            pass

        def analyse(self, request):
            calls.append(("analyser", request.output_dir))
            return AnalysisResult(
                status="success",
                source_video=str(source),
                material_directory=str(tmp_path / "material"),
                source_srt=str(root / "analyser" / "source.srt"),
                processed_subtitle=str(root / "analyser" / "dialogue_merged.srt"),
                dialogues_json=str(root / "analyser" / "dialogues.json"),
                video_description=str(tmp_path / "material" / "video_description.json"),
                video_summary=str(tmp_path / "material" / "video_summary.json"),
                analysis_history=str(tmp_path / "material" / "analysis_history.json"),
                elapsed_sec=1.0,
            )

    class FakePlanner:
        def __init__(self, _config) -> None:
            pass

        def plan(self, request, _analysis):
            calls.append(("planners", request.output_dir))
            return PlanningResult(
                status="success",
                render_plan=str(root / "planners" / "render_plan.json"),
                music_profile="music_profile.json",
                edit_plan="edit_plan.json",
                dialogue_anchors="dialogue_anchors.json",
                candidate_pool="candidate_pool.json",
                raw_script="script_raw.json",
                selection_diagnostics="selection_diagnostics.json",
                planning_history="planning_history.json",
                planning_calls="planning_calls.json",
                target_output_length_sec=2.0,
                planned_output_length_sec=2.0,
                num_raw_clips=2,
                num_planned_clips=2,
                stage_timings_sec={"slot_planning": 0.5},
                wall_clock_sec=2.0,
            )

    class FakeRenderer:
        def __init__(self, _config) -> None:
            pass

        def render(self, request):
            calls.append(("renderer", request.output_dir))
            return RenderResult(
                status="success",
                plan_id="plan",
                render_id="render",
                audio_mode=request.audio_mode,
                output_video=str(root / "renderer" / "output.mp4"),
                montage_video=str(root / "renderer" / "montage.mp4"),
                duration_sec=2.0,
                frames=60,
                montage_reused=False,
                dialogue_audio_reused=False,
                stage_timings_sec={"rendering": 0.25},
                wall_clock_sec=0.25,
            )

    monkeypatch.setattr("cutmaster.orchestrator.Analyser", FakeAnalyser)
    monkeypatch.setattr("cutmaster.orchestrator.Planner", FakePlanner)
    monkeypatch.setattr("cutmaster.orchestrator.Renderer", FakeRenderer)

    result = Orchestrator(SimpleNamespace(renderer=object())).run(
        WorkflowRequest(
            video_path=source,
            audio_path=bgm,
            prompt="test",
            output_dir=root,
            target_output_length_sec=2.0,
            audio_mode="bgm_only",
        )
    )

    assert calls == [
        ("analyser", root / "analyser"),
        ("planners", root / "planners"),
        ("renderer", root / "renderer"),
    ]
    assert result.output_video == str(root / "renderer" / "output.mp4")
    assert json.loads((root / "result.json").read_text())["render_plan"] == str(
        root / "planners" / "render_plan.json"
    )
