from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from cutmaster.prompting.core import PromptPackage, PromptStage, PromptTask
from cutmaster.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)


PromptBuilder = Callable[[Any], PromptPackage]


def _failure_feedback(failure_reasons: tuple[str, ...]) -> str:
    failures = [
        build_prompt_failure(
            PromptFailureCode.RESPONSE_VALIDATION_FAILED,
            attempt=attempt,
            error_message=reason,
        )
        for attempt, reason in enumerate(failure_reasons, 1)
    ]
    return (
        "\n\n# Previous failed attempts\n"
        "The following responses were rejected. Correct every accumulated failure in the next "
        "response; do not repeat an earlier invalid choice.\n"
        "<previous_attempt_failures>\n"
        f"{json.dumps(failures, ensure_ascii=False, indent=2)}\n"
        "</previous_attempt_failures>"
    )


class PromptRegistry:
    def __init__(self) -> None:
        self._builders: dict[tuple[PromptStage, PromptTask], PromptBuilder] = {}

    def register(
        self,
        stage: PromptStage,
        task: PromptTask,
        builder: PromptBuilder,
    ) -> None:
        key = (stage, task)
        if key in self._builders:
            raise ValueError(f"Prompt already registered: {stage.value}.{task.value}")
        self._builders[key] = builder

    def build(
        self,
        stage: PromptStage,
        task: PromptTask,
        details: Any,
        *,
        failure_reasons: tuple[str, ...] = (),
    ) -> PromptPackage:
        try:
            builder = self._builders[(stage, task)]
        except KeyError as exc:
            raise KeyError(f"Unknown prompt: {stage.value}.{task.value}") from exc
        package = builder(details)
        if package.stage != stage or package.task != task:
            raise ValueError("Prompt builder returned a package with a mismatched key")
        normalized_failures = tuple(
            reason.strip() for reason in failure_reasons if reason.strip()
        )
        user_prompt = package.user_prompt
        if normalized_failures:
            user_prompt += _failure_feedback(normalized_failures)
        return replace(
            package,
            user_prompt=user_prompt,
            retry_builder=lambda reasons: self.build(
                stage,
                task,
                details,
                failure_reasons=reasons,
            ),
        )

    def registered_keys(self) -> tuple[tuple[PromptStage, PromptTask], ...]:
        return tuple(sorted(self._builders, key=lambda key: (key[0].value, key[1].value)))


prompt_registry = PromptRegistry()
