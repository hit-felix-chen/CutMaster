"""Versioned logical-key registry and safe Direct Bundle path handling."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from uuid import uuid4

from cutmaster.domain.artifacts import validate_portable_relative_file_path


ARTIFACT_MANIFEST_VERSION = "1.0"
_LOGICAL_KEY_PATTERN = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

REQUIRED_ARTIFACTS: Mapping[str, str] = MappingProxyType(
    {
        "workflow.result": "result.json",
        "workflow.model_usage": "model_usage.json",
        "workflow.log": "cutmaster.log",
        "analyser.video_result": "analyser/analysis_result.json",
        "analyser.music_result": "analyser/music/music_analysis_result.json",
        "planners.result": "planners/planners_result.json",
        "planners.render_plan": "planners/render_plan.json",
        "renderer.result": "renderer/render_result.json",
        "renderer.output_video": "renderer/output.mp4",
    }
)

OPTIONAL_ARTIFACTS: Mapping[str, str] = MappingProxyType(
    {
        "analyser.source_subtitle": "analyser/source.srt",
        "analyser.dialogue_subtitle": "analyser/dialogue_merged.srt",
        "analyser.dialogues": "analyser/dialogues.json",
        "analyser.music_memory": "analyser/music/music_memory.json",
        "planners.music_profile": "planners/music_profile.json",
        "planners.edit_plan": "planners/edit_plan.json",
        "planners.dialogue_anchors": "planners/dialogue_anchors.json",
        "planners.candidate_pool": "planners/candidate_pool.json",
        "planners.raw_script": "planners/script_raw.json",
        "planners.selection_diagnostics": (
            "planners/diagnostics/selection_diagnostics.json"
        ),
        "planners.history": "planners/diagnostics/planners_history.json",
        "planners.calls": "planners/diagnostics/planners_calls.json",
        "planners.model_usage": "planners/diagnostics/model_usage.json",
    }
)


@dataclass(frozen=True)
class DirectArtifactLayout:
    """Canonical compatibility layout for one direct invocation."""

    root: Path
    managed: bool

    @classmethod
    def allocate(
        cls,
        data_root: Path,
        output_dir: Path | None,
    ) -> "DirectArtifactLayout":
        canonical_data_root = data_root.resolve()
        if output_dir is None:
            root = canonical_data_root / "direct" / f"bundle_{uuid4()}"
            managed = True
        else:
            root = output_dir.expanduser().resolve()
            managed = False
            try:
                root.relative_to(canonical_data_root)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "An explicit output directory must be outside the "
                    "Application Data Root"
                )
        if root.exists() and not root.is_dir():
            raise ValueError(f"Output target must be a directory: {root}")
        layout = cls(root=root, managed=managed)
        for directory in (
            layout.root,
            layout.analyser_dir,
            layout.music_analyser_dir,
            layout.planners_dir,
            layout.planners_diagnostics_dir,
            layout.renderer_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return layout

    @property
    def analyser_dir(self) -> Path:
        return self.root / "analyser"

    @property
    def music_analyser_dir(self) -> Path:
        return self.analyser_dir / "music"

    @property
    def planners_dir(self) -> Path:
        return self.root / "planners"

    @property
    def planners_diagnostics_dir(self) -> Path:
        return self.planners_dir / "diagnostics"

    @property
    def renderer_dir(self) -> Path:
        return self.root / "renderer"

    @property
    def analysis_result(self) -> Path:
        return self.analyser_dir / "analysis_result.json"

    @property
    def music_analysis_result(self) -> Path:
        return self.music_analyser_dir / "music_analysis_result.json"

    @property
    def planners_result(self) -> Path:
        return self.planners_dir / "planners_result.json"

    @property
    def render_plan(self) -> Path:
        return self.planners_dir / "render_plan.json"

    @property
    def render_result(self) -> Path:
        return self.renderer_dir / "render_result.json"

    @property
    def output_video(self) -> Path:
        return self.renderer_dir / "output.mp4"

    @property
    def workflow_result(self) -> Path:
        return self.root / "result.json"

    @property
    def workflow_model_usage(self) -> Path:
        return self.root / "model_usage.json"

    @property
    def workflow_log(self) -> Path:
        return self.root / "cutmaster.log"


def build_artifact_manifest(layout: DirectArtifactLayout) -> dict[str, str]:
    """Build and validate a manifest from artifacts emitted by this invocation."""

    artifacts = dict(REQUIRED_ARTIFACTS)
    for logical_key, relative_path in OPTIONAL_ARTIFACTS.items():
        candidate = layout.root.joinpath(*PurePosixPath(relative_path).parts)
        if candidate.is_file():
            artifacts[logical_key] = relative_path
    normalized = validate_artifact_manifest(ARTIFACT_MANIFEST_VERSION, artifacts)
    for logical_key, relative_path in normalized.items():
        if logical_key == "workflow.result":
            continue
        resolve_artifact_path(layout.root, relative_path)
    return normalized


def publish_result(path: Path, result: object) -> Path:
    """Publish the successful result atomically as the bundle's final artifact."""

    if not hasattr(result, "to_dict"):
        raise TypeError("Direct result must expose to_dict()")
    payload = result.to_dict()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def normalize_artifact_path(value: str) -> str:
    """Validate and return one normalized bundle-relative POSIX file path."""

    return validate_portable_relative_file_path(value)


