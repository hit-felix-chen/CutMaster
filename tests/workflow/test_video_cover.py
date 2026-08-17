from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from cutmaster.workflow.analyser.material_analyst import (
    _video_cover_candidate_times,
)
from cutmaster.workflow.shared.video_cover import write_video_cover


class _Capture:
    def __init__(self) -> None:
        self.times_msec: list[float] = []
        self.read_count = 0
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 - OpenCV protocol
        return True

    def set(self, property_id: int, value: float) -> bool:
        assert property_id == cv2.CAP_PROP_POS_MSEC
        self.times_msec.append(value)
        return True

    def read(self):
        self.read_count += 1
        if self.read_count == 1:
            return False, None
        frame = np.full((90, 160, 3), (48, 176, 92), dtype=np.uint8)
        return True, frame

    def release(self) -> None:
        self.released = True


def test_video_cover_uses_middle_then_early_candidate_without_annotations(
    monkeypatch,
    tmp_path: Path,
) -> None:
    capture = _Capture()
    monkeypatch.setattr(
        "cutmaster.workflow.shared.video_cover.cv2.VideoCapture",
        lambda _path: capture,
    )
    destination = tmp_path / "cover.jpg"

    written = write_video_cover(
        tmp_path / "source.mp4",
        destination,
        (5.0, 1.0, 9.0),
    )

    assert written == destination
    assert capture.times_msec == [5000.0, 1000.0]
    assert capture.released is True
    decoded = cv2.imread(str(destination))
    assert decoded.shape == (90, 160, 3)
    # The source colour reaches the top-left corner; no black Shot label is drawn.
    assert float(decoded[:20, :80].mean()) > 80.0
    assert not list(tmp_path.glob(".*.tmp.jpg"))


def test_video_cover_candidates_preserve_existing_shot_selection_rule() -> None:
    shots = [
        {
            "shot_id": "shot_00001",
            "time_range": {"start_sec": 0.0, "end_sec": 12.0},
        },
        {
            "shot_id": "shot_00002",
            "time_range": {"start_sec": 12.0, "end_sec": 18.0},
        },
    ]

    assert _video_cover_candidate_times(shots) == (
        6.0,
        2.0,
        10.0,
        15.0,
        13.0,
        17.0,
    )
