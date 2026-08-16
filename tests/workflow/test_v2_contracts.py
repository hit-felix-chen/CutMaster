from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from math import inf, nan
from pathlib import Path

import pytest

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint, MaterialType
from cutmaster.workflow.contracts import (
    AnalyseMusicRequest,
    AnalyseVideoRequest,
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
    AnalysisWorkspace,
    MaterialRuntimeHandle,
    MusicAnalysisOptions,
    PlannersBrief,
    PlannersOptions,
    PlannersRequest,
    PlannersResult,
    PlannersWorkspace,
    RenderOptions,
    RenderOutputTarget,
    RenderPlan,
    RenderRequest,
    RenderRuntimeBindings,
    VideoAnalysisOptions,
)


FINGERPRINT_A = MaterialFingerprint("a" * 64)
FINGERPRINT_B = MaterialFingerprint("b" * 64)


def _video_handle() -> MaterialRuntimeHandle:
    return MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=MaterialType.VIDEO,
        material_name="feature",
        expected_fingerprint=FINGERPRINT_A,
        source_path=Path("/does/not/exist/source.mp4"),
        memory_root=Path("/does/not/exist/video-memory"),
        subtitle_path=Path("/does/not/exist/source.srt"),
    )


def _music_handle() -> MaterialRuntimeHandle:
    return MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=MaterialType.MUSIC,
        material_name="score",
        expected_fingerprint=FINGERPRINT_B,
        source_path=Path("/does/not/exist/score.wav"),
        memory_root=Path("/does/not/exist/music-memory"),
    )


def _analysed_video() -> AnalysedVideoRuntimeHandle:
    memory_root = _video_handle().memory_root
    material = MaterialRuntimeHandle(
        material_id=MaterialId.new(),
        material_type=MaterialType.VIDEO,
        material_name="feature",
        expected_fingerprint=FINGERPRINT_A,
        source_path=Path("/does/not/exist/source.mp4"),
        memory_root=memory_root,
        subtitle_path=Path("/does/not/exist/source.srt"),
    )
    return AnalysedVideoRuntimeHandle(
        material=material,
        memory_schema_version="1.0",
        video_description_path=memory_root / "video_description.json",
        video_summary_path=memory_root / "video_summary.json",
        dialogues_path=memory_root / "dialogues.json",
    )


def _analysed_music() -> AnalysedMusicRuntimeHandle:
    material = _music_handle()
    return AnalysedMusicRuntimeHandle(
        material=material,
        memory_schema_version="1.0",
        music_memory_path=material.memory_root / "music_memory.json",
    )


def test_runtime_handle_construction_is_frozen_and_performs_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_io(*args: object, **kwargs: object) -> None:
        raise AssertionError("v2 contract construction attempted filesystem I/O")

    for method_name in ("exists", "is_file", "open", "resolve", "stat"):
        monkeypatch.setattr(Path, method_name, unexpected_io)

    video = _video_handle()
    analysed_video = _analysed_video()
    analysed_music = _analysed_music()

    assert video.source_path == Path("/does/not/exist/source.mp4")
    assert video.memory_root == Path("/does/not/exist/video-memory")
    assert analysed_video.material.material_type is MaterialType.VIDEO
    assert analysed_music.material.material_type is MaterialType.MUSIC
    with pytest.raises(FrozenInstanceError):
        video.material_name = "changed"  # type: ignore[misc]


def test_material_runtime_handle_rejects_invalid_in_memory_values() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        MaterialRuntimeHandle(
            material_id=MaterialId.new(),
            material_type=MaterialType.VIDEO,
            material_name="feature",
            expected_fingerprint=MaterialFingerprint("ABC"),
            source_path=Path("/source.mp4"),
            memory_root=Path("/memory"),
        )
    with pytest.raises(ValueError, match="absolute"):
        MaterialRuntimeHandle(
            material_id=MaterialId.new(),
            material_type=MaterialType.VIDEO,
            material_name="feature",
            expected_fingerprint=FINGERPRINT_A,
            source_path=Path("source.mp4"),
            memory_root=Path("/memory"),
        )
    with pytest.raises(ValueError, match="Only a video"):
        MaterialRuntimeHandle(
            material_id=MaterialId.new(),
            material_type=MaterialType.MUSIC,
            material_name="score",
            expected_fingerprint=FINGERPRINT_B,
            source_path=Path("/score.wav"),
            memory_root=Path("/memory"),
            subtitle_path=Path("/score.srt"),
        )


def test_analysed_handle_requires_artifacts_inside_material_memory_root() -> None:
    with pytest.raises(ValueError, match="Material Memory root"):
        AnalysedMusicRuntimeHandle(
            material=_music_handle(),
            memory_schema_version="1.0",
            music_memory_path=Path("/another/material/music_memory.json"),
        )


def test_analysis_requests_require_the_corresponding_material_type() -> None:
    workspace = AnalysisWorkspace(Path("/does/not/exist/analyser"))
    video = _video_handle()
    music = _music_handle()

    assert AnalyseVideoRequest(
        video,
        VideoAnalysisOptions(video_title="Feature"),
        workspace,
    ).material is video
    assert AnalyseMusicRequest(
        music,
        MusicAnalysisOptions(),
        workspace,
    ).material is music
    with pytest.raises(ValueError, match="video Material"):
        AnalyseVideoRequest(music, VideoAnalysisOptions(), workspace)
    with pytest.raises(ValueError, match="music Material"):
        AnalyseMusicRequest(video, MusicAnalysisOptions(), workspace)