def validate_artifact_manifest(
    artifact_manifest_version: str,
    artifacts: Mapping[str, str],
    *,
    require_v1_keys: bool = True,
) -> dict[str, str]:
    """Validate logical keys and paths without touching the filesystem."""

    if not isinstance(artifact_manifest_version, str):
        raise TypeError("Artifact Manifest version must be a string")
    if not isinstance(artifacts, Mapping):
        raise TypeError("Artifact Manifest artifacts must be a mapping")
    if not isinstance(require_v1_keys, bool):
        raise TypeError("require_v1_keys must be a boolean")
    version_match = _VERSION_PATTERN.fullmatch(artifact_manifest_version)
    if version_match is None or version_match.group(1) != "1":
        raise ValueError(
            f"Unsupported Artifact Manifest version: {artifact_manifest_version!r}"
        )
    normalized: dict[str, str] = {}
    for logical_key, relative_path in artifacts.items():
        if not isinstance(logical_key, str) or not _LOGICAL_KEY_PATTERN.fullmatch(
            logical_key
        ):
            raise ValueError(f"Invalid Artifact Manifest logical key: {logical_key!r}")
        normalized[logical_key] = normalize_artifact_path(relative_path)
    if require_v1_keys:
        missing = sorted(set(REQUIRED_ARTIFACTS) - set(normalized))
        if missing:
            raise ValueError(f"Artifact Manifest is missing required keys: {missing}")
    if normalized.get("workflow.result") not in {None, "result.json"}:
        raise ValueError("workflow.result must refer to result.json")
    return normalized


def resolve_artifact_path(
    bundle_root: Path,
    relative_path: str,
    *,
    require_file: bool = True,
) -> Path:
    """Resolve a validated path and reject symlink escapes from the bundle."""

    if not isinstance(bundle_root, Path):
        raise TypeError("Bundle root must be a pathlib.Path")
    if not isinstance(require_file, bool):
        raise TypeError("require_file must be a boolean")
    normalized = normalize_artifact_path(relative_path)
    canonical_root = bundle_root.resolve(strict=True)
    if not canonical_root.is_dir():
        raise ValueError("Direct Workflow Bundle root must be a directory")
    candidate = canonical_root.joinpath(*PurePosixPath(normalized).parts)
    try:
        resolved = candidate.resolve(strict=require_file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Artifact file does not exist: {relative_path}"
        ) from exc
    try:
        resolved.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError("Artifact path escapes the Direct Workflow Bundle") from exc
    if resolved.exists() and not resolved.is_file():
        raise ValueError("Artifact path must identify an existing file")
    return resolved


__all__ = [
    "ARTIFACT_MANIFEST_VERSION",
    "DirectArtifactLayout",
    "OPTIONAL_ARTIFACTS",
    "REQUIRED_ARTIFACTS",
    "build_artifact_manifest",
    "normalize_artifact_path",
    "publish_result",
    "resolve_artifact_path",
    "validate_artifact_manifest",
]
