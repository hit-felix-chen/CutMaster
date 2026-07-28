from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import (
    ASRConfig,
    AppConfig,
    BeamSearchConfig,
    CandidateRetrievalConfig,
    DialogueAnchorConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    RenderConfig,
    ScriptReviewConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    SlotPlanningConfig,
    SourceWindowOptimizationConfig,
    VLMConfig,
)


MODEL_CONFIG_KEYS = {
    "model",
    "base_url",
    "api_key",
    "api_key_env",
    "enable_thinking",
    "temperature",
    "max_tokens",
    "timeout_sec",
    "max_retries",
    "max_concurrency",
}

CONFIG_SCHEMA: dict[str, set[str]] = {
    "llm": MODEL_CONFIG_KEYS,
    "vlm": MODEL_CONFIG_KEYS,
    "material_analysis": {"material_cache_dir"},
    "shot_detection": {
        "adaptive_threshold",
        "adaptive_min_content_val",
        "adaptive_min_scene_len_sec",
        "duplicate_frame_threshold",
    },
    "asr": {
        "backend",
        "api_key",
        "api_key_env",
        "reuse",
        "timeout_sec",
        "poll_interval_sec",
        "max_chars",
        "max_subtitle_duration_sec",
    },
    "shot_annotation": {"shot_sample_frames"},
    "slot_planning": {
        "target_clip_duration_sec",
        "replan_max_rounds",
    },
    "dialogue_anchors": {
        "max_anchors",
        "min_anchor_duration_sec",
        "enable_vocal_separation",
        "separator_model",
        "separator_device",
        "separator_segment_sec",
        "separator_shifts",
        "separator_padding_sec",
        "separated_loudness_lufs",
        "dialogue_volume",
        "bgm_duck_volume",
        "fade_sec",
    },
    "candidate_retrieval": {
        "candidates_per_slot",
        "retrieval_max_rounds",
        "visual_sample_frames",
        "protagonist_visibility_likert_threshold",
        "motion_sample_fps",
        "motion_workers",
        "static_kinetic_energy_threshold",
    },
    "beam_search": {"beam_width"},
    "script_review": {"review_rounds"},
    "source_window_optimization": {
        "search_margin_sec",
        "min_boundary_distance_sec",
        "max_workers",
    },
    "render": {
        "width",
        "height",
        "fps",
        "encoder",
        "threads",
        "bgm_volume",
        "original_volume",
        "audio_sample_rate",
    },
}


def _validate_schema(data: dict[str, Any]) -> None:
    unknown_sections = set(data) - set(CONFIG_SCHEMA)
    if unknown_sections:
        raise ValueError(
            f"Unknown config sections: {sorted(unknown_sections)}"
        )
    for section_name, values in data.items():
        if not isinstance(values, dict):
            raise ValueError(f"Config section [{section_name}] must be a table")
        unknown_keys = set(values) - CONFIG_SCHEMA[section_name]
        if unknown_keys:
            raise ValueError(
                f"Unknown keys in [{section_name}]: {sorted(unknown_keys)}"
            )


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Config section [{name}] must be a table")
    return value


def _secret(section: dict[str, Any], section_name: str) -> str:
    direct = str(section.get("api_key") or "").strip()
    if direct:
        return direct
    env_name = str(section.get("api_key_env") or "").strip()
    value = os.getenv(env_name, "").strip() if env_name else ""
    if not value:
        source = f"environment variable {env_name}" if env_name else "api_key"
        raise ValueError(f"Missing [{section_name}] API key from {source}")
    return value


def _boolean(
    section: dict[str, Any],
    section_name: str,
    key: str,
    default: bool,
) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"[{section_name}].{key} must be true or false")
    return value


def _llm_config(section: dict[str, Any]) -> LLMConfig:
    model = str(section.get("model") or "").strip()
    if not model:
        raise ValueError("Missing [llm].model")
    return LLMConfig(
        model=model,
        base_url=str(section.get("base_url") or "").strip(),
        api_key=_secret(section, "llm"),
        enable_thinking=_boolean(
            section,
            "llm",
            "enable_thinking",
            True,
        ),
        temperature=float(section.get("temperature", 0.1)),
        max_tokens=int(section.get("max_tokens", 4000)),
        timeout_sec=float(section.get("timeout_sec", 180.0)),
        max_retries=int(section.get("max_retries", 3)),
        max_concurrency=int(section.get("max_concurrency", 4)),
    )


