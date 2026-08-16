from __future__ import annotations

from dataclasses import replace

import pytest

from cutmaster.application.renders import (
    CreateRenderVariantCommand,
    RenderSpecification,
)
from cutmaster.configuration.schema import DialogueAudioConfig, RendererConfig
from cutmaster.domain.ids import FrozenEditId


def renderer_config() -> RendererConfig:
    return RendererConfig(
        width=1280,
        height=720,
        fps=24,
        encoder="libx264",
        threads=3,
        bgm_volume=0.25,
        original_volume=0.0,
        audio_sample_rate=48000,
        dialogue_audio=DialogueAudioConfig(separator_device="cpu"),
    )


def test_render_specification_is_strict_versioned_and_round_trips() -> None:
    specification = RenderSpecification.create("dialogue", renderer_config())

    assert RenderSpecification.from_dict(specification.to_dict()) == specification
    assert specification.to_dict()["schema_version"] == "1.0"
    assert specification.to_dict()["renderer"] == {
        "width": 1280,
        "height": 720,
        "fps": 24,
        "encoder": "libx264",
        "threads": 3,
        "bgm_volume": 0.25,
        "original_volume": 0.0,
        "audio_sample_rate": 48000,
        "dialogue_audio": {
            "enable_vocal_separation": True,
            "separator_model": "htdemucs",
            "separator_device": "cpu",
            "separator_segment_sec": 7,
            "separator_shifts": 0,
            "separator_padding_sec": 1.0,
            "separated_loudness_lufs": -16.0,
            "dialogue_volume": 1.0,
            "bgm_duck_factor": 0.5,
            "fade_sec": 0.3,
        },
    }


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda value: value.update({"future": True}), ValueError),
        (lambda value: value.update({1: "not-a-field"}), TypeError),
        (lambda value: value.update({"schema_version": 1}), TypeError),
        (lambda value: value.update({"schema_version": "1"}), ValueError),
        (lambda value: value["renderer"].update({"fps": True}), TypeError),
        (lambda value: value["renderer"].update({1: "not-a-field"}), TypeError),
        (lambda value: value["renderer"].update({"encoder": "auto"}), ValueError),
        (lambda value: value["renderer"].update({"width": 0}), ValueError),
    ],
)
def test_render_specification_rejects_unknown_or_coercible_values(
    mutation,
    error,
) -> None:
    value = RenderSpecification.create("bgm_only", renderer_config()).to_dict()
    mutation(value)

    with pytest.raises(error):
        RenderSpecification.from_dict(value)


def test_render_specification_rejects_nonzero_source_audio() -> None:
    with pytest.raises(ValueError, match="original_volume"):
        RenderSpecification.create(
            "dialogue",
            replace(renderer_config(), original_volume=0.1),
        )


def test_create_render_variant_command_does_not_accept_legacy_mapping() -> None:
    with pytest.raises(TypeError, match="audio_mode must be a string"):
        CreateRenderVariantCommand(
            "command",
            FrozenEditId.new(),
            {"audio_mode": "dialogue"},  # type: ignore[arg-type]
        )
