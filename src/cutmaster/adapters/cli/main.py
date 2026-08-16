"""Command-line adapter for CutMaster's transport-neutral Application API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cutmaster import CutMasterApplication
from cutmaster.application.direct import (
    AnalyseMusicCommand,
    AnalyseVideoCommand,
    PlanCommand,
    RenderCommand,
)
from cutmaster.contracts import ExecuteWorkflowCommand
from cutmaster.infrastructure.observability.logging import error_summary, log_event
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=Path("config.toml"))


def _add_video_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subtitle", type=Path)
    parser.add_argument("--video-title", default="")


def _add_planners_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--target-duration", type=float, default=60.0)
    parser.add_argument("--target-shot-length", type=float, default=4.0)
    parser.add_argument("--prompt-type", default="event")
    parser.add_argument("--max-clip-duration", type=float)


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "External output directory. When omitted, CutMaster allocates a "
            "managed Direct Workflow Bundle under the Application Data Root."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cutmaster",
        description="CutMaster three-stage agentic video-editing workflow",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyse = subparsers.add_parser("analyse", help="Analyse reusable source material")
    analyse.add_argument("--video", type=Path, required=True)
    _add_video_metadata(analyse)
    analyse.add_argument(
        "--material-name",
        default="",
        help="Public Material Name; defaults to the source filename stem",
    )
    _add_config(analyse)
    _add_output(analyse)

    analyse_music = subparsers.add_parser(
        "analyse-music",
        help="Build reusable Music Memory for a complete track",
    )
    analyse_music.add_argument("--audio", type=Path, required=True)
    analyse_music.add_argument(
        "--material-name",
        default="",
        help="Public Material Name; defaults to the source filename stem",
    )
    _add_config(analyse_music)
    _add_output(analyse_music)

    plan = subparsers.add_parser("plan", help="Create an immutable render plan")
    video_input = plan.add_mutually_exclusive_group(required=True)
    video_input.add_argument("--analysis-result", type=Path)
    video_input.add_argument(
        "--video-material",
        help="Use completed analysis selected by exact video Material Name",
    )
    music_input = plan.add_mutually_exclusive_group(required=True)
    music_input.add_argument("--audio", type=Path)
    music_input.add_argument("--music-analysis-result", type=Path)
    music_input.add_argument(
        "--music-material",
        help="Use Music Memory selected by exact music Material Name",
    )
    plan.add_argument(
        "--music-material-name",
        default="",
        help="Candidate Material Name when --audio adds a track",
    )
    _add_planners_options(plan)
    _add_config(plan)
    _add_output(plan)
    plan.add_argument("--overwrite", action="store_true")

    render = subparsers.add_parser("render", help="Render an existing plan")
    render.add_argument("--plan", type=Path, required=True)
    _add_output(render)
    render.add_argument(
        "--audio-mode",
        choices=("bgm_only", "dialogue"),
        default="dialogue",
    )
    render.add_argument("--overwrite", action="store_true")
    _add_config(render)

    run = subparsers.add_parser("run", help="Analyse, plan, and render a montage")
    run_video = run.add_mutually_exclusive_group(required=True)
    run_video.add_argument("--video", type=Path)
    run_video.add_argument(
        "--video-material",
        help="Use a video selected by exact Material Name",
    )
    _add_video_metadata(run)
    run.add_argument(
        "--video-material-name",
        default="",
        help="Candidate Material Name when --video adds footage",
    )
    run_music = run.add_mutually_exclusive_group(required=True)
    run_music.add_argument("--audio", type=Path)
    run_music.add_argument(
        "--music-material",
        help="Use music selected by exact Material Name",
    )
    run.add_argument(
        "--music-material-name",
        default="",
        help="Candidate Material Name when --audio adds a track",
    )
    _add_planners_options(run)
    _add_config(run)
    _add_output(run)
    run.add_argument(
        "--audio-mode",
        choices=("bgm_only", "dialogue"),
        default="dialogue",
    )
    run.add_argument("--overwrite", action="store_true")

    serve = subparsers.add_parser("serve", help="Run the local CutMaster Web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-open", action="store_true")
    _add_config(serve)
    return parser


def _command_component(command: str) -> str:
    return {
        "analyse": "analyser",
        "analyse-music": "analyser",
        "plan": "planners",
        "render": "renderer",
        "run": "cutmaster",
        "serve": "web",
    }[command]


def _run_command(args: argparse.Namespace, config_path: Path) -> Any:
    direct = CutMasterApplication.open(config_path).direct
    output_dir = args.output_dir.resolve() if args.output_dir else None
    if args.command == "analyse":
        return direct.analyse_video(
            AnalyseVideoCommand(
                video_path=args.video.resolve(),
                output_dir=output_dir,
                video_title=args.video_title,
                subtitle_path=args.subtitle.resolve() if args.subtitle else None,
                material_name=args.material_name,
            )
        )
    if args.command == "analyse-music":
        return direct.analyse_music(
            AnalyseMusicCommand(
                audio_path=args.audio.resolve(),
                output_dir=output_dir,
                material_name=args.material_name,
            )
        )
    if args.command == "plan":
        return direct.plan(
            PlanCommand(
                prompt=args.prompt,
                output_dir=output_dir,
                analysis_result_path=(
                    args.analysis_result.resolve()
                    if args.analysis_result is not None
                    else None
                ),
                video_material=args.video_material or "",
                audio_path=args.audio.resolve() if args.audio is not None else None,
                music_analysis_result_path=(
                    args.music_analysis_result.resolve()
                    if args.music_analysis_result is not None
                    else None
                ),
                music_material=args.music_material or "",
                music_material_name=args.music_material_name,
                target_output_length_sec=args.target_duration,
                target_shot_length_sec=args.target_shot_length,
                prompt_type=args.prompt_type,
                max_clip_duration_sec=args.max_clip_duration,
                overwrite=args.overwrite,
            )
        )
    if args.command == "render":
        return direct.render(
            RenderCommand(
                plan_path=args.plan.resolve(),
                output_dir=output_dir,
                audio_mode=args.audio_mode,
                overwrite=args.overwrite,
            )
        )
    if args.command == "run":
        return direct.execute_workflow(
            ExecuteWorkflowCommand(
                prompt=args.prompt,
                video_path=args.video.resolve() if args.video else None,
                audio_path=args.audio.resolve() if args.audio else None,
                video_material=args.video_material or "",
                music_material=args.music_material or "",
                output_dir=output_dir,
                target_output_length_sec=args.target_duration,
                target_shot_length_sec=args.target_shot_length,
                prompt_type=args.prompt_type,
                video_title=args.video_title,
                subtitle_path=args.subtitle.resolve() if args.subtitle else None,
                max_clip_duration_sec=args.max_clip_duration,
                audio_mode=args.audio_mode,
                overwrite=args.overwrite,
                video_material_name=args.video_material_name,
                music_material_name=args.music_material_name,
            )
        )
    raise ValueError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config.resolve()
    try:
        if args.command == "serve":
            from cutmaster.adapters.web.server import serve

            serve(
                config_path,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
            )
            return 0
        result = _run_command(args, config_path)
    except Exception as exc:
        failure = build_prompt_failure(
            PromptFailureCode.WORKFLOW_FAILED,
            error_type=type(exc).__name__,
            error_message=error_summary(exc),
        )
        log_event(
            "ERROR",
            _command_component(args.command),
            "workflow.fail",
            f"CutMaster {args.command} failed",
            **failure,
        )
        raise
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
