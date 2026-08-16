"""Resolve one secret-bearing runtime configuration for a workflow invocation."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Literal

from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.configuration.loader import _decode_effective_config
from cutmaster.configuration.schema import AppConfig, MaterialAnalysisConfig


RuntimeCapability = Literal["llm", "vlm", "asr"]


def _secret(
    effective: EffectiveConfiguration,
    capability: RuntimeCapability,
    *,
    required: frozenset[RuntimeCapability],
) -> str:
    reference = {
        "llm": effective.secret_references.llm_api_key_env,
        "vlm": effective.secret_references.vlm_api_key_env,
        "asr": effective.secret_references.asr_api_key_env,
    }[capability]
    if reference is None:
        if capability in required:
            raise ValueError(
                f"Missing {capability.upper()} api_key_env in Effective Configuration"
            )
        return ""
    value = os.environ.get(reference, "").strip()
    if not value and capability in required:
        raise ValueError(
            f"Required environment variable {reference!r} is not set"
        )
    return value


def resolve_runtime_config(
    effective: EffectiveConfiguration,
    *,
    required: frozenset[RuntimeCapability] = frozenset(),
) -> AppConfig:
    """Materialize the invocation's config without persisting resolved secrets."""

    unknown = set(required) - {"llm", "vlm", "asr"}
    if unknown:
        raise ValueError(f"Unknown runtime capabilities: {sorted(unknown)}")
    decoded = _decode_effective_config(
        effective.to_dict(),
        effective.sources.base_path,
    )
    analyser = replace(
        decoded.analyser,
        material_analysis=MaterialAnalysisConfig(
            material_library_dir=effective.data_root / "media"
        ),
        asr=replace(
            decoded.analyser.asr,
            api_key=_secret(effective, "asr", required=required),
        ),
    )
    return replace(
        decoded,
        llm=replace(
            decoded.llm,
            api_key=_secret(effective, "llm", required=required),
        ),
        vlm=replace(
            decoded.vlm,
            api_key=_secret(effective, "vlm", required=required),
        ),
        analyser=analyser,
    )


__all__ = ["RuntimeCapability", "resolve_runtime_config"]
