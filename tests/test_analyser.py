import threading

import pytest

from cutmaster.models import (
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
)
from cutmaster.analyser import (
    _annotate_segments,
    _detect_full_video_shots,
    _raw_segments,
    _validate_dialogue_segments,
    analyse_video_material,
)


def _shots(count: int = 6):
    return [
        {
            "shot_id": f"shot_{index + 1:05d}",
            "timestamp": (
                f"00:00:{index:02d},000-00:00:{index + 1:02d},000"
            ),
            "time_range": {
                "start_sec": float(index),
                "end_sec": float(index + 1),
            },
            "start_boundary": "video_start" if index == 0 else "adaptive_cut",
            "end_boundary": "video_end" if index == count - 1 else "adaptive_cut",
        }
        for index in range(count)
    ]


def _dialogue(dialogue_id: int, start: float, end: float):
    return {
        "dialogue_id": dialogue_id,
        "start_sec": start,
        "end_sec": end,
        "timestamp": "",
        "speaker": f"Speaker {dialogue_id}",
        "text": f"line {dialogue_id}",
    }


def _group(index: int, dialogue_id: int, shot_index: int):
    return {
        "dialogue_group_id": f"dialogue_group_{index:04d}",
        "first_dialogue_id": dialogue_id,
        "last_dialogue_id": dialogue_id,
        "dialogue_ids": [dialogue_id],
        "speech_mode": "monologue",
        "participants": [f"Speaker {dialogue_id}"],
        "topic": f"topic {dialogue_id}",
        "summary": f"summary {dialogue_id}",
        "grouping_reason": "continuous passage",
        "first_shot_index": shot_index,
        "last_shot_index": shot_index,
    }


def test_full_video_shot_detection_builds_complete_boundary_partition(monkeypatch) -> None:
    monkeypatch.setattr(
        "cutmaster.analyser.detect_source_cuts",
        lambda *_args, **_kwargs: ([1.25, 3.5], 24.0),
    )
    shots, fps = _detect_full_video_shots(
        __import__("pathlib").Path("video.mp4"),
        5.0,
        ShotDetectionConfig(),
    )
    assert fps == 24.0
    assert [
        (shot["time_range"]["start_sec"], shot["time_range"]["end_sec"])
        for shot in shots
    ] == [(0.0, 1.25), (1.25, 3.5), (3.5, 5.0)]
    assert shots[0]["start_boundary"] == "video_start"
    assert shots[-1]["end_boundary"] == "video_end"


def test_dialogue_segmentation_rejects_boundaries_inside_one_shot() -> None:
    shots = _shots(2)
    dialogue = [_dialogue(1, 0.1, 0.3), _dialogue(2, 0.5, 0.7)]
    parsed = {
        "segments": [
            {
                "first_dialogue_id": 1,
                "last_dialogue_id": 1,
                "speech_mode": "monologue",
                "participants": ["Speaker 1"],
                "topic": "first",
                "summary": "first",
                "grouping_reason": "first passage",
            },
            {
                "first_dialogue_id": 2,
                "last_dialogue_id": 2,
                "speech_mode": "monologue",
                "participants": ["Speaker 2"],
                "topic": "second",
                "summary": "second",
                "grouping_reason": "second passage",
            },
        ]
    }
    with pytest.raises(ValueError, match="overlapping Shots"):
        _validate_dialogue_segments(parsed, dialogue, shots)


def test_silent_shots_between_dialogue_groups_become_independent_segments() -> None:
    shots = _shots()
    dialogue = [_dialogue(1, 1.2, 1.8), _dialogue(2, 4.2, 4.8)]
    segments = _raw_segments(
        shots,
        dialogue,
        [_group(1, 1, 1), _group(2, 2, 4)],
    )
    assert [
        (
            segment["time_range"]["start_sec"],
            segment["time_range"]["end_sec"],
            segment["has_dialogue"],
        )
        for segment in segments
    ] == [
        (0.0, 1.0, False),
        (1.0, 2.0, True),
        (2.0, 4.0, False),
        (4.0, 5.0, True),
        (5.0, 6.0, False),
    ]
    assert segments[0]["timeline_role"] == "opening"
    assert segments[-1]["timeline_role"] == "ending"


