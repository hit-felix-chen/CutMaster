import threading
import json

import numpy as np
import pytest

from cutmaster.configuration.schema import (
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.analyser.service import (
    _annotate_segments,
    _detect_full_video_shots,
    _group_dialogue,
    _raw_segments,
    _sample_shot_frames,
    _summarize_segments,
    _video_summary_context,
    _validate_dialogue_segments,
    _validate_shot_annotation,
    analyse_video_material,
)
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.analyser import ShotAnnotationDetails
from cutmaster.contracts.video import (
    CameraAngle,
    CameraMovement,
    InteriorExterior,
    SegmentContentType,
    ShotScale,
    TimeOfDay,
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


def test_video_summary_context_excludes_shots_and_keeps_complete_dialogue() -> None:
    full_dialogue = [
        {
            "dialogue_id": 1,
            "start_sec": 1.0,
            "end_sec": 2.0,
            "speaker": "Speaker 1",
            "text": "Complete line.",
        }
    ]
    context = _video_summary_context(
        {
            "segments": [
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 0.0, "end_sec": 3.0},
                    "segment_summary": "A visible event.",
                    "shots": [{"shot_id": "shot_00001"}],
                }
            ]
        },
        full_dialogue,
    )

    assert context["segments"] == [
        {
            "segment_id": "segment_0001",
            "time_range": {"start_sec": 0.0, "end_sec": 3.0},
            "segment_summary": "A visible event.",
        }
    ]
    assert context["full_dialogue"] == full_dialogue


def test_shot_sampling_repeats_last_decodable_frame(monkeypatch, tmp_path) -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.reads = 0
            self.released = False

        def isOpened(self) -> bool:
            return True

        def set(self, _property, _value) -> None:
            pass

        def read(self):
            self.reads += 1
            if self.reads > 4:
                return False, None
            return True, np.full((8, 8, 3), self.reads, dtype=np.uint8)

        def release(self) -> None:
            self.released = True

    capture = FakeCapture()
    monkeypatch.setattr(
        "cutmaster.analyser.service.cv2.VideoCapture",
        lambda _path: capture,
    )

    images, sampled_times = _sample_shot_frames(
        tmp_path / "short.mp4",
        0.0,
        0.167,
        10.0,
        5,
    )

    assert len(images) == 5
    assert len(sampled_times) == 5
    assert images[-1] == images[-2]
    assert sampled_times[-1] == sampled_times[-2]
    assert capture.released


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


def test_shot_prompt_contract_lists_every_allowed_categorical_value() -> None:
    shot = _shots(1)[0]
    shot["dialogue"] = []
    segment = {
        "segment_id": "segment_0001",
        "has_dialogue": False,
        "speech_mode": "none",
        "dialogue_context": None,
    }

    package = prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.SHOT_ANNOTATION,
        ShotAnnotationDetails(
            segment=segment,
            shot=shot,
            sampled_frame_times_sec=[0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )
    properties = package.response_contract.schema["properties"]

    assert properties["content_type"]["enum"] == [
        SegmentContentType.LANDSCAPE.value,
        SegmentContentType.EMOTIONAL.value,
        SegmentContentType.PANTOMIME.value,
    ]
    assert properties["scene"]["properties"]["interior_exterior"]["enum"] == [
        value.value for value in InteriorExterior
    ]
    assert properties["scene"]["properties"]["time_of_day"]["enum"] == [
        value.value for value in TimeOfDay
    ]
    assert properties["characters"]["items"]["properties"]["identity_likert"][
        "enum"
    ] == [1, 2, 3, 4, 5]
    assert properties["shot_scale"]["enum"] == [
        value.value for value in ShotScale
    ]
    assert properties["camera_angle"]["enum"] == [
        value.value for value in CameraAngle
    ]
    assert properties["camera_movement"]["enum"] == [
        value.value for value in CameraMovement
    ]

    prompt = package.user_prompt
    for allowed_value in (
        *InteriorExterior,
        *TimeOfDay,
        *ShotScale,
        *CameraAngle,
        *CameraMovement,
    ):
        assert f'"{allowed_value.value}"' in prompt
    assert "medium_close_up is not an allowed value" in prompt


def test_dialogue_prompt_contract_lists_every_allowed_speech_mode() -> None:
    captured = {}

    class Context:
        def call_prompt(self, **kwargs):
            package = kwargs["package"]
            captured["prompt"] = package.user_prompt
            parsed = {
                "segments": [
                    {
                        "first_dialogue_id": 1,
                        "last_dialogue_id": 1,
                        "speech_mode": "monologue",
                        "topic": "introduction",
                        "summary": "The speaker introduces the topic",
                    }
                ]
            }
            package.response_contract.validate_structure(parsed)
            return kwargs["validate_business"](parsed)

    _group_dialogue(
        Context(),
        LLMConfig(model="test", base_url="", api_key="test"),
        [_dialogue(1, 0.1, 0.2)],
        _shots(1),
    )

    assert '"dialogue"' in captured["prompt"]
    assert '"monologue"' in captured["prompt"]


def test_shot_annotation_normalization_preserves_shot_id() -> None:
    normalized = _validate_shot_annotation(
        {
            "shot_id": "shot_00001",
            "visual_description": "A woman stands beside a window",
            "dominant_action": "Standing",
            "content_type": "emotional",
            "narrative_function": "Shows a reflective pause",
            "emotional_tone": "pensive",
            "emotional_intensity": 0.4,
            "scene": {
                "interior_exterior": "interior",
                "location": "apartment room",
                "time_of_day": "day",
                "environment_lighting": ["window light"],
                "color_palette": ["blue", "beige"],
                "color_tone": "muted",
                "set_details": ["window"],
                "weather": "",
                "atmosphere": "quiet",
            },
            "characters": [],
            "shot_scale": "medium",
            "camera_angle": "eye_level",
            "camera_movement": "static",
            "composition": "subject beside the window",
            "visual_evidence": "The same framing appears in all five frames",
        },
        "shot_00001",
        False,
    )

    assert normalized["shot_id"] == "shot_00001"


def test_full_video_shot_detection_builds_complete_boundary_partition(monkeypatch) -> None:
    monkeypatch.setattr(
        "cutmaster.analyser.service.detect_source_cuts",
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


def test_dialogue_segmentation_merges_adjacent_groups_sharing_one_shot() -> None:
    shots = _shots(2)
    dialogue = [
        _dialogue(1, 0.1, 0.2),
        _dialogue(2, 0.3, 0.4),
        _dialogue(3, 0.5, 0.6),
    ]
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
            {
                "first_dialogue_id": 3,
                "last_dialogue_id": 3,
                "speech_mode": "monologue",
                "participants": ["Speaker 3"],
                "topic": "third",
                "summary": "third",
                "grouping_reason": "third passage",
            },
        ]
    }
    groups = _validate_dialogue_segments(parsed, dialogue, shots)

    assert len(groups) == 1
    assert groups[0]["dialogue_ids"] == [1, 2, 3]
    assert groups[0]["first_shot_index"] == 0
    assert groups[0]["last_shot_index"] == 0
    assert groups[0]["speech_mode"] == "dialogue"
    assert "share a PySceneDetect Shot" in groups[0]["grouping_reason"]


