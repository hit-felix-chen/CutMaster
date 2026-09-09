from pathlib import Path

import pytest

from cutmaster.configuration.loader import load_config, load_renderer_config


@pytest.mark.parametrize("enabled, mode", [(True, "beam"), (False, "first")])
def test_ablation_switches(tmp_path, enabled, mode):
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nmodel = "test"\napi_key = "secret"\n'
        '[vlm]\nmodel = "test"\napi_key = "secret"\n'
        '[analyser.asr]\napi_key = "secret"\n'
        f'[planners.dialogue_anchors]\nenabled = {str(enabled).lower()}\n'
        f'[planners.beam_search]\nselection_mode = "{mode}"\n'
    )
    config = load_config(path)
    assert not hasattr(config.planners.dialogue_anchors, "enabled")
    assert config.planners.beam_search.selection_mode == mode


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
input_price_yuan_per_million_tokens = 1.5
cached_input_price_yuan_per_million_tokens = 0.15
output_price_yuan_per_million_tokens = 6.0

[vlm]
model = "test-vlm"
base_url = "https://vision.example.invalid/v1"
api_key_env = "CUTMASTER_TEST_KEY"
enable_thinking = true
max_concurrency = 3
input_price_yuan_per_million_tokens = 2.5
cached_input_price_yuan_per_million_tokens = 0.5
output_price_yuan_per_million_tokens = 10.0

[analyser.material_analysis]
material_library_dir = "media"

[analyser.shot_detection]
adaptive_threshold = 2.5
adaptive_min_content_val = 16.0
adaptive_min_scene_len_sec = 0.3
duplicate_frame_threshold = 1.2

[analyser.asr]
backend = "bailian"
api_key_env = "CUTMASTER_TEST_KEY"

[analyser.shot_annotation]
shot_sample_frames = 5
max_images_per_request = 200
max_shots_per_request = 16

[planners.aster_team]
max_rounds = 3
max_local_replans = 2

[planners.arrangement_architect]
target_clip_duration_sec = 4.5
max_model_requests = 3

[planners.dialogue_anchors]
max_anchors = 3
min_anchor_duration_sec = 2.0
max_model_requests = 3

[renderer.dialogue_audio]
enable_vocal_separation = false
separator_model = "htdemucs"
separator_device = "cpu"
separator_segment_sec = 6
separator_shifts = 1
separator_padding_sec = 0.75
separated_loudness_lufs = -18.0

[planners.candidate_retrieval]
target_trajectories_per_group = 3
visual_sample_frames = 4
protagonist_visibility_likert_threshold = 4
motion_sample_fps = 3.0
motion_workers = 2
static_kinetic_energy_threshold = 0.07

[planners.beam_search]
beam_width = 6


[planners.source_window_optimization]
search_margin_sec = 1.5
min_boundary_distance_sec = 0.8
max_workers = 3

