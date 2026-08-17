from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import cv2

from cutmaster.infrastructure.observability.logging import log_event


class SegmentMediaReader:
    """ASTER video access backed by reusable analysed Segment clips."""

    def __init__(
        self,
        source_video: Path,
        segment_cache_directory: Path,
        video_description: dict[str, Any],
    ) -> None:
        if not isinstance(segment_cache_directory, Path):
            raise TypeError("segment_cache_directory must be a pathlib.Path")
        if not segment_cache_directory.is_absolute():
            raise ValueError("segment_cache_directory must be an absolute path")
        self.source_video = source_video
        self.segment_cache_directory = segment_cache_directory.resolve()
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
        segment_id = segment.get("segment_id")
        if (
            not isinstance(segment_id, str)
            or not segment_id
            or segment_id in {".", ".."}
            or "/" in segment_id
            or "\\" in segment_id
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in segment_id
            )
        ):
            raise ValueError(
                "Segment cache identity must be a safe non-empty segment_id"
            )
        clip_path = (self.segment_cache_directory / f"{segment_id}.mp4").resolve()
        if clip_path.parent != self.segment_cache_directory:
            raise ValueError("Segment cache path escapes its supplied directory")
        return clip_path

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
            "aster.media",
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
            "aster.media",
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

    def cached_segments_for_range(
        self,
        start_sec: float,
        end_sec: float,
    ) -> list[tuple[dict[str, Any], Path]]:
        if end_sec <= start_sec:
            raise ValueError("Segment media range must have positive duration")
        matches = [
            segment
            for segment in self.segments
            if float(segment["time_range"]["start_sec"]) < end_sec
            and float(segment["time_range"]["end_sec"]) > start_sec
        ]
        if not matches:
            raise ValueError(
                f"Source range {start_sec:.6f}-{end_sec:.6f}s is outside "
                "the analysed Segment timeline"
            )
        return [
            (segment, self._ensure_segment_clip(segment))
            for segment in matches
        ]

    def _read_segment_frames(
        self,
        segment: dict[str, Any],
        requests: list[tuple[int, float]],
    ) -> dict[int, Any]:
        segment_duration = (
            float(segment["time_range"]["end_sec"])
            - float(segment["time_range"]["start_sec"])
        )
        for attempt in range(2):
            clip_path = self._ensure_segment_clip(
                segment,
                force_rebuild=attempt > 0,
            )
            capture = cv2.VideoCapture(str(clip_path))
            decoded: dict[int, Any] = {}
            try:
                if capture.isOpened():
                    try:
                        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
                        frame_count = int(
                            capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                        )
                    except (AttributeError, TypeError, ValueError):
                        fps = 0.0
                        frame_count = 0
                    last_frame_time = (
                        (frame_count - 1) / fps
                        if fps > 0.0 and frame_count > 0
                        else None
                    )
                    for output_index, local_time_sec in requests:
                        requested_time = max(0.0, local_time_sec)
                        seek_time = (
                            min(requested_time, last_frame_time)
                            if last_frame_time is not None
                            else requested_time
                        )
                        capture.set(
                            cv2.CAP_PROP_POS_MSEC,
                            seek_time * 1000.0,
                        )
                        ok, frame = capture.read()
                        if not ok and self._is_tail_request(
                            requested_time,
                            segment_duration,
                            fps,
                        ):
                            frame = self._read_last_decodable_frame(
                                capture,
                                requested_time=requested_time,
                                fps=fps,
                                frame_count=frame_count,
                            )
                            ok = frame is not None
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
                "aster.media",
                "validation.reject",
                "Segment cache could not decode requested frames",
                segment_id=segment["segment_id"],
                path=clip_path,
                attempt=attempt + 1,
            )
        raise RuntimeError(
            f"Could not decode Segment cache for {segment['segment_id']}"
        )

    @staticmethod
    def _is_tail_request(
        requested_time: float,
        segment_duration: float,
        fps: float,
    ) -> bool:
        frame_duration = 1.0 / fps if fps > 0.0 else 1.0 / 30.0
        return requested_time >= max(
            0.0,
            segment_duration - max(0.5, frame_duration * 3.0),
        )

    @staticmethod
    def _read_last_decodable_frame(
        capture: Any,
        *,
        requested_time: float,
        fps: float,
        frame_count: int,
    ) -> Any | None:
        if frame_count > 0:
            for offset in range(1, min(frame_count, 12) + 1):
                capture.set(
                    cv2.CAP_PROP_POS_FRAMES,
                    float(frame_count - offset),
                )
                ok, frame = capture.read()
                if ok:
                    return frame

        frame_duration = 1.0 / fps if fps > 0.0 else 1.0 / 30.0
        for offset in (1, 2, 4, 8, 16):
            capture.set(
                cv2.CAP_PROP_POS_MSEC,
                max(0.0, requested_time - frame_duration * offset) * 1000.0,
            )
            ok, frame = capture.read()
            if ok:
                return frame
        return None

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
