from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

from cutmaster.llm import generate_text, request_json_with_retries
from cutmaster.models import ModelConfig
from cutmaster.observability import error_summary, log_event
from cutmaster.prompting import PromptPackage


T = TypeVar("T")


def _fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class WorkflowContext:
    """Persistent artifacts and model-call history for a workflow stage."""

    def __init__(self, path: Path) -> None:
        self._lock = RLock()
        self.path = path
        self.data: dict[str, Any] = {
            "schema_version": "1.0",
            "created_at": datetime.now().astimezone().isoformat(),
            "artifacts": {},
            "calls": [],
            "script_versions": [],
        }
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))

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

    def get_successful_prompt_result(
        self,
        package: PromptPackage,
        validate_business: Callable[[dict[str, Any]], T] | None = None,
        default: Any = None,
    ) -> T | dict[str, Any] | Any:
        context_fingerprint = _fingerprint(
            self.context_snapshot(list(package.context_keys))
        )
        with self._lock:
            for call in reversed(self.data.get("calls", [])):
                if (
                    call.get("operation") == package.operation
                    and call.get("prompt_id") == package.prompt_id
                    and call.get("prompt_version") == package.prompt_version
                    and call.get("prompt_fingerprint") == package.fingerprint
                    and call.get("context_fingerprint") == context_fingerprint
                    and call.get("contract_fingerprint")
                    == package.response_contract.fingerprint
                    and call.get("status") == "success"
                    and "structured_result" in call
                ):
                    structured = package.response_contract.validate_structure(
                        call["structured_result"]
                    )
                    return (
                        validate_business(structured)
                        if validate_business is not None
                        else structured
                    )
            return default

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
        context_fingerprint = _fingerprint(snapshot)
        contextual_prompt = package.user_prompt
        if snapshot:
            contextual_prompt = (
                "# Maintained workflow context\n"
                + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                + "\n\n"
                + package.user_prompt
            )
        with self._lock:
            call: dict[str, Any] = {
                "call_id": len(self.data.setdefault("calls", [])) + 1,
                "operation": package.operation,
                "prompt_id": package.prompt_id,
                "prompt_version": package.prompt_version,
                "prompt_fingerprint": package.fingerprint,
                "context_fingerprint": context_fingerprint,
                "contract_version": package.response_contract.version,
                "contract_fingerprint": package.response_contract.fingerprint,
                "created_at": datetime.now().astimezone().isoformat(),
                "model": config.model,
                "enable_thinking": config.enable_thinking,
                "input_modality": package.modality.value,
                "context_keys": list(package.context_keys),
                "context_snapshot": snapshot,
                "prompt": contextual_prompt,
                "response_contract": package.response_contract.schema,
                "status": "running",
            }
            if image_labels:
                call["image_labels"] = image_labels
            self.data["calls"].append(call)
            self.save()

        raw_responses: list[str] = []
        structured_result: dict[str, Any] | None = None

        def request() -> str:
            request_started = time.monotonic()
            modality = "text_and_images" if image_data_urls else "text"
            log_event(
                "INFO",
                "model",
                "model.start",
                "Model request started",
                operation=package.operation,
                prompt_id=package.prompt_id,
                prompt_version=package.prompt_version,
                contract_version=package.response_contract.version,
                model=config.model,
                modality=modality,
                thinking=config.enable_thinking,
                images=len(image_data_urls or []),
                prompt_chars=len(contextual_prompt),
            )
            try:
                raw = generate_text(
                    contextual_prompt,
                    config,
                    system_prompt=package.system_prompt,
                    image_data_urls=image_data_urls,
                )
            except Exception as exc:
                log_event(
                    "WARNING",
                    "model",
                    "model.fail",
                    "Model request failed",
                    operation=package.operation,
                    prompt_id=package.prompt_id,
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
                operation=package.operation,
                prompt_id=package.prompt_id,
                model=config.model,
                modality=modality,
                elapsed_sec=time.monotonic() - request_started,
                response_chars=len(raw),
            )
            with self._lock:
                raw_responses.append(raw)
                call["raw_responses"] = raw_responses
                self.save()
            return raw

        try:
            def validate(parsed: dict[str, Any]) -> T | dict[str, Any]:
                nonlocal structured_result
                structured = package.response_contract.validate_structure(parsed)
                structured_result = structured
                return (
                    validate_business(structured)
                    if validate_business is not None
                    else structured
                )

            result = request_json_with_retries(
                request,
                config,
                operation=package.operation,
                validate=validate,
            )
            with self._lock:
                call["status"] = "success"
                call["completed_at"] = datetime.now().astimezone().isoformat()
                call["structured_result"] = structured_result
                call["parsed_result"] = result
                if package.output_artifact:
                    self.set_artifact(package.output_artifact, result)
                self.save()
            return result
        except Exception as exc:
            with self._lock:
                call["status"] = "failed"
                call["completed_at"] = datetime.now().astimezone().isoformat()
                call["error"] = f"{type(exc).__name__}: {exc}"
                self.save()
            raise
