import threading
import base64
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from cutmaster.configuration.schema import (
    ASRConfig,
    LLMConfig,
    MaterialAnalysisConfig,
    SceneSegmentationConfig,
    ShotAnnotationConfig,
    ShotDetectionConfig,
    VLMConfig,
)
from cutmaster.analyser.material_analyst import (
    _annotate_segments,
    _detect_full_video_shots,
    _sample_shot_frames,
    _shot_frame_contact_sheet,
    _summarize_segments,
    _video_summary_context,
    _validate_shot_annotation,
    _analyse_video_material,
)
from cutmaster.analyser.tools.scene_segmenter import (
    _validate_window_decisions,
    build_scene_windows,
    build_segments_from_scene_boundaries,
    detect_scene_boundaries,
)
from cutmaster.analyser.tools.cache import reuse_compatible_stage_checkpoints
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.analyser import (
    SceneBoundaryDetectionDetails,
    SegmentShotAnnotationDetails,
)
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
        "cutmaster.analyser.material_analyst.cv2.VideoCapture",
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


def test_shot_frame_contact_sheet_preserves_five_numbered_frames() -> None:
    images = []
    for index in range(5):
        frame = np.full((24, 32, 3), index * 40, dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", frame)
        assert ok
        images.append(
            "data:image/jpeg;base64,"
            + base64.b64encode(encoded.tobytes()).decode("ascii")
        )

    contact_sheet = _shot_frame_contact_sheet(images)

    assert contact_sheet.startswith("data:image/jpeg;base64,")
    encoded_sheet = np.frombuffer(
        base64.b64decode(contact_sheet.partition(",")[2]),
        dtype=np.uint8,
    )
    decoded = cv2.imdecode(encoded_sheet, cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape[:2] == (48, 96)


def test_shot_prompt_contract_lists_every_allowed_categorical_value() -> None:
    shot = _shots(1)[0]
    shot["dialogue"] = []
    segment = {
        "segment_id": "segment_0001",
        "has_dialogue": False,
        "speech_mode": "none",
        "shots": [shot],
    }

    package = prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.SHOT_ANNOTATION,
        SegmentShotAnnotationDetails(
            segment=segment,
            sampled_frame_times_by_shot={
                shot["shot_id"]: [0.1, 0.3, 0.5, 0.7, 0.9]
            },
        ),
    )
    properties = package.response_contract.schema["properties"]["shots"][
        "prefixItems"
    ][0]["properties"]

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


def test_scene_boundary_prompt_targets_only_focus_shots() -> None:
    shots = _shots(20)
    for shot in shots:
        shot["dialogue"] = []
    focus_ids = [shot["shot_id"] for shot in shots[5:15]]
    package = prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.SCENE_BOUNDARY_DETECTION,
        SceneBoundaryDetectionDetails(
            window_id="scene_window_00001",
            shots=shots,
            focus_shot_ids=focus_ids,
        ),
    )

    decision_schema = package.response_contract.schema["properties"]["decisions"]
    assert decision_schema["minItems"] == 10
    assert decision_schema["maxItems"] == 10
    assert decision_schema["items"]["properties"]["shot_id"]["enum"] == focus_ids
    assert package.context_keys == ()
    assert package.modality == "text_and_images"
    assert "full transcript" not in package.user_prompt.lower()


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
        "cutmaster.analyser.material_analyst.detect_source_cuts",
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


@pytest.mark.parametrize("shot_count", [1, 5, 20, 21, 37])
def test_scene_windows_cover_every_non_final_shot_once(shot_count: int) -> None:
    windows = build_scene_windows(shot_count, SceneSegmentationConfig())
    focus_indexes = [index for window in windows for index in window.focus_indexes]
    assert focus_indexes == list(range(max(0, shot_count - 1)))
    assert all(len(window.context_indexes) <= 20 for window in windows)
    assert all(
        set(window.focus_indexes).issubset(window.context_indexes)
        for window in windows
    )


def test_scene_boundary_validation_rejects_wrong_focus_order() -> None:
    with pytest.raises(ValueError, match="exact focus Shot order"):
        _validate_window_decisions(
            {
                "decisions": [
                    {
                        "shot_id": "shot_00002",
                        "is_scene_end": False,
                        "confidence_likert": 4,
                    },
                    {
                        "shot_id": "shot_00001",
                        "is_scene_end": True,
                        "confidence_likert": 5,
                    },
                ]
            },
            ["shot_00001", "shot_00002"],
        )


