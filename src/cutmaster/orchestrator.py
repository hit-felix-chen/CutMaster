from __future__ import annotations

import json
import shutil
import time

from loguru import logger

from cutmaster.cuts import optimize_script_source_windows
from cutmaster.music import analyze_music, write_music_profile
from cutmaster.models import AppConfig, OrchestrationResult, RunRequest
from cutmaster.planner import NoFeasiblePathError, Planner
from cutmaster.workflow_context import WorkflowContext
from cutmaster.renderer import media_duration, render_montage
from cutmaster.script import adapt_script, script_duration, write_script
from cutmaster.analyser import analyse_video_material


def _validate_request(request: RunRequest) -> None:
    if not request.video_path.is_file():
        raise FileNotFoundError(f"Video not found: {request.video_path}")
    if not request.audio_path.is_file():
        raise FileNotFoundError(f"BGM not found: {request.audio_path}")
    if not request.prompt.strip():
        raise ValueError("Prompt must not be empty")
    if request.target_output_length_sec <= 0:
        raise ValueError("Target output duration must be positive")
    if request.target_shot_length_sec <= 0:
        raise ValueError("Target shot duration must be positive")
    if request.max_clip_duration_sec is not None and request.max_clip_duration_sec <= 0:
        raise ValueError("Maximum clip duration must be positive")


