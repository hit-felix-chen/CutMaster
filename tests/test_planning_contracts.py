from pathlib import Path

import pytest

from cutmaster.contracts.planning import RenderPlan


def _media(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"media")
    return path


def test_render_plan_round_trip_and_frame_timeline(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "cutmaster.contracts.planning.media_duration",
        lambda _path: 10.0,
    )
    source = _media(tmp_path, "source.mp4")
    bgm = _media(tmp_path, "bgm.wav")
    plan = RenderPlan.create(
        source_video=source,
        background_music=bgm,
        fps=30,
        clips=[
            {
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            },
            {
                "timestamp": "00:00:02,000-00:00:03,000",
                "output_frame_range": [30, 60],
            },
        ],
        planning_metadata={"prompt": "test"},
    )
    path = tmp_path / "render_plan.json"
    plan.write(path)

    loaded = RenderPlan.read(path)

    assert loaded.plan_id == plan.plan_id
    assert loaded.total_frames == 60
    assert loaded.duration_sec == 2.0


def test_render_plan_rejects_renderer_transient_audio_path(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.contracts.planning.media_duration",
        lambda _path: 10.0,
    )
    source = _media(tmp_path, "source.mp4")
    bgm = _media(tmp_path, "bgm.wav")

    with pytest.raises(ValueError, match="prepared audio paths"):
        RenderPlan.create(
            source_video=source,
            background_music=bgm,
            fps=30,
            clips=[
                {
                    "timestamp": "00:00:00,000-00:00:01,000",
                    "output_frame_range": [0, 30],
                    "dialogue_anchor": {
                        "anchor_id": "a1",
                        "prepared_audio_path": "/tmp/transient.wav",
                    },
                }
            ],
            planning_metadata={},
        )
