import hashlib
import json

import pytest

from cutmaster.configuration.schema import (
    ASRConfig,
    LLMConfig,
    SceneSegmentationConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.workflow.analyser.tools.cache import analysis_signature


def _signature(subtitle_path):
    return analysis_signature(
        "a" * 64,
        "Portable title",
        subtitle_path,
        ShotDetectionConfig(),
        ASRConfig(backend="bailian", api_key="test"),
        SceneSegmentationConfig(),
        ShotAnnotationConfig(),
        LLMConfig(model="llm", base_url="", api_key="test"),
        VLMConfig(model="vlm", base_url="", api_key="test"),
    )


def test_analysis_signature_uses_content_identities_not_paths(tmp_path) -> None:
    first = tmp_path / "first.srt"
    second = tmp_path / "nested" / "second.srt"
    second.parent.mkdir()
    first.write_bytes(b"same subtitle bytes")
    second.write_bytes(first.read_bytes())

    first_signature = _signature(first)
    second_signature = _signature(second)

    assert first_signature["schema_version"] == "3.0"
    assert first_signature["source"] == {"sha256": "a" * 64}
    assert first_signature["subtitle"] == {
        "sha256": hashlib.sha256(first.read_bytes()).hexdigest()
    }
    assert first_signature == second_signature
    serialized = json.dumps(first_signature)
    assert str(tmp_path) not in serialized
    assert "mtime" not in serialized
    assert '"path"' not in serialized


@pytest.mark.parametrize("fingerprint", ["A" * 64, "a" * 63, "not-a-digest"])
def test_analysis_signature_rejects_noncanonical_source_fingerprint(
    fingerprint,
) -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        analysis_signature(
            fingerprint,
            "Title",
            None,
            ShotDetectionConfig(),
            ASRConfig(backend="bailian", api_key="test"),
            SceneSegmentationConfig(),
            ShotAnnotationConfig(),
            LLMConfig(model="llm", base_url="", api_key="test"),
            VLMConfig(model="vlm", base_url="", api_key="test"),
        )


def test_analysis_signature_requires_explicit_material_title() -> None:
    with pytest.raises(ValueError, match="video_title"):
        analysis_signature(
            "a" * 64,
            "  ",
            None,
            ShotDetectionConfig(),
            ASRConfig(backend="bailian", api_key="test"),
            SceneSegmentationConfig(),
            ShotAnnotationConfig(),
            LLMConfig(model="llm", base_url="", api_key="test"),
            VLMConfig(model="vlm", base_url="", api_key="test"),
        )