def _vlm_config(section: dict[str, Any]) -> VLMConfig:
    model = str(section.get("model") or "").strip()
    if not model:
        raise ValueError("Missing [vlm].model")
    return VLMConfig(
        model=model,
        base_url=str(section.get("base_url") or "").strip(),
        api_key=_secret(section, "vlm"),
        enable_thinking=_boolean(
            section,
            "vlm",
            "enable_thinking",
            True,
        ),
        temperature=float(section.get("temperature", 0.1)),
        max_tokens=int(section.get("max_tokens", 4000)),
        timeout_sec=float(section.get("timeout_sec", 180.0)),
        max_retries=int(section.get("max_retries", 3)),
        max_concurrency=int(section.get("max_concurrency", 4)),
    )


def _validate_values(config: AppConfig) -> None:
    positive = {
        "llm.max_tokens": config.llm.max_tokens,
        "llm.timeout_sec": config.llm.timeout_sec,
        "llm.max_concurrency": config.llm.max_concurrency,
        "vlm.max_tokens": config.vlm.max_tokens,
        "vlm.timeout_sec": config.vlm.timeout_sec,
        "vlm.max_concurrency": config.vlm.max_concurrency,
        "shot_detection.adaptive_threshold": (
            config.shot_detection.adaptive_threshold
        ),
        "shot_detection.adaptive_min_content_val": (
            config.shot_detection.adaptive_min_content_val
        ),
        "shot_detection.adaptive_min_scene_len_sec": (
            config.shot_detection.adaptive_min_scene_len_sec
        ),
        "shot_detection.duplicate_frame_threshold": (
            config.shot_detection.duplicate_frame_threshold
        ),
        "asr.timeout_sec": config.asr.timeout_sec,
        "asr.poll_interval_sec": config.asr.poll_interval_sec,
        "asr.max_chars": config.asr.max_chars,
        "asr.max_subtitle_duration_sec": config.asr.max_subtitle_duration_sec,
        "slot_planning.target_clip_duration_sec": (
            config.slot_planning.target_clip_duration_sec
        ),
        "candidate_retrieval.candidates_per_slot": (
            config.candidate_retrieval.candidates_per_slot
        ),
        "candidate_retrieval.retrieval_max_rounds": (
            config.candidate_retrieval.retrieval_max_rounds
        ),
        "candidate_retrieval.visual_sample_frames": (
            config.candidate_retrieval.visual_sample_frames
        ),
        "candidate_retrieval.motion_sample_fps": (
            config.candidate_retrieval.motion_sample_fps
        ),
        "candidate_retrieval.motion_workers": (
            config.candidate_retrieval.motion_workers
        ),
        "candidate_retrieval.static_kinetic_energy_threshold": (
            config.candidate_retrieval.static_kinetic_energy_threshold
        ),
        "beam_search.beam_width": config.beam_search.beam_width,
        "source_window_optimization.max_workers": (
            config.source_window_optimization.max_workers
        ),
        "render.width": config.render.width,
        "render.height": config.render.height,
        "render.fps": config.render.fps,
        "render.threads": config.render.threads,
        "render.audio_sample_rate": config.render.audio_sample_rate,
        "dialogue_anchors.separator_segment_sec": (
            config.dialogue_anchors.separator_segment_sec
        ),
        "dialogue_anchors.max_anchors": (
            config.dialogue_anchors.max_anchors
        ),
        "dialogue_anchors.min_anchor_duration_sec": (
            config.dialogue_anchors.min_anchor_duration_sec
        ),
    }
    invalid_positive = [
        name for name, value in positive.items() if float(value) <= 0
    ]
    if invalid_positive:
        raise ValueError(
            f"Config values must be positive: {sorted(invalid_positive)}"
        )
    if config.slot_planning.target_clip_duration_sec < 1.5:
        raise ValueError(
            "slot_planning.target_clip_duration_sec must be at least 1.5"
        )
    non_negative = {
        "llm.temperature": config.llm.temperature,
        "llm.max_retries": config.llm.max_retries,
        "vlm.temperature": config.vlm.temperature,
        "vlm.max_retries": config.vlm.max_retries,
        "slot_planning.replan_max_rounds": (
            config.slot_planning.replan_max_rounds
        ),
        "script_review.review_rounds": config.script_review.review_rounds,
        "source_window_optimization.search_margin_sec": (
            config.source_window_optimization.search_margin_sec
        ),
        "source_window_optimization.min_boundary_distance_sec": (
            config.source_window_optimization.min_boundary_distance_sec
        ),
        "render.bgm_volume": config.render.bgm_volume,
        "render.original_volume": config.render.original_volume,
        "dialogue_anchors.dialogue_volume": config.dialogue_anchors.dialogue_volume,
        "dialogue_anchors.bgm_duck_volume": (
            config.dialogue_anchors.bgm_duck_volume
        ),
        "dialogue_anchors.fade_sec": config.dialogue_anchors.fade_sec,
        "dialogue_anchors.separator_shifts": (
            config.dialogue_anchors.separator_shifts
        ),
        "dialogue_anchors.separator_padding_sec": (
            config.dialogue_anchors.separator_padding_sec
        ),
    }
    invalid_non_negative = [
        name for name, value in non_negative.items() if float(value) < 0
    ]
    if invalid_non_negative:
        raise ValueError(
            f"Config values must be non-negative: {sorted(invalid_non_negative)}"
        )
    if config.shot_annotation.shot_sample_frames != 5:
        raise ValueError("shot_annotation.shot_sample_frames must equal 5")
    threshold = (
        config.candidate_retrieval.protagonist_visibility_likert_threshold
    )
    if threshold not in {1, 2, 3, 4, 5}:
        raise ValueError(
            "candidate_retrieval.protagonist_visibility_likert_threshold "
            "must be an integer from 1 to 5"
        )
    static_threshold = (
        config.candidate_retrieval.static_kinetic_energy_threshold
    )
    if not 0.0 <= static_threshold <= 1.0:
        raise ValueError(
            "candidate_retrieval.static_kinetic_energy_threshold "
            "must be between 0 and 1"
        )
    if config.render.original_volume != 0.0:
        raise ValueError("render.original_volume must be 0 for frame-exact rendering")
    if config.dialogue_anchors.separator_device not in {
        "auto",
        "cpu",
        "mps",
        "cuda",
    }:
        raise ValueError(
            "dialogue_anchors.separator_device must be auto, cpu, mps, or cuda"
        )
    if not config.dialogue_anchors.separator_model.strip():
        raise ValueError("dialogue_anchors.separator_model must not be empty")
    if not -70.0 <= config.dialogue_anchors.separated_loudness_lufs <= 0.0:
        raise ValueError(
            "dialogue_anchors.separated_loudness_lufs must be between -70 and 0"
        )


