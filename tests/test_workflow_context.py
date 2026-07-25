import json

from cutmaster.configuration.schema import LLMConfig
from cutmaster.prompting import (
    PromptModality,
    PromptPackage,
    PromptStage,
    PromptTask,
    ResponseContract,
)
from cutmaster.runtime.workflow_context import WorkflowContext


def test_context_persists_artifacts_without_model_call_history(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "planning_history.json"
    context = WorkflowContext(path)
    context.set_artifact("music_profile", {"tempo_bpm": 120})
    monkeypatch.setattr(
        "cutmaster.runtime.workflow_context.generate_text",
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

    assert result["items"][0]["slot_id"] == "slot_01"
    persisted_data = json.loads(path.read_text(encoding="utf-8"))
    assert persisted_data["artifacts"]["music_profile"][-1]["value"]["tempo_bpm"] == 120
    assert persisted_data["artifacts"]["edit_plan"][-1]["value"] == result
    assert "calls" not in persisted_data
    persisted = path.read_text(encoding="utf-8")
    assert "Create slots" not in persisted
    assert "context_snapshot" not in persisted
    assert "raw_responses" not in persisted
    assert persisted_data["script_versions"][0]["source"] == "beam_search"


def test_context_never_loads_existing_state(tmp_path) -> None:
    path = tmp_path / "analysis_history.json"
    path.write_text(
        '{"schema_version":"2.0","artifacts":{"legacy":[{"value":1}]}}',
        encoding="utf-8",
    )

    context = WorkflowContext(path)

    assert context.data["schema_version"] == "2.0"
    assert context.get_artifact("legacy") is None
