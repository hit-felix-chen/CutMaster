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
    dialogue_group_id: str
    speech_mode: SpeechMode


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
    visual_description: str
    dominant_action: str
    content_type: SegmentContentType
    narrative_function: str
    emotional_tone: str
    emotional_intensity: float
    scene: SceneDescription
    characters: list[CharacterAppearance]
    dialogue: list[DialogueOccurrence]
    shot_scale: ShotScale
    camera_angle: CameraAngle
    camera_movement: CameraMovement
    composition: str
    sampled_frame_times_sec: list[float]
    visual_evidence: str

    def validate(self) -> None:
        if (
            not self.shot_id
            or not self.visual_description
            or not self.dominant_action
            or not self.narrative_function
        ):
            raise ValueError("Shot description fields must not be empty")
        if not 0.0 <= self.emotional_intensity <= 1.0:
            raise ValueError("Shot emotional_intensity must be between 0 and 1")
        if len(self.sampled_frame_times_sec) != 5:
            raise ValueError("Every Shot must be described from exactly five frames")
        self.scene.validate()
        for character in self.characters:
            character.validate()


@dataclass(frozen=True)
class DialogueContext:
    dialogue_group_id: str
    speech_mode: SpeechMode
    dialogue_ids: list[int]
    participants: list[str]
    topic: str
    summary: str
    grouping_reason: str


@dataclass(frozen=True)
class SegmentDescription:
    segment_id: str
    time_range: TimeRange
    clip_path: str
    has_dialogue: bool
    speech_mode: SpeechMode
    content_type: SegmentContentType
    timeline_role: TimelineRole
    shots: list[ShotDescription]
    dialogue_context: DialogueContext | None
    segment_summary: str
    narrative_function: str
    emotional_tone: str
    emotional_intensity: float
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

        expected_dialogue = any(shot.dialogue for shot in self.shots)
        if self.has_dialogue != expected_dialogue:
            raise ValueError(f"{self.segment_id} has inconsistent dialogue metadata")
        if self.has_dialogue:
            if self.speech_mode == SpeechMode.NONE:
                raise ValueError("Narrative Segment must specify dialogue or monologue")
            if self.content_type != SegmentContentType.NARRATIVE:
                raise ValueError("A Segment with spoken content must be narrative")
            if self.dialogue_context is None:
                raise ValueError("A Segment with spoken content needs dialogue_context")
        else:
            if self.speech_mode != SpeechMode.NONE:
                raise ValueError("A silent Segment must use speech_mode=none")
            if self.content_type == SegmentContentType.NARRATIVE:
                raise ValueError("A silent Segment cannot be narrative")
            if self.dialogue_context is not None:
                raise ValueError("A silent Segment cannot have dialogue_context")
        if not 0.0 <= self.emotional_intensity <= 1.0:
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
    dialogue_grouping_model: str
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