def test_dialogue_segmentation_rejects_overlapping_dialogue_ranges() -> None:
    shots = _shots(3)
    dialogue = [
        _dialogue(1, 0.1, 0.2),
        _dialogue(2, 1.1, 1.2),
        _dialogue(3, 2.1, 2.2),
    ]
    parsed = {
        "segments": [
            {
                "first_dialogue_id": 1,
                "last_dialogue_id": 2,
                "speech_mode": "dialogue",
                "topic": "first",
                "summary": "first",
            },
            {
                "first_dialogue_id": 2,
                "last_dialogue_id": 3,
                "speech_mode": "dialogue",
                "topic": "second",
                "summary": "second",
            },
        ]
    }

    with pytest.raises(ValueError, match="overlapping dialogue IDs"):
        _validate_dialogue_segments(parsed, dialogue, shots)


def test_dialogue_segmentation_rejects_missing_dialogue_ids() -> None:
    shots = _shots(3)
    dialogue = [
        _dialogue(1, 0.1, 0.2),
        _dialogue(2, 1.1, 1.2),
        _dialogue(3, 2.1, 2.2),
    ]
    parsed = {
        "segments": [
            {
                "first_dialogue_id": 1,
                "last_dialogue_id": 1,
                "speech_mode": "monologue",
                "topic": "first",
                "summary": "first",
            },
            {
                "first_dialogue_id": 3,
                "last_dialogue_id": 3,
                "speech_mode": "monologue",
                "topic": "third",
                "summary": "third",
            },
        ]
    }

    with pytest.raises(ValueError, match="omit one or more dialogue IDs"):
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
    tmp_path,
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
        "cutmaster.analyser.service._sample_shot_frames",
        lambda *_args: (
            ["data:image/jpeg;base64,stub"] * 5,
            [0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )
    barrier = threading.Barrier(2)
    call_order: dict[str, list[str]] = {"0": [], "1": []}
    lock = threading.Lock()

    class Context:
        def call_prompt(self, **kwargs):
            shot_id = kwargs["package"].operation.split()[-1]
            segment_id, shot_index = shot_id.removeprefix("shot_").split("_")
            with lock:
                call_order[segment_id].append(shot_index)
            if shot_index == "0":
                barrier.wait(timeout=2)
            parsed = {
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
            kwargs["package"].response_contract.validate_structure(parsed)
            return kwargs["validate_business"](parsed)

    descriptions = _annotate_segments(
        segments,
        Context(),
        VLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        5,
        tmp_path / "shot_annotations",
    )
    assert len(descriptions) == 2
    assert call_order == {"0": ["0", "1"], "1": ["0", "1"]}

    class SummaryContext:
        calls: list[str] = []

        def call_prompt(self, **kwargs):
            segment_id = kwargs["package"].operation.split()[-1]
            self.calls.append(segment_id)
            return {
                "segment_id": segment_id,
                "segment_summary": f"Concise summary for {segment_id}.",
            }

    summary_context = SummaryContext()
    summary_directory = tmp_path / "segment_summaries"
    summarized = _summarize_segments(
        descriptions,
        summary_context,
        LLMConfig(model="test", base_url="", api_key="test"),
        summary_directory,
    )
    assert [segment.segment_summary for segment in summarized] == [
        "Concise summary for segment_0.",
        "Concise summary for segment_1.",
    ]
    assert set(summary_context.calls) == {"segment_0", "segment_1"}

    class UnexpectedSummaryContext:
        def call_prompt(self, **_kwargs):
            raise AssertionError("Segment summary checkpoint should be reused")

    cached_summaries = _summarize_segments(
        descriptions,
        UnexpectedSummaryContext(),
        LLMConfig(model="test", base_url="", api_key="test"),
        summary_directory,
    )
    assert [segment.segment_summary for segment in cached_summaries] == [
        "Concise summary for segment_0.",
        "Concise summary for segment_1.",
    ]

    cached_descriptions = _annotate_segments(
        segments,
        Context(),
        VLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        5,
        tmp_path / "shot_annotations",
    )
    assert len(cached_descriptions) == 2
    assert call_order == {"0": ["0", "1"], "1": ["0", "1"]}


def test_shot_annotation_provider_rejection_is_checkpointed_and_nonfatal(
    monkeypatch,
    tmp_path,
) -> None:
    shot = _shots(1)[0]
    shot["dialogue"] = []
    segment = {
        "segment_id": "segment_0001",
        "time_range": {"start_sec": 0.0, "end_sec": 1.0},
        "clip_path": str(tmp_path / "segment.mp4"),
        "has_dialogue": False,
        "speech_mode": "none",
        "timeline_role": "opening",
        "dialogue_context": None,
        "shots": [shot],
    }
    monkeypatch.setattr(
        "cutmaster.analyser.service._sample_shot_frames",
        lambda *_args: (
            ["data:image/jpeg;base64,stub"] * 5,
            [0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )

    class RejectedContext:
        calls = 0

        def call_prompt(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("data_inspection_failed")

    annotation_directory = tmp_path / "shot_annotations"
    rejected_context = RejectedContext()
    descriptions = _annotate_segments(
        [segment],
        rejected_context,
        VLMConfig(model="test", base_url="", api_key="test"),
        5,
        annotation_directory,
    )

    assert rejected_context.calls == 1
    rejected_shot = descriptions[0].shots[0]
    assert rejected_shot.visual_annotation_status == "provider_rejected"
    assert rejected_shot.visual_annotation_failure == "data_inspection_failed"
    checkpoint = json.loads(
        (annotation_directory / "shot_00001.json").read_text(encoding="utf-8")
    )
    assert checkpoint["visual_annotation_status"] == "provider_rejected"
    assert checkpoint["visual_annotation_failure"] == "data_inspection_failed"
    assert "annotation" not in checkpoint

    class UnexpectedCallContext:
        def call_prompt(self, **_kwargs):
            raise AssertionError("Rejected Shot checkpoint should be reused")

    cached_descriptions = _annotate_segments(
        [segment],
        UnexpectedCallContext(),
        VLMConfig(model="test", base_url="", api_key="test"),
        5,
        annotation_directory,
    )
    assert (
        cached_descriptions[0].shots[0].visual_annotation_status
        == "provider_rejected"
    )


def test_analyser_reuses_shot_checkpoint_after_later_stage_failure(
    tmp_path,
    monkeypatch,
) -> None:
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"source")
    detection_calls = 0

    monkeypatch.setattr(
        "cutmaster.analyser.service.probe_media",
            lambda _path: {
                "duration": 2.0,
                "fps": 24.0,
                "width": 1920,
            "height": 1080,
            "has_audio": True,
        },
    )

    def detect(*_args, **_kwargs):
        nonlocal detection_calls
        detection_calls += 1
        return _shots(2), 24.0

    monkeypatch.setattr("cutmaster.analyser.service._detect_full_video_shots", detect)
    monkeypatch.setattr(
        "cutmaster.analyser.service.prepare_subtitles",
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
        "vlm_config": VLMConfig(model="test", base_url="", api_key="test"),
    }

    with pytest.raises(RuntimeError, match="stop after shot detection"):
        analyse_video_material(**kwargs)
    with pytest.raises(RuntimeError, match="stop after shot detection"):
        analyse_video_material(**kwargs)

    assert detection_calls == 1
    assert list((tmp_path / "materials").glob("**/shots.json"))