def test_scene_boundaries_build_complete_segment_partition() -> None:
    shots = _shots(6)
    dialogue = [
        {
            **_dialogue(1, 1.2, 1.8),
            "covering_shot_ids": ["shot_00002"],
        },
        {
            **_dialogue(2, 4.2, 4.8),
            "covering_shot_ids": ["shot_00005"],
        },
    ]
    decisions = [
        {
            "shot_id": shot["shot_id"],
            "is_scene_end": shot["shot_id"] in {"shot_00002", "shot_00004"},
            "confidence_likert": 4,
        }
        for shot in shots[:-1]
    ]
    segments = build_segments_from_scene_boundaries(shots, dialogue, decisions)

    assert [segment["time_range"] for segment in segments] == [
        {"start_sec": 0.0, "end_sec": 2.0},
        {"start_sec": 2.0, "end_sec": 4.0},
        {"start_sec": 4.0, "end_sec": 6.0},
    ]
    assert [segment["has_dialogue"] for segment in segments] == [True, False, True]
    assert {segment["timeline_role"] for segment in segments} == {"body"}
    assert [
        shot["shot_id"] for segment in segments for shot in segment["shots"]
    ] == [shot["shot_id"] for shot in shots]


def test_scene_boundary_windows_are_checkpointed_independently(
    monkeypatch,
    tmp_path,
) -> None:
    shots = _shots(21)
    stub = tmp_path / "frame.jpg"
    stub.write_bytes(b"jpeg")
    frame_map = {
        shot["shot_id"]: [
            {"path": str(stub), "time_sec": float(index)}
            for index in range(3)
        ]
        for shot in shots
    }
    monkeypatch.setattr(
        "cutmaster.analyser.tools.scene_segmenter.prepare_scene_frames",
        lambda *_args, **_kwargs: frame_map,
    )

    class Context:
        calls = 0

        def call_prompt(self, **kwargs):
            self.calls += 1
            focus_ids = kwargs["package"].response_contract.schema["properties"][
                "decisions"
            ]["items"]["properties"]["shot_id"]["enum"]
            parsed = {
                "decisions": [
                    {
                        "shot_id": shot_id,
                        "is_scene_end": False,
                        "confidence_likert": 4,
                    }
                    for shot_id in focus_ids
                ]
            }
            assert len(kwargs["image_data_urls"]) == 60
            return kwargs["validate_business"](parsed)

    context = Context()
    decisions = detect_scene_boundaries(
        Path("source.mp4"),
        shots,
        [],
        context,
        VLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        SceneSegmentationConfig(),
        tmp_path / "frames",
        tmp_path / "windows",
    )
    assert context.calls == 2
    assert [item["shot_id"] for item in decisions] == [
        shot["shot_id"] for shot in shots[:-1]
    ]

    class UnexpectedContext:
        def call_prompt(self, **_kwargs):
            raise AssertionError("Scene boundary window checkpoint should be reused")

    cached = detect_scene_boundaries(
        Path("source.mp4"),
        shots,
        [],
        UnexpectedContext(),
        VLMConfig(model="test", base_url="", api_key="test"),
        SceneSegmentationConfig(),
        tmp_path / "frames",
        tmp_path / "windows",
    )
    assert cached == decisions


