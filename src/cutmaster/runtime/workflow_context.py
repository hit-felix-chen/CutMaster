from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

from cutmaster.runtime.model_gateway import generate_text, request_json_with_retries
from cutmaster.configuration.schema import ModelConfig
from cutmaster.runtime.observability import error_summary, log_event
from cutmaster.prompting import PromptPackage


T = TypeVar("T")


class WorkflowContext:
    """Persistent workflow state without model-call history."""

    def __init__(self, path: Path) -> None:
        self._lock = RLock()
        self.path = path
        self.data: dict[str, Any] = {
            "schema_version": "2.0",
            "created_at": datetime.now().astimezone().isoformat(),
            "artifacts": {},
            "script_versions": [],
        }

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary_path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.path)

    def set_artifact(self, key: str, value: Any) -> None:
        with self._lock:
            versions = self.data.setdefault("artifacts", {}).setdefault(key, [])
            versions.append(
                {
                    "version": len(versions) + 1,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "value": value,
                }
            )
            self.save()

    def get_artifact(self, key: str, default: Any = None) -> Any:
        with self._lock:
            versions = self.data.get("artifacts", {}).get(key, [])
            return versions[-1]["value"] if versions else default

    def context_snapshot(self, keys: list[str]) -> dict[str, Any]:
        with self._lock:
            return {
                key: self.get_artifact(key)
                for key in keys
                if self.get_artifact(key) is not None
            }

    def record_script_version(
        self,
        script: list[dict[str, Any]],
        *,
        source: str,
        patches: list[dict[str, Any]] | None = None,
        rejected_patches: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            versions = self.data.setdefault("script_versions", [])
            versions.append(
                {
                    "version": len(versions) + 1,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "source": source,
                    "patches": patches or [],
                    "rejected_patches": rejected_patches or [],
                    "script": script,
                }
            )
            self.set_artifact("current_script", script)

    def call_prompt(
        self,
        *,
        package: PromptPackage,
        config: ModelConfig,
        validate_business: Callable[[dict[str, Any]], T] | None = None,
        image_data_urls: list[str] | None = None,
        image_labels: list[str] | None = None,
    ) -> T | dict[str, Any]:
        has_images = bool(image_data_urls)
        expects_images = package.modality.value == "text_and_images"
        if has_images != expects_images:
            raise ValueError(
                f"{package.prompt_id} expects modality={package.modality.value}"
            )
        snapshot = self.context_snapshot(list(package.context_keys))
        active_package = package
        failure_reasons: list[str] = []

        def contextual_prompt() -> str:
            if not snapshot:
                return active_package.user_prompt
            return (
                "# Maintained workflow context\n"
                + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                + "\n\n"
                + active_package.user_prompt
            )

        def request() -> str:
            prompt = contextual_prompt()
            request_started = time.monotonic()
            modality = "text_and_images" if image_data_urls else "text"
            log_event(
                "INFO",
                "model",
                "model.start",
                "Model request started",
                operation=active_package.operation,
                prompt_id=active_package.prompt_id,
                prompt_version=active_package.prompt_version,
                contract_version=active_package.response_contract.version,
                model=config.model,
                modality=modality,
                thinking=config.enable_thinking,
                images=len(image_data_urls or []),
                prompt_chars=len(prompt),
                retry_failures=len(failure_reasons),
            )
            try:
                raw = generate_text(
                    prompt,
                    config,
                    system_prompt=active_package.system_prompt,
                    image_data_urls=image_data_urls,
                )
            except Exception as exc:
                log_event(
                    "WARNING",
                    "model",
                    "model.fail",
                    "Model request failed",
                    operation=active_package.operation,
                    prompt_id=active_package.prompt_id,
                    model=config.model,
                    modality=modality,
                    elapsed_sec=time.monotonic() - request_started,
                    error_type=type(exc).__name__,
                    reason=error_summary(exc),
                )
                raise
            log_event(
                "INFO",
                "model",
                "model.complete",
                "Model request completed",
                operation=active_package.operation,
                prompt_id=active_package.prompt_id,
                model=config.model,
                modality=modality,
                elapsed_sec=time.monotonic() - request_started,
                response_chars=len(raw),
            )
            return raw

        def validate(parsed: dict[str, Any]) -> T | dict[str, Any]:
            structured = active_package.response_contract.validate_structure(parsed)
            return (
                validate_business(structured)
                if validate_business is not None
                else structured
            )

        def rebuild_for_retry(error: BaseException, _attempt: int) -> None:
            nonlocal active_package
            failure_reasons.append(error_summary(error))
            if package.retry_builder is not None:
                active_package = package.retry_builder(tuple(failure_reasons))

        result = request_json_with_retries(
            request,
            config,
            operation=active_package.operation,
            validate=validate,
            on_retry=rebuild_for_retry,
        )
        if package.output_artifact:
            self.set_artifact(package.output_artifact, result)
        return result