def test_segment_annotation_is_parallel_but_shots_are_serial_within_segment(
    monkeypatch,
) -> None:
    segments = []
    for segment_index in range(2):
        start = float(segment_index * 2)
        segment_shots = _shots(2)
        for shot_index, shot in enumerate(segment_shots):
            shot["shot_id"] = f"shot_{segment_index}_{shot_index}"
            shot["time_range"] = {
                "start_sec": start + shot_index,
                "end_sec": start + shot_index + 1,
            }
            shot["dialogue"] = []
        segments.append(
            {
                "segment_id": f"segment_{segment_index}",
                "time_range": {"start_sec": start, "end_sec": start + 2},
                "clip_path": f"/tmp/segment_{segment_index}.mp4",
                "has_dialogue": False,
                "speech_mode": "none",
                "timeline_role": "opening" if segment_index == 0 else "ending",
                "dialogue_context": None,
                "shots": segment_shots,
            }
        )

    monkeypatch.setattr(
        "cutmaster.analyser._sample_shot_frames",
        lambda *_args: (
            ["data:image/jpeg;base64,stub"] * 5,
            [0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )
    barrier = threading.Barrier(2)
    call_order: dict[str, list[str]] = {"0": [], "1": []}
    lock = threading.Lock()

    class Context:
        def get_successful_call_result(self, _operation):
            return None

        def call_json(self, **kwargs):
            shot_id = kwargs["operation"].split()[-1]
            segment_id, shot_index = shot_id.removeprefix("shot_").split("_")
            with lock:
                call_order[segment_id].append(shot_index)
            if shot_index == "0":
                barrier.wait(timeout=2)
            return kwargs["validate"](
                {
                    "shot_id": shot_id,
                    "visual_description": "A visible landscape",
                    "dominant_action": "Clouds move",
                    "content_type": "landscape",
                    "narrative_function": "Establishes the location",
                    "emotional_tone": "calm",
                    "emotional_intensity": 0.2,
                    "scene": {
                        "interior_exterior": "exterior",
                        "location": "open hillside",
                        "time_of_day": "day",
                        "environment_lighting": ["sunlight"],
                        "color_palette": ["green", "blue"],
                        "color_tone": "natural",
                        "set_details": ["grass", "sky"],
                        "weather": "clear",
                        "atmosphere": "quiet",
                    },
                    "characters": [],
                    "shot_scale": "wide",
                    "camera_angle": "eye_level",
                    "camera_movement": "static",
                    "composition": "horizon across the upper third",
                    "visual_evidence": "five frames show the same hillside",
                }
            )

    descriptions = _annotate_segments(
        segments,
        Context(),
        LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        5,
    )
    assert len(descriptions) == 2
    assert call_order == {"0": ["0", "1"], "1": ["0", "1"]}


def test_analyser_reuses_shot_checkpoint_after_later_stage_failure(
    tmp_path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"source")
    detection_calls = 0

    monkeypatch.setattr(
        "cutmaster.analyser.probe_media",
        lambda _path: {
            "duration": 2.0,
            "width": 1920,
            "height": 1080,
            "has_audio": True,
        },
    )

    def detect(*_args, **_kwargs):
        nonlocal detection_calls
        detection_calls += 1
        return _shots(2), 24.0

    monkeypatch.setattr("cutmaster.analyser._detect_full_video_shots", detect)
    monkeypatch.setattr(
        "cutmaster.analyser.prepare_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after shot detection")
        ),
    )
    kwargs = {
        "video_path": video_path,
        "video_title": "Source",
        "provided_subtitle": None,
        "material_config": MaterialAnalysisConfig(tmp_path / "materials"),
        "detection_config": ShotDetectionConfig(),
        "asr_config": ASRConfig(backend="bailian", api_key="test"),
        "annotation_config": ShotAnnotationConfig(),
        "llm_config": LLMConfig(model="test", base_url="", api_key="test"),
    }

    with pytest.raises(RuntimeError, match="stop after shot detection"):
        analyse_video_material(**kwargs)
    with pytest.raises(RuntimeError, match="stop after shot detection"):
        analyse_video_material(**kwargs)

    assert detection_calls == 1
    assert list((tmp_path / "materials").glob("**/shots.json"))
