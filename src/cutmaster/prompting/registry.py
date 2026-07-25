from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cutmaster.prompting.core import PromptPackage, PromptStage, PromptTask


PromptBuilder = Callable[[Any], PromptPackage]


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
    ) -> PromptPackage:
        try:
            builder = self._builders[(stage, task)]
        except KeyError as exc:
            raise KeyError(f"Unknown prompt: {stage.value}.{task.value}") from exc
        package = builder(details)
        if package.stage != stage or package.task != task:
            raise ValueError("Prompt builder returned a package with a mismatched key")
        return package

    def registered_keys(self) -> tuple[tuple[PromptStage, PromptTask], ...]:
        return tuple(sorted(self._builders, key=lambda key: (key[0].value, key[1].value)))


prompt_registry = PromptRegistry()
