from cutmaster.models import LLMConfig
from cutmaster.prompting import (
    PromptModality,
    PromptPackage,
    PromptStage,
    PromptTask,
    ResponseContract,
)
from cutmaster.workflow_context import WorkflowContext


def test_context_persists_artifacts_calls_and_script_versions(tmp_path, monkeypatch) -> None:
    path = tmp_path / "planning_history.json"
    context = WorkflowContext(path)
    context.set_artifact("music_profile", {"tempo_bpm": 120})
    monkeypatch.setattr(
        "cutmaster.workflow_context.generate_text",
        lambda prompt, *_args, **_kwargs: '{"items":[{"slot_id":"slot_01"}]}',
    )
    package = PromptPackage(
        stage=PromptStage.PLANNER,
        task=PromptTask.SLOT_PLANNING,
        prompt_version="test",
        operation="plan",
        system_prompt="Return JSON",
        user_prompt="Create slots",
        response_contract=ResponseContract(
            version="test",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["items"],
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["slot_id"],
                            "properties": {"slot_id": {"type": "string"}},
                        },
                    }
                },
            },
        ),
        context_keys=("music_profile",),
        modality=PromptModality.TEXT,
        output_artifact="edit_plan",
    )
    result = context.call_prompt(
        package=package,
        config=LLMConfig(model="test", base_url="", api_key="test"),
    )
    context.record_script_version([{"_id": 1}], source="beam_search")

    loaded = WorkflowContext(path)
    assert result["items"][0]["slot_id"] == "slot_01"
    assert loaded.get_artifact("music_profile")["tempo_bpm"] == 120
    assert loaded.get_artifact("edit_plan") == result
    assert loaded.data["calls"][0]["status"] == "success"
    assert loaded.data["calls"][0]["model"] == "test"
    assert loaded.data["calls"][0]["enable_thinking"] is True
    assert loaded.data["calls"][0]["input_modality"] == "text"
    assert loaded.data["calls"][0]["prompt_id"] == "planner.slot_planning"
    assert loaded.data["calls"][0]["contract_fingerprint"]
    assert loaded.get_successful_prompt_result(package) == result
    loaded.set_artifact("music_profile", {"tempo_bpm": 90})
    assert loaded.get_successful_prompt_result(package) is None
    assert loaded.data["calls"][0]["context_snapshot"]["music_profile"]["tempo_bpm"] == 120
    assert loaded.data["script_versions"][0]["source"] == "beam_search"
