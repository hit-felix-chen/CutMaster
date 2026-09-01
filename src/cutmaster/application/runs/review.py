"""Pure helpers for Frozen Edit Review projections and Guided Revision."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cutmaster.application.errors import ReviewArtifactUnavailableError
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.shared.timecode import parse_range


REVIEW_BUNDLE_SCHEMA_VERSION = "3.0"
_LEGACY_REVIEW_BUNDLE_SCHEMA_VERSION = "2.0"
REVIEW_BUNDLE_FILENAME = "review_bundle.json"


def resolve_managed_review_file(data_root: Path, relative_path: str) -> Path:
    """Resolve one regular managed file without permitting path escape."""

    normalized = validate_portable_relative_file_path(relative_path)
    root = data_root.resolve()
    candidate = data_root.joinpath(*normalized.split("/"))
    if candidate.is_symlink() or not candidate.is_file():
        raise ReviewArtifactUnavailableError(
            "Frozen Edit artifact is missing or is not a regular file"
        )
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReviewArtifactUnavailableError(
            "Frozen Edit artifact escapes the Application Data Root"
        ) from exc
    return resolved


def load_render_plan(data_root: Path, relative_path: str) -> tuple[RenderPlan, Path]:
    path = resolve_managed_review_file(data_root, relative_path)
    try:
        return RenderPlan.read(path), path
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ReviewArtifactUnavailableError(
            "Frozen Edit RenderPlan is unreadable or unsupported"
        ) from exc


def load_review_bundle(plan_path: Path) -> dict[str, Any]:
    """Load a committed, versioned Review bundle for a Frozen Edit.

    The manifest is the commit point. Guided Revision children retain their
    source Run's immutable bundle, so the bounded ancestor lookup is part of
    the current artifact contract rather than a legacy fallback.
    """

    candidate_directories = (
        plan_path.parent,
        plan_path.parent.parent,
        plan_path.parent.parent.parent,
    )
    manifest_path = next(
        (
            directory / REVIEW_BUNDLE_FILENAME
            for directory in candidate_directories
            if (directory / REVIEW_BUNDLE_FILENAME).exists()
        ),
        None,
    )
    if manifest_path is None:
        raise ReviewArtifactUnavailableError(
            "Frozen Edit Candidate Bundle is unavailable"
        )
    manifest = _read_json_object(manifest_path, "Review bundle manifest")
    schema_version = manifest.get("schema_version")
    if schema_version not in {
        REVIEW_BUNDLE_SCHEMA_VERSION,
        _LEGACY_REVIEW_BUNDLE_SCHEMA_VERSION,
    }:
        raise ReviewArtifactUnavailableError("Unsupported Review bundle schema")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, Mapping):
        raise ReviewArtifactUnavailableError("Review bundle has no artifact map")
    loaded: dict[str, Any] = {}
    logical_names = [
        "candidate_pool",
        "edit_plan",
        "music_profile",
        "selection_diagnostics",
    ]
    if schema_version == REVIEW_BUNDLE_SCHEMA_VERSION:
        logical_names.extend(("planning_segments", "planning_groups"))
    for logical_name in logical_names:
        entry = raw_artifacts.get(logical_name)
        if not isinstance(entry, Mapping):
            raise ReviewArtifactUnavailableError(
                f"Review bundle is missing {logical_name}"
            )
        try:
            relative = validate_portable_relative_file_path(
                str(entry.get("path") or "")
            )
        except ValueError as exc:
            raise ReviewArtifactUnavailableError(
                f"Review bundle artifact path for {logical_name} is invalid"
            ) from exc
        if "/" in relative:
            raise ReviewArtifactUnavailableError(
                "Review bundle artifacts must stay beside the manifest"
            )
        artifact_path = manifest_path.parent / relative
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ReviewArtifactUnavailableError(
                f"Review bundle artifact {logical_name} is unavailable"
            )
        expected_digest = str(entry.get("sha256") or "")
        if len(expected_digest) != 64 or _sha256(artifact_path) != expected_digest:
            raise ReviewArtifactUnavailableError(
                f"Review bundle artifact {logical_name} failed integrity validation"
            )
        loaded[logical_name] = _read_json(artifact_path, logical_name)
    expected_shapes = {
        "candidate_pool": dict,
        "edit_plan": list,
        "music_profile": dict,
        "selection_diagnostics": dict,
    }
    if schema_version == REVIEW_BUNDLE_SCHEMA_VERSION:
        expected_shapes.update(
            {
                "planning_segments": list,
                "planning_groups": list,
            }
        )
    for logical_name, expected_type in expected_shapes.items():
        if not isinstance(loaded[logical_name], expected_type):
            raise ReviewArtifactUnavailableError(
                f"Review bundle artifact {logical_name} has an invalid shape"
            )
    return loaded


def project_plan_summary(plan: RenderPlan) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "schema_version": plan.schema_version,
            "plan_id": plan.plan_id,
            "fps": plan.fps,
            "total_frames": plan.total_frames,
            "duration_sec": plan.duration_sec,
        }
    )


def project_slots(plan: RenderPlan) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    for position, clip in enumerate(plan.clips, start=1):
        source_start, source_end = parse_range(str(clip["timestamp"]))
        frame_start, frame_end = (int(value) for value in clip["output_frame_range"])
        output_start = float(clip.get("output_start_sec", frame_start / plan.fps))
        output_end = float(clip.get("output_end_sec", frame_end / plan.fps))
        anchor = clip.get("dialogue_anchor")
        result.append(
            MappingProxyType(
                {
                    "slot_id": str(clip["slot_id"]),
                    "group_id": _required_identity(clip, "group_id", "RenderPlan clip"),
                    "position": position,
                    "is_anchor": isinstance(anchor, Mapping),
                    "output_start_sec": output_start,
                    "output_end_sec": output_end,
                    "source_start_sec": source_start,
                    "source_end_sec": source_end,
                    "source_timestamp": str(clip["timestamp"]),
                    "selected_candidate_id": str(clip["candidate_id"]),
                    "selected_trajectory_id": _required_identity(
                        clip,
                        "trajectory_id",
                        "RenderPlan clip",
                    ),
                    "picture": str(clip.get("picture") or ""),
                    "selection_scores": MappingProxyType(
                        dict(clip.get("selection_scores") or {})
                    ),
                    "dialogue_anchor": (
                        None
                        if not isinstance(anchor, Mapping)
                        else MappingProxyType(dict(anchor))
                    ),
                }
            )
        )
    return tuple(result)


def project_candidates(
    plan: RenderPlan,
    candidate_pool: Mapping[str, Any],
    planning_segments: Sequence[Mapping[str, Any]] | None = None,
    planning_groups: Sequence[Mapping[str, Any]] | None = None,
) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    groups = _plan_groups(plan)
    replaceable_groups = {
        group_id: clips
        for group_id, clips in groups.items()
        if not any(isinstance(clip.get("dialogue_anchor"), Mapping) for clip in clips)
    }
    pool_group_ids = tuple(candidate_pool.keys())
    if any(not isinstance(group_id, str) for group_id in pool_group_ids) or set(
        pool_group_ids
    ) != set(replaceable_groups):
        raise ReviewArtifactUnavailableError(
            "Candidate trajectory Group identities do not match the RenderPlan"
        )
    planning_contract = _planning_contract(
        replaceable_groups,
        planning_segments,
        planning_groups,
    )

    projected: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for group_id, clips in replaceable_groups.items():
        raw_trajectories = candidate_pool.get(group_id)
        if not isinstance(raw_trajectories, Sequence) or isinstance(
            raw_trajectories, (str, bytes)
        ):
            raise ReviewArtifactUnavailableError(
                f"Candidate trajectories for {group_id} must be an array"
            )
        selected_ids = {
            _required_identity(clip, "trajectory_id", f"RenderPlan Group {group_id}")
            for clip in clips
        }
        if len(selected_ids) != 1:
            raise ReviewArtifactUnavailableError(
                f"RenderPlan Group {group_id} mixes selected trajectories"
            )
        selected_trajectory_id = next(iter(selected_ids))
        expected_slot_ids = tuple(str(clip["slot_id"]) for clip in clips)
        expected_candidate_ids = tuple(str(clip["candidate_id"]) for clip in clips)
        trajectories: list[Mapping[str, Any]] = []
        seen_trajectory_ids: set[str] = set()
        for raw_trajectory in raw_trajectories:
            if not isinstance(raw_trajectory, Mapping):
                raise ReviewArtifactUnavailableError(
                    f"Candidate trajectories for {group_id} contain an invalid item"
                )
            trajectory_id = _required_identity(
                raw_trajectory,
                "trajectory_id",
                f"Candidate trajectory Group {group_id}",
            )
            if trajectory_id in seen_trajectory_ids:
                raise ReviewArtifactUnavailableError(
                    f"Candidate trajectories contain duplicate {trajectory_id}"
                )
            seen_trajectory_ids.add(trajectory_id)
            if _required_identity(
                raw_trajectory,
                "group_id",
                f"Candidate trajectory {trajectory_id}",
            ) != group_id:
                raise ReviewArtifactUnavailableError(
                    f"Candidate trajectory {trajectory_id} belongs to another Group"
                )
            planning_segment_id = _required_identity(
                raw_trajectory,
                "planning_segment_id",
                f"Candidate trajectory {trajectory_id}",
            )
            segment_bounds = planning_contract.get(group_id)
            if (
                segment_bounds is not None
                and planning_segment_id != segment_bounds[0]
            ):
                raise ReviewArtifactUnavailableError(
                    f"Candidate trajectory {trajectory_id} does not belong to "
                    f"the Planning Segment for {group_id}"
                )
            raw_items = raw_trajectory.get("items")
            if not isinstance(raw_items, Sequence) or isinstance(
                raw_items, (str, bytes)
            ):
                raise ReviewArtifactUnavailableError(
                    f"Candidate trajectory {trajectory_id} has no item array"
                )
            valid_item_identities = all(
                isinstance(item, Mapping)
                and isinstance(item.get("slot_id"), str)
                and bool(str(item["slot_id"]).strip())
                for item in raw_items
            )
            trajectory_slot_ids = (
                tuple(str(item["slot_id"]).strip() for item in raw_items)
                if valid_item_identities
                else ()
            )
            if (
                trajectory_slot_ids != expected_slot_ids
                or len(raw_items) != len(expected_slot_ids)
            ):
                raise ReviewArtifactUnavailableError(
                    f"Candidate trajectory {trajectory_id} does not cover its "
                    "Group exactly"
                )
            projected_items: list[Mapping[str, Any]] = []
            prior_end = -1.0
            candidate_ids: list[str] = []
            for clip, raw in zip(clips, raw_items, strict=True):
                if not isinstance(raw, Mapping):
                    raise ReviewArtifactUnavailableError(
                        f"Candidate trajectory {trajectory_id} contains an invalid clip"
                    )
                slot_id = str(clip["slot_id"])
                candidate_id = _required_identity(
                    raw,
                    "candidate_id",
                    f"Candidate trajectory {trajectory_id} Slot {slot_id}",
                )
                if candidate_id in candidate_ids:
                    raise ReviewArtifactUnavailableError(
                        f"Candidate trajectory {trajectory_id} contains duplicate "
                        f"Candidate {candidate_id}"
                    )
                try:
                    source_start, source_end = parse_range(str(raw["timestamp"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ReviewArtifactUnavailableError(
                        f"Candidate {candidate_id} has an invalid source range"
                    ) from exc
                if source_start < prior_end:
                    raise ReviewArtifactUnavailableError(
                        f"Candidate trajectory {trajectory_id} overlaps inside "
                        "its Group"
                    )
                if segment_bounds is not None:
                    start_ms = round(source_start * 1000)
                    end_ms = round(source_end * 1000)
                    if start_ms < segment_bounds[1] or end_ms > segment_bounds[2]:
                        raise ReviewArtifactUnavailableError(
                            f"Candidate {candidate_id} escapes Planning Segment "
                            f"{planning_segment_id}"
                        )
                prior_end = source_end
                candidate_ids.append(candidate_id)
                projected_items.append(
                    MappingProxyType(
                        {
                            "candidate_id": candidate_id,
                            "slot_id": slot_id,
                            "source_start_sec": source_start,
                            "source_end_sec": source_end,
                            "source_timestamp": str(raw["timestamp"]),
                            "description": str(
                                raw.get("structured_context")
                                or raw.get("description")
                                or clip.get("picture")
                                or ""
                            ),
                            "semantic_relevance": _optional_number(
                                raw.get("semantic_relevance")
                            ),
                            "visual_score": _optional_number(
                                raw.get("visual_slot_relevance_likert")
                                or raw.get("visual_slot_relevance")
                            ),
                            "protagonist_visibility_score": _optional_number(
                                raw.get("protagonist_visibility_likert")
                            ),
                            "emotional_intensity": _optional_number(
                                raw.get("emotional_intensity")
                            ),
                            "kinetic_energy": _optional_number(
                                raw.get("kinetic_energy")
                            ),
                            "salience": _optional_number(raw.get("salience")),
                            "visual_evidence": (
                                None
                                if raw.get("visual_evidence") is None
                                else str(raw["visual_evidence"])
                            ),
                        }
                    )
                )
            selected = trajectory_id == selected_trajectory_id
            if selected and tuple(candidate_ids) != expected_candidate_ids:
                raise ReviewArtifactUnavailableError(
                    f"Selected trajectory {trajectory_id} does not match the RenderPlan"
                )
            trajectories.append(
                MappingProxyType(
                    {
                        "trajectory_id": trajectory_id,
                        "group_id": group_id,
                        "planning_segment_id": planning_segment_id,
                        "items": tuple(projected_items),
                        "selected": selected,
                        "eligible_for_replacement": not selected,
                    }
                )
            )
        if selected_trajectory_id not in seen_trajectory_ids:
            raise ReviewArtifactUnavailableError(
                f"Candidate trajectories for {group_id} omit the selected trajectory"
            )
        projected[group_id] = tuple(
            sorted(
                trajectories,
                key=lambda item: (
                    not bool(item["selected"]),
                    str(item["trajectory_id"]),
                ),
            )
        )
    return MappingProxyType(projected)


def _planning_contract(
    replaceable_groups: Mapping[str, tuple[Mapping[str, Any], ...]],
    planning_segments: Sequence[Mapping[str, Any]] | None,
    planning_groups: Sequence[Mapping[str, Any]] | None,
) -> dict[str, tuple[str, int, int]]:
    """Validate the Story-to-Timeline boundary used by new Review bundles."""

    if planning_segments is None and planning_groups is None:
        # Completed schema-2 bundles remain viewable. New schema-3 bundles always
        # provide both artifacts and therefore take the strict branch below.
        return {}
    if planning_segments is None or planning_groups is None:
        raise ReviewArtifactUnavailableError(
            "Review bundle has an incomplete Planning Segment contract"
        )
    if isinstance(planning_segments, (str, bytes)) or isinstance(
        planning_groups, (str, bytes)
    ):
        raise ReviewArtifactUnavailableError(
            "Review bundle Planning Segment artifacts must be arrays"
        )

    segments: dict[str, tuple[str, int, int]] = {}
    for raw_segment in planning_segments:
        if not isinstance(raw_segment, Mapping):
            raise ReviewArtifactUnavailableError(
                "Planning Segments contain an invalid item"
            )
        planning_segment_id = _required_identity(
            raw_segment,
            "planning_segment_id",
            "Planning Segment",
        )
        if planning_segment_id in segments:
            raise ReviewArtifactUnavailableError(
                f"Planning Segments contain duplicate {planning_segment_id}"
            )
        source_segment_id = _required_identity(
            raw_segment,
            "source_segment_id",
            f"Planning Segment {planning_segment_id}",
        )
        start_ms = raw_segment.get("start_ms")
        end_ms = raw_segment.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise ReviewArtifactUnavailableError(
                f"Planning Segment {planning_segment_id} has invalid boundaries"
            )
        segments[planning_segment_id] = (source_segment_id, start_ms, end_ms)

    contracts: dict[str, tuple[str, int, int]] = {}
    for raw_group in planning_groups:
        if not isinstance(raw_group, Mapping):
            raise ReviewArtifactUnavailableError(
                "Planning Groups contain an invalid item"
            )
        group_id = _required_identity(raw_group, "group_id", "Planning Group")
        if group_id in contracts:
            raise ReviewArtifactUnavailableError(
                f"Planning Groups contain duplicate {group_id}"
            )
        clips = replaceable_groups.get(group_id)
        if clips is None:
            raise ReviewArtifactUnavailableError(
                f"Planning Group {group_id} does not match the RenderPlan"
            )
        raw_slot_ids = raw_group.get("slot_ids")
        if not isinstance(raw_slot_ids, Sequence) or isinstance(
            raw_slot_ids, (str, bytes)
        ):
            raise ReviewArtifactUnavailableError(
                f"Planning Group {group_id} has no Slot sequence"
            )
        slot_ids = tuple(
            value.strip() if isinstance(value, str) else ""
            for value in raw_slot_ids
        )
        expected_slot_ids = tuple(str(clip["slot_id"]) for clip in clips)
        if not all(slot_ids) or slot_ids != expected_slot_ids:
            raise ReviewArtifactUnavailableError(
                f"Planning Group {group_id} does not cover its Slots exactly"
            )
        planning_segment_id = _required_identity(
            raw_group,
            "planning_segment_id",
            f"Planning Group {group_id}",
        )
        segment = segments.get(planning_segment_id)
        if segment is None:
            raise ReviewArtifactUnavailableError(
                f"Planning Group {group_id} references an unknown Planning Segment"
            )
        source_segment_id = _required_identity(
            raw_group,
            "source_segment_id",
            f"Planning Group {group_id}",
        )
        if source_segment_id != segment[0]:
            raise ReviewArtifactUnavailableError(
                f"Planning Group {group_id} and its Planning Segment disagree"
            )
        contracts[group_id] = (planning_segment_id, segment[1], segment[2])

    if set(contracts) != set(replaceable_groups):
        raise ReviewArtifactUnavailableError(
            "Planning Group identities do not match the RenderPlan"
        )
    return contracts


def project_dialogue_cues(plan: RenderPlan) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    for clip in plan.clips:
        anchor = clip.get("dialogue_anchor")
        if not isinstance(anchor, Mapping):
            continue
        frame_start, frame_end = (int(value) for value in clip["output_frame_range"])
        output_start = float(clip.get("output_start_sec", frame_start / plan.fps))
        output_end = float(clip.get("output_end_sec", frame_end / plan.fps))
        speakers = anchor.get("speakers")
        speaker = (
            ", ".join(str(value) for value in speakers)
            if isinstance(speakers, Sequence) and not isinstance(speakers, (str, bytes))
            else None
        )
        result.append(
            MappingProxyType(
                {
                    "slot_id": str(clip["slot_id"]),
                    "start_sec": float(
                        anchor.get("output_audio_start_sec", output_start)
                    ),
                    "end_sec": float(anchor.get("output_audio_end_sec", output_end)),
                    "text": str(anchor.get("text") or ""),
                    "speaker": speaker,
                }
            )
        )
    return tuple(result)


def replacement_clip(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    trajectory_id: str,
) -> dict[str, Any]:
    """Apply one item from a validated Group trajectory without changing rhythm."""

    updated = dict(original)
    updated["candidate_id"] = str(candidate["candidate_id"])
    updated["trajectory_id"] = trajectory_id
    updated["timestamp"] = str(candidate["timestamp"])
    updated["selection_scores"] = {
        key: candidate[key]
        for key in (
            "semantic_relevance",
            "visual_slot_relevance_likert",
            "protagonist_visibility_likert",
            "emotional_intensity",
            "kinetic_energy",
            "salience",
        )
        if candidate.get(key) is not None
    }
    updated["cut_optimization"] = {
        "mode": "guided_revision_candidate",
        "source_shift_sec": 0.0,
    }
    updated.pop("dialogue_anchor", None)
    return updated


def _plan_groups(plan: RenderPlan) -> dict[str, tuple[Mapping[str, Any], ...]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    closed: set[str] = set()
    current_group_id: str | None = None
    seen_slots: set[str] = set()
    for clip in plan.clips:
        slot_id = _required_identity(clip, "slot_id", "RenderPlan clip")
        if slot_id in seen_slots:
            raise ReviewArtifactUnavailableError(
                f"RenderPlan contains duplicate Slot {slot_id}"
            )
        seen_slots.add(slot_id)
        group_id = _required_identity(clip, "group_id", f"RenderPlan Slot {slot_id}")
        _required_identity(clip, "candidate_id", f"RenderPlan Slot {slot_id}")
        _required_identity(clip, "trajectory_id", f"RenderPlan Slot {slot_id}")
        if current_group_id != group_id:
            if current_group_id is not None:
                closed.add(current_group_id)
            if group_id in closed:
                raise ReviewArtifactUnavailableError(
                    f"RenderPlan Group {group_id} is not contiguous"
                )
            current_group_id = group_id
        groups.setdefault(group_id, []).append(clip)
    for group_id, clips in groups.items():
        anchor_count = sum(
            isinstance(clip.get("dialogue_anchor"), Mapping) for clip in clips
        )
        if anchor_count and (anchor_count != 1 or len(clips) != 1):
            raise ReviewArtifactUnavailableError(
                f"Story Anchor Group {group_id} must contain exactly one Slot"
            )
    return {group_id: tuple(clips) for group_id, clips in groups.items()}


def _required_identity(value: Mapping[str, Any], key: str, label: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ReviewArtifactUnavailableError(f"{label} has no {key}")
    return raw.strip()


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def artifact_manifest_entry(path: Path) -> dict[str, str]:
    return {"path": path.name, "sha256": _sha256(path)}


def _read_json(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ReviewArtifactUnavailableError(f"{label} is unavailable")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewArtifactUnavailableError(f"{label} is unreadable") from exc


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    value = _read_json(path, label)
    if not isinstance(value, dict):
        raise ReviewArtifactUnavailableError(f"{label} must contain an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = [
    "REVIEW_BUNDLE_FILENAME",
    "REVIEW_BUNDLE_SCHEMA_VERSION",
    "artifact_manifest_entry",
    "load_render_plan",
    "load_review_bundle",
    "project_candidates",
    "project_dialogue_cues",
    "project_plan_summary",
    "project_slots",
    "replacement_clip",
    "resolve_managed_review_file",
    "write_json_atomic",
]
