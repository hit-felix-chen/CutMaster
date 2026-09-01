"""Strict, transport-neutral ASTER stage checkpoint contracts.

Checkpoints contain only parsed workflow artifacts required to continue from a
completed agent boundary.  They deliberately exclude prompts, raw provider
responses, credentials, response identifiers, and filesystem paths.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

PLANNERS_CHECKPOINT_SCHEMA_VERSION = "4.0"
TOKEN_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_prompt_tokens",
    "uncached_prompt_tokens",
    "reasoning_tokens",
)
COST_USAGE_FIELDS = (
    "uncached_input_cost_yuan",
    "cached_input_cost_yuan",
    "output_cost_yuan",
    "total_cost_yuan",
)
USAGE_COUNT_FIELDS = (
    "request_count",
    "reported_usage_count",
    "unreported_usage_count",
    "priced_usage_count",
    "unpriced_usage_count",
)


class PlannersCheckpointStage(StrEnum):
    REPLAN_PENDING = "replan_pending"
    ARRANGEMENT = "arrangement_architect"
    STORY = "story_editor"
    TIMELINE = "timeline_scout"
    EDIT = "edit_composer"
    REVISION = "revision_editor"


class PlannersReplanScope(StrEnum):
    GLOBAL = "global"
    CANDIDATE_LOCAL = "candidate_local"


_STAGE_ORDER = {
    PlannersCheckpointStage.REPLAN_PENDING: 0,
    PlannersCheckpointStage.ARRANGEMENT: 1,
    PlannersCheckpointStage.STORY: 2,
    PlannersCheckpointStage.TIMELINE: 3,
    PlannersCheckpointStage.EDIT: 4,
    PlannersCheckpointStage.REVISION: 5,
}

_CHECKPOINT_KEYS = frozenset(
    {
        "schema_version",
        "completed_stage",
        "aster_attempt",
        "replan_scope",
        "local_replan_attempt",
        "music_profile",
        "slots",
        "arrangement_groups",
        "planning_segments",
        "planning_groups",
        "dialogue_anchors",
        "candidate_pool",
        "replan_reuse",
        "beam_path",
        "selected_trajectory_ids",
        "selection",
        "pairwise_scores",
        "raw_script",
        "planners_feedback",
        "stage_timings_sec",
        "prior_model_usage",
        "prior_model_call_count",
    }
)

_REPLAN_REUSE_KEYS = frozenset(
    {
        "previous_slots",
        "previous_planning_segments",
        "previous_planning_groups",
        "previous_dialogue_anchors",
        "previous_candidate_pool",
        "affected_parent_group_ids",
    }
)


def _json_copy(value: Any, label: str) -> Any:
    """Return a detached JSON value while rejecting non-finite numbers."""

    def require_string_keys(item: Any) -> None:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise TypeError(f"{label} must use string keys")
            for nested in item.values():
                require_string_keys(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                require_string_keys(nested)

    require_string_keys(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite JSON data") from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    result = _json_copy(dict(value), label)
    if not isinstance(result, dict) or any(not isinstance(key, str) for key in result):
        raise TypeError(f"{label} must use string keys")
    return result


def _object_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be a list")
    result = _json_copy(value, label)
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise TypeError(f"{label} must contain only mappings")
    return result


def normalize_checkpoint_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach the aggregate-only usage allowed in checkpoints."""

    if not isinstance(value, Mapping):
        raise TypeError("prior_model_usage must be a mapping")
    if value.get("currency") != "CNY":
        raise ValueError("prior_model_usage currency must be CNY")
    if value.get("price_unit") != "yuan_per_million_tokens":
        raise ValueError(
            "prior_model_usage price_unit must be yuan_per_million_tokens"
        )

    def validate_bucket(bucket: Any, label: str) -> None:
        if not isinstance(bucket, Mapping):
            raise TypeError(f"{label} must be a mapping")
        for field_name in (*USAGE_COUNT_FIELDS, *TOKEN_USAGE_FIELDS):
            item = bucket.get(field_name)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise ValueError(
                    f"{label} {field_name} must be a non-negative integer"
                )
        for field_name in COST_USAGE_FIELDS:
            item = bucket.get(field_name)
            if (
                not isinstance(item, (int, float))
                or isinstance(item, bool)
                or not math.isfinite(float(item))
                or float(item) < 0
            ):
                raise ValueError(
                    f"{label} {field_name} must be finite and non-negative"
                )
        if (
            int(bucket["reported_usage_count"])
            + int(bucket["unreported_usage_count"])
            != int(bucket["request_count"])
        ):
            raise ValueError(f"{label} request counts are inconsistent")
        if (
            int(bucket["priced_usage_count"])
            + int(bucket["unpriced_usage_count"])
            != int(bucket["reported_usage_count"])
        ):
            raise ValueError(f"{label} pricing counts are inconsistent")

    validate_bucket(value, "prior_model_usage")
    for group_name in ("by_model", "by_task"):
        group = value.get(group_name)
        if not isinstance(group, Mapping):
            raise TypeError(f"prior_model_usage {group_name} must be a mapping")
        for key, bucket in group.items():
            if (
                not isinstance(key, str)
                or not key
                or any(ord(character) < 32 or ord(character) == 127 for character in key)
            ):
                raise ValueError(
                    f"prior_model_usage {group_name} keys must be safe text"
                )
            validate_bucket(bucket, f"prior_model_usage {group_name} bucket")
    return _copy_usage_summary(value)


