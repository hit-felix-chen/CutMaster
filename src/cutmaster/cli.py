from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from cutmaster import Analyser, Orchestrator, Planner, Renderer
from cutmaster.configuration.loader import load_config, load_renderer_config
from cutmaster.contracts.analyser import AnalysisRequest, AnalysisResult
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


def _add_analysis_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--subtitle", type=Path)
    parser.add_argument("--video-title", default="")


def _add_planning_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audio", type=Path, required=True)
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
    _add_analysis_inputs(analyse)
    _add_config(analyse)
    analyse.add_argument("--output-dir", type=Path, required=True)

    plan = subparsers.add_parser("plan", help="Create an immutable render plan")
    plan.add_argument("--analysis-result", type=Path, required=True)
    _add_planning_inputs(plan)
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
    _add_analysis_inputs(run)
    _add_planning_inputs(run)
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
            )
        )
    if args.command == "plan":
        analysis = AnalysisResult.read(args.analysis_result.resolve())
        return Planner(load_config(config_path)).plan(
            PlanningRequest(
                video_path=Path(analysis.source_video),
                audio_path=args.audio.resolve(),
                prompt=args.prompt,
                output_dir=args.output_dir.resolve(),
                target_output_length_sec=args.target_duration,
                target_shot_length_sec=args.target_shot_length,
                prompt_type=args.prompt_type,
                video_title=Path(analysis.source_video).stem,
                max_clip_duration_sec=args.max_clip_duration,
                overwrite=args.overwrite,
            ),
            analysis,
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
        return Orchestrator(load_config(config_path)).run(
            WorkflowRequest(
                video_path=args.video.resolve(),
                audio_path=args.audio.resolve(),
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
