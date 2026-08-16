"""Strict, portable snapshots for deterministic managed rendering."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, cast

from cutmaster.configuration.schema import DialogueAudioConfig, RendererConfig
from cutmaster.workflow.contracts.rendering import AudioMode


RENDER_SPECIFICATION_SCHEMA_VERSION = "1.0"


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} field names must be strings")
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise ValueError(f"{label} has invalid fields: {', '.join(details)}")


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return result


def _text(value: Any, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    result = value.strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{label} must not contain control characters")
    return result


@dataclass(frozen=True)
class DialogueAudioSettingsSnapshot:
    enable_vocal_separation: bool
    separator_model: str
    separator_device: str
    separator_segment_sec: int
    separator_shifts: int
    separator_padding_sec: float
    separated_loudness_lufs: float
    dialogue_volume: float
    bgm_duck_factor: float
    fade_sec: float

    def __post_init__(self) -> None:
        if not isinstance(self.enable_vocal_separation, bool):
            raise TypeError("enable_vocal_separation must be a boolean")
        object.__setattr__(
            self,
            "separator_model",
            _text(self.separator_model, "separator_model"),
        )
        device = _text(self.separator_device, "separator_device", maximum=20).lower()
        if device not in {"cpu", "mps", "cuda"}:
            raise ValueError("separator_device must be concrete: cpu, mps, or cuda")
        object.__setattr__(self, "separator_device", device)
        _integer(self.separator_segment_sec, "separator_segment_sec", 1, 3600)
        _integer(self.separator_shifts, "separator_shifts", 0, 100)
        object.__setattr__(
            self,
            "separator_padding_sec",
            _number(self.separator_padding_sec, "separator_padding_sec", 0.0, 60.0),
        )
        object.__setattr__(
            self,
            "separated_loudness_lufs",
            _number(
                self.separated_loudness_lufs,
                "separated_loudness_lufs",
                -100.0,
                0.0,
            ),
        )
        object.__setattr__(
            self,
            "dialogue_volume",
            _number(self.dialogue_volume, "dialogue_volume", 0.0, 10.0),
        )
        object.__setattr__(
            self,
            "bgm_duck_factor",
            _number(self.bgm_duck_factor, "bgm_duck_factor", 0.0, 1.0),
        )
        object.__setattr__(
            self,
            "fade_sec",
            _number(self.fade_sec, "fade_sec", 0.0, 60.0),
        )

    @classmethod
    def from_config(cls, value: DialogueAudioConfig) -> DialogueAudioSettingsSnapshot:
        from cutmaster.workflow.renderer.dialogue_audio import resolve_separator_device

        return cls(
            enable_vocal_separation=value.enable_vocal_separation,
            separator_model=value.separator_model,
            separator_device=resolve_separator_device(value.separator_device),
            separator_segment_sec=value.separator_segment_sec,
            separator_shifts=value.separator_shifts,
            separator_padding_sec=value.separator_padding_sec,
            separated_loudness_lufs=value.separated_loudness_lufs,
            dialogue_volume=value.dialogue_volume,
            bgm_duck_factor=value.bgm_duck_factor,
            fade_sec=value.fade_sec,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DialogueAudioSettingsSnapshot:
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "dialogue_audio")
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})

    def to_config(self) -> DialogueAudioConfig:
        return DialogueAudioConfig(**asdict(self))


@dataclass(frozen=True)
class RendererSettingsSnapshot:
    width: int
    height: int
    fps: int
    encoder: str
    threads: int
    bgm_volume: float
    original_volume: float
    audio_sample_rate: int
    dialogue_audio: DialogueAudioSettingsSnapshot

    def __post_init__(self) -> None:
        _integer(self.width, "width", 16, 16384)
        _integer(self.height, "height", 16, 16384)
        _integer(self.fps, "fps", 1, 240)
        encoder = _text(self.encoder, "encoder").lower()
        if encoder == "auto":
            raise ValueError("encoder must be materialized and cannot be auto")
        object.__setattr__(self, "encoder", encoder)
        _integer(self.threads, "threads", 1, 512)
        object.__setattr__(
            self,
            "bgm_volume",
            _number(self.bgm_volume, "bgm_volume", 0.0, 10.0),
        )
        original = _number(self.original_volume, "original_volume", 0.0, 10.0)
        if original != 0.0:
            raise ValueError("original_volume must be 0 for frame-exact rendering")
        object.__setattr__(self, "original_volume", original)
        _integer(self.audio_sample_rate, "audio_sample_rate", 8000, 384000)
        if not isinstance(self.dialogue_audio, DialogueAudioSettingsSnapshot):
            raise TypeError("dialogue_audio must be a strict settings snapshot")

    @classmethod
    def from_config(cls, value: RendererConfig) -> RendererSettingsSnapshot:
        from cutmaster.workflow.renderer.ffmpeg import select_encoder

        return cls(
            width=value.width,
            height=value.height,
            fps=value.fps,
            encoder=select_encoder(value.encoder),
            threads=value.threads,
            bgm_volume=value.bgm_volume,
            original_volume=value.original_volume,
            audio_sample_rate=value.audio_sample_rate,
            dialogue_audio=DialogueAudioSettingsSnapshot.from_config(
                value.dialogue_audio
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RendererSettingsSnapshot:
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "renderer")
        dialogue = value["dialogue_audio"]
        if not isinstance(dialogue, Mapping):
            raise TypeError("renderer.dialogue_audio must be an object")
        return cls(
            **{
                name: value[name]
                for name in cls.__dataclass_fields__
                if name != "dialogue_audio"
            },
            dialogue_audio=DialogueAudioSettingsSnapshot.from_dict(dialogue),
        )

    def to_config(self) -> RendererConfig:
        values = asdict(self)
        values["dialogue_audio"] = self.dialogue_audio.to_config()
        return RendererConfig(**values)


@dataclass(frozen=True)
class RenderSpecification:
    schema_version: str
    audio_mode: AudioMode
    renderer: RendererSettingsSnapshot

    def __post_init__(self) -> None:
        if self.schema_version != RENDER_SPECIFICATION_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported Render Specification schema: {self.schema_version!r}"
            )
        if self.audio_mode not in {"dialogue", "bgm_only"}:
            raise ValueError(f"Unsupported audio_mode: {self.audio_mode!r}")
        if not isinstance(self.renderer, RendererSettingsSnapshot):
            raise TypeError("renderer must be a strict settings snapshot")

    @classmethod
    def create(
        cls,
        audio_mode: AudioMode,
        renderer: RendererConfig,
    ) -> RenderSpecification:
        return cls(
            schema_version=RENDER_SPECIFICATION_SCHEMA_VERSION,
            audio_mode=cast(AudioMode, audio_mode),
            renderer=RendererSettingsSnapshot.from_config(renderer),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RenderSpecification:
        _exact_keys(
            value,
            frozenset({"schema_version", "audio_mode", "renderer"}),
            "Render Specification",
        )
        schema_version = value["schema_version"]
        if not isinstance(schema_version, str):
            raise TypeError("Render Specification schema_version must be a string")
        audio_mode = value["audio_mode"]
        if not isinstance(audio_mode, str):
            raise TypeError("Render Specification audio_mode must be a string")
        renderer = value["renderer"]
        if not isinstance(renderer, Mapping):
            raise TypeError("Render Specification renderer must be an object")
        return cls(
            schema_version=schema_version,
            audio_mode=cast(AudioMode, audio_mode),
            renderer=RendererSettingsSnapshot.from_dict(renderer),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "DialogueAudioSettingsSnapshot",
    "RENDER_SPECIFICATION_SCHEMA_VERSION",
    "RenderSpecification",
    "RendererSettingsSnapshot",
]
