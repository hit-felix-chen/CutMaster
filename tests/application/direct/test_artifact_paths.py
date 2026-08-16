from __future__ import annotations

import os
from pathlib import Path

import pytest

from cutmaster.application.direct.artifacts import (
    ARTIFACT_MANIFEST_VERSION,
    OPTIONAL_ARTIFACTS,
    REQUIRED_ARTIFACTS,
    normalize_artifact_path,
    resolve_artifact_path,
    validate_artifact_manifest,
)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute/result.json",
        "folder\\result.json",
        "folder/../result.json",
        "folder/./result.json",
        "folder//result.json",
        "folder/",
        "folder/result\n.json",
        "folder/result\x00.json",
    ],
)
def test_normalize_artifact_path_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_artifact_path(value)


def test_manifest_registry_matches_the_version_one_contract() -> None:
    artifacts = dict(REQUIRED_ARTIFACTS)
    artifacts.update(OPTIONAL_ARTIFACTS)

    assert validate_artifact_manifest(ARTIFACT_MANIFEST_VERSION, artifacts) == artifacts


def test_manifest_requires_every_success_key() -> None:
    artifacts = dict(REQUIRED_ARTIFACTS)
    del artifacts["renderer.output_video"]

    with pytest.raises(ValueError, match="renderer.output_video"):
        validate_artifact_manifest(ARTIFACT_MANIFEST_VERSION, artifacts)


def test_manifest_accepts_future_valid_logical_keys() -> None:
    artifacts = dict(REQUIRED_ARTIFACTS)
    artifacts["renderer.preview_video"] = "renderer/preview.mp4"

    assert validate_artifact_manifest("1.1", artifacts)[
        "renderer.preview_video"
    ] == "renderer/preview.mp4"


@pytest.mark.parametrize("version", ["2.0", "01.0", "1.00", "1.beta"])
def test_manifest_rejects_an_unsupported_or_malformed_version(version: str) -> None:
    with pytest.raises(ValueError, match="Unsupported Artifact Manifest version"):
        validate_artifact_manifest(version, REQUIRED_ARTIFACTS)


def test_safe_resolution_rejects_a_symlink_escape(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    bundle.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, bundle / "escaped")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    with pytest.raises(ValueError, match="escapes"):
        resolve_artifact_path(bundle, "escaped/secret.txt")


def test_safe_resolution_returns_an_existing_bundle_file(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    artifact = bundle / "renderer" / "output.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"video")

    assert resolve_artifact_path(bundle, "renderer/output.mp4") == artifact


def test_safe_resolution_rejects_an_existing_directory(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    directory = bundle / "renderer" / "output.mp4"
    directory.mkdir(parents=True)

    with pytest.raises(ValueError, match="existing file"):
        resolve_artifact_path(
            bundle,
            "renderer/output.mp4",
            require_file=False,
        )
