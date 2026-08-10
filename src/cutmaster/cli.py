from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cutmaster import Analyser, Orchestrator, Planner, Renderer
from cutmaster.configuration.loader import load_config, load_renderer_config
from cutmaster.contracts.analyser import (
    AnalysisRequest,
    AnalysisResult,
    MusicAnalysisRequest,
    MusicAnalysisResult,
)
from cutmaster.contracts.planning import PlanningRequest
from cutmaster.contracts.renderer import RenderRequest
from cutmaster.contracts.workflow import WorkflowRequest
from cutmaster.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.runtime.observability import configure_logging, error_summary, log_event


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=Path("config.toml"))


def _add_video_metadata(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subtitle", type=Path)
    parser.add_argument("--video-title", default="")


def _add_planning_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--target-duration", type=float, default=60.0)
    parser.add_argument("--target-shot-length", type=float, default=4.0)
    parser.add_argument("--prompt-type", default="event")
    parser.add_argument("--max-clip-duration", type=float)


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
    analyse.add_argument("--output-dir", type=Path, required=True)

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
    analyse_music.add_argument("--output-dir", type=Path, required=True)

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
    _add_planning_options(plan)
    _add_config(plan)
    plan.add_argument("--output-dir", type=Path, required=True)
    plan.add_argument("--overwrite", action="store_true")

    render = subparsers.add_parser("render", help="Render an existing plan")
    render.add_argument("--plan", type=Path, required=True)
    render.add_argument("--output-dir", type=Path, required=True)
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
    _add_planning_options(run)
    _add_config(run)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument(
        "--audio-mode",
        choices=("bgm_only", "dialogue"),
        default="dialogue",
    )
    run.add_argument("--overwrite", action="store_true")
    return parser


def _load_runtime_environment(config_path: Path) -> None:
    load_dotenv(config_path.parent / ".env", override=False)


def _command_component(command: str) -> str:
    return {
        "analyse": "analyser",
        "analyse-music": "analyser",
        "plan": "planner",
        "render": "renderer",
        "run": "orchestrator",
    }[command]


def _run_command(args: argparse.Namespace, config_path: Path) -> Any:
    if args.command == "analyse":
        return Analyser(load_config(config_path)).analyse(
            AnalysisRequest(
                video_path=args.video.resolve(),
                output_dir=args.output_dir.resolve(),
                video_title=args.video_title,
                subtitle_path=args.subtitle.resolve() if args.subtitle else None,
                material_name=args.material_name,
            )
        )
    if args.command == "analyse-music":
        return Analyser(load_config(config_path)).analyse_music(
            MusicAnalysisRequest(
                audio_path=args.audio.resolve(),
                output_dir=args.output_dir.resolve(),
                material_name=args.material_name,
            )
        )
    if args.command == "plan":
        config = load_config(config_path)
        analyser = Analyser(config)
        analysis = (
            AnalysisResult.read(args.analysis_result.resolve())
            if args.analysis_result is not None
            else analyser.resolve_video(args.video_material)
        )
        if args.audio is not None:
            music_analysis = analyser.analyse_music(
                MusicAnalysisRequest(
                    audio_path=args.audio.resolve(),
                    output_dir=(args.output_dir / "music_analysis").resolve(),
                    material_name=args.music_material_name,
                )
            )
        elif args.music_analysis_result is not None:
            if args.music_material_name:
                raise ValueError(
                    "--music-material-name is only valid together with --audio"
                )
            music_analysis = MusicAnalysisResult.read(
                args.music_analysis_result.resolve()
            )
        else:
            if args.music_material_name:
                raise ValueError(
                    "--music-material-name is only valid together with --audio"
                )
            music_analysis = analyser.resolve_music(args.music_material)
        return Planner(config).plan(
            PlanningRequest(
                video_path=Path(analysis.source_video),
                audio_path=Path(music_analysis.source_audio),
                prompt=args.prompt,
                output_dir=args.output_dir.resolve(),
                target_output_length_sec=args.target_duration,
                target_shot_length_sec=args.target_shot_length,
                prompt_type=args.prompt_type,
                video_title=analysis.material_name or Path(analysis.source_video).stem,
                max_clip_duration_sec=args.max_clip_duration,
                overwrite=args.overwrite,
                video_material_name=analysis.material_name,
                music_material_name=music_analysis.material_name,
            ),
            analysis,
            music_analysis,
        )
    if args.command == "render":
        return Renderer(load_renderer_config(config_path)).render(
            RenderRequest(
                plan_path=args.plan.resolve(),
                output_dir=args.output_dir.resolve(),
                audio_mode=args.audio_mode,
                overwrite=args.overwrite,
            )
        )
    if args.command == "run":
        config = load_config(config_path)
        analyser = Analyser(config)
        if args.video_material is not None:
            if args.video_material_name:
                raise ValueError(
                    "--video-material-name is only valid together with --video"
                )
            if args.subtitle is not None:
                raise ValueError(
                    "--subtitle cannot replace analysis for an existing Material"
                )
            selected_video = analyser.resolve_video(args.video_material)
            video_path = Path(selected_video.source_video)
            video_material_name = selected_video.material_name
        else:
            video_path = args.video.resolve()
            video_material_name = args.video_material_name
        if args.music_material is not None:
            if args.music_material_name:
                raise ValueError(
                    "--music-material-name is only valid together with --audio"
                )
            selected_music = analyser.resolve_music(args.music_material)
            audio_path = Path(selected_music.source_audio)
            music_material_name = selected_music.material_name
        else:
            audio_path = args.audio.resolve()
            music_material_name = args.music_material_name
        return Orchestrator(config).run(
            WorkflowRequest(
                video_path=video_path,
                audio_path=audio_path,
                prompt=args.prompt,
                output_dir=args.output_dir.resolve(),
                target_output_length_sec=args.target_duration,
                target_shot_length_sec=args.target_shot_length,
                prompt_type=args.prompt_type,
                video_title=args.video_title,
                subtitle_path=args.subtitle.resolve() if args.subtitle else None,
                max_clip_duration_sec=args.max_clip_duration,
                audio_mode=args.audio_mode,
                overwrite=args.overwrite,
                video_material_name=video_material_name,
                music_material_name=music_material_name,
            )
        )
    raise ValueError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config.resolve()
    _load_runtime_environment(config_path)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(args.output_dir / "cutmaster.log", console_color=True)
    try:
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
