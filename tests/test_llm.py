from types import SimpleNamespace

import pytest

from cutmaster.llm import generate_text, request_json_with_retries
from cutmaster.models import LLMConfig, VLMConfig


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            LLMConfig(
                model="text",
                base_url="",
                api_key="test",
                enable_thinking=False,
            ),
            False,
        ),
        (
            VLMConfig(
                model="vision",
                base_url="",
                api_key="test",
                enable_thinking=True,
            ),
            True,
        ),
    ],
)
def test_model_thinking_config_reaches_api_request(
    monkeypatch,
    config,
    expected,
) -> None:
    captured = {}

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )

        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"ok":true}')
                    )
                ]
            )

    monkeypatch.setattr("cutmaster.llm.OpenAI", FakeOpenAI)

    assert generate_text("test", config, "Return JSON") == '{"ok":true}'
    assert captured["model"] == config.model
    assert captured["extra_body"] == {"enable_thinking": expected}


def test_json_request_retries_validation_failure(monkeypatch) -> None:
    responses = iter(['{"items": []}', '{"items": [1]}'])
    monkeypatch.setattr("cutmaster.llm.time.sleep", lambda _delay: None)

    def validate(parsed):
        if not parsed["items"]:
            raise ValueError("items must not be empty")
        return parsed["items"]

    result = request_json_with_retries(
        lambda: next(responses),
        LLMConfig(model="test", base_url="", api_key="test", max_retries=1),
        operation="test operation",
        validate=validate,
    )

    assert result == [1]


def test_json_request_reports_final_failure(monkeypatch) -> None:
    monkeypatch.setattr("cutmaster.llm.time.sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        request_json_with_retries(
            lambda: "not json",
            LLMConfig(model="test", base_url="", api_key="test", max_retries=1),
            operation="test operation",
        )


def test_json_request_does_not_repeat_provider_image_inspection_rejection(
    monkeypatch,
) -> None:
    monkeypatch.setattr("cutmaster.llm.time.sleep", lambda _delay: None)
    attempts = 0

    def rejected_request():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("data_inspection_failed")

    with pytest.raises(RuntimeError, match="data_inspection_failed"):
        request_json_with_retries(
            rejected_request,
            LLMConfig(model="test", base_url="", api_key="test", max_retries=3),
            operation="visual operation",
        )

    assert attempts == 1
