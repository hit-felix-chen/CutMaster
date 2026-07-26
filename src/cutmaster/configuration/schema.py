from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelConfig:
    model: str
    base_url: str
    api_key: str
    enable_thinking: bool = True
    temperature: float = 0.1
    max_tokens: int = 4000
    timeout_sec: float = 180.0
    max_retries: int = 3
    max_concurrency: int = 4


@dataclass(frozen=True)
class LLMConfig(ModelConfig):
    pass


@dataclass(frozen=True)
class VLMConfig(ModelConfig):
    pass


@dataclass(frozen=True)
class ASRConfig:
    backend: str
    api_key: str
    reuse: bool = True
    timeout_sec: float = 1800.0
    poll_interval_sec: float = 2.0
    max_chars: int = 20
    max_subtitle_duration_sec: float = 3.5


@dataclass(frozen=True)
class MaterialAnalysisConfig:
    material_cache_dir: Path = Path(".cutmaster/materials")


@dataclass(frozen=True)
class ShotDetectionConfig:
    adaptive_threshold: float = 2.0
    adaptive_min_content_val: float = 15.0
    adaptive_min_scene_len_sec: float = 0.25
    duplicate_frame_threshold: float = 1.0


@dataclass(frozen=True)
class ShotAnnotationConfig:
    shot_sample_frames: int = 5


@dataclass(frozen=True)
class SlotPlanningConfig:
    target_clip_duration_sec: float = 4.0
    replan_max_rounds: int = 3


@dataclass(frozen=True)
class DialogueAnchorConfig:
    max_anchors: int = 4
    min_anchor_duration_sec: float = 1.5
    enable_vocal_separation: bool = True
    separator_model: str = "htdemucs"
    separator_device: str = "auto"
    separator_segment_sec: int = 7
    separator_shifts: int = 0
    separator_padding_sec: float = 1.0
    separated_loudness_lufs: float = -16.0
    dialogue_volume: float = 1.0
    bgm_duck_volume: float = 0.08
    fade_sec: float = 0.05


@dataclass(frozen=True)
class CandidateRetrievalConfig:
    candidates_per_slot: int = 3
    retrieval_max_rounds: int = 3
    motion_sample_fps: float = 2.0
    motion_workers: int = 4
    visual_sample_frames: int = 4
    protagonist_visibility_threshold: float = 0.55
    protagonist_visibility_fallback_threshold: float = 0.5


@dataclass(frozen=True)
class BeamSearchConfig:
    beam_width: int = 8


@dataclass(frozen=True)
class ScriptReviewConfig:
    review_rounds: int = 1


@dataclass(frozen=True)
class SourceWindowOptimizationConfig:
    search_margin_sec: float = 2.0
    min_boundary_distance_sec: float = 1.0
    max_workers: int = 8


@dataclass(frozen=True)
class RenderConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    encoder: str = "auto"
    threads: int = 8
    bgm_volume: float = 0.3
    original_volume: float = 0.0
    audio_sample_rate: int = 48000


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig
    vlm: VLMConfig
    material_analysis: MaterialAnalysisConfig
    shot_detection: ShotDetectionConfig
    asr: ASRConfig
    shot_annotation: ShotAnnotationConfig
    slot_planning: SlotPlanningConfig
    dialogue_anchors: DialogueAnchorConfig
    candidate_retrieval: CandidateRetrievalConfig
    beam_search: BeamSearchConfig
    script_review: ScriptReviewConfig
    source_window_optimization: SourceWindowOptimizationConfig
    render: RenderConfig
