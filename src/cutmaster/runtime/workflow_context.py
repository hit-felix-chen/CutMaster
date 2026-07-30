from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

from cutmaster.runtime.model_gateway import generate_text, request_json_with_retries
from cutmaster.configuration.schema import ModelConfig
from cutmaster.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.runtime.observability import error_summary, log_event
from cutmaster.prompting import PromptPackage


T = TypeVar("T")


class WorkflowContext:
    """Persistent workflow state with optional separate model-call tracing."""

    def __init__(
        self,
        path: Path,
        *,
        model_call_tree_path: Path | None = None,
    ) -> None:
        self._lock = RLock()
        self.path = path
        self._model_call_tree_path = model_call_tree_path
        self._model_call_tree_started_at = datetime.now().astimezone().isoformat()
        self._model_calls: list[dict[str, Any]] = []
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

    def _start_model_call(
        self,
        package: PromptPackage,
        config: ModelConfig,
        *,
        image_count: int,
    ) -> dict[str, Any] | None:
        if self._model_call_tree_path is None:
            return None
        with self._lock:
            call = {
                "call_id": len(self._model_calls) + 1,
                "task": package.task.value,
                "operation": package.operation,
                "started_at": datetime.now().astimezone().isoformat(),
                "completed_at": None,
                "status": "running",
                "model": {
                    "name": config.model,
                    "modality": package.modality.value,
                    "thinking": config.enable_thinking,
                    "temperature": config.temperature,
                    "max_tokens": config.max_tokens,
                    "image_count": image_count,
                },
                "attempts": [],
            }
            self._model_calls.append(call)
            return call

    def _record_model_attempt_start(
        self,
        call: dict[str, Any] | None,
        package: PromptPackage,
        *,
        contextual_prompt_chars: int,
        context_chars: int,
    ) -> dict[str, Any] | None:
        if call is None:
            return None
        with self._lock:
            attempt = {
                "attempt": len(call["attempts"]) + 1,
                "started_at": datetime.now().astimezone().isoformat(),
                "completed_at": None,
                "status": "running",
                "prompt": {
                    "prompt_id": package.prompt_id,
                    "prompt_version": package.prompt_version,
                    "prompt_fingerprint": package.fingerprint,
                    "contract_version": package.response_contract.version,
                    "contract_fingerprint": package.response_contract.fingerprint,
                    "context_keys": list(package.context_keys),
                    "context_chars": context_chars,
                    "system_prompt_chars": len(package.system_prompt),
                    "user_prompt_chars": len(package.user_prompt),
                    "contextual_prompt_chars": contextual_prompt_chars,
                },
                "response": None,
                "response_chars": 0,
                "error": None,
            }
            call["attempts"].append(attempt)
            return attempt

    def _finish_model_attempt(
        self,
        attempt: dict[str, Any] | None,
        *,
        status: str,
        response: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        if attempt is None:
            return
        with self._lock:
            attempt["completed_at"] = datetime.now().astimezone().isoformat()
            attempt["status"] = status
            if response is not None:
                attempt["response"] = response
                attempt["response_chars"] = len(response)
            if error is not None:
                attempt["error"] = {
                    "type": type(error).__name__,
                    **build_prompt_failure(
                        PromptFailureCode.MODEL_CALL_FAILED,
                        task="model_attempt",
                        error_type=type(error).__name__,
                        error_message=error_summary(error),
                    ),
                }

    def _finish_model_call(
        self,
        call: dict[str, Any] | None,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> None:
        if call is None:
            return
        with self._lock:
            call["completed_at"] = datetime.now().astimezone().isoformat()
            call["status"] = status
            if error is not None:
                call["error"] = {
                    "type": type(error).__name__,
                    **build_prompt_failure(
                        PromptFailureCode.MODEL_CALL_FAILED,
                        task=str(call["task"]),
                        error_type=type(error).__name__,
                        error_message=error_summary(error),
                    ),
                }

    def save_model_call_tree(self, *, status: str = "complete") -> Path | None:
        if self._model_call_tree_path is None:
            return None
        with self._lock:
            tasks: list[dict[str, Any]] = []
            tasks_by_name: dict[str, dict[str, Any]] = {}
            for call in self._model_calls:
                task_name = str(call["task"])
                task_node = tasks_by_name.get(task_name)
                if task_node is None:
                    task_node = {
                        "node_type": "task",
                        "task": task_name,
                        "calls": [],
                    }
                    tasks_by_name[task_name] = task_node
                    tasks.append(task_node)
                task_node["calls"].append(call)
            tree = {
                "schema_version": "1.0",
                "root": {
                    "node_type": "stage",
                    "stage": "planning",
                    "started_at": self._model_call_tree_started_at,
                    "completed_at": datetime.now().astimezone().isoformat(),
                    "status": status,
                    "call_count": len(self._model_calls),
                    "attempt_count": sum(
                        len(call["attempts"]) for call in self._model_calls
                    ),
                    "children": tasks,
                },
            }
            self._model_call_tree_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._model_call_tree_path.with_suffix(
                self._model_call_tree_path.suffix + ".tmp"
            )
            temporary_path.write_text(
                json.dumps(tree, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self._model_call_tree_path)
            return self._model_call_tree_path

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
        call = self._start_model_call(
            package,
            config,
            image_count=len(image_data_urls or []),
        )
        current_attempt: dict[str, Any] | None = None
        latest_response: str | None = None

        def contextual_prompt() -> tuple[str, int]:
            if not snapshot:
                return active_package.user_prompt, 0
            serialized_context = json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return (
                "# Maintained workflow context\n"
                + serialized_context
                + "\n\n"
                + active_package.user_prompt,
                len(serialized_context),
            )

        def request() -> str:
            nonlocal current_attempt, latest_response
            prompt, context_chars = contextual_prompt()
            current_attempt = self._record_model_attempt_start(
                call,
                active_package,
                contextual_prompt_chars=len(prompt),
                context_chars=context_chars,
            )
            latest_response = None
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
                failure = build_prompt_failure(
                    PromptFailureCode.MODEL_REQUEST_FAILED,
                    operation=active_package.operation,
                    error_type=type(exc).__name__,
                    error_message=error_summary(exc),
                )
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
                    **failure,
                )
                self._finish_model_attempt(
                    current_attempt,
                    status="request_failed",
                    error=exc,
                )
                raise
            latest_response = raw
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
            self._finish_model_attempt(
                current_attempt,
                status=(
                    "response_rejected"
                    if latest_response is not None
                    else "request_failed"
                ),
                response=latest_response,
                error=error,
            )
            failure_reasons.append(error_summary(error))
            if package.retry_builder is not None:
                active_package = package.retry_builder(tuple(failure_reasons))

        try:
            result = request_json_with_retries(
                request,
                config,
                operation=active_package.operation,
                validate=validate,
                on_retry=rebuild_for_retry,
            )
        except Exception as exc:
            self._finish_model_attempt(
                current_attempt,
                status=(
                    "response_rejected"
                    if latest_response is not None
                    else "request_failed"
                ),
                response=latest_response,
                error=exc,
            )
            self._finish_model_call(call, status="failed", error=exc)
            self.save_model_call_tree(status="failed")
            raise
        self._finish_model_attempt(
            current_attempt,
            status="accepted",
            response=latest_response,
        )
        self._finish_model_call(call, status="success")
        if package.output_artifact:
            self.set_artifact(package.output_artifact, result)
        return result
