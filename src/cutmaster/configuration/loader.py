from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import (
    ASRConfig,
    AnalyserConfig,
    AppConfig,
    ArrangementArchitectConfig,
    AsterTeamConfig,
    BeamSearchConfig,
    CandidateRetrievalConfig,
    DialogueAnchorConfig,
    DialogueAudioConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    PlannersConfig,
    RendererConfig,
    SceneSegmentationConfig,
    ScriptReviewConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
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
    "input_price_yuan_per_million_tokens",
    "cached_input_price_yuan_per_million_tokens",
    "output_price_yuan_per_million_tokens",
}

ANALYSER_SCHEMA: dict[str, set[str]] = {
    "material_analysis": {"material_library_dir"},
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
    "scene_segmentation": {
        "context_shots",
        "focus_shots",
        "frames_per_shot",
    },
    "shot_annotation": {
        "shot_sample_frames",
        "max_images_per_request",
        "max_shots_per_request",
    },
}

PLANNERS_SCHEMA: dict[str, set[str]] = {
    "aster_team": {"max_rounds", "max_local_replans_per_round"},
    "arrangement_architect": {
        "target_clip_duration_sec",
        "max_model_requests",
    },
    "dialogue_anchors": {
        "max_anchors",
        "min_anchor_duration_sec",
        "max_model_requests",
    },
    "candidate_retrieval": {
        "target_trajectories_per_group",
        "max_rounds",
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
}

RENDERER_KEYS = {
    "width",
    "height",
    "fps",
    "encoder",
    "threads",
    "bgm_volume",
    "original_volume",
    "audio_sample_rate",
    "dialogue_audio",
}

DIALOGUE_AUDIO_KEYS = {
    "enable_vocal_separation",
    "separator_model",
    "separator_device",
    "separator_segment_sec",
    "separator_shifts",
    "separator_padding_sec",
    "separated_loudness_lufs",
    "dialogue_volume",
    "bgm_duck_factor",
    "fade_sec",
}


def _read_data(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    _validate_schema(data)
    return data


def _validate_nested_stage(
    data: dict[str, Any],
    stage: str,
    schema: dict[str, set[str]],
) -> None:
    stage_value = data.get(stage, {})
    if not isinstance(stage_value, dict):
        raise ValueError(f"Config section [{stage}] must be a table")
    unknown_sections = set(stage_value) - set(schema)
    if unknown_sections:
        raise ValueError(
            f"Unknown config sections in [{stage}]: {sorted(unknown_sections)}"
        )
    for name, value in stage_value.items():
        if not isinstance(value, dict):
            raise ValueError(f"Config section [{stage}.{name}] must be a table")
        unknown_keys = set(value) - schema[name]
        if unknown_keys:
            raise ValueError(
                f"Unknown keys in [{stage}.{name}]: {sorted(unknown_keys)}"
            )


def _validate_schema(data: dict[str, Any]) -> None:
    allowed = {"llm", "vlm", "analyser", "planners", "renderer"}
    unknown_sections = set(data) - allowed
    if unknown_sections:
        raise ValueError(f"Unknown config sections: {sorted(unknown_sections)}")
    for model_section in ("llm", "vlm"):
        value = data.get(model_section, {})
        if not isinstance(value, dict):
            raise ValueError(f"Config section [{model_section}] must be a table")
        unknown_keys = set(value) - MODEL_CONFIG_KEYS
        if unknown_keys:
            raise ValueError(
                f"Unknown keys in [{model_section}]: {sorted(unknown_keys)}"
            )
    _validate_nested_stage(data, "analyser", ANALYSER_SCHEMA)
    _validate_nested_stage(data, "planners", PLANNERS_SCHEMA)
    renderer = data.get("renderer", {})
    if not isinstance(renderer, dict):
        raise ValueError("Config section [renderer] must be a table")
    unknown_renderer = set(renderer) - RENDERER_KEYS
    if unknown_renderer:
        raise ValueError(f"Unknown keys in [renderer]: {sorted(unknown_renderer)}")
    dialogue_audio = renderer.get("dialogue_audio", {})
    if not isinstance(dialogue_audio, dict):
        raise ValueError("Config section [renderer.dialogue_audio] must be a table")
    unknown_dialogue = set(dialogue_audio) - DIALOGUE_AUDIO_KEYS
    if unknown_dialogue:
        raise ValueError(
            "Unknown keys in [renderer.dialogue_audio]: "
            f"{sorted(unknown_dialogue)}"
        )


def _section(data: dict[str, Any], *names: str) -> dict[str, Any]:
    value: Any = data
    for name in names:
        if not isinstance(value, dict):
            raise ValueError(f"Config section [{'.'.join(names)}] must be a table")
        value = value.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Config section [{'.'.join(names)}] must be a table")
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


def _llm_config(
    section: dict[str, Any],
    *,
    resolve_secrets: bool = True,
) -> LLMConfig:
    model = str(section.get("model") or "").strip()
    if not model:
        raise ValueError("Missing [llm].model")
    return LLMConfig(
        model=model,
        base_url=str(section.get("base_url") or "").strip(),
        api_key=_secret(section, "llm") if resolve_secrets else "",
        enable_thinking=_boolean(section, "llm", "enable_thinking", True),
        temperature=float(section.get("temperature", 0.1)),
        max_tokens=int(section.get("max_tokens", 4000)),
        timeout_sec=float(section.get("timeout_sec", 180.0)),
        max_retries=int(section.get("max_retries", 3)),
        max_concurrency=int(section.get("max_concurrency", 4)),
        input_price_yuan_per_million_tokens=float(
            section.get("input_price_yuan_per_million_tokens", 0.0)
        ),
        cached_input_price_yuan_per_million_tokens=float(
            section.get("cached_input_price_yuan_per_million_tokens", 0.0)
        ),
        output_price_yuan_per_million_tokens=float(
            section.get("output_price_yuan_per_million_tokens", 0.0)
        ),
    )


def _vlm_config(
    section: dict[str, Any],
    *,
    resolve_secrets: bool = True,
) -> VLMConfig:
    model = str(section.get("model") or "").strip()
    if not model:
        raise ValueError("Missing [vlm].model")
    return VLMConfig(
        model=model,
        base_url=str(section.get("base_url") or "").strip(),
        api_key=_secret(section, "vlm") if resolve_secrets else "",
        enable_thinking=_boolean(section, "vlm", "enable_thinking", True),
        temperature=float(section.get("temperature", 0.1)),
        max_tokens=int(section.get("max_tokens", 4000)),
        timeout_sec=float(section.get("timeout_sec", 180.0)),
        max_retries=int(section.get("max_retries", 3)),
        max_concurrency=int(section.get("max_concurrency", 4)),
        input_price_yuan_per_million_tokens=float(
            section.get("input_price_yuan_per_million_tokens", 0.0)
        ),
        cached_input_price_yuan_per_million_tokens=float(
            section.get("cached_input_price_yuan_per_million_tokens", 0.0)
        ),
        output_price_yuan_per_million_tokens=float(
            section.get("output_price_yuan_per_million_tokens", 0.0)
        ),
    )


def _renderer_config(data: dict[str, Any]) -> RendererConfig:
    renderer = _section(data, "renderer")
    dialogue = _section(data, "renderer", "dialogue_audio")
    return RendererConfig(
        width=int(renderer.get("width", 1920)),
        height=int(renderer.get("height", 1080)),
        fps=int(renderer.get("fps", 30)),
        encoder=str(renderer.get("encoder") or "auto").strip(),
        threads=int(renderer.get("threads", 8)),
        bgm_volume=float(renderer.get("bgm_volume", 0.3)),
        original_volume=float(renderer.get("original_volume", 0.0)),
        audio_sample_rate=int(renderer.get("audio_sample_rate", 48000)),
        dialogue_audio=DialogueAudioConfig(
            enable_vocal_separation=_boolean(
                dialogue,
                "renderer.dialogue_audio",
                "enable_vocal_separation",
                True,
            ),
            separator_model=str(
                dialogue.get("separator_model") or "htdemucs"
            ).strip(),
            separator_device=str(
                dialogue.get("separator_device") or "auto"
            ).strip().lower(),
            separator_segment_sec=int(dialogue.get("separator_segment_sec", 7)),
            separator_shifts=int(dialogue.get("separator_shifts", 0)),
            separator_padding_sec=float(dialogue.get("separator_padding_sec", 1.0)),
            separated_loudness_lufs=float(
                dialogue.get("separated_loudness_lufs", -16.0)
            ),
            dialogue_volume=float(dialogue.get("dialogue_volume", 1.0)),
            bgm_duck_factor=float(dialogue.get("bgm_duck_factor", 0.5)),
            fade_sec=float(dialogue.get("fade_sec", 0.3)),
        ),
    )


def _validate_values(config: AppConfig) -> None:
    analyser = config.analyser
    planners = config.planners
    renderer = config.renderer
    dialogue = renderer.dialogue_audio
    positive = {
        "llm.max_tokens": config.llm.max_tokens,
        "llm.timeout_sec": config.llm.timeout_sec,
        "llm.max_concurrency": config.llm.max_concurrency,
        "vlm.max_tokens": config.vlm.max_tokens,
        "vlm.timeout_sec": config.vlm.timeout_sec,
        "vlm.max_concurrency": config.vlm.max_concurrency,
        "analyser.shot_detection.adaptive_threshold": analyser.shot_detection.adaptive_threshold,
        "analyser.shot_detection.adaptive_min_content_val": analyser.shot_detection.adaptive_min_content_val,
        "analyser.shot_detection.adaptive_min_scene_len_sec": analyser.shot_detection.adaptive_min_scene_len_sec,
        "analyser.shot_detection.duplicate_frame_threshold": analyser.shot_detection.duplicate_frame_threshold,
        "analyser.asr.timeout_sec": analyser.asr.timeout_sec,
        "analyser.asr.poll_interval_sec": analyser.asr.poll_interval_sec,
        "analyser.asr.max_chars": analyser.asr.max_chars,
        "analyser.asr.max_subtitle_duration_sec": analyser.asr.max_subtitle_duration_sec,
        "analyser.scene_segmentation.context_shots": analyser.scene_segmentation.context_shots,
        "analyser.scene_segmentation.focus_shots": analyser.scene_segmentation.focus_shots,
        "analyser.scene_segmentation.frames_per_shot": analyser.scene_segmentation.frames_per_shot,
        "planners.arrangement_architect.target_clip_duration_sec": planners.arrangement_architect.target_clip_duration_sec,
        "planners.arrangement_architect.max_model_requests": planners.arrangement_architect.max_model_requests,
        "planners.aster_team.max_rounds": planners.aster_team.max_rounds,
        "planners.aster_team.max_local_replans_per_round": planners.aster_team.max_local_replans_per_round,
        "planners.dialogue_anchors.max_anchors": planners.dialogue_anchors.max_anchors,
        "planners.dialogue_anchors.min_anchor_duration_sec": planners.dialogue_anchors.min_anchor_duration_sec,
        "planners.dialogue_anchors.max_model_requests": planners.dialogue_anchors.max_model_requests,
        "planners.candidate_retrieval.target_trajectories_per_group": planners.candidate_retrieval.target_trajectories_per_group,
        "planners.candidate_retrieval.max_rounds": planners.candidate_retrieval.max_rounds,
        "planners.candidate_retrieval.visual_sample_frames": planners.candidate_retrieval.visual_sample_frames,
        "planners.candidate_retrieval.motion_sample_fps": planners.candidate_retrieval.motion_sample_fps,
        "planners.candidate_retrieval.motion_workers": planners.candidate_retrieval.motion_workers,
        "planners.candidate_retrieval.static_kinetic_energy_threshold": planners.candidate_retrieval.static_kinetic_energy_threshold,
        "planners.beam_search.beam_width": planners.beam_search.beam_width,
        "planners.source_window_optimization.max_workers": planners.source_window_optimization.max_workers,
        "renderer.width": renderer.width,
        "renderer.height": renderer.height,
        "renderer.fps": renderer.fps,
        "renderer.threads": renderer.threads,
        "renderer.audio_sample_rate": renderer.audio_sample_rate,
        "renderer.dialogue_audio.separator_segment_sec": dialogue.separator_segment_sec,
    }
    invalid_positive = [
        name for name, value in positive.items() if float(value) <= 0
    ]
    if invalid_positive:
        raise ValueError(
            f"Config values must be positive: {sorted(invalid_positive)}"
        )
    if planners.arrangement_architect.target_clip_duration_sec < 1.5:
        raise ValueError(
            "planners.arrangement_architect.target_clip_duration_sec must be at least 1.5"
        )
    non_negative = {
        "llm.temperature": config.llm.temperature,
        "llm.max_retries": config.llm.max_retries,
        "llm.input_price_yuan_per_million_tokens": (
            config.llm.input_price_yuan_per_million_tokens
        ),
        "llm.cached_input_price_yuan_per_million_tokens": (
            config.llm.cached_input_price_yuan_per_million_tokens
        ),
        "llm.output_price_yuan_per_million_tokens": (
            config.llm.output_price_yuan_per_million_tokens
        ),
        "vlm.temperature": config.vlm.temperature,
        "vlm.max_retries": config.vlm.max_retries,
        "vlm.input_price_yuan_per_million_tokens": (
            config.vlm.input_price_yuan_per_million_tokens
        ),
        "vlm.cached_input_price_yuan_per_million_tokens": (
            config.vlm.cached_input_price_yuan_per_million_tokens
        ),
        "vlm.output_price_yuan_per_million_tokens": (
            config.vlm.output_price_yuan_per_million_tokens
        ),
        "planners.script_review.review_rounds": planners.script_review.review_rounds,
        "planners.source_window_optimization.search_margin_sec": planners.source_window_optimization.search_margin_sec,
        "planners.source_window_optimization.min_boundary_distance_sec": planners.source_window_optimization.min_boundary_distance_sec,
        "renderer.bgm_volume": renderer.bgm_volume,
        "renderer.original_volume": renderer.original_volume,
        "renderer.dialogue_audio.dialogue_volume": dialogue.dialogue_volume,
        "renderer.dialogue_audio.bgm_duck_factor": dialogue.bgm_duck_factor,
        "renderer.dialogue_audio.fade_sec": dialogue.fade_sec,
        "renderer.dialogue_audio.separator_shifts": dialogue.separator_shifts,
        "renderer.dialogue_audio.separator_padding_sec": dialogue.separator_padding_sec,
    }
    invalid_non_negative = [
        name for name, value in non_negative.items() if float(value) < 0
    ]
    if invalid_non_negative:
        raise ValueError(
            f"Config values must be non-negative: {sorted(invalid_non_negative)}"
        )
    if analyser.shot_annotation.shot_sample_frames != 5:
        raise ValueError("analyser.shot_annotation.shot_sample_frames must equal 5")
    if analyser.shot_annotation.max_images_per_request <= 0:
        raise ValueError(
            "analyser.shot_annotation.max_images_per_request must be positive"
        )
    if analyser.shot_annotation.max_shots_per_request <= 0:
        raise ValueError(
            "analyser.shot_annotation.max_shots_per_request must be positive"
        )
    if analyser.scene_segmentation.context_shots != 20:
        raise ValueError("analyser.scene_segmentation.context_shots must equal 20")
    if analyser.scene_segmentation.focus_shots != 10:
        raise ValueError("analyser.scene_segmentation.focus_shots must equal 10")
    if analyser.scene_segmentation.frames_per_shot != 3:
        raise ValueError("analyser.scene_segmentation.frames_per_shot must equal 3")
    threshold = planners.candidate_retrieval.protagonist_visibility_likert_threshold
    if threshold not in {1, 2, 3, 4, 5}:
        raise ValueError(
            "planners.candidate_retrieval.protagonist_visibility_likert_threshold "
            "must be an integer from 1 to 5"
        )
    static_threshold = planners.candidate_retrieval.static_kinetic_energy_threshold
    if not 0.0 <= static_threshold <= 1.0:
        raise ValueError(
            "planners.candidate_retrieval.static_kinetic_energy_threshold "
            "must be between 0 and 1"
        )
    if renderer.original_volume != 0.0:
        raise ValueError(
            "renderer.original_volume must be 0 for frame-exact rendering"
        )
    if dialogue.separator_device not in {"auto", "cpu", "mps", "cuda"}:
        raise ValueError(
            "renderer.dialogue_audio.separator_device must be auto, cpu, mps, or cuda"
        )
    if not dialogue.separator_model:
        raise ValueError(
            "renderer.dialogue_audio.separator_model must not be empty"
        )
    if not -70.0 <= dialogue.separated_loudness_lufs <= 0.0:
        raise ValueError(
            "renderer.dialogue_audio.separated_loudness_lufs must be between -70 and 0"
        )


def load_renderer_config(path: Path) -> RendererConfig:
    """Load rendering configuration without resolving any API credentials."""

    data = _read_data(path)
    renderer = _renderer_config(data)
    if renderer.width <= 0 or renderer.height <= 0 or renderer.fps <= 0:
        raise ValueError("Renderer dimensions and fps must be positive")
    if renderer.original_volume != 0.0:
        raise ValueError(
            "renderer.original_volume must be 0 for frame-exact rendering"
        )
    return renderer


def _build_config(
    data: dict[str, Any],
    path: Path,
    *,
    resolve_secrets: bool,
) -> AppConfig:
    analyser = _section(data, "analyser")
    planners = _section(data, "planners")
    material = _section(data, "analyser", "material_analysis")
    shots = _section(data, "analyser", "shot_detection")
    asr = _section(data, "analyser", "asr")
    scene = _section(data, "analyser", "scene_segmentation")
    annotation = _section(data, "analyser", "shot_annotation")
    aster_team = _section(data, "planners", "aster_team")
    arrangement = _section(data, "planners", "arrangement_architect")
    anchors = _section(data, "planners", "dialogue_anchors")
    retrieval = _section(data, "planners", "candidate_retrieval")
    beam = _section(data, "planners", "beam_search")
    review = _section(data, "planners", "script_review")
    source_window = _section(data, "planners", "source_window_optimization")
    visibility = retrieval.get("protagonist_visibility_likert_threshold", 3)
    if isinstance(visibility, bool) or not isinstance(visibility, int):
        raise ValueError(
            "planners.candidate_retrieval.protagonist_visibility_likert_threshold "
            "must be an integer from 1 to 5"
        )
    config = AppConfig(
        llm=_llm_config(
            _section(data, "llm"),
            resolve_secrets=resolve_secrets,
        ),
        vlm=_vlm_config(
            _section(data, "vlm"),
            resolve_secrets=resolve_secrets,
        ),
        analyser=AnalyserConfig(
            material_analysis=MaterialAnalysisConfig(
                material_library_dir=(
                    path.parent
                    / str(
                        material.get("material_library_dir")
                        or ".cutmaster/media"
                    )
                ).resolve(),
            ),
            shot_detection=ShotDetectionConfig(
                adaptive_threshold=float(shots.get("adaptive_threshold", 2.0)),
                adaptive_min_content_val=float(
                    shots.get("adaptive_min_content_val", 15.0)
                ),
                adaptive_min_scene_len_sec=float(
                    shots.get("adaptive_min_scene_len_sec", 0.25)
                ),
                duplicate_frame_threshold=float(
                    shots.get("duplicate_frame_threshold", 1.0)
                ),
            ),
            asr=ASRConfig(
                backend=str(asr.get("backend") or "bailian").strip().lower(),
                api_key=(
                    _secret(asr, "analyser.asr") if resolve_secrets else ""
                ),
                reuse=_boolean(asr, "analyser.asr", "reuse", True),
                timeout_sec=float(asr.get("timeout_sec", 600.0)),
                poll_interval_sec=float(asr.get("poll_interval_sec", 2.0)),
                max_chars=int(asr.get("max_chars", 20)),
                max_subtitle_duration_sec=float(
                    asr.get("max_subtitle_duration_sec", 3.5)
                ),
            ),
            scene_segmentation=SceneSegmentationConfig(
                context_shots=int(scene.get("context_shots", 20)),
                focus_shots=int(scene.get("focus_shots", 10)),
                frames_per_shot=int(scene.get("frames_per_shot", 3)),
            ),
            shot_annotation=ShotAnnotationConfig(
                shot_sample_frames=int(annotation.get("shot_sample_frames", 5)),
                max_images_per_request=int(
                    annotation.get("max_images_per_request", 250)
                ),
                max_shots_per_request=int(
                    annotation.get("max_shots_per_request", 20)
                ),
            ),
        ),
        planners=PlannersConfig(
            aster_team=AsterTeamConfig(
                max_rounds=int(aster_team.get("max_rounds", 3)),
                max_local_replans_per_round=int(
                    aster_team.get("max_local_replans_per_round", 2)
                ),
            ),
            arrangement_architect=ArrangementArchitectConfig(
                target_clip_duration_sec=float(
                    arrangement.get("target_clip_duration_sec", 4.0)
                ),
                max_model_requests=int(arrangement.get("max_model_requests", 3)),
            ),
            dialogue_anchors=DialogueAnchorConfig(
                max_anchors=int(anchors.get("max_anchors", 4)),
                min_anchor_duration_sec=float(
                    anchors.get("min_anchor_duration_sec", 1.5)
                ),
                max_model_requests=int(anchors.get("max_model_requests", 3)),
            ),
            candidate_retrieval=CandidateRetrievalConfig(
                target_trajectories_per_group=int(
                    retrieval.get("target_trajectories_per_group", 3)
                ),
                max_rounds=int(retrieval.get("max_rounds", 4)),
                motion_sample_fps=float(retrieval.get("motion_sample_fps", 2.0)),
                motion_workers=int(retrieval.get("motion_workers", 4)),
                static_kinetic_energy_threshold=float(
                    retrieval.get("static_kinetic_energy_threshold", 0.01)
                ),
                visual_sample_frames=int(retrieval.get("visual_sample_frames", 4)),
                protagonist_visibility_likert_threshold=visibility,
            ),
            beam_search=BeamSearchConfig(
                beam_width=int(beam.get("beam_width", 8))
            ),
            script_review=ScriptReviewConfig(
                review_rounds=int(review.get("review_rounds", 1))
            ),
            source_window_optimization=SourceWindowOptimizationConfig(
                search_margin_sec=float(source_window.get("search_margin_sec", 2.0)),
                min_boundary_distance_sec=float(
                    source_window.get("min_boundary_distance_sec", 1.0)
                ),
                max_workers=int(source_window.get("max_workers", 8)),
            ),
        ),
        renderer=_renderer_config(data),
    )
    _validate_values(config)
    return config


def _decode_effective_config(data: dict[str, Any], path: Path) -> AppConfig:
    """Decode merged configuration without resolving credentials."""

    _validate_schema(data)
    return _build_config(data, path, resolve_secrets=False)


def load_config(path: Path) -> AppConfig:
    data = _read_data(path)
    return _build_config(data, path, resolve_secrets=True)


__all__ = ["load_config", "load_renderer_config"]
