from cutmaster.models import LLMConfig
from cutmaster.planner_context import PlanningContext


def test_context_persists_artifacts_calls_and_script_versions(tmp_path, monkeypatch) -> None:
    path = tmp_path / "planning_history.json"
    context = PlanningContext(path)
    context.set_artifact("music_profile", {"tempo_bpm": 120})
    monkeypatch.setattr(
        "cutmaster.planner_context.generate_text",
        lambda prompt, *_args, **_kwargs: '{"items":[{"slot_id":"slot_01"}]}',
    )
    result = context.call_json(
        operation="plan",
        prompt="Create slots",
        config=LLMConfig(model="test", base_url="", api_key="test"),
        context_keys=["music_profile"],
        system_prompt="Return JSON",
        output_artifact="edit_plan",
    )
    context.record_script_version([{"_id": 1}], source="beam_search")

    loaded = PlanningContext(path)
    assert result["items"][0]["slot_id"] == "slot_01"
    assert loaded.get_artifact("music_profile")["tempo_bpm"] == 120
    assert loaded.get_artifact("edit_plan") == result
    assert loaded.data["calls"][0]["status"] == "success"
    assert loaded.data["calls"][0]["context_snapshot"]["music_profile"]["tempo_bpm"] == 120
    assert loaded.data["script_versions"][0]["source"] == "beam_search"
