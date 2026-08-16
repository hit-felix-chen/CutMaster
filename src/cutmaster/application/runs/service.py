"""Immutable ASTER Run and Guided Revision use cases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from cutmaster.application.errors import (
    ApplicationError,
    AnchorLockedError,
    CandidateSpaceUnavailableError,
    InvalidCandidateReplacementError,
    ReviewArtifactUnavailableError,
    RevisionInfeasibleError,
)
from cutmaster.application.jobs.views import attempt_view, job_view
from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.renders.service import RendersService
from cutmaster.application.runs.commands import (
    CandidateReplacement,
    CompleteRunCommand,
    CreateRevisionCommand,
    CreateRunCommand,
    DeleteRunCommand,
    RecoverRunCommand,
    SaveGuidedRevisionCommand,
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
    CompletedRunView,
    DeletedRunView,
    FrozenEditReviewView,
    FrozenEditView,
    RunSubmissionView,
    RunView,
    frozen_edit_view,
    run_submission_view,
    run_view,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.artifacts import validate_portable_relative_file_path
from cutmaster.domain.ids import AttemptId, FrozenEditId, ProjectId, RunId
from cutmaster.infrastructure.persistence.sqlite import SQLiteApplicationStore
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.shared.timecode import parse_range


class RunsService:
    """Own ASTER Run and Guided Revision use cases."""

    __slots__ = (
        "_effective_configuration",
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
    ) -> None:
        self._effective_configuration = effective_configuration
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

    def create(self, command: CreateRunCommand) -> RunSubmissionView:
        if not isinstance(command.project_id, ProjectId):
            raise TypeError("project_id must be a ProjectId")
        return run_submission_view(
            self._store.create_run(
                command.command_id,
                command.project_id,
                self._effective_configuration.to_dict(),
            ).value
        )

    def get(self, run_id: RunId) -> RunView:
        _require_run_id(run_id)
        return run_view(self._store.get_run(run_id))

    def list(self, project_id: ProjectId) -> tuple[RunView, ...]:
        if not isinstance(project_id, ProjectId):
            raise TypeError("project_id must be a ProjectId")
        return tuple(run_view(value) for value in self._store.list_runs(project_id))

    def complete(self, command: CompleteRunCommand) -> CompletedRunView:
        _require_run_id(command.run_id)
        if not isinstance(command.attempt_id, AttemptId):
            raise TypeError("attempt_id must be an AttemptId")
        plan_path = validate_portable_relative_file_path(command.plan_relative_path)
        value = self._store.complete_run(
            command.command_id,
            command.run_id,
            command.attempt_id,
            plan_path,
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

    def get_frozen_edit(self, edit_id: FrozenEditId) -> FrozenEditView:
        if not isinstance(edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        return frozen_edit_view(self._store.get_frozen_edit(edit_id))

    def list_frozen_edits(self, run_id: RunId) -> tuple[FrozenEditView, ...]:
        _require_run_id(run_id)
        return tuple(
            frozen_edit_view(value)
            for value in self._store.list_frozen_edits(run_id)
        )

    def review(self, edit_id: FrozenEditId) -> FrozenEditReviewView:
        """Build one coherent, privacy-safe Review page projection."""

        edit = self.get_frozen_edit(edit_id)
        run = self.get(edit.run_id)
        plan, plan_path = load_render_plan(
            self._effective_configuration.data_root,
            edit.plan.relative_path,
        )
        self._validate_plan_ownership(plan, run)
        bundle, unavailable_reason = load_review_bundle(plan_path)
        candidate_pool = (
            bundle.get("candidate_pool") if bundle is not None else None
        )
        if candidate_pool is not None and not isinstance(candidate_pool, Mapping):
            raise CandidateSpaceUnavailableError("Candidate Space is invalid")
        beats, beats_available = self._music_beats(plan)
        return FrozenEditReviewView(
            edit=edit,
            run=run,
            versions=self.list_frozen_edits(run.run_id),
            plan=project_plan_summary(plan),
            video_material_id=plan.video_material_id,
            music_material_id=plan.music_material_id,
            candidate_space_available=candidate_pool is not None,
            candidate_space_unavailable_reason=unavailable_reason,
            slots=project_slots(plan),
            candidates=project_candidates(plan, candidate_pool),
            dialogue_cues=project_dialogue_cues(plan),
            music_beats_sec=beats,
            music_beats_available=beats_available,
            variants=self._renders.list(edit_id),
        )

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
        bundle, _ = load_review_bundle(plan_path)
        if bundle is None:
            raise CandidateSpaceUnavailableError(
                "This Frozen Edit was created before Candidate Space persistence"
            )
        pool = bundle.get("candidate_pool")
        if not isinstance(pool, Mapping):
            raise CandidateSpaceUnavailableError("Candidate Space is invalid")
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
        command_key = hashlib.sha256(command.command_id.encode("utf-8")).hexdigest()[:16]
        content_key = hashlib.sha256(replacement_payload.encode("utf-8")).hexdigest()[:16]
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

    def retry(self, command: RecoverRunCommand) -> RunSubmissionView:
        _require_run_id(command.run_id)
        return run_submission_view(
            self._store.retry_run(
                command.command_id,
                command.run_id,
                resume=False,
            ).value
        )

    def resume(self, command: RecoverRunCommand) -> RunSubmissionView:
        _require_run_id(command.run_id)
        return run_submission_view(
            self._store.retry_run(
                command.command_id,
                command.run_id,
                resume=True,
            ).value
        )

    def delete(self, command: DeleteRunCommand) -> DeletedRunView:
        _require_run_id(command.run_id)
        value = self._store.delete_run(command.command_id, command.run_id).value
        return DeletedRunView(
            run_id=RunId.parse(str(value["run_id"])),
            deleted=bool(value["deleted"]),
        )

    @property
    def _materials(self) -> MaterialsService:
        service = self._materials_instance
        if service is None:
            service = MaterialsService(self._effective_configuration)
            self._materials_instance = service
        return service

    @property
    def _renders(self) -> RendersService:
        service = self._renders_instance
        if service is None:
            service = RendersService(self._effective_configuration)
            self._renders_instance = service
        return service

    @staticmethod
    def _validate_plan_ownership(plan: RenderPlan, run: RunView) -> None:
        if (
            tuple(run.video_material_ids) != (plan.video_material_id,)
            or tuple(run.music_material_ids) != (plan.music_material_id,)
        ):
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


__all__ = ["RunsService"]
