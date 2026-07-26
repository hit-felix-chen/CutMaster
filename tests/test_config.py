from pathlib import Path

import pytest

from cutmaster.configuration.loader import load_config


def test_workflow_ordered_config_maps_each_stage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CUTMASTER_TEST_KEY", "secret")
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test-llm"
base_url = "https://example.invalid/v1"
api_key_env = "CUTMASTER_TEST_KEY"
enable_thinking = false
max_concurrency = 2

[vlm]
model = "test-vlm"
base_url = "https://vision.example.invalid/v1"
api_key_env = "CUTMASTER_TEST_KEY"
enable_thinking = true
max_concurrency = 3

[material_analysis]
material_cache_dir = "materials"

[shot_detection]
adaptive_threshold = 2.5
adaptive_min_content_val = 16.0
adaptive_min_scene_len_sec = 0.3
duplicate_frame_threshold = 1.2

[asr]
backend = "bailian"
api_key_env = "CUTMASTER_TEST_KEY"

[shot_annotation]
shot_sample_frames = 5

[slot_planning]
target_clip_duration_sec = 4.5
replan_max_rounds = 2

[dialogue_anchors]
enable_vocal_separation = false
max_anchors = 3
min_anchor_duration_sec = 2.0
separator_model = "htdemucs"
separator_device = "cpu"
separator_segment_sec = 6
separator_shifts = 1
separator_padding_sec = 0.75
separated_loudness_lufs = -18.0

[candidate_retrieval]
candidates_per_slot = 3
retrieval_max_rounds = 2
visual_sample_frames = 4
protagonist_visibility_threshold = 0.6
protagonist_visibility_fallback_threshold = 0.5
motion_sample_fps = 3.0
motion_workers = 2

[beam_search]
beam_width = 6

[script_review]
review_rounds = 1

[source_window_optimization]
search_margin_sec = 1.5
min_boundary_distance_sec = 0.8
max_workers = 3

[render]
width = 1280
height = 720
fps = 24
threads = 2
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.llm.model == "test-llm"
    assert config.llm.enable_thinking is False
    assert config.llm.max_concurrency == 2
    assert config.vlm.model == "test-vlm"
    assert config.vlm.enable_thinking is True
    assert config.vlm.max_concurrency == 3
    assert config.material_analysis.material_cache_dir == tmp_path / "materials"
    assert config.shot_detection.adaptive_threshold == 2.5
    assert config.shot_annotation.shot_sample_frames == 5
    assert config.slot_planning.target_clip_duration_sec == 4.5
    assert config.slot_planning.replan_max_rounds == 2
    assert config.dialogue_anchors.enable_vocal_separation is False
    assert config.dialogue_anchors.max_anchors == 3
    assert config.dialogue_anchors.min_anchor_duration_sec == 2.0
    assert config.dialogue_anchors.separator_device == "cpu"
    assert config.dialogue_anchors.separator_segment_sec == 6
    assert config.dialogue_anchors.separator_padding_sec == 0.75
    assert config.dialogue_anchors.separated_loudness_lufs == -18.0
    assert config.beam_search.beam_width == 6
    assert config.script_review.review_rounds == 1
    assert config.source_window_optimization.max_workers == 3
    assert config.render.fps == 24


def test_legacy_model_section_is_not_accepted(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[model]
model = "legacy"
api_key = "secret"

[asr]
api_key = "secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown config sections"):
        load_config(path)


def test_unknown_stage_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"
max_concurency = 4

[vlm]
model = "test"
api_key = "secret"

[asr]
api_key = "secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"Unknown keys in \[llm\]"):
        load_config(path)


def test_invalid_stage_value_is_rejected(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"

[vlm]
model = "test"
api_key = "secret"

[asr]
api_key = "secret"

[shot_annotation]
shot_sample_frames = 4
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="shot_sample_frames must equal 5"):
        load_config(path)


def test_thinking_switch_requires_toml_boolean(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"
enable_thinking = "false"

[vlm]
model = "test"
api_key = "secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\[llm\]\.enable_thinking"):
        load_config(path)
