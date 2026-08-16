from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

from cutmaster.infrastructure.models.openai_compatible import (
    COST_USAGE_FIELDS,
    TOKEN_USAGE_FIELDS,
    USAGE_COUNT_FIELDS,
    ModelUsage,
    empty_usage_summary,
    generate_text,
    merge_usage_summaries,
    request_json_with_retries,
)
from cutmaster.configuration.schema import ModelConfig
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.infrastructure.observability.logging import error_summary, log_event
from cutmaster.workflow.prompting import PromptPackage


T = TypeVar("T")


class WorkflowContext:
    """Persistent workflow state with optional separate model-call tracing."""

    def __init__(
        self,
        path: Path,
        *,
        model_call_tree_path: Path | None = None,
        model_usage_path: Path | None = None,
        stage_name: str = "planners",
        prior_model_usage_summary: Mapping[str, Any] | None = None,
        prior_model_call_count: int = 0,
    ) -> None:
        if (
            not isinstance(prior_model_call_count, int)
            or isinstance(prior_model_call_count, bool)
            or prior_model_call_count < 0
        ):
            raise ValueError("prior_model_call_count must be non-negative")
        if prior_model_usage_summary is None and prior_model_call_count:
            raise ValueError(
                "prior_model_usage_summary is required with prior model calls"
            )
        self._lock = RLock()
        self.path = path
        self._model_call_tree_path = model_call_tree_path
        self._model_usage_path = model_usage_path
        self._model_stage_name = stage_name
        self._model_call_tree_started_at = datetime.now().astimezone().isoformat()
        self._prior_usage_summary = (
            None
            if prior_model_usage_summary is None
            else merge_usage_summaries([dict(prior_model_usage_summary)])
        )
        self._prior_usage_calls = (
            self._load_prior_usage_calls()
            if prior_model_usage_summary is None
            else []
        )
        self._model_call_id_offset = (
            len(self._prior_usage_calls)
            if prior_model_usage_summary is None
            else prior_model_call_count
        )
        self._model_calls: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {
            "schema_version": "2.0",
            "created_at": datetime.now().astimezone().isoformat(),
            "artifacts": {},
            "script_versions": [],
        }

    def _load_prior_usage_calls(self) -> list[dict[str, Any]]:
        if self._model_usage_path is None or not self._model_usage_path.is_file():
            return []
        try:
            payload = json.loads(self._model_usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        calls = payload.get("calls") if isinstance(payload, dict) else None
        return list(calls) if isinstance(calls, list) else []

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
        if self._model_call_tree_path is None and self._model_usage_path is None:
            return None
        with self._lock:
            call = {
                "call_id": self._model_call_id_offset + len(self._model_calls) + 1,
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
                    "pricing": {
                        "currency": "CNY",
                        "unit": "yuan_per_million_tokens",
                        "input": (
                            config.input_price_yuan_per_million_tokens
                        ),
                        "cached_input": (
                            config.cached_input_price_yuan_per_million_tokens
                        ),
                        "output": (
                            config.output_price_yuan_per_million_tokens
                        ),
                    },
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
                "response_id": None,
                "response_model": None,
                "usage": None,
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
        usage: ModelUsage | None = None,
        response_id: str | None = None,
        response_model: str | None = None,
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
            if usage is not None:
                attempt["usage"] = usage.to_dict()
            if response_id is not None:
                attempt["response_id"] = response_id
            if response_model is not None:
                attempt["response_model"] = response_model
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
                "schema_version": "1.1",
                "root": {
                    "node_type": "stage",
                    "stage": self._model_stage_name,
                    "started_at": self._model_call_tree_started_at,
                    "completed_at": datetime.now().astimezone().isoformat(),
                    "status": status,
                    "call_count": len(self._model_calls),
                    "attempt_count": sum(
                        len(call["attempts"]) for call in self._model_calls
                    ),
                    "usage": self.model_usage_summary(),
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

    @staticmethod
    def _usage_call(call: dict[str, Any]) -> dict[str, Any]:
        return {
            "call_id": call["call_id"],
            "task": call["task"],
            "operation": call["operation"],
            "started_at": call["started_at"],
            "completed_at": call.get("completed_at"),
            "status": call["status"],
            "model": call["model"],
            "attempts": [
                {
                    "attempt": attempt["attempt"],
                    "prompt_id": attempt.get("prompt", {}).get("prompt_id"),
                    "started_at": attempt["started_at"],
                    "completed_at": attempt.get("completed_at"),
                    "status": attempt["status"],
                    "response_id": attempt.get("response_id"),
                    "response_model": attempt.get("response_model"),
                    "usage": attempt.get("usage"),
                }
                for attempt in call["attempts"]
            ],
        }

    @staticmethod
    def _summarize_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
        summary = empty_usage_summary()

        def empty_bucket() -> dict[str, Any]:
            return {
                **{field_name: 0 for field_name in USAGE_COUNT_FIELDS},
                **{field_name: 0 for field_name in TOKEN_USAGE_FIELDS},
                **{field_name: 0.0 for field_name in COST_USAGE_FIELDS},
            }

        def add_attempt(
            bucket: dict[str, Any],
            usage: Any,
            pricing: Any,
        ) -> None:
            bucket["request_count"] += 1
            if not isinstance(usage, dict):
                bucket["unreported_usage_count"] += 1
                return
            bucket["reported_usage_count"] += 1
            for field_name in TOKEN_USAGE_FIELDS:
                bucket[field_name] += int(usage.get(field_name) or 0)
            if not isinstance(pricing, dict):
                bucket["unpriced_usage_count"] += 1
                return
            bucket["priced_usage_count"] += 1
            million = Decimal(1_000_000)
            input_cost = (
                Decimal(int(usage.get("uncached_prompt_tokens") or 0))
                * Decimal(str(pricing.get("input") or 0.0))
                / million
            )
            cached_cost = (
                Decimal(int(usage.get("cached_prompt_tokens") or 0))
                * Decimal(str(pricing.get("cached_input") or 0.0))
                / million
            )
            output_cost = (
                Decimal(int(usage.get("completion_tokens") or 0))
                * Decimal(str(pricing.get("output") or 0.0))
                / million
            )
            bucket["uncached_input_cost_yuan"] += float(input_cost)
            bucket["cached_input_cost_yuan"] += float(cached_cost)
            bucket["output_cost_yuan"] += float(output_cost)
            bucket["total_cost_yuan"] += float(
                input_cost + cached_cost + output_cost
            )

        for call in calls:
            model_name = str((call.get("model") or {}).get("name") or "unknown")
            task_name = str(call.get("task") or "unknown")
            pricing = (call.get("model") or {}).get("pricing")
            model_summary = summary["by_model"].setdefault(
                model_name,
                empty_bucket(),
            )
            task_summary = summary["by_task"].setdefault(task_name, empty_bucket())
            for attempt in call.get("attempts") or []:
                usage = attempt.get("usage")
                add_attempt(summary, usage, pricing)
                add_attempt(model_summary, usage, pricing)
                add_attempt(task_summary, usage, pricing)
        for bucket in [
            summary,
            *summary["by_model"].values(),
            *summary["by_task"].values(),
        ]:
            for field_name in COST_USAGE_FIELDS:
                bucket[field_name] = round(float(bucket[field_name]), 12)
        return summary

    def model_usage_summary(self, *, include_prior: bool = False) -> dict[str, Any]:
        with self._lock:
            current = self._summarize_calls(list(self._model_calls))
            if not include_prior:
                return current
            if self._prior_usage_summary is not None:
                return merge_usage_summaries(
                    [dict(self._prior_usage_summary), current]
                )
            return self._summarize_calls(
                [*self._prior_usage_calls, *self._model_calls]
            )

    def model_call_count(self, *, include_prior: bool = False) -> int:
        with self._lock:
            current = len(self._model_calls)
            return self._model_call_id_offset + current if include_prior else current

    def save_model_usage(self) -> Path | None:
        if self._model_usage_path is None:
            return None
        with self._lock:
            current_calls = [self._usage_call(call) for call in self._model_calls]
            calls = (
                [*self._prior_usage_calls, *current_calls]
                if self._prior_usage_summary is None
                else current_calls
            )
            current_summary = self._summarize_calls(current_calls)
            cumulative_summary = self.model_usage_summary(include_prior=True)
            payload = {
                "schema_version": "2.0",
                "stage": self._model_stage_name,
                "started_at": self._model_call_tree_started_at,
                "updated_at": datetime.now().astimezone().isoformat(),
                "currency": "CNY",
                "price_unit": "yuan_per_million_tokens",
                "current_run": {
                    "summary": current_summary,
                    "call_ids": [call["call_id"] for call in self._model_calls],
                },
                "cumulative": {"summary": cumulative_summary},
                # Compatibility aliases for existing readers. `summary` is cumulative.
                "summary": cumulative_summary,
                "calls": calls,
            }
            self._model_usage_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._model_usage_path.with_suffix(
                self._model_usage_path.suffix + ".tmp"
            )
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self._model_usage_path)
            return self._model_usage_path

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
        latest_usage: ModelUsage | None = None
        latest_response_id: str | None = None
        latest_response_model: str | None = None

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
            nonlocal current_attempt, latest_response, latest_usage
            nonlocal latest_response_id, latest_response_model
            prompt, context_chars = contextual_prompt()
            current_attempt = self._record_model_attempt_start(
                call,
                active_package,
                contextual_prompt_chars=len(prompt),
                context_chars=context_chars,
            )
            latest_response = None
            latest_usage = None
            latest_response_id = None
            latest_response_model = None
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
                model_response = generate_text(
                    prompt,
                    config,
                    system_prompt=active_package.system_prompt,
                    image_data_urls=image_data_urls,
                    image_labels=image_labels,
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
            raw = model_response.content
            latest_response = raw
            latest_usage = model_response.usage
            latest_response_id = model_response.response_id
            latest_response_model = model_response.response_model
            usage_fields = (
                {
                    "prompt_tokens": latest_usage.prompt_tokens,
                    "completion_tokens": latest_usage.completion_tokens,
                    "total_tokens": latest_usage.total_tokens,
                    "cached_prompt_tokens": latest_usage.cached_prompt_tokens,
                    "uncached_prompt_tokens": latest_usage.uncached_prompt_tokens,
                    "reasoning_tokens": latest_usage.reasoning_tokens,
                }
                if latest_usage is not None
                else {"usage": "unreported"}
            )
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
                **usage_fields,
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
                usage=latest_usage,
                response_id=latest_response_id,
                response_model=latest_response_model,
                error=error,
            )
            self.save_model_usage()
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
                usage=latest_usage,
                response_id=latest_response_id,
                response_model=latest_response_model,
                error=exc,
            )
            self._finish_model_call(call, status="failed", error=exc)
            self.save_model_usage()
            self.save_model_call_tree(status="failed")
            raise
        self._finish_model_attempt(
            current_attempt,
            status="accepted",
            response=latest_response,
            usage=latest_usage,
            response_id=latest_response_id,
            response_model=latest_response_model,
        )
        self._finish_model_call(call, status="success")
        self.save_model_usage()
        if package.output_artifact:
            self.set_artifact(package.output_artifact, result)
        return result
