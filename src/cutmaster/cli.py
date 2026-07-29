from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from cutmaster import CutMaster
from cutmaster.configuration.loader import load_config
from cutmaster.contracts.workflow import RunRequest
from cutmaster.runtime.observability import configure_logging, error_summary, log_event


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cutmaster",
        description="CutMaster agentic video-editing workflow",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Generate a montage from one source video and one BGM track")
    run.add_argument("--video", type=Path, required=True)
    run.add_argument("--audio", type=Path, required=True)
    run.add_argument("--prompt", required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--config", type=Path, default=Path("config.toml"))
    run.add_argument("--subtitle", type=Path)
    run.add_argument("--target-duration", type=float, default=60.0)
    run.add_argument("--target-shot-length", type=float, default=4.0)
    run.add_argument("--prompt-type", default="event")
    run.add_argument("--video-title", default="")
    run.add_argument("--max-clip-duration", type=float)
    run.add_argument("--overwrite", action="store_true")
    return parser


def _load_runtime_environment(config_path: Path) -> None:
    load_dotenv(config_path.parent / ".env", override=False)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        return 2
    config_path = args.config.resolve()
    _load_runtime_environment(config_path)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Benchmark adapters commonly pipe child output before forwarding it to a real
    # terminal, so isatty() alone cannot determine whether colors are visible.
    configure_logging(args.output_dir / "cutmaster.log", console_color=True)
    config = load_config(config_path)
    request = RunRequest(
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
        overwrite=args.overwrite,
    )
    try:
        result = CutMaster(config).run(request)
    except Exception as exc:
        log_event(
            "ERROR",
            "cutmaster",
            "workflow.fail",
            "CutMaster workflow failed",
            error_type=type(exc).__name__,
            reason=error_summary(exc),
        )
        raise
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
