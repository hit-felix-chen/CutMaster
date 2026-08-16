from types import SimpleNamespace

import pytest

from cutmaster.infrastructure.models.openai_compatible import (
    generate_text,
    request_json_with_retries,
)
from cutmaster.configuration.schema import LLMConfig, VLMConfig


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
        (
            LLMConfig(
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                api_key="test",
                enable_thinking=True,
            ),
            {"thinking": {"type": "enabled"}},
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
                id="response-1",
                model=config.model,
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_cache_hit_tokens": 60,
                    "completion_tokens_details": {"reasoning_tokens": 12},
                },
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"ok":true}')
                    )
                ]
            )

    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.OpenAI", FakeOpenAI
    )

    response = generate_text("test", config, "Return JSON")
    assert response.content == '{"ok":true}'
    assert response.response_id == "response-1"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 100
    assert response.usage.cached_prompt_tokens == 60
    assert response.usage.uncached_prompt_tokens == 40
    assert response.usage.reasoning_tokens == 12
    assert captured["model"] == config.model
    expected_body = (
        expected
        if isinstance(expected, dict)
        else {"enable_thinking": expected}
    )
    assert captured["extra_body"] == expected_body


def test_multimodal_labels_are_interleaved_with_images(monkeypatch) -> None:
    captured = {}

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )

        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.OpenAI", FakeOpenAI
    )
    generate_text(
        "prompt",
        VLMConfig(model="vision", base_url="", api_key="test"),
        "system",
        image_data_urls=["data:image/jpeg;base64,a", "data:image/jpeg;base64,b"],
        image_labels=["shot_1 frame_1", "shot_1 frame_2"],
    )

    content = captured["messages"][1]["content"]
    assert [item["type"] for item in content] == [
        "text",
        "text",
        "image_url",
        "text",
        "image_url",
    ]
    assert content[1]["text"] == "shot_1 frame_1"
    assert content[3]["text"] == "shot_1 frame_2"


def test_json_request_retries_validation_failure(monkeypatch) -> None:
    responses = iter(['{"items": []}', '{"items": [1]}'])
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )

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
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        request_json_with_retries(
            lambda: "not json",
            LLMConfig(model="test", base_url="", api_key="test", max_retries=1),
            operation="test operation",
        )


def test_json_request_does_not_repeat_provider_image_inspection_rejection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )
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


def test_json_request_does_not_repeat_provider_data_uri_limit_rejection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.infrastructure.models.openai_compatible.time.sleep",
        lambda _delay: None,
    )
    attempts = 0

    def rejected_request():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("Exceeded limit on max data-uri per request: 250")

    with pytest.raises(RuntimeError, match="Exceeded limit"):
        request_json_with_retries(
            rejected_request,
            LLMConfig(model="test", base_url="", api_key="test", max_retries=3),
            operation="visual operation",
        )

    assert attempts == 1
