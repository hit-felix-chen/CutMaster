from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SpeechMode(StrEnum):
    NONE = "none"
    DIALOGUE = "dialogue"
    MONOLOGUE = "monologue"


class SegmentContentType(StrEnum):
    NARRATIVE = "narrative"
    LANDSCAPE = "landscape"
    EMOTIONAL = "emotional"
    PANTOMIME = "pantomime"


class TimelineRole(StrEnum):
    OPENING = "opening"
    BODY = "body"
    ENDING = "ending"


class InteriorExterior(StrEnum):
    INTERIOR = "interior"
    EXTERIOR = "exterior"


class TimeOfDay(StrEnum):
    DAWN = "dawn"
    DAY = "day"
    DUSK = "dusk"
    NIGHT = "night"


class BoundarySource(StrEnum):
    VIDEO_START = "video_start"
    ADAPTIVE_CUT = "adaptive_cut"
    VIDEO_END = "video_end"


class ShotScale(StrEnum):
    EXTREME_WIDE = "extreme_wide"
    WIDE = "wide"
    MEDIUM = "medium"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"


class CameraAngle(StrEnum):
    EYE_LEVEL = "eye_level"
    HIGH_ANGLE = "high_angle"
    LOW_ANGLE = "low_angle"
    OVERHEAD = "overhead"
    DUTCH_ANGLE = "dutch_angle"


class CameraMovement(StrEnum):
    STATIC = "static"
    PAN = "pan"
    TILT = "tilt"
    TRACKING = "tracking"
    HANDHELD = "handheld"
    ZOOM = "zoom"
    CRANE = "crane"


class VisualAnnotationStatus(StrEnum):
    COMPLETE = "complete"
    PROVIDER_REJECTED = "provider_rejected"


@dataclass(frozen=True)
class TimeRange:
    start_sec: float
    end_sec: float

    def __post_init__(self) -> None:
        if self.start_sec < 0:
            raise ValueError("start_sec must be non-negative")
        if self.end_sec <= self.start_sec:
            raise ValueError("end_sec must be greater than start_sec")

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class SceneDetectionConfig:
    detector: str = "AdaptiveDetector"
    adaptive_threshold: float = 2.0
    adaptive_min_content_val: float = 15.0
    adaptive_min_scene_len_sec: float = 0.25
    duplicate_frame_threshold: float = 1.0


@dataclass(frozen=True)
class DialogueOccurrence:
    dialogue_id: int
    time_range: TimeRange
    speaker: str
    text: str


@dataclass(frozen=True)
class CharacterAppearance:
    character_id: str
    name: str
    description: str
    identity_likert: int
    identity_evidence: str
    screen_presence: float

    def validate(self) -> None:
        if not self.character_id or not self.name or not self.description:
            raise ValueError("Character identity fields must not be empty")
        if not 1 <= self.identity_likert <= 5:
            raise ValueError("identity_likert must be between 1 and 5")
        if not 0.0 <= self.screen_presence <= 1.0:
            raise ValueError("screen_presence must be between 0 and 1")


@dataclass(frozen=True)
class SceneDescription:
    interior_exterior: InteriorExterior
    location: str
    time_of_day: TimeOfDay
    environment_lighting: list[str]
    color_palette: list[str]
    color_tone: str
    set_details: list[str]
    weather: str
    atmosphere: str

    def validate(self) -> None:
        if not self.location or not self.color_tone or not self.atmosphere:
            raise ValueError("Scene description fields must not be empty")
        if not self.environment_lighting or not self.color_palette:
            raise ValueError("Scene lighting and color palette must not be empty")


@dataclass(frozen=True)
class ShotDescription:
    shot_id: str
    time_range: TimeRange
    segment_time_range: TimeRange
    start_boundary: BoundarySource
    end_boundary: BoundarySource
    visual_description: str | None
    dominant_action: str | None
    content_type: SegmentContentType | None
    narrative_function: str | None
    emotional_tone: str | None
    emotional_intensity: float | None
    scene: SceneDescription | None
    characters: list[CharacterAppearance]
    dialogue: list[DialogueOccurrence]
    shot_scale: ShotScale | None
    camera_angle: CameraAngle | None
    camera_movement: CameraMovement | None
    composition: str | None
    sampled_frame_times_sec: list[float]
    visual_evidence: str | None
    visual_annotation_status: VisualAnnotationStatus = VisualAnnotationStatus.COMPLETE
    visual_annotation_failure: str | None = None

    def validate(self) -> None:
        if self.visual_annotation_status == VisualAnnotationStatus.PROVIDER_REJECTED:
            if self.visual_annotation_failure != "data_inspection_failed":
                raise ValueError(
                    "Provider-rejected Shot must record data_inspection_failed"
                )
            unavailable_fields = (
                self.visual_description,
                self.dominant_action,
                self.content_type,
                self.narrative_function,
                self.emotional_tone,
                self.emotional_intensity,
                self.scene,
                self.shot_scale,
                self.camera_angle,
                self.camera_movement,
                self.composition,
                self.visual_evidence,
            )
            if any(value is not None for value in unavailable_fields):
                raise ValueError(
                    "Provider-rejected Shot cannot contain visual annotation values"
                )
            return
        if self.visual_annotation_failure is not None:
            raise ValueError(
                "A completely annotated Shot cannot record an annotation failure"
            )
        if (
            not self.shot_id
            or not self.visual_description
            or not self.dominant_action
            or not self.narrative_function
        ):
            raise ValueError("Shot description fields must not be empty")
        if self.emotional_intensity is None:
            raise ValueError("Annotated Shot must define emotional_intensity")
        if not 0.0 <= self.emotional_intensity <= 1.0:
            raise ValueError("Shot emotional_intensity must be between 0 and 1")
        if len(self.sampled_frame_times_sec) != 5:
            raise ValueError("Every Shot must be described from exactly five frames")
        if self.scene is None:
            raise ValueError("Annotated Shot must define a scene")
        self.scene.validate()
        for character in self.characters:
            character.validate()


