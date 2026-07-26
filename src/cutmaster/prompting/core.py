from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class PromptStage(StrEnum):
    ANALYSER = "analyser"
    PLANNER = "planner"


class PromptTask(StrEnum):
    DIALOGUE_RECONSTRUCTION = "dialogue_reconstruction"
    DIALOGUE_SEGMENTATION = "dialogue_segmentation"
    SHOT_ANNOTATION = "shot_annotation"
    VIDEO_SUMMARY = "video_summary"
    SLOT_PLANNING = "slot_planning"
    DIALOGUE_ANCHOR_SELECTION = "dialogue_anchor_selection"
    CANDIDATE_RETRIEVAL = "candidate_retrieval"
    CANDIDATE_VISUAL_SCORING = "candidate_visual_scoring"
    PAIRWISE_SCORING = "pairwise_scoring"
    SCRIPT_REVIEW = "script_review"


class PromptModality(StrEnum):
    TEXT = "text"
    TEXT_AND_IMAGES = "text_and_images"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _range_placeholder(schema: dict[str, Any], kind: str) -> str:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and maximum is not None:
        return f"<{kind}: {minimum} to {maximum}>"
    if minimum is not None:
        return f"<{kind}: >= {minimum}>"
    if maximum is not None:
        return f"<{kind}: <= {maximum}>"
    return f"<{kind}>"


def response_template_from_schema(schema: dict[str, Any]) -> Any:
    """Derive a readable response-shape template from the executable schema."""
    for union_key in ("oneOf", "anyOf"):
        alternatives = schema.get(union_key)
        if alternatives:
            return response_template_from_schema(alternatives[0])
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return "<one of: " + ", ".join(str(value) for value in schema["enum"]) + ">"
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties") or {}
        return {
            key: response_template_from_schema(properties[key])
            for key in schema.get("required") or []
        }
    if schema_type == "array":
        return [response_template_from_schema(schema.get("items") or {})]
    if schema_type == "string":
        return "<non-empty string>" if schema.get("minLength", 0) > 0 else "<string>"
    if schema_type == "integer":
        return _range_placeholder(schema, "integer")
    if schema_type == "number":
        return _range_placeholder(schema, "number")
    if schema_type == "boolean":
        return "<boolean>"
    return "<value>"


@dataclass(frozen=True)
class ResponseContract:
    version: str
    schema: dict[str, Any]

    def __post_init__(self) -> None:
        try:
            Draft202012Validator.check_schema(self.schema)
        except SchemaError as exc:
            raise ValueError(f"Invalid response contract schema: {exc.message}") from exc

    @property
    def fingerprint(self) -> str:
        payload = f"{self.version}\n{_canonical_json(self.schema)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def template(self) -> dict[str, Any]:
        template = response_template_from_schema(self.schema)
        if not isinstance(template, dict):
            raise ValueError("Top-level response contract must describe an object")
        return template

    def validate_structure(self, parsed: dict[str, Any]) -> dict[str, Any]:
        try:
            Draft202012Validator(self.schema).validate(parsed)
        except ValidationError as exc:
            path = "$"
            if exc.absolute_path:
                path += "".join(
                    f"[{value}]" if isinstance(value, int) else f".{value}"
                    for value in exc.absolute_path
                )
            raise ValueError(
                f"Response contract violation at {path}: {exc.message}"
            ) from exc
        return parsed


@dataclass(frozen=True)
class PromptPackage:
    stage: PromptStage
    task: PromptTask
    prompt_version: str
    operation: str
    system_prompt: str
    user_prompt: str
    response_contract: ResponseContract
    context_keys: tuple[str, ...]
    modality: PromptModality
    output_artifact: str | None = None
    retry_builder: Callable[[tuple[str, ...]], PromptPackage] | None = None

    @property
    def prompt_id(self) -> str:
        return f"{self.stage.value}.{self.task.value}"

    @property
    def fingerprint(self) -> str:
        payload = "\n".join(
            [
                self.prompt_id,
                self.prompt_version,
                self.system_prompt,
                self.user_prompt,
                self.response_contract.fingerprint,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def assemble_user_prompt(
    instructions: str,
    contract: ResponseContract,
) -> str:
    schema_text = json.dumps(contract.schema, ensure_ascii=False, indent=2)
    template_text = json.dumps(contract.template, ensure_ascii=False, indent=2)
    return f"""{instructions.strip()}

The response contract below is authoritative. Return one JSON data object that validates against
it. Do not return the contract itself. Do not invent, combine, qualify, or rename enum values.

<response_contract>
{schema_text}
</response_contract>

The following response template is generated from that same contract and shows the required
shape. Replace every angle-bracket placeholder with a valid value before returning the object.

<response_template>
{template_text}
</response_template>

Return only the JSON data object."""
