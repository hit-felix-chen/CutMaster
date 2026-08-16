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


REVIEW_BUNDLE_SCHEMA_VERSION = "1.0"
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReviewArtifactUnavailableError(
            "Frozen Edit RenderPlan is unreadable or unsupported"
        ) from exc


def load_review_bundle(plan_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a committed Review bundle beside a Run's initial RenderPlan.

    The manifest is the commit point. Historical Runs created before bundle
    persistence intentionally return ``not_persisted`` instead of pretending
    that their selected clips are a complete Candidate Space.
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
        return None, "not_persisted"
    manifest = _read_json_object(manifest_path, "Review bundle manifest")
    if manifest.get("schema_version") != REVIEW_BUNDLE_SCHEMA_VERSION:
        raise ReviewArtifactUnavailableError("Unsupported Review bundle schema")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, Mapping):
        raise ReviewArtifactUnavailableError("Review bundle has no artifact map")
    loaded: dict[str, Any] = {}
    for logical_name in (
        "candidate_pool",
        "edit_plan",
        "music_profile",
        "selection_diagnostics",
    ):
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
    if not isinstance(loaded["candidate_pool"], dict):
        raise ReviewArtifactUnavailableError("Candidate Space must be an object")
    return loaded, None


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
                    "position": position,
                    "is_anchor": isinstance(anchor, Mapping),
                    "output_start_sec": output_start,
                    "output_end_sec": output_end,
                    "source_start_sec": source_start,
                    "source_end_sec": source_end,
                    "source_timestamp": str(clip["timestamp"]),
                    "selected_candidate_id": str(clip["candidate_id"]),
                    "picture": str(clip.get("picture") or ""),
                    "selection_scores": MappingProxyType(
                        dict(clip.get("selection_scores") or {})
                    ),
                    "dialogue_anchor": (
                        None if not isinstance(anchor, Mapping) else MappingProxyType(dict(anchor))
                    ),
                }
            )
        )
    return tuple(result)


def project_candidates(
    plan: RenderPlan,
    candidate_pool: Mapping[str, Any] | None,
) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    clips_by_slot = {str(clip["slot_id"]): clip for clip in plan.clips}
    projected: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for slot_id, clip in clips_by_slot.items():
        anchor = isinstance(clip.get("dialogue_anchor"), Mapping)
        raw_items = (
            candidate_pool.get(slot_id, [])
            if isinstance(candidate_pool, Mapping)
            else []
        )
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise ReviewArtifactUnavailableError(
                f"Candidate Space for {slot_id} must be an array"
            )
        by_id: dict[str, dict[str, Any]] = {}
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise ReviewArtifactUnavailableError(
                    f"Candidate Space for {slot_id} contains an invalid item"
                )
            candidate_id = str(raw.get("candidate_id") or "")
            if not candidate_id or str(raw.get("slot_id") or slot_id) != slot_id:
                raise ReviewArtifactUnavailableError(
                    f"Candidate Space identity mismatch for {slot_id}"
                )
            by_id[candidate_id] = dict(raw)
        selected_id = str(clip["candidate_id"])
        selected_raw = by_id.setdefault(selected_id, {})
        selected_raw.setdefault("candidate_id", selected_id)
        selected_raw.setdefault("slot_id", slot_id)
        selected_raw.setdefault("timestamp", str(clip["timestamp"]))
        selected_raw.setdefault("description", str(clip.get("picture") or ""))
        scores = clip.get("selection_scores")
        if isinstance(scores, Mapping):
            for key in (
                "semantic_relevance",
                "visual_slot_relevance_likert",
                "protagonist_visibility_likert",
                "emotional_intensity",
                "kinetic_energy",
                "salience",
            ):
                if key in scores:
                    selected_raw.setdefault(key, scores[key])

        items: list[Mapping[str, Any]] = []
        for candidate_id, raw in by_id.items():
            try:
                source_start, source_end = parse_range(str(raw["timestamp"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ReviewArtifactUnavailableError(
                    f"Candidate {candidate_id} has an invalid source range"
                ) from exc
            selected = candidate_id == selected_id
            items.append(
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
                        "kinetic_energy": _optional_number(raw.get("kinetic_energy")),
                        "salience": _optional_number(raw.get("salience")),
                        "visual_evidence": (
                            None
                            if raw.get("visual_evidence") is None
                            else str(raw["visual_evidence"])
                        ),
                        "selected": selected,
                        "eligible_for_replacement": (
                            candidate_pool is not None and not anchor and not selected
                        ),
                    }
                )
            )
        projected[slot_id] = tuple(
            sorted(items, key=lambda item: (not bool(item["selected"]), str(item["candidate_id"])))
        )
    return MappingProxyType(projected)


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
                    "end_sec": float(
                        anchor.get("output_audio_end_sec", output_end)
                    ),
                    "text": str(anchor.get("text") or ""),
                    "speaker": speaker,
                }
            )
        )
    return tuple(result)


def replacement_clip(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a user-selected validated candidate without changing edit rhythm."""

    updated = dict(original)
    updated["candidate_id"] = str(candidate["candidate_id"])
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
