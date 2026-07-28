from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import cv2

from cutmaster.runtime.observability import log_event


class SegmentMediaReader:
    """Planner video access backed by reusable analysed Segment clips."""

    def __init__(
        self,
        source_video: Path,
        video_description: dict[str, Any],
    ) -> None:
        self.source_video = source_video
        self.segments = list(video_description["segments"])
        self._locks_guard = threading.Lock()
        self._segment_locks: dict[str, threading.Lock] = {}

    def _segment_lock(self, segment_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._segment_locks.setdefault(segment_id, threading.Lock())

    def _segment_for_time(self, time_sec: float) -> dict[str, Any]:
        for index, segment in enumerate(self.segments):
            start = float(segment["time_range"]["start_sec"])
            end = float(segment["time_range"]["end_sec"])
            if start <= time_sec < end:
                return segment
            if index == len(self.segments) - 1 and start <= time_sec <= end:
                return segment
        raise ValueError(
            f"Source time {time_sec:.6f}s is outside the analysed Segment timeline"
        )

    def _clip_path(self, segment: dict[str, Any]) -> Path:
        raw_path = str(segment.get("clip_path") or "").strip()
        if not raw_path:
            raise ValueError(
                f"Segment {segment['segment_id']} does not define clip_path"
            )
        return Path(raw_path).expanduser().resolve()

    def _extract_segment_clip(
        self,
        segment: dict[str, Any],
        clip_path: Path,
    ) -> None:
        start = float(segment["time_range"]["start_sec"])
        end = float(segment["time_range"]["end_sec"])
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = clip_path.with_suffix(".partial.mp4")
        temporary_path.unlink(missing_ok=True)
        log_event(
            "WARNING",
            "planner.media",
            "cache.miss",
            "Segment video cache missing or unreadable; rebuilding from source",
            segment_id=segment["segment_id"],
            source_start_sec=start,
            source_end_sec=end,
            path=clip_path,
        )
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{end - start:.6f}",
            "-i",
            str(self.source_video),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary_path),
        ]
        subprocess.run(command, check=True)
        temporary_path.replace(clip_path)
        log_event(
            "INFO",
            "planner.media",
            "checkpoint.write",
            "Segment video cache rebuilt",
            segment_id=segment["segment_id"],
            path=clip_path,
        )

    def _ensure_segment_clip(
        self,
        segment: dict[str, Any],
        *,
        force_rebuild: bool = False,
    ) -> Path:
        clip_path = self._clip_path(segment)
        segment_id = str(segment["segment_id"])
        with self._segment_lock(segment_id):
            if (
                not force_rebuild
                and clip_path.is_file()
                and clip_path.stat().st_size > 0
            ):
                return clip_path
            self._extract_segment_clip(segment, clip_path)
        return clip_path

    def _read_segment_frames(
        self,
        segment: dict[str, Any],
        requests: list[tuple[int, float]],
    ) -> dict[int, Any]:
        for attempt in range(2):
            clip_path = self._ensure_segment_clip(
                segment,
                force_rebuild=attempt > 0,
            )
            capture = cv2.VideoCapture(str(clip_path))
            decoded: dict[int, Any] = {}
            try:
                if capture.isOpened():
                    for output_index, local_time_sec in requests:
                        capture.set(
                            cv2.CAP_PROP_POS_MSEC,
                            max(0.0, local_time_sec) * 1000.0,
                        )
                        ok, frame = capture.read()
                        if not ok:
                            decoded = {}
                            break
                        decoded[output_index] = frame
            finally:
                capture.release()
            if len(decoded) == len(requests):
                return decoded
            log_event(
                "WARNING",
                "planner.media",
                "validation.reject",
                "Segment cache could not decode requested frames",
                segment_id=segment["segment_id"],
                path=clip_path,
                attempt=attempt + 1,
            )
        raise RuntimeError(
            f"Could not decode Segment cache for {segment['segment_id']}"
        )

    def sample_frames(self, source_times_sec: list[float]) -> list[Any]:
        if not source_times_sec:
            return []
        grouped: dict[str, tuple[dict[str, Any], list[tuple[int, float]]]] = {}
        for output_index, source_time_sec in enumerate(source_times_sec):
            segment = self._segment_for_time(float(source_time_sec))
            segment_id = str(segment["segment_id"])
            segment_start = float(segment["time_range"]["start_sec"])
            segment_duration = (
                float(segment["time_range"]["end_sec"]) - segment_start
            )
            if segment_id not in grouped:
                grouped[segment_id] = (segment, [])
            grouped[segment_id][1].append(
                (
                    output_index,
                    min(
                        max(0.0, float(source_time_sec) - segment_start),
                        max(0.0, segment_duration - 1e-6),
                    ),
                )
            )

        decoded: dict[int, Any] = {}
        for segment, requests in grouped.values():
            decoded.update(self._read_segment_frames(segment, requests))
        return [decoded[index] for index in range(len(source_times_sec))]