@dataclass(frozen=True)
class SegmentDescription:
    segment_id: str
    time_range: TimeRange
    clip_path: str
    has_dialogue: bool
    speech_mode: SpeechMode
    content_type: SegmentContentType | None
    timeline_role: TimelineRole
    shots: list[ShotDescription]
    dialogue_items: list[DialogueOccurrence]
    segment_summary: str | None
    narrative_function: str | None
    emotional_tone: str | None
    emotional_intensity: float | None
    appearing_characters: list[str]

    def validate(self) -> None:
        if not self.shots:
            raise ValueError(f"{self.segment_id} must contain at least one Shot")
        if self.time_range.start_sec != self.shots[0].time_range.start_sec:
            raise ValueError(f"{self.segment_id} must start at its first Shot boundary")
        if self.time_range.end_sec != self.shots[-1].time_range.end_sec:
            raise ValueError(f"{self.segment_id} must end at its last Shot boundary")
        for previous, current in zip(self.shots, self.shots[1:], strict=False):
            if previous.time_range.end_sec != current.time_range.start_sec:
                raise ValueError(f"{self.segment_id} contains non-contiguous Shots")

        dialogue_by_id = {
            occurrence.dialogue_id: occurrence
            for shot in self.shots
            for occurrence in shot.dialogue
        }
        if len(dialogue_by_id) != len(self.dialogue_items):
            raise ValueError(f"{self.segment_id} has inconsistent dialogue items")
        if [item.dialogue_id for item in self.dialogue_items] != sorted(dialogue_by_id):
            raise ValueError(
                f"{self.segment_id} dialogue_items must be unique and source ordered"
            )
        expected_dialogue = bool(dialogue_by_id)
        if self.has_dialogue != expected_dialogue:
            raise ValueError(f"{self.segment_id} has inconsistent dialogue metadata")
        expected_speech_mode = (
            SpeechMode.NONE
            if not self.dialogue_items
            else (
                SpeechMode.MONOLOGUE
                if len({item.speaker for item in self.dialogue_items}) == 1
                else SpeechMode.DIALOGUE
            )
        )
        if self.speech_mode != expected_speech_mode:
            raise ValueError(f"{self.segment_id} has inconsistent speech_mode")
        if self.has_dialogue:
            if self.speech_mode == SpeechMode.NONE:
                raise ValueError("Narrative Segment must specify dialogue or monologue")
            if self.content_type != SegmentContentType.NARRATIVE:
                raise ValueError("A Segment with spoken content must be narrative")
        else:
            if self.speech_mode != SpeechMode.NONE:
                raise ValueError("A silent Segment must use speech_mode=none")
            if self.content_type == SegmentContentType.NARRATIVE:
                raise ValueError("A silent Segment cannot be narrative")
        completed_shots = [
            shot
            for shot in self.shots
            if shot.visual_annotation_status == VisualAnnotationStatus.COMPLETE
        ]
        if not completed_shots:
            if self.has_dialogue and self.content_type != SegmentContentType.NARRATIVE:
                raise ValueError("Spoken Segment must remain narrative")
            if not self.has_dialogue and self.content_type is not None:
                raise ValueError(
                    "Silent Segment without visual annotations has no content_type"
                )
            if (
                (not self.has_dialogue and self.segment_summary is not None)
                or self.emotional_tone is not None
                or self.emotional_intensity is not None
            ):
                raise ValueError(
                    "Segment without visual annotations cannot contain visual aggregates"
                )
        elif self.content_type is None:
            raise ValueError("Visually annotated Segment must define content_type")
        if (
            self.emotional_intensity is not None
            and not 0.0 <= self.emotional_intensity <= 1.0
        ):
            raise ValueError("Segment emotional_intensity must be between 0 and 1")
        for shot in self.shots:
            shot.validate()


@dataclass(frozen=True)
class SourceVideoMetadata:
    path: str
    title: str
    duration_sec: float
    fps: float
    width: int
    height: int


@dataclass(frozen=True)
class VideoDescription:
    schema_version: str
    source: SourceVideoMetadata
    scene_detection: SceneDetectionConfig
    segments: list[SegmentDescription]
    asr_model: str
    scene_boundary_model: str
    visual_description_model: str

    def validate(self) -> None:
        if not self.segments:
            raise ValueError("Video description must contain Segments")
        previous_end = 0.0
        shot_ids: set[str] = set()
        for segment in self.segments:
            segment.validate()
            if abs(segment.time_range.start_sec - previous_end) > 1e-3:
                raise ValueError("Segments contain a gap or overlap")
            for shot in segment.shots:
                if shot.shot_id in shot_ids:
                    raise ValueError(f"Shot occurs in multiple Segments: {shot.shot_id}")
                shot_ids.add(shot.shot_id)
            previous_end = segment.time_range.end_sec
        if abs(previous_end - self.source.duration_sec) > 1e-3:
            raise ValueError("Segments do not cover the complete source video")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)
