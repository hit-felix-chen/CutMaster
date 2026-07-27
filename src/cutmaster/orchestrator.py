from __future__ import annotations

import json
import shutil
import time

from cutmaster.editing.source_windows import optimize_script_source_windows
from cutmaster.editing.dialogue_audio import prepare_dialogue_audio
from cutmaster.music.analysis import (
    analyze_music,
    compact_music_profile,
    write_music_profile,
)
from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.workflow import OrchestrationResult, RunRequest
from cutmaster.runtime.observability import error_summary, log_event
from cutmaster.planner import NoFeasiblePathError, Planner
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.editing.renderer import render_montage
from cutmaster.runtime.media_probe import media_duration
from cutmaster.editing.script import adapt_script, script_duration, write_script
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
    dialogue_anchors_path = output_dir / "dialogue_anchors.json"
    candidate_pool_path = output_dir / "candidate_pool.json"
    selection_path = output_dir / "selection_diagnostics.json"
    planning_history_path = output_dir / "planning_history.json"
    planning_calls_path = output_dir / "planning_calls.json"
    result_path = output_dir / "result.json"
    if request.overwrite:
        for stale_path in (planning_history_path, planning_calls_path):
            if stale_path.exists():
                stale_path.unlink()
    timings: dict[str, float] = {}
    started = time.monotonic()
    log_event(
        "INFO",
        "orchestrator",
        "workflow.start",
        "CutMaster workflow started",
        source_video=request.video_path.name,
        source_audio=request.audio_path.name,
        prompt_type=request.prompt_type,
        target_duration_sec=request.target_output_length_sec,
        output_dir=output_dir,
    )

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "analyser",
        "stage.start",
        "Video material analysis started",
        stage="video_material_analysis",
    )
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
    log_event(
        "INFO",
        "analyser",
        "stage.complete",
        "Video material analysis completed",
        stage="video_material_analysis",
        elapsed_sec=timings["video_material_analysis"],
        material_directory=material.material_directory,
    )
    source_srt_path = output_dir / "source.srt"
    processed_subtitle_path = output_dir / "dialogue_merged.srt"
    dialogues_json_path = output_dir / "dialogues.json"
    shutil.copy2(material.source_srt, source_srt_path)
    shutil.copy2(material.processed_subtitle, processed_subtitle_path)
    shutil.copy2(material.dialogues_json, dialogues_json_path)

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "music",
        "stage.start",
        "Music analysis started",
        stage="music_analysis",
    )
    music_profile = analyze_music(request.audio_path, request.target_output_length_sec)
    write_music_profile(music_profile_path, music_profile)
    timings["music_analysis"] = time.monotonic() - stage_started
    log_event(
        "INFO",
        "music",
        "stage.complete",
        "Music analysis completed",
        stage="music_analysis",
        elapsed_sec=timings["music_analysis"],
    )

    planning_context = WorkflowContext(
        planning_history_path,
        model_call_tree_path=planning_calls_path,
    )
    planning_context.set_artifact(
        "music_profile",
        compact_music_profile(music_profile),
    )
    planning_context.set_artifact("video_description", material.video_description)
    planning_context.set_artifact("video_summary", material.video_summary)
    planner = Planner(request.video_path, config, planning_context)

    planning_seconds = 0.0
    dialogue_anchor_seconds = 0.0
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
        log_event(
            "INFO",
            "planner.slot",
            "stage.start",
            "Slot planning started",
            stage="slot_planning",
            attempt=planning_attempt,
        )
        slots = planner.plan_slots(request, music_profile)
        planning_context.set_artifact("edit_plan", slots)
        edit_plan_path.write_text(
            json.dumps(slots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        elapsed = time.monotonic() - stage_started
        planning_seconds += elapsed
        log_event(
            "INFO",
            "planner.slot",
            "stage.complete",
            "Slot planning completed",
            stage="slot_planning",
            attempt=planning_attempt,
            slots=len(slots),
            elapsed_sec=elapsed,
        )

        try:
            attempt_stage = "dialogue_anchor_selection"
            stage_started = time.monotonic()
            log_event(
                "INFO",
                "planner.anchor",
                "stage.start",
                "Original-dialogue anchor selection started",
                stage=attempt_stage,
                attempt=planning_attempt,
            )
            slots = planner.anchor_dialogue(slots)
            planning_context.set_artifact("edit_plan", slots)
            edit_plan_path.write_text(
                json.dumps(slots, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            anchors = planning_context.get_artifact("dialogue_anchors", [])
            dialogue_anchors_path.write_text(
                json.dumps(anchors, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            elapsed = time.monotonic() - stage_started
            dialogue_anchor_seconds += elapsed
            log_event(
                "INFO",
                "planner.anchor",
                "stage.complete",
                "Original-dialogue anchor selection completed",
                stage=attempt_stage,
                attempt=planning_attempt,
                anchors=len(anchors),
                elapsed_sec=elapsed,
            )

            attempt_stage = "retrieval"
            stage_started = time.monotonic()
            log_event(
                "INFO",
                "planner.candidate",
                "stage.start",
                "Candidate retrieval started",
                stage=attempt_stage,
                attempt=planning_attempt,
                slots=len(slots),
            )
            candidate_pool = planner.retrieve(slots)
            planning_context.set_artifact("edit_plan", slots)
            edit_plan_path.write_text(
                json.dumps(slots, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            elapsed = time.monotonic() - stage_started
            retrieval_seconds += elapsed
            log_event(
                "INFO",
                "planner.candidate",
                "stage.complete",
                "Candidate retrieval completed",
                stage=attempt_stage,
                attempt=planning_attempt,
                candidates=sum(len(items) for items in candidate_pool.values()),
                elapsed_sec=elapsed,
            )

            attempt_stage = "chronology_preflight"
            stage_started = time.monotonic()
            log_event(
                "INFO",
                "planner.sequence",
                "stage.start",
                "Chronology preflight started",
                stage=attempt_stage,
                attempt=planning_attempt,
            )
            planner.validate_sequence(slots, candidate_pool)
            elapsed = time.monotonic() - stage_started
            selection_seconds += elapsed
            log_event(
                "INFO",
                "planner.sequence",
                "stage.complete",
                "Chronology preflight completed",
                stage=attempt_stage,
                attempt=planning_attempt,
                elapsed_sec=elapsed,
            )

            attempt_stage = "pairwise_visual_scoring"
            stage_started = time.monotonic()
            log_event(
                "INFO",
                "planner.sequence",
                "stage.start",
                "Pairwise visual scoring started",
                stage=attempt_stage,
                attempt=planning_attempt,
            )
            pairwise_scores = planner.score_pairs(slots, candidate_pool)
            elapsed = time.monotonic() - stage_started
            pairwise_seconds += elapsed
            log_event(
                "INFO",
                "planner.sequence",
                "stage.complete",
                "Pairwise visual scoring completed",
                stage=attempt_stage,
                attempt=planning_attempt,
                pairs=len(pairwise_scores),
                elapsed_sec=elapsed,
            )

            attempt_stage = "beam_selection"
            stage_started = time.monotonic()
            log_event(
                "INFO",
                "planner.sequence",
                "stage.start",
                "Beam selection started",
                stage=attempt_stage,
                attempt=planning_attempt,
            )
            beam_path, selection = planner.select(
                slots,
                candidate_pool,
                pairwise_scores,
            )
            elapsed = time.monotonic() - stage_started
            selection_seconds += elapsed
            log_event(
                "INFO",
                "planner.sequence",
                "stage.complete",
                "Beam selection completed",
                stage=attempt_stage,
                attempt=planning_attempt,
                selected_clips=len(beam_path),
                elapsed_sec=elapsed,
            )
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
                planning_context.save_model_call_tree(status="failed")
                raise
            log_event(
                "WARNING",
                "planner.sequence",
                "validation.reject",
                "Planning attempt was infeasible; replanning with diagnostics",
                stage=attempt_stage,
                attempt=planning_attempt,
                error_type=type(exc).__name__,
                reason=error_summary(exc),
                failed_slots=sorted(failed_slot_ids),
            )
    else:
        raise RuntimeError("Planning loop ended without a feasible path")

    candidate_pool_path.write_text(
        json.dumps(candidate_pool, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    timings["slot_planning"] = planning_seconds
    timings["dialogue_anchor_selection"] = dialogue_anchor_seconds
    timings["candidate_retrieval"] = retrieval_seconds
    timings["pairwise_visual_scoring"] = pairwise_seconds

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "planner.review",
        "stage.start",
        "Script construction and review started",
        stage="sequence_selection_and_review",
        review_rounds=config.script_review.review_rounds,
    )
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
    planning_context.save_model_call_tree()
    write_script(raw_script_path, raw_script)
    selection_path.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    timings["sequence_selection_and_review"] = (
        selection_seconds + time.monotonic() - stage_started
    )
    log_event(
        "INFO",
        "planner.review",
        "stage.complete",
        "Script construction and review completed",
        stage="sequence_selection_and_review",
        clips=len(raw_script),
        elapsed_sec=time.monotonic() - stage_started,
    )

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "script",
        "stage.start",
        "Script adaptation started",
        stage="script_adaptation",
    )
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
    log_event(
        "INFO",
        "script",
        "stage.complete",
        "Script adaptation completed",
        stage="script_adaptation",
        clips=len(adapted_script),
        elapsed_sec=timings["script_adaptation"],
    )

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "source_window",
        "stage.start",
        "Source-window optimization started",
        stage="source_window_optimization",
        clips=len(adapted_script),
    )
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
    log_event(
        "INFO",
        "source_window",
        "stage.complete",
        "Source-window optimization completed",
        stage="source_window_optimization",
        clips=len(adapted_script),
        elapsed_sec=timings["source_cut_optimization"],
    )

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "dialogue_audio",
        "stage.start",
        "Dialogue audio preparation started",
        stage="dialogue_audio_preparation",
        enabled=config.dialogue_anchors.enable_vocal_separation,
        anchors=sum(
            item.get("dialogue_anchor") is not None
            for item in adapted_script
        ),
    )
    adapted_script = prepare_dialogue_audio(
        request.video_path,
        adapted_script,
        output_dir,
        config.dialogue_anchors,
    )
    write_script(adapted_script_path, adapted_script)
    timings["dialogue_audio_preparation"] = time.monotonic() - stage_started
    log_event(
        "INFO",
        "dialogue_audio",
        "stage.complete",
        "Dialogue audio preparation completed",
        stage="dialogue_audio_preparation",
        enabled=config.dialogue_anchors.enable_vocal_separation,
        elapsed_sec=timings["dialogue_audio_preparation"],
    )

    stage_started = time.monotonic()
    log_event(
        "INFO",
        "renderer",
        "stage.start",
        "Montage rendering started",
        stage="rendering",
        clips=len(adapted_script),
    )
    montage_path, output_path = render_montage(
        request.video_path,
        request.audio_path,
        adapted_script,
        output_dir,
        config.render,
        config.dialogue_anchors,
    )
    timings["rendering"] = time.monotonic() - stage_started
    log_event(
        "INFO",
        "renderer",
        "stage.complete",
        "Montage rendering completed",
        stage="rendering",
        elapsed_sec=timings["rendering"],
        output_path=output_path,
    )

    result = OrchestrationResult(
        status="success",
        output_video=str(output_path),
        source_srt=str(source_srt_path),
        processed_subtitle=str(processed_subtitle_path),
        dialogues_json=str(dialogues_json_path),
        music_profile=str(music_profile_path),
        material_directory=str(material.material_directory),
        video_description=str(material.video_description_path),
        video_summary=str(material.video_summary_path),
        analysis_history=str(material.analysis_history_path),
        planning_history=str(planning_history_path),
        planning_calls=str(planning_calls_path),
        edit_plan=str(edit_plan_path),
        candidate_pool=str(candidate_pool_path),
        dialogue_anchors=str(dialogue_anchors_path),
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
    log_event(
        "SUCCESS",
        "orchestrator",
        "workflow.complete",
        "CutMaster workflow completed",
        output_path=output_path,
        wall_clock_sec=result.wall_clock_sec,
        output_duration_sec=result.actual_output_length_sec,
        clips=result.num_adapted_clips,
    )
    return result
