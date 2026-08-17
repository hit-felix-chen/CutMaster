"""Immutable ASTER Run and Guided Revision use cases."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cutmaster.application.errors import (
    AnchorLockedError,
    ApplicationError,
    InvalidCandidateReplacementError,
    ReviewArtifactUnavailableError,
    RevisionInfeasibleError,
)
from cutmaster.application.jobs.views import attempt_view, job_view
from cutmaster.application.jobs.usage import normalize_usage_summary
from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.ports.data_root import (
    DataRootCoordinator,
    root_shared_operation,
)
from cutmaster.application.ports.material_catalog import MaterialBinding
from cutmaster.application.renders.service import RendersService
from cutmaster.application.runs.commands import (
    CandidateReplacement,
    CompleteRunCommand,
    CreateRevisionCommand,
    CreateRunCommand,
    DeleteRunCommand,
    RecoverRunCommand,
    RunAgainCommand,
    SaveGuidedRevisionCommand,
)
from cutmaster.application.runs.checkpoints import (
    ManagedRunCheckpointRepository,
    ManagedRunCheckpointSession,
    RunCheckpointIdentity,
)
from cutmaster.application.runs.review import (
    load_render_plan,
    load_review_bundle,
    project_candidates,
    project_dialogue_cues,
    project_plan_summary,
    project_slots,
    replacement_clip,
    write_json_atomic,
)
from cutmaster.application.runs.views import (
    AttemptUsageView,
    CompletedRunView,
    DeletedRunView,
    FrozenEditReviewView,
    FrozenEditView,
    RunSubmissionView,
    RunUsageView,
    RunView,
    frozen_edit_view,
    run_submission_view,
    run_view,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.domain.ids import (
    AttemptId,
    FrozenEditId,
    JobId,
    MaterialId,
    ProjectId,
    RenderVariantId,
    RunId,
)
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.domain.runs import RunStatus
from cutmaster.infrastructure.persistence.sqlite import (
    ManagedStateConflict,
    SQLiteApplicationStore,
)
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.shared.timecode import parse_range


@dataclass(frozen=True)
class _ReadyRunSnapshot:
    video_binding: MaterialBinding
    music_binding: MaterialBinding
    music_duration_sec: float

    @property
    def video_id(self) -> MaterialId:
        return self.video_binding.material.material_id

    @property
    def music_id(self) -> MaterialId:
        return self.music_binding.material.material_id


class RunsService:
    """Own ASTER Run and Guided Revision use cases."""

    __slots__ = (
        "_effective_configuration",
        "_data_root_coordinator",
        "_checkpoints_instance",
        "_materials_instance",
        "_renders_instance",
        "_store_instance",
    )

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        store: SQLiteApplicationStore | None = None,
        *,
        materials: MaterialsService | None = None,
        renders: RendersService | None = None,
        data_root_coordinator: DataRootCoordinator | None = None,
    ) -> None:
        self._effective_configuration = effective_configuration
        self._data_root_coordinator = data_root_coordinator
        self._checkpoints_instance: ManagedRunCheckpointRepository | None = None
        self._store_instance = store
        self._materials_instance = materials
        self._renders_instance = renders

    @property
    def _store(self) -> SQLiteApplicationStore:
        store = self._store_instance
        if store is None:
            store = SQLiteApplicationStore(self._effective_configuration.data_root)
            self._store_instance = store
        return store

    @property
    def _checkpoints(self) -> ManagedRunCheckpointRepository:
        value = self._checkpoints_instance
        if value is None:
            value = ManagedRunCheckpointRepository(
                self._store,
                self._effective_configuration.data_root,
            )
            self._checkpoints_instance = value
        return value

    @root_shared_operation
    def create(self, command: CreateRunCommand) -> RunSubmissionView:
        if not isinstance(command.project_id, ProjectId):
            raise TypeError("project_id must be a ProjectId")
        configuration = self._effective_configuration.to_dict()
        planning_options = {
            "target_shot_length_sec": float(command.target_shot_length_sec),
            "prompt_type": command.prompt_type,
            "video_title": command.video_title,
            "max_clip_duration_sec": command.max_clip_duration_sec,
        }
        replay = self._store.replay_idempotent(
            command.command_id,
            "runs.create",
            {
                "project_id": str(command.project_id),
                "configuration": configuration,
                "planning_options": planning_options,
            },
        )
        if replay is not None:
            return run_submission_view(replay.value)
        project = self._store.get_project(command.project_id)
        brief = project.get("creative_brief")
        if brief is None:
            raise ManagedStateConflict(
                "creative_brief_missing",
                "An ASTER Run requires a saved Creative Brief",
            )
        if not isinstance(brief, Mapping):
            raise TypeError("Invalid Project Creative Brief persistence result")
        videos = _material_ids(project.get("video_material_ids"), "video")
        music = _material_ids(project.get("music_material_ids"), "music")
        target_duration = float(brief["target_duration_sec"])
        with self._ready_snapshot(videos, music, target_duration) as validated:
            result = self._store.create_run(
                command.command_id,
                command.project_id,
                configuration,
                planning_options,
                validated_video_material_id=validated.video_id,
                validated_music_material_id=validated.music_id,
                music_duration_sec=validated.music_duration_sec,
            )
        return run_submission_view(result.value)

    @root_shared_operation
    def get(self, run_id: RunId) -> RunView:
        _require_run_id(run_id)
        return run_view(self._store.get_run(run_id))

    @root_shared_operation
    def list(self, project_id: ProjectId) -> tuple[RunView, ...]:
        if not isinstance(project_id, ProjectId):
            raise TypeError("project_id must be a ProjectId")
        return tuple(run_view(value) for value in self._store.list_runs(project_id))

    @root_shared_operation
    def usage(self, run_id: RunId) -> RunUsageView:
        from cutmaster.infrastructure.models.openai_compatible import (
            merge_usage_summaries,
        )

        _require_run_id(run_id)
        persisted_attempts: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            page = self._store.list_attempts(
                owner_type="run",
                owner_id=str(run_id),
                limit=500,
                offset=offset,
            )
            persisted_attempts.extend(page)
            if len(page) < 500:
                break
            offset += len(page)
        attempts = tuple(
            sorted(
                (attempt_view(value) for value in persisted_attempts),
                key=lambda item: item.sequence,
            )
        )
        summaries = [
            dict(attempt.model_usage_summary)
            for attempt in attempts
            if attempt.model_usage_summary is not None
        ]
        return RunUsageView(
            attempt_usage=tuple(
                AttemptUsageView(
                    attempt_id=attempt.attempt_id,
                    sequence=attempt.sequence,
                    status=attempt.status,
                    model_usage_summary=attempt.model_usage_summary,
                )
                for attempt in attempts
            ),
            run_total=MappingProxyType(merge_usage_summaries(summaries)),
        )

    @root_shared_operation
    def complete(self, command: CompleteRunCommand) -> CompletedRunView:
        _require_run_id(command.run_id)
        if not isinstance(command.attempt_id, AttemptId):
            raise TypeError("attempt_id must be an AttemptId")
        plan_path = validate_portable_relative_file_path(command.plan_relative_path)
        model_usage_summary = (
            None
            if command.model_usage_summary is None
            else normalize_usage_summary(command.model_usage_summary)
        )
        value = self._store.complete_run(
            command.command_id,
            command.run_id,
            command.attempt_id,
            plan_path,
            model_usage_summary,
        ).value
        run = value["run"]
        edit = value["frozen_edit"]
        attempt = value["attempt"]
        job = value["job"]
        if not all(isinstance(item, dict) for item in (run, edit, attempt, job)):
            raise TypeError("Invalid completed Run persistence result")
        return CompletedRunView(
            run=run_view(run),
            frozen_edit=frozen_edit_view(edit),
            attempt=attempt_view(attempt),
            job=job_view(job),
        )

    @root_shared_operation
    def create_revision(self, command: CreateRevisionCommand) -> FrozenEditView:
        if not isinstance(command.source_edit_id, FrozenEditId):
            raise TypeError("source_edit_id must be a FrozenEditId")
        plan_path = validate_portable_relative_file_path(command.plan_relative_path)
        return frozen_edit_view(
            self._store.create_revision(
                command.command_id,
                command.source_edit_id,
                plan_path,
            ).value
        )

    @root_shared_operation
    def get_frozen_edit(self, edit_id: FrozenEditId) -> FrozenEditView:
        if not isinstance(edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        return frozen_edit_view(self._store.get_frozen_edit(edit_id))

    @root_shared_operation
    def list_frozen_edits(self, run_id: RunId) -> tuple[FrozenEditView, ...]:
        _require_run_id(run_id)
        return tuple(
            frozen_edit_view(value) for value in self._store.list_frozen_edits(run_id)
        )

    @root_shared_operation
    def review(self, edit_id: FrozenEditId) -> FrozenEditReviewView:
        """Build one coherent, privacy-safe Review page projection."""

        edit = self.get_frozen_edit(edit_id)
        run = self.get(edit.run_id)
        plan, plan_path = load_render_plan(
            self._effective_configuration.data_root,
            edit.plan.relative_path,
        )
        self._validate_plan_ownership(plan, run)
        bundle = load_review_bundle(plan_path)
        candidate_pool = bundle.get("candidate_pool")
        if not isinstance(candidate_pool, Mapping):
            raise ReviewArtifactUnavailableError("Candidate Space is invalid")
        candidates = project_candidates(plan, candidate_pool)
        beats, beats_available = self._music_beats(plan)
        return FrozenEditReviewView(
            edit=edit,
            run=run,
            versions=self.list_frozen_edits(run.run_id),
            plan=project_plan_summary(plan),
            video_material_id=plan.video_material_id,
            music_material_id=plan.music_material_id,
            slots=project_slots(plan),
            candidates=candidates,
            dialogue_cues=project_dialogue_cues(plan),
            music_beats_sec=beats,
            music_beats_available=beats_available,
            variants=self._renders.list(edit_id),
        )

    @root_shared_operation
    def save_guided_revision(
        self,
        command: SaveGuidedRevisionCommand,
    ) -> FrozenEditView:
        """Validate and atomically commit one immutable child Frozen Edit."""

        if not isinstance(command, SaveGuidedRevisionCommand):
            raise TypeError("command must be a SaveGuidedRevisionCommand")
        source_edit = self.get_frozen_edit(command.source_edit_id)
        run = self.get(source_edit.run_id)
        plan, plan_path = load_render_plan(
            self._effective_configuration.data_root,
            source_edit.plan.relative_path,
        )
        self._validate_plan_ownership(plan, run)
        bundle = load_review_bundle(plan_path)
        pool = bundle.get("candidate_pool")
        if not isinstance(pool, Mapping):
            raise ReviewArtifactUnavailableError("Candidate Space is invalid")
        # Validate the complete Candidate Space before accepting a mutation.
        # A replacement cannot repair or conceal a corrupt unrelated Slot.
        project_candidates(plan, pool)
        replacements = self._validate_replacements(command.replacements, plan, pool)
        clips = self._apply_replacements(plan, replacements, pool)
        self._validate_revision_chronology(clips)
        metadata = dict(plan.planners_metadata)
        metadata["guided_revision"] = {
            "source_plan_id": plan.plan_id,
            "replacements": [
                {"slot_id": value.slot_id, "candidate_id": value.candidate_id}
                for value in replacements
            ],
        }
        child_plan = RenderPlan.create(
            video_material_id=plan.video_material_id,
            video_expected_fingerprint=plan.video_expected_fingerprint,
            music_material_id=plan.music_material_id,
            music_expected_fingerprint=plan.music_expected_fingerprint,
            fps=plan.fps,
            clips=clips,
            planners_metadata=metadata,
        )
        replacement_payload = json.dumps(
            [
                {"slot_id": value.slot_id, "candidate_id": value.candidate_id}
                for value in replacements
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        command_key = hashlib.sha256(command.command_id.encode("utf-8")).hexdigest()[
            :16
        ]
        content_key = hashlib.sha256(replacement_payload.encode("utf-8")).hexdigest()[
            :16
        ]
        relative_path = (
            f"projects/{run.project_id}/runs/{run.run_id}/revisions/"
            f"{command_key}-{content_key}/plan.json"
        )
        destination = self._effective_configuration.data_root.joinpath(
            *relative_path.split("/")
        )
        existed_before = destination.exists()
        if existed_before:
            existing = RenderPlan.read(destination)
            if existing.to_dict() != child_plan.to_dict():
                raise InvalidCandidateReplacementError(
                    "Revision artifact identity conflicts with an existing command"
                )
        else:
            write_json_atomic(destination, child_plan.to_dict())
        try:
            return self.create_revision(
                CreateRevisionCommand(
                    command.command_id,
                    command.source_edit_id,
                    relative_path,
                )
            )
        except Exception:
            if not existed_before:
                destination.unlink(missing_ok=True)
            raise

    @root_shared_operation
    def retry(self, command: RecoverRunCommand) -> RunSubmissionView:
        return self._recover(command, resume=False)

    @root_shared_operation
    def resume(self, command: RecoverRunCommand) -> RunSubmissionView:
        return self._recover(command, resume=True)

    @root_shared_operation
    def run_again(self, command: RunAgainCommand) -> RunSubmissionView:
        if not isinstance(command.source_run_id, RunId):
            raise TypeError("source_run_id must be a RunId")
        replay = self._store.replay_idempotent(
            command.command_id,
            "runs.run_again",
            {"source_run_id": str(command.source_run_id)},
        )
        if replay is not None:
            return run_submission_view(replay.value)
        source = self.get(command.source_run_id)
        if source.status is not RunStatus.COMPLETE:
            raise ManagedStateConflict(
                "run_again_not_allowed",
                f"Run {command.source_run_id} must be complete before it can run again",
            )
        with self._ready_snapshot(
            source.video_material_ids,
            source.music_material_ids,
            source.creative_brief.target_duration_sec,
        ) as validated:
            result = self._store.create_run_again(
                command.command_id,
                command.source_run_id,
                validated_video_material_id=validated.video_id,
                validated_music_material_id=validated.music_id,
                music_duration_sec=validated.music_duration_sec,
            )
        return run_submission_view(result.value)

    def _recover(
        self,
        command: RecoverRunCommand,
        *,
        resume: bool,
    ) -> RunSubmissionView:
        _require_run_id(command.run_id)
        mode = "resume" if resume else "retry"
        replay = self._store.replay_idempotent(
            command.command_id,
            f"runs.{mode}",
            {"run_id": str(command.run_id), "resume": resume},
        )
        if replay is not None:
            return run_submission_view(replay.value)
        run = self.get(command.run_id)
        expected = RunStatus.INTERRUPTED if resume else RunStatus.FAILED
        if run.status is not expected:
            raise ManagedStateConflict(
                "run_recovery_not_allowed",
                f"Run {command.run_id} must be {expected.value} for this recovery action",
            )
        with self._ready_snapshot(
            run.video_material_ids,
            run.music_material_ids,
            run.creative_brief.target_duration_sec,
        ) as validated:
            checkpoint_id = None
            if resume:
                identity = self._checkpoint_identity(run, validated)
                checkpoint_id = self._checkpoints.validate_current(identity)
            result = self._store.retry_run(
                command.command_id,
                command.run_id,
                resume=resume,
                resume_checkpoint_id=checkpoint_id,
            )
        discarded = result.value.get("discarded_checkpoint_relative_path")
        self._checkpoints.discard_path(
            discarded if isinstance(discarded, str) else None
        )
        return run_submission_view(result.value)

    def checkpoint_session(
        self,
        run_id: RunId,
        attempt_id: AttemptId,
    ) -> ManagedRunCheckpointSession:
        """Bind one worker Attempt to its exact Run checkpoint receipt."""

        run = self.get(run_id)
        with self._ready_snapshot(
            run.video_material_ids,
            run.music_material_ids,
            run.creative_brief.target_duration_sec,
        ) as validated:
            identity = self._checkpoint_identity(run, validated)
        return self._checkpoints.session(identity, attempt_id)

    @staticmethod
    def _checkpoint_identity(
        run: RunView,
        snapshot: _ReadyRunSnapshot,
    ) -> RunCheckpointIdentity:
        options = run.planning_options
        return RunCheckpointIdentity.create(
            run,
            snapshot.video_binding,
            snapshot.music_binding,
            target_shot_length_sec=float(options.get("target_shot_length_sec", 4.0)),
            prompt_type=str(options.get("prompt_type") or "event"),
            max_clip_duration_sec=(
                None
                if options.get("max_clip_duration_sec") is None
                else float(options["max_clip_duration_sec"])
            ),
        )

    @root_shared_operation
    def delete(self, command: DeleteRunCommand) -> DeletedRunView:
        _require_run_id(command.run_id)
        value = self._store.delete_run(command.command_id, command.run_id).value
        self._delete_run_artifacts(value)
        return DeletedRunView(
            run_id=RunId.parse(str(value["run_id"])),
            deleted=bool(value["deleted"]),
        )

    @contextmanager
    def _ready_snapshot(
        self,
        video_ids: Sequence[MaterialId],
        music_ids: Sequence[MaterialId],
        target_duration_sec: float,
    ) -> Iterator[_ReadyRunSnapshot]:
        if len(video_ids) != 1 or len(music_ids) != 1:
            raise ManagedStateConflict(
                "project_materials_incomplete",
                "An ASTER Run requires exactly one video and one music Material",
            )
        video_id = video_ids[0]
        music_id = music_ids[0]
        with ExitStack() as leases:
            bindings = {
                material_id: leases.enter_context(
                    self._materials.consume_lease(material_id)
                )
                for material_id in sorted((video_id, music_id), key=str)
            }
            for material_id, expected_type in (
                (video_id, MaterialType.VIDEO),
                (music_id, MaterialType.MUSIC),
            ):
                material = bindings[material_id].material
                if material.material_type is not expected_type:
                    raise ManagedStateConflict(
                        "project_material_type_mismatch",
                        f"Material {material_id} is not {expected_type.value}",
                    )
                if material.condition is not MaterialCondition.READY:
                    raise ManagedStateConflict(
                        "run_material_not_ready",
                        f"Material {material_id} must be ready before starting ASTER",
                    )
            music_duration = _music_source_duration(
                bindings[music_id].memory_root / "music_memory.json"
            )
            if target_duration_sec > music_duration + 1e-6:
                raise ManagedStateConflict(
                    "target_duration_exceeds_music_duration",
                    "Target Duration must not exceed the selected Music Material duration",
                )
            yield _ReadyRunSnapshot(
                video_binding=bindings[video_id],
                music_binding=bindings[music_id],
                music_duration_sec=music_duration,
            )

    def _delete_run_artifacts(self, value: Mapping[str, Any]) -> None:
        project_raw = value.get("project_id")
        if project_raw is None:
            # Receipts created before managed artifact cleanup did not persist
            # ownership metadata. Historical artifacts are intentionally left
            # untouched rather than guessed from mutable state.
            return
        project_id = ProjectId.parse(str(project_raw))
        run_id = RunId.parse(str(value["run_id"]))
        root = self._effective_configuration.data_root.resolve()
        _delete_owned_directory(
            root,
            root / "projects" / str(project_id) / "runs" / str(run_id),
        )
        render_ids = value.get("render_variant_ids", ())
        if not isinstance(render_ids, Sequence) or isinstance(render_ids, (str, bytes)):
            raise TypeError("Invalid deleted Run Render ownership metadata")
        for raw in render_ids:
            render_id = RenderVariantId.parse(str(raw))
            _delete_owned_directory(
                root,
                root / "projects" / str(project_id) / "renders" / str(render_id),
            )
        job_ids = value.get("job_ids", ())
        if not isinstance(job_ids, Sequence) or isinstance(job_ids, (str, bytes)):
            raise TypeError("Invalid deleted Run Job ownership metadata")
        for raw in job_ids:
            job_id = JobId.parse(str(raw))
            _delete_owned_file(root, root / "logs" / "jobs" / f"{job_id}.log")

    @property
    def _materials(self) -> MaterialsService:
        service = self._materials_instance
        if service is None:
            service = MaterialsService(
                self._effective_configuration,
                data_root_coordinator=self._data_root_coordinator,
            )
            self._materials_instance = service
        return service

    @property
    def _renders(self) -> RendersService:
        service = self._renders_instance
        if service is None:
            service = RendersService(
                self._effective_configuration,
                data_root_coordinator=self._data_root_coordinator,
            )
            self._renders_instance = service
        return service

    @staticmethod
    def _validate_plan_ownership(plan: RenderPlan, run: RunView) -> None:
        if tuple(run.video_material_ids) != (plan.video_material_id,) or tuple(
            run.music_material_ids
        ) != (plan.music_material_id,):
            raise ReviewArtifactUnavailableError(
                "Frozen Edit RenderPlan does not match its Run Material snapshot"
            )

    def _music_beats(self, plan: RenderPlan) -> tuple[tuple[float, ...], bool]:
        try:
            memory = self._materials.memory(
                plan.music_material_id,
                "structure",
                limit=500,
            )
            raw = memory.payload.get("beats_sec")
            values = raw.get("items") if isinstance(raw, Mapping) else None
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                return (), False
            beats = tuple(
                float(value)
                for value in values
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 <= float(value) <= plan.duration_sec
            )
            return beats, bool(beats)
        except (ApplicationError, KeyError, OSError, TypeError, ValueError):
            return (), False

    @staticmethod
    def _validate_replacements(
        raw_replacements: tuple[CandidateReplacement, ...],
        plan: RenderPlan,
        pool: Mapping[str, Any],
    ) -> tuple[CandidateReplacement, ...]:
        if not raw_replacements:
            raise InvalidCandidateReplacementError(
                "Guided Revision requires at least one replacement"
            )
        clips = {str(clip["slot_id"]): clip for clip in plan.clips}
        seen: set[str] = set()
        normalized: list[CandidateReplacement] = []
        for replacement in raw_replacements:
            if not isinstance(replacement, CandidateReplacement):
                raise TypeError("replacements must contain CandidateReplacement values")
            slot_id = replacement.slot_id.strip()
            candidate_id = replacement.candidate_id.strip()
            if not slot_id or not candidate_id or slot_id in seen:
                raise InvalidCandidateReplacementError(
                    "Revision Slots and Candidates must be non-empty and unique"
                )
            seen.add(slot_id)
            clip = clips.get(slot_id)
            if clip is None:
                raise InvalidCandidateReplacementError(
                    f"Unknown Revision Slot: {slot_id}"
                )
            if isinstance(clip.get("dialogue_anchor"), Mapping):
                raise AnchorLockedError(f"Story Anchor Slot is locked: {slot_id}")
            if candidate_id == str(clip["candidate_id"]):
                raise InvalidCandidateReplacementError(
                    f"Replacement for {slot_id} does not change its Candidate"
                )
            values = pool.get(slot_id)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise InvalidCandidateReplacementError(
                    f"Candidate Space has no Slot {slot_id}"
                )
            match = next(
                (
                    value
                    for value in values
                    if isinstance(value, Mapping)
                    and str(value.get("candidate_id")) == candidate_id
                    and str(value.get("slot_id") or slot_id) == slot_id
                ),
                None,
            )
            if match is None:
                raise InvalidCandidateReplacementError(
                    f"Candidate {candidate_id} does not belong to {slot_id}"
                )
            source_start, source_end = parse_range(str(match.get("timestamp") or ""))
            output_frames = clip["output_frame_range"]
            output_duration = (int(output_frames[1]) - int(output_frames[0])) / plan.fps
            if source_end - source_start + (1 / plan.fps) < output_duration:
                raise InvalidCandidateReplacementError(
                    f"Candidate {candidate_id} is shorter than Slot {slot_id}"
                )
            normalized.append(CandidateReplacement(slot_id, candidate_id))
        return tuple(sorted(normalized, key=lambda value: value.slot_id))

    @staticmethod
    def _apply_replacements(
        plan: RenderPlan,
        replacements: tuple[CandidateReplacement, ...],
        pool: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        replacements_by_slot = {value.slot_id: value for value in replacements}
        clips: list[dict[str, Any]] = []
        for original in plan.clips:
            slot_id = str(original["slot_id"])
            requested = replacements_by_slot.get(slot_id)
            if requested is None:
                clips.append(dict(original))
                continue
            values = pool[slot_id]
            candidate = next(
                value
                for value in values
                if isinstance(value, Mapping)
                and str(value.get("candidate_id")) == requested.candidate_id
            )
            clips.append(replacement_clip(original, candidate))
        return clips

    @staticmethod
    def _validate_revision_chronology(clips: Sequence[Mapping[str, Any]]) -> None:
        previous_end = -1.0
        previous_slot = ""
        for clip in clips:
            start, end = parse_range(str(clip["timestamp"]))
            if start < previous_end:
                raise RevisionInfeasibleError(
                    f"Replacement creates source overlap between {previous_slot} "
                    f"and {clip['slot_id']}"
                )
            previous_end = end
            previous_slot = str(clip["slot_id"])


def _require_run_id(value: RunId) -> None:
    if not isinstance(value, RunId):
        raise TypeError("run_id must be a RunId")


def _material_ids(value: Any, label: str) -> tuple[MaterialId, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"Invalid Project {label} Material persistence result")
    return tuple(MaterialId.parse(str(item)) for item in value)


def _music_source_duration(path: Path) -> float:
    if path.is_symlink() or not path.is_file():
        raise ManagedStateConflict(
            "run_material_not_ready",
            "The selected Music Material Memory is unavailable",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError("Music Memory must contain an object")
        duration = float(payload["source_duration_sec"])
    except (
        KeyError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ManagedStateConflict(
            "run_material_not_ready",
            "The selected Music Material has no valid source duration",
        ) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ManagedStateConflict(
            "run_material_not_ready",
            "The selected Music Material has no valid source duration",
        )
    return duration


def _require_owned_path(root: Path, target: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Application Data Root is not a safe directory")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Managed Run artifact escapes the Application Data Root"
        ) from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Managed Run artifact parent cannot be a symlink")
        if current.exists() and not current.is_dir():
            raise ValueError("Managed Run artifact parent is not a directory")
    try:
        target.parent.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Managed Run artifact parent escapes the Application Data Root"
        ) from exc


def _delete_owned_directory(root: Path, target: Path) -> None:
    _require_owned_path(root, target)
    if target.is_symlink():
        target.unlink()
        return
    if not target.exists():
        return
    if not target.is_dir():
        raise ValueError("Managed Run artifact owner path is not a directory")
    shutil.rmtree(target)


def _delete_owned_file(root: Path, target: Path) -> None:
    _require_owned_path(root, target)
    if target.is_symlink() or target.is_file():
        target.unlink()
        return
    if target.exists():
        raise ValueError("Managed Run Job log path is not a file")


__all__ = ["RunsService"]
