from pathlib import Path

import cv2
import numpy as np

from cutmaster.planner.media import SegmentMediaReader


def _video_description(first_clip: Path, second_clip: Path) -> dict:
    return {
        "segments": [
            {
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 0.0, "end_sec": 5.0},
                "clip_path": str(first_clip),
            },
            {
                "segment_id": "segment_0002",
                "time_range": {"start_sec": 5.0, "end_sec": 10.0},
                "clip_path": str(second_clip),
            },
        ]
    }


def test_segment_media_reader_maps_absolute_times_to_cached_clips(
    tmp_path,
    monkeypatch,
) -> None:
    first_clip = tmp_path / "segment_0001.mp4"
    second_clip = tmp_path / "segment_0002.mp4"
    first_clip.write_bytes(b"first")
    second_clip.write_bytes(b"second")
    seek_calls: dict[str, list[float]] = {}

    class Capture:
        def __init__(self, path: str) -> None:
            self.path = path
            seek_calls[path] = []

        def isOpened(self) -> bool:
            return True

        def set(self, _property: int, value: float) -> None:
            seek_calls[self.path].append(value)

        def read(self):
            value = 1 if self.path == str(first_clip) else 2
            return True, np.full((2, 2, 3), value, dtype=np.uint8)

        def release(self) -> None:
            return None

    monkeypatch.setattr("cutmaster.planner.media.cv2.VideoCapture", Capture)
    reader = SegmentMediaReader(
        tmp_path / "source.mp4",
        _video_description(first_clip, second_clip),
    )

    frames = reader.sample_frames([1.0, 5.0, 9.5])

    assert [int(frame[0, 0, 0]) for frame in frames] == [1, 2, 2]
    assert seek_calls[str(first_clip)] == [1000.0]
    assert seek_calls[str(second_clip)] == [0.0, 4500.0]


def test_segment_media_reader_rebuilds_missing_cache(
    tmp_path,
    monkeypatch,
) -> None:
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"source")
    first_clip = tmp_path / "segments" / "segment_0001.mp4"
    second_clip = tmp_path / "segments" / "segment_0002.mp4"
    first_clip.parent.mkdir()
    first_clip.write_bytes(b"first")
    commands: list[list[str]] = []

    def run(command: list[str], check: bool) -> None:
        assert check is True
        commands.append(command)
        Path(command[-1]).write_bytes(b"rebuilt")

    class Capture:
        def __init__(self, path: str) -> None:
            self.path = path

        def isOpened(self) -> bool:
            return Path(self.path).is_file()

        def set(self, _property: int, _value: float) -> None:
            return None

        def read(self):
            return True, np.zeros((2, 2, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    monkeypatch.setattr("cutmaster.planner.media.subprocess.run", run)
    monkeypatch.setattr("cutmaster.planner.media.cv2.VideoCapture", Capture)
    reader = SegmentMediaReader(
        source_video,
        _video_description(first_clip, second_clip),
    )

    frames = reader.sample_frames([7.0])

    assert len(frames) == 1
    assert second_clip.read_bytes() == b"rebuilt"
    assert len(commands) == 1
    assert commands[0][commands[0].index("-ss") + 1] == "5.000000"
    assert commands[0][commands[0].index("-t") + 1] == "5.000000"
    assert commands[0][commands[0].index("-i") + 1] == str(source_video)


def test_segment_media_reader_clamps_tail_sample_to_last_video_frame(
    tmp_path,
    monkeypatch,
) -> None:
    first_clip = tmp_path / "segment_0001.mp4"
    second_clip = tmp_path / "segment_0002.mp4"
    first_clip.write_bytes(b"first")
    second_clip.write_bytes(b"second")
    seek_calls: list[float] = []

    class Capture:
        def __init__(self, path: str) -> None:
            self.path = path
            self.seek_time_ms = 0.0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            if property_id == cv2.CAP_PROP_FPS:
                return 25.0
            if property_id == cv2.CAP_PROP_FRAME_COUNT:
                return 120.0
            return 0.0

        def set(self, property_id: int, value: float) -> None:
            if property_id == cv2.CAP_PROP_POS_MSEC:
                self.seek_time_ms = value
                seek_calls.append(value)

        def read(self):
            if self.seek_time_ms > 4760.0:
                return False, None
            return True, np.full((2, 2, 3), 7, dtype=np.uint8)

        def release(self) -> None:
            return None

    monkeypatch.setattr("cutmaster.planner.media.cv2.VideoCapture", Capture)
    reader = SegmentMediaReader(
        tmp_path / "source.mp4",
        _video_description(first_clip, second_clip),
    )

    frames = reader.sample_frames([4.999999])

    assert len(frames) == 1
    assert int(frames[0][0, 0, 0]) == 7
    assert seek_calls == [4760.0]
