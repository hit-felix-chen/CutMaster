import json

from cutmaster.configuration.schema import LLMConfig
from cutmaster.prompting import (
    PromptModality,
    PromptPackage,
    PromptStage,
    PromptTask,
    ResponseContract,
)
from cutmaster.prompting.registry import PromptRegistry
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


def test_context_feeds_all_previous_failures_into_retry_prompts(
    tmp_path,
    monkeypatch,
) -> None:
    prompts = []
    validations = 0
    registry = PromptRegistry()

    def build_package(_details):
        return PromptPackage(
            stage=PromptStage.PLANNER,
            task=PromptTask.CANDIDATE_RETRIEVAL,
            prompt_version="test",
            operation="retrieve",
            system_prompt="Return JSON",
            user_prompt="Retrieve candidates",
            response_contract=ResponseContract(
                version="test",
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                },
            ),
            context_keys=(),
            modality=PromptModality.TEXT,
        )

    registry.register(
        PromptStage.PLANNER,
        PromptTask.CANDIDATE_RETRIEVAL,
        build_package,
    )
    package = registry.build(
        PromptStage.PLANNER,
        PromptTask.CANDIDATE_RETRIEVAL,
        details=None,
    )

    def generate(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return '{"ok":true}'

    def validate(parsed):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise ValueError("Candidate is too short")
        if validations == 2:
            raise ValueError("Duplicate candidate range")
        return parsed

    monkeypatch.setattr(
        "cutmaster.runtime.workflow_context.generate_text",
        generate,
    )
    monkeypatch.setattr(
        "cutmaster.runtime.model_gateway.time.sleep",
        lambda _delay: None,
    )

    call_tree_path = tmp_path / "planning_calls.json"
    context = WorkflowContext(
        tmp_path / "history.json",
        model_call_tree_path=call_tree_path,
    )
    result = context.call_prompt(
        package=package,
        config=LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_retries=2,
        ),
        validate_business=validate,
    )
    context.save_model_call_tree()

    assert result == {"ok": True}
    assert "Candidate is too short" not in prompts[0]
    assert "Candidate is too short" in prompts[1]
    assert "Duplicate candidate range" not in prompts[1]
    assert "Candidate is too short" in prompts[2]
    assert "Duplicate candidate range" in prompts[2]
    tree = json.loads(call_tree_path.read_text(encoding="utf-8"))
    root = tree["root"]
    assert root["stage"] == "planning"
    assert root["call_count"] == 1
    assert root["attempt_count"] == 3
    call = root["children"][0]["calls"][0]
    assert call["status"] == "success"
    assert [attempt["status"] for attempt in call["attempts"]] == [
        "response_rejected",
        "response_rejected",
        "accepted",
    ]
    assert [attempt["response"] for attempt in call["attempts"]] == [
        '{"ok":true}',
        '{"ok":true}',
        '{"ok":true}',
    ]
    persisted = call_tree_path.read_text(encoding="utf-8")
    assert "Retrieve candidates" not in persisted
    assert "Return JSON" not in persisted
    assert call["attempts"][0]["prompt"]["prompt_id"] == (
        "planner.candidate_retrieval"
    )
