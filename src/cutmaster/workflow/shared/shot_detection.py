"""PySceneDetect-based source shot detection."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from scenedetect import open_video
from scenedetect.detectors import AdaptiveDetector

from cutmaster.infrastructure.observability.progress import progress_bar
from cutmaster.workflow.ports import (
    CancellationToken,
    ProgressReporter,
    ProgressUpdate,
    raise_if_cancelled,
)

ADAPTIVE_THRESHOLD = 2.0
ADAPTIVE_MIN_CONTENT_VAL = 15.0
ADAPTIVE_MIN_SCENE_LEN_SEC = 0.25
DUPLICATE_FRAME_THRESHOLD = 1.0


def _frame_signature(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)


def _mean_frame_difference(current: np.ndarray, previous: np.ndarray) -> float:
    return float(np.mean(cv2.absdiff(current, previous)))


def detect_source_cuts(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    *,
    adaptive_threshold: float = ADAPTIVE_THRESHOLD,
    adaptive_min_content_val: float = ADAPTIVE_MIN_CONTENT_VAL,
    adaptive_min_scene_len_sec: float = ADAPTIVE_MIN_SCENE_LEN_SEC,
    duplicate_frame_threshold: float = DUPLICATE_FRAME_THRESHOLD,
    progress_label: str | None = None,
    progress_reporter: ProgressReporter | None = None,
    progress_description: str | None = None,
    cancellation_token: CancellationToken | None = None,
) -> tuple[list[float], float]:
    raise_if_cancelled(cancellation_token)
    if end_sec <= start_sec:
        return [], 30.0

    video = open_video(str(video_path))
    try:
        frame_rate = float(video.frame_rate)
        video.seek(start_sec)
        detector = AdaptiveDetector(
            adaptive_threshold=adaptive_threshold,
            min_content_val=adaptive_min_content_val,
            min_scene_len=f"{adaptive_min_scene_len_sec}s",
        )
        cuts: list[float] = []
        previous_signature: np.ndarray | None = None
        progress = (
            progress_bar(
                total=max(1, round((end_sec - start_sec) * frame_rate)),
                description=progress_label,
                unit="frame",
            )
            if progress_label
            else None
        )
        progress_total = max(1, round((end_sec - start_sec) * frame_rate))
        reported_completed = 0
        if progress_reporter is not None and progress_description is not None:
            progress_reporter.report(
                ProgressUpdate(0, progress_total, progress_description, "frame")
            )
        pending_progress = 0
        # The drawer refreshes every two seconds; reporting more often only adds
        # SQLite writes without making the visible progress smoother.
        progress_batch_size = max(1, round(frame_rate * 2))
        while True:
            frame = video.read()
            if frame is False:
                break
            pending_progress += 1
            if progress is not None and pending_progress >= progress_batch_size:
                raise_if_cancelled(cancellation_token)
                progress.update(
                    min(pending_progress, max(0, progress.total - progress.n))
                )
                reported_completed = min(
                    progress_total,
                    reported_completed + pending_progress,
                )
                if progress_reporter is not None and progress_description is not None:
                    progress_reporter.report(
                        ProgressUpdate(
                            reported_completed,
                            progress_total,
                            progress_description,
                            "frame",
                        )
                    )
                pending_progress = 0
            position = video.position
            if float(position.seconds) >= end_sec:
                break

            signature = _frame_signature(frame)
            if (
                previous_signature is not None
                and _mean_frame_difference(signature, previous_signature)
                < duplicate_frame_threshold
            ):
                continue

            cuts.extend(
                float(cut.seconds) for cut in detector.process_frame(position, frame)
            )
            previous_signature = signature
        raise_if_cancelled(cancellation_token)
    finally:
        if "progress" in locals() and progress is not None:
            progress.update(min(pending_progress, max(0, progress.total - progress.n)))
            progress.close()
        video.capture.release()
    if progress_reporter is not None and progress_description is not None:
        progress_reporter.report(
            ProgressUpdate(
                progress_total,
                progress_total,
                progress_description,
                "frame",
            )
        )
    return cuts, frame_rate
