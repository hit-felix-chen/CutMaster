import pytest

from cutmaster.adapters.cli.main import _command_component, build_parser


def test_cli_commands_map_to_stable_log_components() -> None:
    assert _command_component("analyse") == "analyser"
    assert _command_component("analyse-music") == "analyser"
    assert _command_component("plan") == "planners"
    assert _command_component("render") == "renderer"
    assert _command_component("run") == "cutmaster"
    assert _command_component("serve") == "web"


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
            "planners",
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


def test_serve_cli_uses_local_only_defaults() -> None:
    args = build_parser().parse_args(["serve", "--no-open"])

    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.no_open is True


def test_serve_cli_help_is_available(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["serve", "--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--host" in output
    assert "--port" in output
    assert "--no-open" in output