def load_config(path: Path) -> AppConfig:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    _validate_schema(data)

    llm = _section(data, "llm")
    vlm = _section(data, "vlm")
    material_analysis = _section(data, "material_analysis")
    shot_detection = _section(data, "shot_detection")
    asr = _section(data, "asr")
    shot_annotation = _section(data, "shot_annotation")
    slot_planning = _section(data, "slot_planning")
    dialogue_anchors = _section(data, "dialogue_anchors")
    candidate_retrieval = _section(data, "candidate_retrieval")
    beam_search = _section(data, "beam_search")
    script_review = _section(data, "script_review")
    source_window_optimization = _section(data, "source_window_optimization")
    render = _section(data, "render")
    visibility_likert_threshold = candidate_retrieval.get(
        "protagonist_visibility_likert_threshold",
        3,
    )
    if (
        isinstance(visibility_likert_threshold, bool)
        or not isinstance(visibility_likert_threshold, int)
    ):
        raise ValueError(
            "candidate_retrieval.protagonist_visibility_likert_threshold "
            "must be an integer from 1 to 5"
        )
    config = AppConfig(
        llm=_llm_config(llm),
        vlm=_vlm_config(vlm),
        material_analysis=MaterialAnalysisConfig(
            material_cache_dir=(
                path.parent
                / str(
                    material_analysis.get("material_cache_dir")
                    or ".cutmaster/materials"
                )
            ).resolve(),
        ),
        shot_detection=ShotDetectionConfig(
            adaptive_threshold=float(
                shot_detection.get("adaptive_threshold", 2.0)
            ),
            adaptive_min_content_val=float(
                shot_detection.get("adaptive_min_content_val", 15.0)
            ),
            adaptive_min_scene_len_sec=float(
                shot_detection.get("adaptive_min_scene_len_sec", 0.25)
            ),
            duplicate_frame_threshold=float(
                shot_detection.get("duplicate_frame_threshold", 1.0)
            ),
        ),
        asr=ASRConfig(
            backend=str(asr.get("backend") or "bailian").strip().lower(),
            api_key=_secret(asr, "asr"),
            reuse=bool(asr.get("reuse", True)),
            timeout_sec=float(asr.get("timeout_sec", 1800.0)),
            poll_interval_sec=float(asr.get("poll_interval_sec", 2.0)),
            max_chars=int(asr.get("max_chars", 20)),
            max_subtitle_duration_sec=float(asr.get("max_subtitle_duration_sec", 3.5)),
        ),
        shot_annotation=ShotAnnotationConfig(
            shot_sample_frames=int(shot_annotation.get("shot_sample_frames", 5)),
        ),
        slot_planning=SlotPlanningConfig(
            target_clip_duration_sec=float(
                slot_planning.get("target_clip_duration_sec", 4.0)
            ),
            replan_max_rounds=int(slot_planning.get("replan_max_rounds", 3)),
        ),
        dialogue_anchors=DialogueAnchorConfig(
            max_anchors=int(dialogue_anchors.get("max_anchors", 4)),
            min_anchor_duration_sec=float(
                dialogue_anchors.get("min_anchor_duration_sec", 1.5)
            ),
            enable_vocal_separation=_boolean(
                dialogue_anchors,
                "dialogue_anchors",
                "enable_vocal_separation",
                True,
            ),
            separator_model=str(
                dialogue_anchors.get("separator_model") or "htdemucs"
            ).strip(),
            separator_device=str(
                dialogue_anchors.get("separator_device") or "auto"
            ).strip().lower(),
            separator_segment_sec=int(
                dialogue_anchors.get("separator_segment_sec", 7)
            ),
            separator_shifts=int(dialogue_anchors.get("separator_shifts", 0)),
            separator_padding_sec=float(
                dialogue_anchors.get("separator_padding_sec", 1.0)
            ),
            separated_loudness_lufs=float(
                dialogue_anchors.get("separated_loudness_lufs", -16.0)
            ),
            dialogue_volume=float(dialogue_anchors.get("dialogue_volume", 1.0)),
            bgm_duck_volume=float(
                dialogue_anchors.get("bgm_duck_volume", 0.08)
            ),
            fade_sec=float(dialogue_anchors.get("fade_sec", 0.05)),
        ),
        candidate_retrieval=CandidateRetrievalConfig(
            candidates_per_slot=int(
                candidate_retrieval.get("candidates_per_slot", 3)
            ),
            retrieval_max_rounds=int(
                candidate_retrieval.get("retrieval_max_rounds", 3)
            ),
            motion_sample_fps=float(
                candidate_retrieval.get("motion_sample_fps", 2.0)
            ),
            motion_workers=int(candidate_retrieval.get("motion_workers", 4)),
            static_kinetic_energy_threshold=float(
                candidate_retrieval.get(
                    "static_kinetic_energy_threshold",
                    0.05,
                )
            ),
            visual_sample_frames=int(
                candidate_retrieval.get("visual_sample_frames", 4)
            ),
            protagonist_visibility_likert_threshold=visibility_likert_threshold,
        ),
        beam_search=BeamSearchConfig(
            beam_width=int(beam_search.get("beam_width", 8)),
        ),
        script_review=ScriptReviewConfig(
            review_rounds=int(script_review.get("review_rounds", 1)),
        ),
        source_window_optimization=SourceWindowOptimizationConfig(
            search_margin_sec=float(
                source_window_optimization.get("search_margin_sec", 2.0)
            ),
            min_boundary_distance_sec=float(
                source_window_optimization.get(
                    "min_boundary_distance_sec",
                    1.0,
                )
            ),
            max_workers=int(source_window_optimization.get("max_workers", 8)),
        ),
        render=RenderConfig(
            width=int(render.get("width", 1920)),
            height=int(render.get("height", 1080)),
            fps=int(render.get("fps", 30)),
            encoder=str(render.get("encoder") or "auto").strip(),
            threads=int(render.get("threads", 8)),
            bgm_volume=float(render.get("bgm_volume", 0.3)),
            original_volume=float(render.get("original_volume", 0.0)),
            audio_sample_rate=int(render.get("audio_sample_rate", 48000)),
        ),
    )
    _validate_values(config)
    return config