def _copy_usage_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    copied = {
        **{field: int(value[field]) for field in USAGE_COUNT_FIELDS},
        **{field: int(value[field]) for field in TOKEN_USAGE_FIELDS},
        **{field: round(float(value[field]), 12) for field in COST_USAGE_FIELDS},
        "currency": "CNY",
        "price_unit": "yuan_per_million_tokens",
        "by_model": {},
        "by_task": {},
    }
    for group_name in ("by_model", "by_task"):
        group = value[group_name]
        assert isinstance(group, Mapping)
        for key, bucket in group.items():
            assert isinstance(key, str) and isinstance(bucket, Mapping)
            copied[group_name][key] = {
                **{field: int(bucket[field]) for field in USAGE_COUNT_FIELDS},
                **{field: int(bucket[field]) for field in TOKEN_USAGE_FIELDS},
                **{
                    field: round(float(bucket[field]), 12)
                    for field in COST_USAGE_FIELDS
                },
            }
    return copied


@dataclass(frozen=True)
class PlannersCheckpoint:
    """State after one fully completed ASTER agent boundary."""

    completed_stage: PlannersCheckpointStage
    aster_attempt: int
    music_profile: Mapping[str, Any]
    slots: tuple[Mapping[str, Any], ...]
    arrangement_groups: tuple[Mapping[str, Any], ...]
    planning_segments: tuple[Mapping[str, Any], ...] | None
    planning_groups: tuple[Mapping[str, Any], ...] | None
    dialogue_anchors: tuple[Mapping[str, Any], ...] | None
    candidate_pool: Mapping[str, tuple[Mapping[str, Any], ...]] | None
    replan_reuse: Mapping[str, Any] | None
    beam_path: tuple[Mapping[str, Any], ...] | None
    selected_trajectory_ids: Mapping[str, Any] | None
    selection: Mapping[str, Any] | None
    pairwise_scores: Mapping[str, Any] | None
    raw_script: tuple[Mapping[str, Any], ...] | None
    planners_feedback: Mapping[str, Any] | None
    stage_timings_sec: Mapping[str, float]
    prior_model_usage: Mapping[str, Any]
    prior_model_call_count: int
    replan_scope: PlannersReplanScope | None = None
    local_replan_attempt: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.completed_stage, PlannersCheckpointStage):
            raise TypeError("completed_stage must be a PlannersCheckpointStage")
        if (
            not isinstance(self.aster_attempt, int)
            or isinstance(self.aster_attempt, bool)
            or self.aster_attempt < 1
        ):
            raise ValueError("aster_attempt must be a positive integer")
        if (
            not isinstance(self.prior_model_call_count, int)
            or isinstance(self.prior_model_call_count, bool)
            or self.prior_model_call_count < 0
        ):
            raise ValueError("prior_model_call_count must be non-negative")
        if self.replan_scope is not None and not isinstance(
            self.replan_scope,
            PlannersReplanScope,
        ):
            raise TypeError("replan_scope must be a PlannersReplanScope or None")
        if (
            not isinstance(self.local_replan_attempt, int)
            or isinstance(self.local_replan_attempt, bool)
            or self.local_replan_attempt < 0
        ):
            raise ValueError("local_replan_attempt must be non-negative")

        music_profile = _object(self.music_profile, "music_profile")
        slots = _object_list(self.slots, "slots")
        if not slots:
            raise ValueError("slots must not be empty")
        arrangement_groups = _object_list(
            self.arrangement_groups,
            "arrangement_groups",
        )
        if not arrangement_groups:
            raise ValueError("arrangement_groups must not be empty")
        planning_segments = self._optional_object_list(
            self.planning_segments,
            "planning_segments",
        )
        planning_groups = self._optional_object_list(
            self.planning_groups,
            "planning_groups",
        )
        dialogue_anchors = self._optional_object_list(
            self.dialogue_anchors,
            "dialogue_anchors",
        )
        candidate_pool = self._optional_candidate_pool(self.candidate_pool)
        replan_reuse = self._optional_replan_reuse(self.replan_reuse)
        beam_path = self._optional_object_list(self.beam_path, "beam_path")
        selected_trajectory_ids = self._optional_object(
            self.selected_trajectory_ids,
            "selected_trajectory_ids",
        )
        selection = self._optional_object(self.selection, "selection")
        pairwise_scores = self._optional_object(
            self.pairwise_scores,
            "pairwise_scores",
        )
        raw_script = self._optional_object_list(self.raw_script, "raw_script")
        planners_feedback = self._optional_object(
            self.planners_feedback,
            "planners_feedback",
        )
        timings = _object(self.stage_timings_sec, "stage_timings_sec")
        for key, value in timings.items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(
                    f"stage_timings_sec {key!r} must be finite and non-negative"
                )
            timings[key] = float(value)
        usage = normalize_checkpoint_usage(self.prior_model_usage)

        stage_index = _STAGE_ORDER[self.completed_stage]
        if self.completed_stage is PlannersCheckpointStage.REPLAN_PENDING:
            if planners_feedback is None:
                raise ValueError("planners_feedback is required at a replan boundary")
            if self.replan_scope is None:
                raise ValueError("replan_scope is required at a replan boundary")
            if self.replan_scope is PlannersReplanScope.CANDIDATE_LOCAL:
                diagnostics = planners_feedback.get("diagnostics")
                failed_parent_group_ids = (
                    diagnostics.get("failed_parent_group_ids")
                    if isinstance(diagnostics, Mapping)
                    else None
                )
                if (
                    not isinstance(failed_parent_group_ids, list)
                    or not failed_parent_group_ids
                    or any(
                        not isinstance(group_id, str) or not group_id.strip()
                        for group_id in failed_parent_group_ids
                    )
                    or len(set(failed_parent_group_ids))
                    != len(failed_parent_group_ids)
                ):
                    raise ValueError(
                        "candidate_local replan checkpoint requires unique, "
                        "non-empty failed_parent_group_ids"
                    )
        if (
            self.replan_scope is not None
            and self.completed_stage
            not in {
                PlannersCheckpointStage.REPLAN_PENDING,
                PlannersCheckpointStage.STORY,
            }
        ):
            raise ValueError(
                "replan_scope is allowed only while repair or candidate reuse is pending"
            )
        if (
            replan_reuse is not None
            and self.replan_scope is not PlannersReplanScope.CANDIDATE_LOCAL
        ):
            raise ValueError("replan_reuse is reserved for candidate_local repair")
        if (
            self.replan_scope is PlannersReplanScope.GLOBAL
            and self.completed_stage is not PlannersCheckpointStage.REPLAN_PENDING
        ):
            raise ValueError("global replan_scope is valid only at a replan boundary")
        if (
            self.replan_scope is PlannersReplanScope.CANDIDATE_LOCAL
            and replan_reuse is None
        ):
            raise ValueError("candidate_local replan_scope requires replan_reuse")
        if self.replan_scope is PlannersReplanScope.CANDIDATE_LOCAL:
            if self.local_replan_attempt < 1:
                raise ValueError(
                    "candidate_local replan_scope requires a positive local_replan_attempt"
                )
        elif self.local_replan_attempt != 0:
            raise ValueError(
                "local_replan_attempt is allowed only for candidate_local repair"
            )
        if (
            replan_reuse is not None
            and self.completed_stage
            not in {
                PlannersCheckpointStage.REPLAN_PENDING,
                PlannersCheckpointStage.STORY,
            }
        ):
            raise ValueError(
                "replan_reuse is allowed only while repair or candidate reuse is pending"
            )
        required = (
            (2, planning_segments, "planning_segments"),
            (2, planning_groups, "planning_groups"),
            (2, dialogue_anchors, "dialogue_anchors"),
            (3, candidate_pool, "candidate_pool"),
            (4, beam_path, "beam_path"),
            (4, selected_trajectory_ids, "selected_trajectory_ids"),
            (4, selection, "selection"),
            (4, pairwise_scores, "pairwise_scores"),
            (5, raw_script, "raw_script"),
        )
        for boundary, value, label in required:
            if stage_index >= boundary and value is None:
                raise ValueError(
                    f"{label} is required after {self.completed_stage.value}"
                )
            if stage_index < boundary and value is not None:
                raise ValueError(
                    f"{label} is not allowed after {self.completed_stage.value}"
                )

        object.__setattr__(self, "music_profile", MappingProxyType(music_profile))
        object.__setattr__(
            self,
            "slots",
            tuple(MappingProxyType(item) for item in slots),
        )
        object.__setattr__(
            self,
            "arrangement_groups",
            tuple(MappingProxyType(item) for item in arrangement_groups),
        )
        object.__setattr__(self, "planning_segments", planning_segments)
        object.__setattr__(self, "planning_groups", planning_groups)
        object.__setattr__(self, "dialogue_anchors", dialogue_anchors)
        object.__setattr__(self, "candidate_pool", candidate_pool)
        object.__setattr__(self, "replan_reuse", replan_reuse)
        object.__setattr__(self, "beam_path", beam_path)
        object.__setattr__(
            self,
            "selected_trajectory_ids",
            selected_trajectory_ids,
        )
        object.__setattr__(self, "selection", selection)
        object.__setattr__(self, "pairwise_scores", pairwise_scores)
        object.__setattr__(self, "raw_script", raw_script)
        object.__setattr__(self, "planners_feedback", planners_feedback)
        object.__setattr__(self, "stage_timings_sec", MappingProxyType(timings))
        object.__setattr__(self, "prior_model_usage", MappingProxyType(usage))

    @staticmethod
    def _optional_object(
        value: Mapping[str, Any] | None,
        label: str,
    ) -> Mapping[str, Any] | None:
        return None if value is None else MappingProxyType(_object(value, label))

    @staticmethod
    def _optional_object_list(
        value: tuple[Mapping[str, Any], ...] | None,
        label: str,
    ) -> tuple[Mapping[str, Any], ...] | None:
        if value is None:
            return None
        return tuple(MappingProxyType(item) for item in _object_list(value, label))

    @staticmethod
    def _optional_candidate_pool(
        value: Mapping[str, tuple[Mapping[str, Any], ...]] | None,
    ) -> Mapping[str, tuple[Mapping[str, Any], ...]] | None:
        if value is None:
            return None
        raw = _object(value, "candidate_pool")
        result: dict[str, tuple[Mapping[str, Any], ...]] = {}
        for group_id, trajectories in raw.items():
            result[group_id] = tuple(
                MappingProxyType(item)
                for item in _object_list(
                    trajectories,
                    f"candidate_pool[{group_id!r}]",
                )
            )
        return MappingProxyType(result)

    @staticmethod
    def _optional_replan_reuse(
        value: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        if value is None:
            return None
        raw = _object(value, "replan_reuse")
        if set(raw) != _REPLAN_REUSE_KEYS:
            raise ValueError("replan_reuse fields do not match schema 4.0")
        previous_slots = _object_list(
            raw["previous_slots"],
            "replan_reuse previous_slots",
        )
        if not previous_slots:
            raise ValueError("replan_reuse previous_slots must not be empty")
        planning_segments = _object_list(
            raw["previous_planning_segments"],
            "replan_reuse previous_planning_segments",
        )
        planning_groups = _object_list(
            raw["previous_planning_groups"],
            "replan_reuse previous_planning_groups",
        )
        dialogue_anchors = _object_list(
            raw["previous_dialogue_anchors"],
            "replan_reuse previous_dialogue_anchors",
        )
        pool_raw = _object(
            raw["previous_candidate_pool"],
            "replan_reuse previous_candidate_pool",
        )
        candidate_pool = {
            group_id: _object_list(
                trajectories,
                f"replan_reuse previous_candidate_pool[{group_id!r}]",
            )
            for group_id, trajectories in pool_raw.items()
        }
        affected = raw["affected_parent_group_ids"]
        if (
            not isinstance(affected, list)
            or any(not isinstance(value, str) or not value for value in affected)
            or len(set(affected)) != len(affected)
        ):
            raise ValueError(
                "replan_reuse affected_parent_group_ids must be unique strings"
            )
        return MappingProxyType(
            {
                "previous_slots": previous_slots,
                "previous_planning_segments": planning_segments,
                "previous_planning_groups": planning_groups,
                "previous_dialogue_anchors": dialogue_anchors,
                "previous_candidate_pool": candidate_pool,
                "affected_parent_group_ids": list(affected),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PLANNERS_CHECKPOINT_SCHEMA_VERSION,
            "completed_stage": self.completed_stage.value,
            "aster_attempt": self.aster_attempt,
            "replan_scope": (
                None if self.replan_scope is None else self.replan_scope.value
            ),
            "local_replan_attempt": self.local_replan_attempt,
            "music_profile": dict(self.music_profile),
            "slots": [dict(item) for item in self.slots],
            "arrangement_groups": [
                dict(item) for item in self.arrangement_groups
            ],
            "planning_segments": (
                None
                if self.planning_segments is None
                else [dict(item) for item in self.planning_segments]
            ),
            "planning_groups": (
                None
                if self.planning_groups is None
                else [dict(item) for item in self.planning_groups]
            ),
            "dialogue_anchors": (
                None
                if self.dialogue_anchors is None
                else [dict(item) for item in self.dialogue_anchors]
            ),
            "candidate_pool": (
                None
                if self.candidate_pool is None
                else {
                    group_id: [dict(item) for item in trajectories]
                    for group_id, trajectories in self.candidate_pool.items()
                }
            ),
            "replan_reuse": (
                None if self.replan_reuse is None else dict(self.replan_reuse)
            ),
            "beam_path": (
                None
                if self.beam_path is None
                else [dict(item) for item in self.beam_path]
            ),
            "selected_trajectory_ids": (
                None
                if self.selected_trajectory_ids is None
                else dict(self.selected_trajectory_ids)
            ),
            "selection": None if self.selection is None else dict(self.selection),
            "pairwise_scores": (
                None
                if self.pairwise_scores is None
                else dict(self.pairwise_scores)
            ),
            "raw_script": (
                None
                if self.raw_script is None
                else [dict(item) for item in self.raw_script]
            ),
            "planners_feedback": (
                None
                if self.planners_feedback is None
                else dict(self.planners_feedback)
            ),
            "stage_timings_sec": dict(self.stage_timings_sec),
            "prior_model_usage": dict(self.prior_model_usage),
            "prior_model_call_count": self.prior_model_call_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlannersCheckpoint":
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str) for key in value
        ):
            raise TypeError("ASTER checkpoint must be a string-keyed mapping")
        if value.get("schema_version") != PLANNERS_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported ASTER checkpoint schema: {value.get('schema_version')!r}"
            )
        if set(value) != _CHECKPOINT_KEYS:
            raise ValueError("ASTER checkpoint fields do not match schema 4.0")
        stage_raw = value.get("completed_stage")
        if not isinstance(stage_raw, str):
            raise TypeError("completed_stage must be a string")
        stage = PlannersCheckpointStage(stage_raw)
        aster_attempt = value.get("aster_attempt")
        scope_raw = value.get("replan_scope")
        if scope_raw is not None and not isinstance(scope_raw, str):
            raise TypeError("replan_scope must be a string or null")
        replan_scope = (
            None if scope_raw is None else PlannersReplanScope(scope_raw)
        )
        local_replan_attempt = value.get("local_replan_attempt")
        prior_model_call_count = value.get("prior_model_call_count")
        return cls(
            completed_stage=stage,
            aster_attempt=aster_attempt,  # type: ignore[arg-type]
            replan_scope=replan_scope,
            local_replan_attempt=local_replan_attempt,  # type: ignore[arg-type]
            music_profile=_object(value.get("music_profile"), "music_profile"),
            slots=tuple(_object_list(value.get("slots"), "slots")),
            arrangement_groups=tuple(
                _object_list(value.get("arrangement_groups"), "arrangement_groups")
            ),
            planning_segments=(
                None
                if value.get("planning_segments") is None
                else tuple(
                    _object_list(value.get("planning_segments"), "planning_segments")
                )
            ),
            planning_groups=(
                None
                if value.get("planning_groups") is None
                else tuple(
                    _object_list(value.get("planning_groups"), "planning_groups")
                )
            ),
            dialogue_anchors=(
                None
                if value.get("dialogue_anchors") is None
                else tuple(
                    _object_list(value.get("dialogue_anchors"), "dialogue_anchors")
                )
            ),
            candidate_pool=(
                None
                if value.get("candidate_pool") is None
                else {
                    key: tuple(
                        _object_list(item, f"candidate_pool[{key!r}]")
                    )
                    for key, item in _object(
                        value.get("candidate_pool"),
                        "candidate_pool",
                    ).items()
                }
            ),
            replan_reuse=(
                None
                if value.get("replan_reuse") is None
                else _object(value.get("replan_reuse"), "replan_reuse")
            ),
            beam_path=(
                None
                if value.get("beam_path") is None
                else tuple(_object_list(value.get("beam_path"), "beam_path"))
            ),
            selected_trajectory_ids=(
                None
                if value.get("selected_trajectory_ids") is None
                else _object(
                    value.get("selected_trajectory_ids"),
                    "selected_trajectory_ids",
                )
            ),
            selection=(
                None
                if value.get("selection") is None
                else _object(value.get("selection"), "selection")
            ),
            pairwise_scores=(
                None
                if value.get("pairwise_scores") is None
                else _object(value.get("pairwise_scores"), "pairwise_scores")
            ),
            raw_script=(
                None
                if value.get("raw_script") is None
                else tuple(_object_list(value.get("raw_script"), "raw_script"))
            ),
            planners_feedback=(
                None
                if value.get("planners_feedback") is None
                else _object(value.get("planners_feedback"), "planners_feedback")
            ),
            stage_timings_sec=_object(
                value.get("stage_timings_sec"),
                "stage_timings_sec",
            ),
            prior_model_usage=normalize_checkpoint_usage(
                _object(value.get("prior_model_usage"), "prior_model_usage")
            ),
            prior_model_call_count=prior_model_call_count,  # type: ignore[arg-type]
        )


@runtime_checkable
class PlannersCheckpointStore(Protocol):
    """A pre-bound Run owner store consumed by one Planners invocation."""

    def load(self) -> PlannersCheckpoint | None:
        """Load the exact checkpoint pinned to this Attempt, if any."""

    def save(self, checkpoint: PlannersCheckpoint) -> None:
        """Atomically publish the latest completed ASTER boundary."""


__all__ = [
    "PLANNERS_CHECKPOINT_SCHEMA_VERSION",
    "PlannersCheckpoint",
    "PlannersCheckpointStage",
    "PlannersReplanScope",
    "PlannersCheckpointStore",
    "normalize_checkpoint_usage",
]