def test_planners_request_separates_brief_from_advanced_options() -> None:
    brief = PlannersBrief(
        editing_intent="Follow the protagonist's decision",
        target_duration_sec=60.0,
    )
    options = PlannersOptions(
        target_shot_length_sec=4.0,
        prompt_type="event",
        video_title="Feature",
        max_clip_duration_sec=8.0,
    )
    request = PlannersRequest(
        video=_analysed_video(),
        music=_analysed_music(),
        brief=brief,
        options=options,
        workspace=PlannersWorkspace(Path("/does/not/exist/planners")),
    )

    assert request.brief.target_duration_sec == 60.0
    assert request.options.prompt_type == "event"
    with pytest.raises(ValueError, match="Editing Intent"):
        PlannersBrief(" ", 60.0)
    with pytest.raises(ValueError, match="Target Duration"):
        PlannersBrief("intent", 0.0)
    with pytest.raises(ValueError, match="Maximum Clip Duration"):
        PlannersOptions(max_clip_duration_sec=0.0)
    for invalid in (nan, inf, -inf):
        with pytest.raises(ValueError, match="finite"):
            PlannersBrief("intent", invalid)


def _plan(bindings: RenderRuntimeBindings, *, video_id: MaterialId | None = None) -> RenderPlan:
    return RenderPlan.create(
        video_material_id=video_id or bindings.video.material_id,
        video_expected_fingerprint=bindings.video.expected_fingerprint,
        music_material_id=bindings.music.material_id,
        music_expected_fingerprint=bindings.music.expected_fingerprint,
        fps=30,
        clips=[
            {
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            }
        ],
        planners_metadata={"editing_intent": "test"},
    )


def test_planners_result_round_trip_preserves_plan_identity(tmp_path: Path) -> None:
    bindings = RenderRuntimeBindings(video=_video_handle(), music=_music_handle())
    plan = _plan(bindings)
    render_plan_path = tmp_path / "render_plan.json"
    plan.write(render_plan_path)
    result = PlannersResult(
        status="success",
        render_plan=plan,
        render_plan_path=render_plan_path,
        music_profile_path=tmp_path / "music_profile.json",
        edit_plan_path=tmp_path / "edit_plan.json",
        dialogue_anchors_path=tmp_path / "dialogue_anchors.json",
        candidate_pool_path=tmp_path / "candidate_pool.json",
        raw_script_path=tmp_path / "raw_script.json",
        selection_diagnostics_path=tmp_path / "selection_diagnostics.json",
        planners_history_path=tmp_path / "planners_history.json",
        planners_calls_path=tmp_path / "planners_calls.json",
        target_output_length_sec=1.0,
        planned_output_length_sec=1.0,
        num_raw_clips=1,
        num_planned_clips=1,
        stage_timings_sec={"sequence_selection": 0.25},
        wall_clock_sec=0.5,
    )
    result_path = tmp_path / "planners_result.json"

    result.write(result_path)

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["plan_id"] == plan.plan_id
    assert PlannersResult.read(result_path) == result

    payload["plan_id"] = "mismatched-plan-id"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="plan_id does not match RenderPlan"):
        PlannersResult.read(result_path)


def test_render_request_matches_portable_plan_to_runtime_bindings() -> None:
    bindings = RenderRuntimeBindings(video=_video_handle(), music=_music_handle())
    plan = _plan(bindings)

    request = RenderRequest(
        plan=plan,
        bindings=bindings,
        options=RenderOptions(audio_mode="dialogue"),
        output_target=RenderOutputTarget(Path("/does/not/exist/renderer")),
    )

    assert isinstance(plan, RenderPlan)
    assert request.output_target.output_filename == "output.mp4"


def test_render_request_rejects_mismatched_binding_without_io() -> None:
    bindings = RenderRuntimeBindings(video=_video_handle(), music=_music_handle())
    plan = _plan(bindings, video_id=MaterialId.new())

    with pytest.raises(ValueError, match="video Material"):
        RenderRequest(
            plan=plan,
            bindings=bindings,
            options=RenderOptions(),
            output_target=RenderOutputTarget(Path("/renderer")),
        )


def test_render_output_target_accepts_only_one_filename() -> None:
    with pytest.raises(ValueError, match="one file name"):
        RenderOutputTarget(Path("/renderer"), "nested/output.mp4")
    with pytest.raises(ValueError, match="one file name"):
        RenderOutputTarget(Path("/renderer"), "nested\\output.mp4")
    bindings = RenderRuntimeBindings(video=_video_handle(), music=_music_handle())
    value = _plan(bindings).to_dict()
    for schema_version in ("1.0", "2.beta"):
        value["schema_version"] = schema_version
        with pytest.raises(ValueError, match="Unsupported render plan schema"):
            RenderPlan.from_dict(value)


def test_portable_render_plan_rejects_runtime_media_paths() -> None:
    bindings = RenderRuntimeBindings(video=_video_handle(), music=_music_handle())
    with pytest.raises(ValueError, match="runtime media field"):
        RenderPlan.create(
            video_material_id=bindings.video.material_id,
            video_expected_fingerprint=bindings.video.expected_fingerprint,
            music_material_id=bindings.music.material_id,
            music_expected_fingerprint=bindings.music.expected_fingerprint,
            fps=30,
            clips=[
                {
                    "timestamp": "00:00:00,000-00:00:01,000",
                    "output_frame_range": [0, 30],
                    "clip_path": "/private/source.mp4",
                }
            ],
            planners_metadata={"editing_intent": "test"},
        )
