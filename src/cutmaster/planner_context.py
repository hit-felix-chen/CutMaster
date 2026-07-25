from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, TypeVar

from cutmaster.llm import generate_text, request_json_with_retries
from cutmaster.models import LLMConfig


T = TypeVar("T")


class PlanningContext:
    """Append-only, project-owned context for planning and patch calls."""

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

    def get_successful_call_result(
        self,
        operation: str,
        default: Any = None,
    ) -> Any:
        with self._lock:
            for call in reversed(self.data.get("calls", [])):
                if (
                    call.get("operation") == operation
                    and call.get("status") == "success"
                    and "parsed_result" in call
                ):
                    return call["parsed_result"]
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

    def call_json(
        self,
        *,
        operation: str,
        prompt: str,
        config: LLMConfig,
        context_keys: list[str] | None = None,
        system_prompt: str,
        enable_thinking: bool = True,
        validate: Callable[[dict[str, Any]], T] | None = None,
        output_artifact: str | None = None,
        image_data_urls: list[str] | None = None,
        image_labels: list[str] | None = None,
    ) -> T | dict[str, Any]:
        snapshot = self.context_snapshot(context_keys or [])
        contextual_prompt = prompt
        if snapshot:
            contextual_prompt = (
                "# Maintained planning context\n"
                + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
                + "\n\n"
                + prompt
            )
        with self._lock:
            call: dict[str, Any] = {
                "call_id": len(self.data.setdefault("calls", [])) + 1,
                "operation": operation,
                "created_at": datetime.now().astimezone().isoformat(),
                "context_keys": context_keys or [],
                "context_snapshot": snapshot,
                "prompt": contextual_prompt,
                "status": "running",
            }
            if image_labels:
                call["image_labels"] = image_labels
            self.data["calls"].append(call)
            self.save()

        raw_responses: list[str] = []

        def request() -> str:
            raw = generate_text(
                contextual_prompt,
                config,
                system_prompt=system_prompt,
                enable_thinking=enable_thinking,
                image_data_urls=image_data_urls,
            )
            with self._lock:
                raw_responses.append(raw)
                call["raw_responses"] = raw_responses
                self.save()
            return raw

        try:
            result = request_json_with_retries(
                request,
                config,
                operation=operation,
                validate=validate,
            )
            with self._lock:
                call["status"] = "success"
                call["completed_at"] = datetime.now().astimezone().isoformat()
                call["parsed_result"] = result
                if output_artifact:
                    self.set_artifact(output_artifact, result)
                self.save()
            return result
        except Exception as exc:
            with self._lock:
                call["status"] = "failed"
                call["completed_at"] = datetime.now().astimezone().isoformat()
                call["error"] = f"{type(exc).__name__}: {exc}"
                self.save()
            raise