[renderer]
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
    assert config.llm.input_price_yuan_per_million_tokens == 1.5
    assert config.llm.cached_input_price_yuan_per_million_tokens == 0.15
    assert config.llm.output_price_yuan_per_million_tokens == 6.0
    assert config.vlm.model == "test-vlm"
    assert config.vlm.enable_thinking is True
    assert config.vlm.max_concurrency == 3
    assert config.vlm.input_price_yuan_per_million_tokens == 2.5
    assert config.vlm.cached_input_price_yuan_per_million_tokens == 0.5
    assert config.vlm.output_price_yuan_per_million_tokens == 10.0
    assert config.analyser.material_analysis.material_library_dir == tmp_path / "media"
    assert config.analyser.shot_detection.adaptive_threshold == 2.5
    assert config.analyser.scene_segmentation.context_shots == 20
    assert config.analyser.scene_segmentation.focus_shots == 10
    assert config.analyser.scene_segmentation.frames_per_shot == 3
    assert config.analyser.shot_annotation.shot_sample_frames == 5
    assert config.analyser.shot_annotation.max_images_per_request == 200
    assert config.analyser.shot_annotation.max_shots_per_request == 16
    assert config.planners.aster_team.max_rounds == 3
    assert config.planners.aster_team.max_local_replans == 2
    assert config.planners.arrangement_architect.target_clip_duration_sec == 4.5
    assert config.planners.arrangement_architect.max_model_requests == 3
    assert (
        config.planners.candidate_retrieval.target_trajectories_per_group == 3
    )
    assert (
        config.planners.candidate_retrieval.protagonist_visibility_likert_threshold
        == 4
    )
    assert (
        config.planners.candidate_retrieval.static_kinetic_energy_threshold
        == 0.07
    )
    assert config.planners.dialogue_anchors.max_anchors == 3
    assert config.planners.dialogue_anchors.min_anchor_duration_sec == 2.0
    assert config.planners.dialogue_anchors.max_model_requests == 3
    assert config.renderer.dialogue_audio.enable_vocal_separation is False
    assert config.renderer.dialogue_audio.separator_device == "cpu"
    assert config.renderer.dialogue_audio.separator_segment_sec == 6
    assert config.renderer.dialogue_audio.separator_padding_sec == 0.75
    assert config.renderer.dialogue_audio.separated_loudness_lufs == -18.0
    assert config.planners.beam_search.beam_width == 6
    assert config.planners.source_window_optimization.max_workers == 3
    assert config.renderer.fps == 24


def test_material_library_defaults_to_media_beside_config(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"

[vlm]
model = "test"
api_key = "secret"

[analyser.asr]
api_key = "secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.analyser.material_analysis.material_library_dir == (
        tmp_path / ".cutmaster" / "media"
    )


def test_legacy_material_cache_config_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"

[vlm]
model = "test"
api_key = "secret"

[analyser.material_analysis]
material_cache_dir = ".cutmaster/materials-backup"

[analyser.asr]
api_key = "secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"Unknown keys.*material_cache_dir"):
        load_config(path)


def test_legacy_model_section_is_not_accepted(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[model]
model = "legacy"
api_key = "secret"

[analyser.asr]
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

[analyser.asr]
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

[analyser.asr]
api_key = "secret"

[analyser.shot_annotation]
shot_sample_frames = 4
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="shot_sample_frames must equal 5"):
        load_config(path)


def test_visibility_likert_threshold_requires_integer(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"

[vlm]
model = "test"
api_key = "secret"

[analyser.asr]
api_key = "secret"

[planners.candidate_retrieval]
protagonist_visibility_likert_threshold = 3.5
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be an integer from 1 to 5"):
        load_config(path)


@pytest.mark.parametrize(
    "legacy_key",
    ["candidates_per_slot", "retrieval_max_rounds", "max_rounds"],
)
def test_legacy_candidate_retrieval_keys_are_rejected(
    tmp_path,
    legacy_key: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[llm]
model = "test"
api_key = "secret"

[vlm]
model = "test"
api_key = "secret"

[analyser.asr]
api_key = "secret"

[planners.candidate_retrieval]
{legacy_key} = 3
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=rf"Unknown keys.*{legacy_key}"):
        load_config(path)


def test_per_round_local_replan_budget_key_is_rejected(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"

[vlm]
model = "test"
api_key = "secret"

[analyser.asr]
api_key = "secret"

[planners.aster_team]
max_local_replans_per_round = 2
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"Unknown keys.*max_local_replans_per_round",
    ):
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


def test_model_prices_must_be_non_negative(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
model = "test"
api_key = "secret"
input_price_yuan_per_million_tokens = -1

[vlm]
model = "test"
api_key = "secret"

[analyser.asr]
api_key = "secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be non-negative"):
        load_config(path)


def test_renderer_config_does_not_require_api_credentials(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[renderer]
width = 1280
height = 720
fps = 24

[renderer.dialogue_audio]
enable_vocal_separation = false
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_renderer_config(path)

    assert config.width == 1280
    assert config.fps == 24
    assert config.dialogue_audio.enable_vocal_separation is False