def run_orchestrator(
    request: RunRequest,
    config: AppConfig,
) -> OrchestrationResult:
    _validate_request(request)
    output_dir = request.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    final_output = output_dir / "output.mp4"
    if final_output.exists() and not request.overwrite:
        raise FileExistsError(f"Output already exists; pass --overwrite to replace it: {final_output}")

    raw_script_path = output_dir / "script_raw.json"
    adapted_script_path = output_dir / "script_adapted.json"
    music_profile_path = output_dir / "music_profile.json"
    edit_plan_path = output_dir / "edit_plan.json"
    candidate_pool_path = output_dir / "candidate_pool.json"
    selection_path = output_dir / "selection_diagnostics.json"
    planning_history_path = output_dir / "planning_history.json"
    result_path = output_dir / "result.json"
    if request.overwrite and planning_history_path.exists():
        planning_history_path.unlink()
    timings: dict[str, float] = {}
    started = time.monotonic()

    stage_started = time.monotonic()
    material = analyse_video_material(
        request.video_path,
        request.video_title or request.video_path.stem,
        request.subtitle_path,
        config.material_analysis,
        config.shot_detection,
        config.asr,
        config.shot_annotation,
        config.llm,
        config.vlm,
    )
    timings["video_material_analysis"] = time.monotonic() - stage_started
    source_srt_path = output_dir / "source.srt"
    processed_subtitle_path = output_dir / "dialogue_merged.srt"
    dialogues_json_path = output_dir / "dialogues.json"
    shutil.copy2(material.source_srt, source_srt_path)
    shutil.copy2(material.processed_subtitle, processed_subtitle_path)
    shutil.copy2(material.dialogues_json, dialogues_json_path)

    stage_started = time.monotonic()
    music_profile = analyze_music(request.audio_path, request.target_output_length_sec)
    write_music_profile(music_profile_path, music_profile)
    timings["music_analysis"] = time.monotonic() - stage_started

    planning_context = WorkflowContext(planning_history_path)
    planning_context.set_artifact("music_profile", music_profile)
    planning_context.set_artifact("video_description", material.video_description)
    planner = Planner(request.video_path, config, planning_context)

    planning_seconds = 0.0
    retrieval_seconds = 0.0
    pairwise_seconds = 0.0
    selection_seconds = 0.0
    slots: list[dict] = []
    candidate_pool: dict[str, list[dict]] = {}
    beam_path: list[dict] = []
    pairwise_scores: dict[str, dict] = {}
    selection: dict = {}
    for planning_attempt in range(
        1,
        config.slot_planning.replan_max_rounds + 2,
    ):
        stage_started = time.monotonic()
        slots = planner.plan_slots(request, music_profile)
        planning_context.set_artifact("edit_plan", slots)
        edit_plan_path.write_text(
            json.dumps(slots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        planning_seconds += time.monotonic() - stage_started

        try:
            attempt_stage = "retrieval"
            stage_started = time.monotonic()
            candidate_pool = planner.retrieve(slots)
            retrieval_seconds += time.monotonic() - stage_started

            attempt_stage = "chronology_preflight"
            stage_started = time.monotonic()
            planner.validate_sequence(slots, candidate_pool)
            selection_seconds += time.monotonic() - stage_started

            attempt_stage = "pairwise_visual_scoring"
            stage_started = time.monotonic()
            pairwise_scores = planner.score_pairs(slots, candidate_pool)
            pairwise_seconds += time.monotonic() - stage_started

            attempt_stage = "beam_selection"
            stage_started = time.monotonic()
            beam_path, selection = planner.select(
                slots,
                candidate_pool,
                pairwise_scores,
            )
            selection_seconds += time.monotonic() - stage_started
            selection["planning_attempt"] = planning_attempt
            break
        except (NoFeasiblePathError, ValueError) as exc:
            elapsed = max(0.0, time.monotonic() - stage_started)
            if attempt_stage == "retrieval":
                retrieval_seconds += elapsed
            elif attempt_stage == "pairwise_visual_scoring":
                pairwise_seconds += elapsed
            else:
                selection_seconds += elapsed
            if not isinstance(exc, NoFeasiblePathError) and attempt_stage != "retrieval":
                raise
            diagnostics = (
                exc.diagnostics
                if isinstance(exc, NoFeasiblePathError)
                else planning_context.get_artifact(
                    "retrieval_failure",
                    {"reason": str(exc)},
                )
            )
            shortages = diagnostics.get("shortages") or {}
            failed_slot_ids = set(shortages)
            failed_slot_id = diagnostics.get("failed_slot_id")
            if failed_slot_id:
                failed_slot_ids.add(str(failed_slot_id))
            failed_slots = [
                {
                    "slot_id": slot["slot_id"],
                    "content_description": slot["content_description"],
                    "source_segment_ids": slot.get("source_segment_ids") or [],
                    "missing_candidates": shortages.get(slot["slot_id"]),
                }
                for slot in slots
                if slot["slot_id"] in failed_slot_ids
            ]
            planner.record_failure(
                attempt=planning_attempt,
                error=str(exc),
                diagnostics=diagnostics,
                failed_slots=failed_slots,
            )
            if planning_attempt > config.slot_planning.replan_max_rounds:
                raise
            logger.warning(
                "Planning attempt {} was infeasible; replanning with recorded diagnostics",
                planning_attempt,
            )
    else:
        raise RuntimeError("Planning loop ended without a feasible path")

    candidate_pool_path.write_text(
        json.dumps(candidate_pool, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    timings["slot_planning"] = planning_seconds
    timings["candidate_retrieval"] = retrieval_seconds
    timings["pairwise_visual_scoring"] = pairwise_seconds

    stage_started = time.monotonic()
    raw_script = planner.build_script(slots, beam_path)
    planning_context.set_artifact("selection_diagnostics", selection)
    planning_context.record_script_version(raw_script, source="beam_search")
    for _ in range(config.script_review.review_rounds):
        raw_script, _ = planner.review(
            slots,
            candidate_pool,
            raw_script,
            pairwise_scores,
        )
    write_script(raw_script_path, raw_script)
    selection_path.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    timings["sequence_selection_and_review"] = (
        selection_seconds + time.monotonic() - stage_started
    )

    stage_started = time.monotonic()
    source_duration = media_duration(request.video_path)
    adapted_script = adapt_script(
        raw_script,
        request.target_output_length_sec,
        request.target_shot_length_sec,
        request.max_clip_duration_sec,
        music_profile["accents_sec"],
        source_duration,
        config.render.fps,
    )
    timings["script_adaptation"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    adapted_script = optimize_script_source_windows(
        request.video_path,
        adapted_script,
        music_profile["beats_sec"],
        source_duration,
        output_fps=config.render.fps,
        detection_config=config.shot_detection,
        optimization_config=config.source_window_optimization,
    )
    write_script(adapted_script_path, adapted_script)
    timings["source_cut_optimization"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    montage_path, output_path = render_montage(
        request.video_path,
        request.audio_path,
        adapted_script,
        output_dir,
        config.render,
    )
    timings["rendering"] = time.monotonic() - stage_started

    result = OrchestrationResult(
        status="success",
        output_video=str(output_path),
        source_srt=str(source_srt_path),
        processed_subtitle=str(processed_subtitle_path),
        dialogues_json=str(dialogues_json_path),
        music_profile=str(music_profile_path),
        material_directory=str(material.material_directory),
        video_description=str(material.video_description_path),
        analysis_history=str(material.analysis_history_path),
        planning_history=str(planning_history_path),
        edit_plan=str(edit_plan_path),
        candidate_pool=str(candidate_pool_path),
        raw_script=str(raw_script_path),
        adapted_script=str(adapted_script_path),
        montage_video=str(montage_path),
        target_output_length_sec=request.target_output_length_sec,
        actual_output_length_sec=media_duration(output_path),
        raw_script_duration_sec=script_duration(raw_script),
        adapted_script_duration_sec=script_duration(adapted_script),
        num_raw_clips=len(raw_script),
        num_adapted_clips=len(adapted_script),
        stage_timings_sec=timings,
        wall_clock_sec=time.monotonic() - started,
    )
    result_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.success("CutMaster orchestration complete: {}", output_path)
    return result
