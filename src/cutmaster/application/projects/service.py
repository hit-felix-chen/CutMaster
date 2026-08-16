"""Edit Project use cases backed by durable local persistence."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.ports.data_root import (
    DataRootCoordinator,
    root_shared_operation,
)
from cutmaster.application.projects.commands import (
    CreateProjectCommand,
    DeleteProjectCommand,
    RenameProjectCommand,
    SaveCreativeBriefCommand,
    SaveProjectSetupCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.projects.views import DeletedProjectView, ProjectView
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.ids import JobId, MaterialId, ProjectId
from cutmaster.domain.materials import MaterialType
from cutmaster.domain.projects import CreativeBrief
from cutmaster.infrastructure.persistence.sqlite import SQLiteApplicationStore


class ProjectsService:
    """Own Edit Project and Creative Brief use cases."""

    __slots__ = (
        "_effective_configuration",
        "_data_root_coordinator",
        "_materials_instance",
        "_store_instance",
    )

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        store: SQLiteApplicationStore | None = None,
        *,
        materials: MaterialsService | None = None,
        data_root_coordinator: DataRootCoordinator | None = None,
    ) -> None:
        self._effective_configuration = effective_configuration
        self._data_root_coordinator = data_root_coordinator
        self._store_instance = store
        self._materials_instance = materials

    @property
    def _store(self) -> SQLiteApplicationStore:
        store = self._store_instance
        if store is None:
            store = SQLiteApplicationStore(self._effective_configuration.data_root)
            self._store_instance = store
        return store

    @property
    def reference_checker(self) -> SQLiteApplicationStore:
        """Return the checker wired into the Material Catalog on composition."""

        return self._store

    @property
    def _materials(self) -> MaterialsService:
        materials = self._materials_instance
        if materials is None:
            materials = MaterialsService(
                self._effective_configuration,
                data_root_coordinator=self._data_root_coordinator,
            )
            self._materials_instance = materials
        return materials

    @root_shared_operation
    def create(self, command: CreateProjectCommand) -> ProjectView:
        result = self._store.create_project(command.command_id, command.name)
        return _project_view(result.value)

    @root_shared_operation
    def get(self, project_id: ProjectId) -> ProjectView:
        return _project_view(self._store.get_project(project_id))

    @root_shared_operation
    def list(
        self,
        *,
        search: str = "",
        sort: str = "updated_desc",
    ) -> tuple[ProjectView, ...]:
        if not isinstance(search, str):
            raise TypeError("search must be a string")
        if sort not in {"updated_desc", "updated_asc", "name_asc", "name_desc"}:
            raise ValueError("Unsupported Project sort")
        query = search.strip().casefold()
        items = [_project_view(item) for item in self._store.list_projects()]
        if query:
            items = [item for item in items if query in item.name.casefold()]
        if sort.startswith("name_"):
            items.sort(
                key=lambda item: (item.name.casefold(), str(item.project_id)),
                reverse=sort == "name_desc",
            )
        else:
            items.sort(
                key=lambda item: (item.updated_at, str(item.project_id)),
                reverse=sort == "updated_desc",
            )
        return tuple(items)

    @root_shared_operation
    def rename(self, command: RenameProjectCommand) -> ProjectView:
        _require_project_id(command.project_id)
        result = self._store.rename_project(
            command.command_id,
            command.project_id,
            command.name,
        )
        return _project_view(result.value)

    @root_shared_operation
    def set_materials(self, command: SetProjectMaterialsCommand) -> ProjectView:
        _require_project_id(command.project_id)
        if len(command.video_material_ids) > 1 or len(command.music_material_ids) > 1:
            raise ValueError(
                "The first release supports at most one video and one music Material"
            )
        expected_types: dict[MaterialId, MaterialType] = {}
        for material_id, material_type in (
            *((item, MaterialType.VIDEO) for item in command.video_material_ids),
            *((item, MaterialType.MUSIC) for item in command.music_material_ids),
        ):
            if not isinstance(material_id, MaterialId):
                raise TypeError("Project Material IDs must be MaterialId values")
            previous_type = expected_types.setdefault(material_id, material_type)
            if previous_type is not material_type:
                raise ValueError(
                    f"Material {material_id} cannot be both video and music"
                )

        # Material deletion acquires the same per-Material catalog lock before
        # consulting SQLite references. Holding every lease through the SQLite
        # commit makes adding a reference and deleting its Material one ordered,
        # race-free operation. Sorting gives all callers one lock order.
        with ExitStack() as leases:
            for material_id in sorted(expected_types, key=str):
                binding = leases.enter_context(self._materials.lease(material_id))
                expected_type = expected_types[material_id]
                if binding.material.material_type is not expected_type:
                    raise ValueError(
                        f"Material {material_id} is "
                        f"{binding.material.material_type.value}, expected "
                        f"{expected_type.value}"
                    )
            result = self._store.set_project_materials(
                command.command_id,
                command.project_id,
                command.video_material_ids,
                command.music_material_ids,
            )
        return _project_view(result.value)

    @root_shared_operation
    def save_creative_brief(
        self,
        command: SaveCreativeBriefCommand,
    ) -> ProjectView:
        _require_project_id(command.project_id)
        brief = CreativeBrief(
            editing_intent=command.editing_intent,
            target_duration_sec=command.target_duration_sec,
        )
        result = self._store.save_creative_brief(
            command.command_id,
            command.project_id,
            brief.editing_intent,
            brief.target_duration_sec,
        )
        return _project_view(result.value)

    @root_shared_operation
    def save_setup(self, command: SaveProjectSetupCommand) -> ProjectView:
        """Atomically save Material selection and Creative Brief."""

        _require_project_id(command.project_id)
        expected_types = self._validated_material_selection(
            command.video_material_ids,
            command.music_material_ids,
        )
        brief = CreativeBrief(
            editing_intent=command.editing_intent,
            target_duration_sec=command.target_duration_sec,
        )
        with ExitStack() as leases:
            for material_id in sorted(expected_types, key=str):
                binding = leases.enter_context(self._materials.lease(material_id))
                expected_type = expected_types[material_id]
                if binding.material.material_type is not expected_type:
                    raise ValueError(
                        f"Material {material_id} is "
                        f"{binding.material.material_type.value}, expected "
                        f"{expected_type.value}"
                    )
            result = self._store.save_project_setup(
                command.command_id,
                command.project_id,
                command.video_material_ids,
                command.music_material_ids,
                brief.editing_intent,
                brief.target_duration_sec,
            )
        return _project_view(result.value)

    @staticmethod
    def _validated_material_selection(
        video_material_ids: tuple[MaterialId, ...],
        music_material_ids: tuple[MaterialId, ...],
    ) -> dict[MaterialId, MaterialType]:
        if len(video_material_ids) > 1 or len(music_material_ids) > 1:
            raise ValueError(
                "The first release supports at most one video and one music Material"
            )
        expected_types: dict[MaterialId, MaterialType] = {}
        for material_id, material_type in (
            *((item, MaterialType.VIDEO) for item in video_material_ids),
            *((item, MaterialType.MUSIC) for item in music_material_ids),
        ):
            if not isinstance(material_id, MaterialId):
                raise TypeError("Project Material IDs must be MaterialId values")
            previous_type = expected_types.setdefault(material_id, material_type)
            if previous_type is not material_type:
                raise ValueError(
                    f"Material {material_id} cannot be both video and music"
                )
        return expected_types

    @root_shared_operation
    def delete(self, command: DeleteProjectCommand) -> DeletedProjectView:
        _require_project_id(command.project_id)
        result = self._store.delete_project(command.command_id, command.project_id)
        self._delete_project_artifacts(result.value)
        return DeletedProjectView(
            project_id=ProjectId.parse(result.value["project_id"]),
            deleted=bool(result.value["deleted"]),
        )

    def _delete_project_artifacts(self, value: Mapping[str, Any]) -> None:
        owner_raw = value.get("artifact_owner_relative_path")
        if owner_raw is None:
            # Historical receipts did not retain exact ownership metadata. Do
            # not infer deletion targets from mutable state.
            return
        project_id = ProjectId.parse(str(value["project_id"]))
        expected_owner = (Path("projects") / str(project_id)).as_posix()
        if owner_raw != expected_owner:
            raise ValueError("Invalid deleted Project artifact ownership metadata")
        root = self._effective_configuration.data_root.resolve()
        _delete_owned_directory(root, root / expected_owner)
        job_ids = value.get("job_ids", ())
        if not isinstance(job_ids, Sequence) or isinstance(job_ids, (str, bytes)):
            raise TypeError("Invalid deleted Project Job ownership metadata")
        for raw in job_ids:
            job_id = JobId.parse(str(raw))
            _delete_owned_file(root, root / "logs" / "jobs" / f"{job_id}.log")

    @root_shared_operation
    def references(self, material_id: MaterialId) -> tuple[str, ...]:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        return tuple(self._store.references(material_id))


def _require_project_id(value: ProjectId) -> None:
    if not isinstance(value, ProjectId):
        raise TypeError("project_id must be a ProjectId")


def _project_view(value: dict[str, object]) -> ProjectView:
    brief_value = value["creative_brief"]
    if brief_value is None:
        brief = None
    else:
        if not isinstance(brief_value, dict):
            raise TypeError("Invalid Creative Brief persistence result")
        brief = CreativeBrief(
            editing_intent=str(brief_value["editing_intent"]),
            target_duration_sec=float(brief_value["target_duration_sec"]),
        )
    return ProjectView(
        project_id=ProjectId.parse(str(value["project_id"])),
        name=str(value["name"]),
        video_material_ids=tuple(
            MaterialId.parse(str(item)) for item in value["video_material_ids"]
        ),
        music_material_ids=tuple(
            MaterialId.parse(str(item)) for item in value["music_material_ids"]
        ),
        creative_brief=brief,
        created_at=datetime.fromisoformat(str(value["created_at"])),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
    )


def _require_owned_path(root: Path, target: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Application Data Root is not a safe directory")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Managed Project artifact escapes the Application Data Root"
        ) from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Managed Project artifact parent cannot be a symlink")
        if current.exists() and not current.is_dir():
            raise ValueError("Managed Project artifact parent is not a directory")
    try:
        target.parent.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Managed Project artifact parent escapes the Application Data Root"
        ) from exc


def _delete_owned_directory(root: Path, target: Path) -> None:
    _require_owned_path(root, target)
    if target.is_symlink():
        target.unlink()
        return
    if not target.exists():
        return
    if not target.is_dir():
        raise ValueError("Managed Project artifact owner path is not a directory")
    shutil.rmtree(target)


def _delete_owned_file(root: Path, target: Path) -> None:
    _require_owned_path(root, target)
    if target.is_symlink() or target.is_file():
        target.unlink()
        return
    if target.exists():
        raise ValueError("Managed Project Job log path is not a file")


__all__ = ["ProjectsService"]
