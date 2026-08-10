import os

from cutmaster.cli import _command_component, _load_runtime_environment, build_parser


def test_runtime_environment_loads_dotenv_next_to_config(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.touch()
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=from-dotenv\n"
        "PRESERVED_VALUE=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("PRESERVED_VALUE", "from-process")

    _load_runtime_environment(config_path)

    assert os.environ["DASHSCOPE_API_KEY"] == "from-dotenv"
    assert os.environ["PRESERVED_VALUE"] == "from-process"


def test_cli_commands_map_to_stable_log_components() -> None:
    assert _command_component("analyse") == "analyser"
    assert _command_component("analyse-music") == "analyser"
    assert _command_component("plan") == "planner"
    assert _command_component("render") == "renderer"
    assert _command_component("run") == "orchestrator"


def test_run_cli_preserves_raw_path_inputs_and_accepts_candidate_names() -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--video",
            "movie.mp4",
            "--audio",
            "score.mp3",
            "--prompt",
            "A tense reunion",
            "--video-material-name",
            "Feature Film",
            "--music-material-name",
            "Main Score",
            "--output-dir",
            "output",
        ]
    )

    assert args.video.name == "movie.mp4"
    assert args.audio.name == "score.mp3"
    assert args.video_material_name == "Feature Film"
    assert args.music_material_name == "Main Score"


def test_plan_and_run_cli_can_select_materials_by_exact_name() -> None:
    parser = build_parser()
    plan = parser.parse_args(
        [
            "plan",
            "--video-material",
            "Feature Film (2)",
            "--music-material",
            "Main Score",
            "--prompt",
            "A tense reunion",
            "--output-dir",
            "planning",
        ]
    )
    run = parser.parse_args(
        [
            "run",
            "--video-material",
            "Feature Film (2)",
            "--music-material",
            "Main Score",
            "--prompt",
            "A tense reunion",
            "--output-dir",
            "output",
        ]
    )

    assert plan.video_material == run.video_material == "Feature Film (2)"
    assert plan.music_material == run.music_material == "Main Score"
