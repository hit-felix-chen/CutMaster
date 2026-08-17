import json

import numpy as np
import pytest

from cutmaster.configuration.schema import SceneSegmentationConfig
from cutmaster.workflow.analyser.tools.scene_segmenter import (
    _resolve_frame_file,
    prepare_scene_frames,
)


def test_scene_frame_manifest_persists_filename_refs_only(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.released = False

        def isOpened(self) -> bool:
            return True

        def set(self, _property, _value) -> None:
            pass

        def read(self):
            return True, np.zeros((16, 24, 3), dtype=np.uint8)

        def release(self) -> None:
            self.released = True

    capture = FakeCapture()
    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.scene_segmenter.cv2.VideoCapture",
        lambda _path: capture,
    )
    frame_directory = tmp_path / "scene_frames"
    shots = [
        {
            "shot_id": "shot_00001",
            "time_range": {"start_sec": 0.0, "end_sec": 1.0},
        }
    ]

    frames = prepare_scene_frames(
        tmp_path / "source.mp4",
        shots,
        SceneSegmentationConfig(frames_per_shot=3),
        frame_directory,
    )

    assert capture.released
    assert frames["shot_00001"] == [
        {"file": "shot_00001_01.jpg", "time_sec": 0.166667},
        {"file": "shot_00001_02.jpg", "time_sec": 0.5},
        {"file": "shot_00001_03.jpg", "time_sec": 0.833333},
    ]
    manifest = json.loads(
        (frame_directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == {
        "schema_version": "2.0",
        "frame_cache_version": "2.0",
        "frames_per_shot": 3,
        "shots": frames,
    }
    assert '"path"' not in json.dumps(manifest)
    assert str(tmp_path) not in json.dumps(manifest)

    monkeypatch.setattr(
        "cutmaster.workflow.analyser.tools.scene_segmenter.cv2.VideoCapture",
        lambda _path: pytest.fail("current frame manifest should be reused"),
    )
    assert prepare_scene_frames(
        tmp_path / "source.mp4",
        shots,
        SceneSegmentationConfig(frames_per_shot=3),
        frame_directory,
    ) == frames


@pytest.mark.parametrize("file_ref", ["../escape.jpg", "/tmp/escape.jpg", ""])
def test_scene_frame_ref_must_be_a_local_filename(tmp_path, file_ref) -> None:
    with pytest.raises(ValueError, match="file reference"):
        _resolve_frame_file(tmp_path, file_ref)
