from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loguru import logger

from cutmaster.config import load_config
from cutmaster.models import RunRequest
from cutmaster.orchestrator import run_orchestrator


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
    run.add_argument("--custom-clips", type=int)
    run.add_argument("--max-clip-duration", type=float)
    run.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(args.output_dir / "cutmaster.log", level="DEBUG", encoding="utf-8")
    config = load_config(args.config.resolve())
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
        custom_clips=args.custom_clips,
        max_clip_duration_sec=args.max_clip_duration,
        overwrite=args.overwrite,
    )
    result = run_orchestrator(request, config)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
