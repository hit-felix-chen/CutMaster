"""Composition root for CutMaster's transport-neutral Application Layer."""

from __future__ import annotations

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


_Service = TypeVar("_Service")


class _ApplicationMaterialReferenceChecker:
    """Lazily consult managed state only when a deletion is attempted."""

    __slots__ = ("_application",)

    def __init__(self, application: "CutMasterApplication") -> None:
        self._application = application

    def references(self, material_id: MaterialId) -> Sequence[str]:
        return self._application.projects.references(material_id)


class CutMasterApplication:
    """Wire configuration and lazily constructed Application service groups."""

    __slots__ = ("_effective_configuration", "_service_lock", "_services")

    _CONSTRUCTION_TOKEN = object()

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not self._CONSTRUCTION_TOKEN:
            raise TypeError("Use CutMasterApplication.open() to create an Application")
        if not isinstance(effective_configuration, EffectiveConfiguration):
            raise TypeError("effective_configuration must be EffectiveConfiguration")
        self._effective_configuration = effective_configuration
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
        load_dotenv(effective_configuration.sources.dotenv_path, override=False)
        return cls(effective_configuration, _token=cls._CONSTRUCTION_TOKEN)

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
            ),
        )

    @property
    def materials(self) -> MaterialsService:
        return self._get_service(
            "materials",
            lambda: MaterialsService(
                self._effective_configuration,
                reference_checker=_ApplicationMaterialReferenceChecker(self),
            ),
        )

    @property
    def projects(self) -> ProjectsService:
        return self._get_service(
            "projects",
            lambda: ProjectsService(
                self._effective_configuration,
                materials=self.materials,
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
            ),
        )

    @property
    def renders(self) -> RendersService:
        return self._get_service(
            "renders",
            lambda: RendersService(self._effective_configuration),
        )

    @property
    def jobs(self) -> JobsService:
        return self._get_service(
            "jobs",
            lambda: JobsService(self._effective_configuration),
        )

    @property
    def settings(self) -> SettingsService:
        return self._get_service(
            "settings",
            lambda: SettingsService(self._effective_configuration),
        )


__all__ = ["CutMasterApplication"]
