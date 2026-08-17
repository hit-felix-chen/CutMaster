"""Command-line adapter for CutMaster's transport-neutral Application API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cutmaster import CutMasterApplication
from cutmaster.adapters.local_workflow import LocalManagedWorkflow
from cutmaster.contracts import ExecuteManagedWorkflowCommand
from cutmaster.domain.ids import FrozenEditId
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

    plan = subparsers.add_parser(
        "plan",
        help="Create a Web-visible Project, ASTER Run, and Frozen Edit",
    )
    plan.add_argument(
        "--video-material",
        required=True,
        help="Use completed analysis selected by exact video Material Name",
    )
    plan.add_argument(
        "--music-material",
        required=True,
        help="Use Music Memory selected by exact music Material Name",
    )
    plan.add_argument("--project-name", default="CutMaster CLI")
    _add_planners_options(plan)
    _add_config(plan)

    render = subparsers.add_parser(
        "render",
        help="Create a Web-visible Render Variant for a Frozen Edit",
    )
    render.add_argument("--edit-id", required=True)
    render.add_argument(
        "--audio-mode",
        choices=("bgm_only", "dialogue"),
        default="dialogue",
    )
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
    run.add_argument("--project-name", default="CutMaster CLI")
    _add_planners_options(run)
    _add_config(run)
    run.add_argument(
        "--audio-mode",
        choices=("bgm_only", "dialogue"),
        default="dialogue",
    )

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
    managed = LocalManagedWorkflow(CutMasterApplication.open(config_path))
    if args.command == "analyse":
        return managed.analyse_video(
            args.video.resolve(),
            # Managed Material Analysis uses the public Material Name as its
            # analysis title, just like the Web UI. Preserve the legacy
            # --video-title input as the fallback name when no explicit
            # Material Name was supplied.
            material_name=args.material_name or args.video_title,
            subtitle_path=args.subtitle.resolve() if args.subtitle else None,
        )
    if args.command == "analyse-music":
        return managed.analyse_music(
            args.audio.resolve(),
            material_name=args.material_name,
        )
    if args.command == "plan":
        return managed.plan(
            video_material=args.video_material,
            music_material=args.music_material,
            prompt=args.prompt,
            project_name=args.project_name,
            target_output_length_sec=args.target_duration,
            target_shot_length_sec=args.target_shot_length,
            prompt_type=args.prompt_type,
            max_clip_duration_sec=args.max_clip_duration,
        )
    if args.command == "render":
        return managed.render(
            FrozenEditId.parse(args.edit_id),
            audio_mode=args.audio_mode,
        )
    if args.command == "run":
        return managed.execute_workflow(
            ExecuteManagedWorkflowCommand(
                prompt=args.prompt,
                video_path=args.video.resolve() if args.video else None,
                audio_path=args.audio.resolve() if args.audio else None,
                video_material=args.video_material or "",
                music_material=args.music_material or "",
                project_name=args.project_name,
                target_output_length_sec=args.target_duration,
                target_shot_length_sec=args.target_shot_length,
                prompt_type=args.prompt_type,
                video_title=args.video_title,
                subtitle_path=args.subtitle.resolve() if args.subtitle else None,
                max_clip_duration_sec=args.max_clip_duration,
                audio_mode=args.audio_mode,
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
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