def test_segment_annotation_uses_one_parallel_vlm_call_per_segment(
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
                "dialogue_items": [],
                "shots": segment_shots,
            }
        )

    monkeypatch.setattr(
        "cutmaster.analyser.material_analyst._sample_shot_frames",
        lambda *_args: (
            ["data:image/jpeg;base64,stub"] * 5,
            [0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )
    barrier = threading.Barrier(2)
    call_order: list[str] = []
    lock = threading.Lock()

    class Context:
        def call_prompt(self, **kwargs):
            segment_id = kwargs["package"].operation.split()[-1]
            with lock:
                call_order.append(segment_id)
            barrier.wait(timeout=2)
            source_segment = next(
                segment
                for segment in segments
                if segment["segment_id"] == segment_id
            )
            parsed = {
                "shots": [
                    {
                        "shot_id": shot["shot_id"],
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
                    for shot in source_segment["shots"]
                ]
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
    assert set(call_order) == {"segment_0", "segment_1"}
    assert len(call_order) == 2

    class SummaryContext:
        calls: list[str] = []

        def call_prompt(self, **kwargs):
            segment_id = kwargs["package"].operation.split()[-1]
            self.calls.append(segment_id)
            return {
                "segment_id": segment_id,
                "segment_summary": f"Concise summary for {segment_id}.",
                "timeline_role": "body",
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
    assert len(call_order) == 2


def test_segment_annotation_packs_frames_when_provider_image_limit_is_exceeded(
    monkeypatch,
    tmp_path,
) -> None:
    segment_shots = _shots(2)
    for shot in segment_shots:
        shot["dialogue"] = []
    segment = {
        "segment_id": "segment_oversized",
        "time_range": {"start_sec": 0.0, "end_sec": 2.0},
        "clip_path": str(tmp_path / "segment.mp4"),
        "has_dialogue": False,
        "speech_mode": "none",
        "timeline_role": "body",
        "dialogue_items": [],
        "shots": segment_shots,
    }
    monkeypatch.setattr(
        "cutmaster.analyser.material_analyst._sample_shot_frames",
        lambda *_args: (
            ["data:image/jpeg;base64,stub"] * 5,
            [0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )
    packed_calls = 0

    def pack(images):
        nonlocal packed_calls
        packed_calls += 1
        assert len(images) == 5
        return "data:image/jpeg;base64,packed"

    monkeypatch.setattr(
        "cutmaster.analyser.material_analyst._shot_frame_contact_sheet",
        pack,
    )

    class InspectContext:
        def call_prompt(self, **kwargs):
            assert len(kwargs["image_data_urls"]) == 2
            assert all(
                "contact sheet" in label
                for label in kwargs["image_labels"]
            )
            assert "one contact sheet per Shot" in kwargs["package"].user_prompt
            raise RuntimeError("inspection complete")

    with pytest.raises(RuntimeError, match="inspection complete"):
        _annotate_segments(
            [segment],
            InspectContext(),
            VLMConfig(model="test", base_url="", api_key="test"),
            5,
            tmp_path / "shot_annotations",
            max_images_per_request=2,
        )

    assert packed_calls == 2


def test_long_segment_annotation_uses_serial_twenty_shot_batches(
    monkeypatch,
    tmp_path,
) -> None:
    segment_shots = _shots(21)
    for shot in segment_shots:
        shot["dialogue"] = []
    segment = {
        "segment_id": "segment_long",
        "time_range": {"start_sec": 0.0, "end_sec": 21.0},
        "clip_path": str(tmp_path / "segment.mp4"),
        "has_dialogue": False,
        "speech_mode": "none",
        "timeline_role": "body",
        "dialogue_items": [],
        "shots": segment_shots,
    }
    monkeypatch.setattr(
        "cutmaster.analyser.material_analyst._sample_shot_frames",
        lambda *_args: (
            ["data:image/jpeg;base64,stub"] * 5,
            [0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )
    monkeypatch.setattr(
        "cutmaster.analyser.material_analyst._shot_frame_contact_sheet",
        lambda _images: "data:image/jpeg;base64,packed",
    )

    class Context:
        calls: list[tuple[str, int]] = []

        def call_prompt(self, **kwargs):
            package = kwargs["package"]
            shot_schemas = package.response_contract.schema["properties"][
                "shots"
            ]["prefixItems"]
            shot_ids = [
                schema["properties"]["shot_id"]["const"]
                for schema in shot_schemas
            ]
            self.calls.append((package.operation, len(shot_ids)))
            parsed = {
                "shots": [
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
                        "visual_evidence": "contact sheet shows a hillside",
                    }
                    for shot_id in shot_ids
                ]
            }
            assert len(kwargs["image_data_urls"]) == len(shot_ids)
            return kwargs["validate_business"](parsed)

    annotation_directory = tmp_path / "shot_annotations"
    context = Context()
    descriptions = _annotate_segments(
        [segment],
        context,
        VLMConfig(model="test", base_url="", api_key="test"),
        5,
        annotation_directory,
        max_images_per_request=250,
        max_shots_per_request=20,
    )

    assert len(descriptions[0].shots) == 21
    assert context.calls == [
        ("Segment Shot visual annotation segment_long batch 1/2", 20),
        ("Segment Shot visual annotation segment_long batch 2/2", 1),
    ]
    assert (annotation_directory / "segment_long" / "batch_001.json").is_file()
    assert (annotation_directory / "segment_long" / "batch_002.json").is_file()
    assert (annotation_directory / "segment_long.json").is_file()

    class UnexpectedContext:
        def call_prompt(self, **_kwargs):
            raise AssertionError("Merged Segment checkpoint should be reused")

    cached = _annotate_segments(
        [segment],
        UnexpectedContext(),
        VLMConfig(model="test", base_url="", api_key="test"),
        5,
        annotation_directory,
        max_images_per_request=250,
        max_shots_per_request=20,
    )
    assert len(cached[0].shots) == 21


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
        "dialogue_items": [],
        "shots": [shot],
    }
    monkeypatch.setattr(
        "cutmaster.analyser.material_analyst._sample_shot_frames",
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
        (annotation_directory / "segment_0001.json").read_text(encoding="utf-8")
    )
    assert checkpoint["visual_annotation_status"] == "provider_rejected"
    assert checkpoint["visual_annotation_failure"] == "data_inspection_failed"
    assert "annotation" not in checkpoint

    class UnexpectedCallContext:
        def call_prompt(self, **_kwargs):
            raise AssertionError("Rejected Segment checkpoint should be reused")

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
        "cutmaster.analyser.material_analyst.probe_media",
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

    monkeypatch.setattr("cutmaster.analyser.material_analyst._detect_full_video_shots", detect)
    monkeypatch.setattr(
        "cutmaster.analyser.material_analyst.prepare_subtitles",
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
        "scene_config": SceneSegmentationConfig(),
        "annotation_config": ShotAnnotationConfig(),
        "llm_config": LLMConfig(model="test", base_url="", api_key="test"),
        "vlm_config": VLMConfig(model="test", base_url="", api_key="test"),
    }

    with pytest.raises(RuntimeError, match="stop after shot detection"):
        _analyse_video_material(**kwargs)
    with pytest.raises(RuntimeError, match="stop after shot detection"):
        _analyse_video_material(**kwargs)

    assert detection_calls == 1
    assert list((tmp_path / "materials").glob("**/shots.json"))


def test_new_analysis_schema_reuses_compatible_upstream_stages(tmp_path) -> None:
    asset_directory = tmp_path / "asset"
    previous = asset_directory / "analysis-old"
    current = asset_directory / "analysis-new"
    previous.mkdir(parents=True)
    current.mkdir(parents=True)
    signature = {
        "source": {"path": "/source.mp4", "size": 1, "mtime_ns": 2},
        "scene_detection": {"adaptive_threshold": 2.0},
        "subtitle": {"backend": "bailian"},
        "llm": {"model": "test"},
    }
    (previous / "analysis_manifest.json").write_text(
        json.dumps(signature),
        encoding="utf-8",
    )
    (previous / "shots.json").write_text(
        json.dumps(_shots(2)),
        encoding="utf-8",
    )
    (previous / "source.srt").write_text("source", encoding="utf-8")
    (previous / "dialogue_merged.srt").write_text("merged", encoding="utf-8")
    (previous / "dialogues.json").write_text(
        json.dumps({"sentences": []}),
        encoding="utf-8",
    )

    reuse_compatible_stage_checkpoints(current, signature, 2.0)

    assert (current / "shots.json").is_file()
    assert (current / "source.srt").read_text(encoding="utf-8") == "source"
    assert (current / "dialogue_merged.srt").read_text(encoding="utf-8") == "merged"


def test_llm_change_reuses_raw_asr_but_not_dialogue_reconstruction(tmp_path) -> None:
    asset_directory = tmp_path / "asset"
    previous = asset_directory / "analysis-old"
    current = asset_directory / "analysis-new"
    previous.mkdir(parents=True)
    current.mkdir(parents=True)
    source_signature = {"path": "/source.mp4", "size": 1, "mtime_ns": 2}
    subtitle_signature = {"backend": "bailian"}
    (previous / "analysis_manifest.json").write_text(
        json.dumps(
            {
                "source": source_signature,
                "subtitle": subtitle_signature,
                "llm": {"model": "old"},
            }
        ),
        encoding="utf-8",
    )
    (previous / "source.srt").write_text("raw ASR", encoding="utf-8")
    (previous / "dialogue_merged.srt").write_text("old merge", encoding="utf-8")
    (previous / "dialogues.json").write_text(
        json.dumps({"sentences": []}),
        encoding="utf-8",
    )

    reuse_compatible_stage_checkpoints(
        current,
        {
            "source": source_signature,
            "subtitle": subtitle_signature,
            "llm": {"model": "new"},
        },
        2.0,
    )

    assert (current / "source.srt").read_text(encoding="utf-8") == "raw ASR"
    assert not (current / "dialogue_merged.srt").exists()
    assert not (current / "dialogues.json").exists()
