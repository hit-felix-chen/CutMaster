"""Composition root for CutMaster's transport-neutral Application Layer."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import RLock
from typing import TypeVar, cast

from dotenv import load_dotenv

from cutmaster.application.direct.service import DirectService
from cutmaster.application.jobs.service import JobsService
from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.projects.service import ProjectsService
from cutmaster.application.renders.service import RendersService
from cutmaster.application.runs.service import RunsService
from cutmaster.application.settings.service import SettingsService
from cutmaster.configuration.effective import (
    EffectiveConfiguration,
    load_effective_configuration,
)
from cutmaster.domain.ids import MaterialId
from cutmaster.infrastructure.storage.local.data_root_coordination import (
    LocalDataRootCoordinator,
)

_Service = TypeVar("_Service")


class _ApplicationMaterialReferenceChecker:
    """Lazily consult managed state only when a deletion is attempted."""

    __slots__ = ("_application",)

    def __init__(self, application: CutMasterApplication) -> None:
        self._application = application

    def references(self, material_id: MaterialId) -> Sequence[str]:
        managed = tuple(self._application.projects.references(material_id))
        active_attempts = tuple(
            f"attempt:{attempt_id}:active"
            for attempt_id in self._application.jobs.active_material_attempt_ids(
                material_id
            )
        )
        return (*managed, *active_attempts)


class CutMasterApplication:
    """Wire configuration and lazily constructed Application service groups."""

    __slots__ = (
        "_effective_configuration",
        "_data_root_coordinator",
        "_process_environment_names",
        "_service_lock",
        "_services",
    )

    _CONSTRUCTION_TOKEN = object()

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        *,
        _process_environment_names: frozenset[str] = frozenset(),
        _token: object | None = None,
    ) -> None:
        if _token is not self._CONSTRUCTION_TOKEN:
            raise TypeError("Use CutMasterApplication.open() to create an Application")
        if not isinstance(effective_configuration, EffectiveConfiguration):
            raise TypeError("effective_configuration must be EffectiveConfiguration")
        self._effective_configuration = effective_configuration
        self._data_root_coordinator = LocalDataRootCoordinator(effective_configuration)
        self._process_environment_names = _process_environment_names
        self._service_lock = RLock()
        self._services: dict[str, object] = {}

    @classmethod
    def open(
        cls,
        config_path: Path | str = Path("config.toml"),
    ) -> CutMasterApplication:
        """Load one Application context without opening runtime resources."""

        resolved_config_path = Path(config_path).expanduser().resolve()
        effective_configuration = load_effective_configuration(resolved_config_path)
        # Capture names, never values, before dotenv loading.  This preserves
        # process precedence even when Custom later points at another existing
        # environment variable.
        process_environment_names = frozenset(os.environ)
        load_dotenv(effective_configuration.sources.dotenv_path, override=False)
        return cls(
            effective_configuration,
            _process_environment_names=process_environment_names,
            _token=cls._CONSTRUCTION_TOKEN,
        )

    def _get_service(
        self,
        name: str,
        factory: Callable[[], _Service],
    ) -> _Service:
        with self._service_lock:
            service = self._services.get(name)
            if service is None:
                service = factory()
                self._services[name] = service
            return cast(_Service, service)

    @property
    def direct(self) -> DirectService:
        return self._get_service(
            "direct",
            lambda: DirectService(
                self._effective_configuration,
                self.materials,
                self._data_root_coordinator,
            ),
        )

    @property
    def materials(self) -> MaterialsService:
        return self._get_service(
            "materials",
            lambda: MaterialsService(
                self._effective_configuration,
                reference_checker=_ApplicationMaterialReferenceChecker(self),
                data_root_coordinator=self._data_root_coordinator,
            ),
        )

    @property
    def projects(self) -> ProjectsService:
        return self._get_service(
            "projects",
            lambda: ProjectsService(
                self._effective_configuration,
                materials=self.materials,
                data_root_coordinator=self._data_root_coordinator,
            ),
        )

    @property
    def runs(self) -> RunsService:
        return self._get_service(
            "runs",
            lambda: RunsService(
                self._effective_configuration,
                materials=self.materials,
                renders=self.renders,
                data_root_coordinator=self._data_root_coordinator,
            ),
        )

    @property
    def renders(self) -> RendersService:
        return self._get_service(
            "renders",
            lambda: RendersService(
                self._effective_configuration,
                materials=self.materials,
                data_root_coordinator=self._data_root_coordinator,
            ),
        )

    @property
    def jobs(self) -> JobsService:
        return self._get_service(
            "jobs",
            lambda: JobsService(
                self._effective_configuration,
                data_root_coordinator=self._data_root_coordinator,
            ),
        )

    @property
    def settings(self) -> SettingsService:
        return self._get_service(
            "settings",
            lambda: SettingsService(
                self._effective_configuration,
                process_environment_names=self._process_environment_names,
                data_root_coordinator=self._data_root_coordinator,
            ),
        )

    @property
    def data_root_coordinator(self) -> LocalDataRootCoordinator:
        """Return the process-local adapter for the stable root authority."""

        return self._data_root_coordinator


__all__ = ["CutMasterApplication"]
