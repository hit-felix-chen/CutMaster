import json

import pytest

from cutmaster.configuration.schema import LLMConfig
from cutmaster.workflow.prompting import (
    PromptModality,
    PromptPackage,
    PromptStage,
    PromptTask,
    ResponseContract,
)
from cutmaster.infrastructure.models.openai_compatible import (
    ModelResponse,
    ModelUsage,
)
from cutmaster.workflow.prompting.registry import PromptRegistry
from cutmaster.workflow.shared.execution_context import WorkflowContext


def model_response(content: str = '{"ok":true}') -> ModelResponse:
    return ModelResponse(
        content=content,
        response_id="response-test",
        response_model="test-resolved",
        usage=ModelUsage(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cached_prompt_tokens=40,
            uncached_prompt_tokens=60,
            reasoning_tokens=10,
            provider_usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        ),
    )


def test_context_persists_artifacts_without_model_call_history(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "planners_history.json"
    context = WorkflowContext(path)
    context.set_artifact("music_profile", {"tempo_bpm": 120})
    monkeypatch.setattr(
        "cutmaster.workflow.shared.execution_context.generate_text",
        lambda prompt, *_args, **_kwargs: model_response(
            '{"items":[{"slot_id":"slot_01"}]}'
        ),
    )
    package = PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.SLOT_ARRANGEMENT,
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
            stage=PromptStage.PLANNERS,
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
        PromptStage.PLANNERS,
        PromptTask.CANDIDATE_RETRIEVAL,
        build_package,
    )
    package = registry.build(
        PromptStage.PLANNERS,
        PromptTask.CANDIDATE_RETRIEVAL,
        details=None,
    )

    def generate(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return model_response()

    def validate(parsed):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise ValueError("Candidate is too short")
        if validations == 2:
            raise ValueError("Duplicate candidate range")
        return parsed

    monkeypatch.setattr(
        "cutmaster.workflow.shared.execution_context.generate_text",
        generate,
    )
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )

    call_tree_path = tmp_path / "planners_calls.json"
    context = WorkflowContext(
        tmp_path / "history.json",
        model_call_tree_path=call_tree_path,
        model_usage_path=tmp_path / "model_usage.json",
    )
    result = context.call_prompt(
        package=package,
        config=LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_retries=2,
            input_price_yuan_per_million_tokens=2.0,
            cached_input_price_yuan_per_million_tokens=0.5,
            output_price_yuan_per_million_tokens=10.0,
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
    assert root["stage"] == "planners"
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
        "planners.candidate_retrieval"
    )
    assert root["usage"]["request_count"] == 3
    assert root["usage"]["prompt_tokens"] == 300
    assert call["attempts"][0]["usage"]["cached_prompt_tokens"] == 40
    usage = json.loads((tmp_path / "model_usage.json").read_text(encoding="utf-8"))
    assert usage["summary"]["total_tokens"] == 360
    assert usage["current_run"]["summary"]["total_tokens"] == 360
    assert usage["cumulative"]["summary"]["total_tokens"] == 360
    assert usage["current_run"]["summary"]["total_cost_yuan"] == 0.00102
    task_usage = usage["current_run"]["summary"]["by_task"]
    assert task_usage["candidate_retrieval"]["total_tokens"] == 360
    assert task_usage["candidate_retrieval"]["total_cost_yuan"] == 0.00102
    pricing = usage["calls"][0]["model"]["pricing"]
    assert pricing == {
        "currency": "CNY",
        "unit": "yuan_per_million_tokens",
        "input": 2.0,
        "cached_input": 0.5,
        "output": 10.0,
    }
    assert usage["calls"][0]["attempts"][0]["response_id"] == "response-test"
    serialized_usage = json.dumps(usage, ensure_ascii=False)
    assert '{"ok":true}' not in serialized_usage
    assert "Retrieve candidates" not in serialized_usage


def test_usage_separates_current_run_from_cumulative(tmp_path) -> None:
    usage_path = tmp_path / "model_usage.json"
    usage_path.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "calls": [
                    {
                        "call_id": 1,
                        "task": "analyser.video_summary",
                        "operation": "summarize",
                        "status": "success",
                        "model": {
                            "name": "old-model",
                            "pricing": {
                                "currency": "CNY",
                                "unit": "yuan_per_million_tokens",
                                "input": 1.0,
                                "cached_input": 0.1,
                                "output": 2.0,
                            },
                        },
                        "attempts": [
                            {
                                "attempt": 1,
                                "status": "accepted",
                                "usage": {
                                    "prompt_tokens": 10,
                                    "completion_tokens": 2,
                                    "total_tokens": 12,
                                    "cached_prompt_tokens": 0,
                                    "uncached_prompt_tokens": 10,
                                    "reasoning_tokens": 0,
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    context = WorkflowContext(
        tmp_path / "history.json",
        model_usage_path=usage_path,
        stage_name="analyser",
    )

    context.save_model_usage()

    persisted = json.loads(usage_path.read_text(encoding="utf-8"))
    assert persisted["current_run"]["summary"]["total_tokens"] == 0
    assert persisted["cumulative"]["summary"]["total_tokens"] == 12
    assert persisted["cumulative"]["summary"]["total_cost_yuan"] == 0.000014


def test_request_failure_logging_does_not_duplicate_operation(
    tmp_path,
    monkeypatch,
) -> None:
    package = PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.SLOT_ARRANGEMENT,
        prompt_version="test",
        operation="plan",
        system_prompt="Return JSON",
        user_prompt="Create slots",
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
    events: list[dict[str, object]] = []

    def fail_request(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    def capture_event(level, component, event, message, **fields):
        events.append(
            {
                "level": level,
                "component": component,
                "event": event,
                "message": message,
                **fields,
            }
        )

    monkeypatch.setattr(
        "cutmaster.workflow.shared.execution_context.generate_text",
        fail_request,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.shared.execution_context.log_event",
        capture_event,
    )

    context = WorkflowContext(tmp_path / "history.json")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        context.call_prompt(
            package=package,
            config=LLMConfig(
                model="test",
                base_url="",
                api_key="test",
                max_retries=0,
            ),
        )

    request_failure = next(
        item
        for item in events
        if item["component"] == "model"
        and item["event"] == "model.fail"
        and item["message"] == "Model request failed"
    )
    assert request_failure["operation"] == "plan"
    assert request_failure["reason_code"] == "model_request_failed"
