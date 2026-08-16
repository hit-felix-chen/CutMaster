from pathlib import Path

import pytest

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint
from cutmaster.workflow.contracts.render_plan import RenderPlan


def _identity() -> dict[str, object]:
    return {
        "video_material_id": MaterialId.new(),
        "video_expected_fingerprint": MaterialFingerprint("a" * 64),
        "music_material_id": MaterialId.new(),
        "music_expected_fingerprint": MaterialFingerprint("b" * 64),
    }


def test_render_plan_round_trip_and_frame_timeline(tmp_path: Path) -> None:
    plan = RenderPlan.create(
        **_identity(),
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
        planners_metadata={"editing_intent": "test"},
    )
    path = tmp_path / "render_plan.json"
    plan.write(path)

    loaded = RenderPlan.read(path)

    assert loaded == plan
    assert loaded.total_frames == 60
    assert loaded.duration_sec == 2.0
    payload = loaded.to_dict()
    assert "source_video" not in payload
    assert "background_music" not in payload


def test_render_plan_rejects_renderer_transient_audio_path() -> None:
    with pytest.raises(ValueError, match="prepared audio paths"):
        RenderPlan.create(
            **_identity(),
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
            planners_metadata={},
        )
