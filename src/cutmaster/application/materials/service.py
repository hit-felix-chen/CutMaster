"""Material lifecycle Application service."""

from __future__ import annotations

import json
import math
import mimetypes
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from threading import RLock
from typing import Any

from cutmaster.application.errors import (
    MaterialMemoryTabNotFoundError,
    MaterialMemoryUnavailableError,
    MaterialPreviewUnavailableError,
)
from cutmaster.application.materials._previews import (
    build_preview,
    preview_available,
)
from cutmaster.application.materials.views import (
    MaterialDetailView,
    MaterialMemoryView,
    MaterialPreviewView,
    MaterialView,
    frozen_payload,
)
from cutmaster.application.ports.data_root import (
    DataRootCoordinator,
    root_shared_context,
    root_shared_operation,
)
from cutmaster.application.ports.material_catalog import (
    MaterialBinding,
    MaterialCatalog,
    MaterialRecord,
    MaterialReferenceChecker,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialCondition, MaterialType


class MaterialsService:
    """Own Material import, lookup, consistency, leasing, and deletion."""

    __slots__ = (
        "_catalog_instance",
        "_catalog_lock",
        "_data_root_coordinator",
        "_effective_configuration",
        "_reference_checker",
    )

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        *,
        catalog: MaterialCatalog | None = None,
        reference_checker: MaterialReferenceChecker | None = None,
        data_root_coordinator: DataRootCoordinator | None = None,
    ) -> None:
        if not isinstance(effective_configuration, EffectiveConfiguration):
            raise TypeError("effective_configuration must be EffectiveConfiguration")
        if catalog is not None and reference_checker is not None:
            raise ValueError(
                "reference_checker must be configured on an injected catalog"
            )
        self._effective_configuration = effective_configuration
        self._data_root_coordinator = data_root_coordinator
        self._catalog_instance = catalog
        self._reference_checker = reference_checker
        self._catalog_lock = RLock()

    @root_shared_operation
    def add(
        self,
        source_path: Path | str,
        material_type: MaterialType | str,
        name: str | None = None,
    ) -> MaterialView:
        return self._view(self._catalog().add(source_path, material_type, name))

    @root_shared_operation
    def ensure(
        self,
        source_path: Path | str,
        material_type: MaterialType | str,
        name: str | None = None,
    ) -> MaterialView:
        return self._view(self._catalog().ensure(source_path, material_type, name))

    @root_shared_operation
    def get(self, material_id: MaterialId) -> MaterialView | None:
        record = self._catalog().get(material_id)
        return self._view(record) if record is not None else None

    @root_shared_operation
    def find_by_name(
        self,
        material_type: MaterialType | str,
        name: str,
    ) -> MaterialView | None:
        record = self._catalog().find_by_name(material_type, name)
        return self._view(record) if record is not None else None

    @root_shared_operation
    def list(
        self,
        material_type: MaterialType | str | None = None,
        *,
        search: str = "",
        sort: str = "name_asc",
    ) -> list[MaterialView]:
        if not isinstance(search, str):
            raise TypeError("search must be a string")
        if sort not in {"name_asc", "name_desc", "type_name"}:
            raise ValueError("Unsupported Material sort")
        query = search.strip().casefold()
        items = [self._view(record) for record in self._catalog().list(material_type)]
        if query:
            items = [item for item in items if query in item.name.casefold()]
        if sort == "type_name":
            items.sort(
                key=lambda item: (
                    item.material_type.value,
                    item.name.casefold(),
                    str(item.material_id),
                )
            )
        else:
            items.sort(
                key=lambda item: (item.name.casefold(), str(item.material_id)),
                reverse=sort == "name_desc",
            )
        return items

    @root_shared_operation
    def detail(self, material_id: MaterialId) -> MaterialDetailView | None:
        """Return a lightweight drawer projection without opening source media."""

        material = self.get(material_id)
        if material is None:
            return None
        references = self.references(material_id)
        try:
            with self.read_lease(material_id) as binding:
                if material.condition is MaterialCondition.READY:
                    primary_memory, secondary_memory = _projection_documents(
                        binding.memory_root,
                        material.material_type,
                    )
                else:
                    primary_memory, secondary_memory = {}, {}
                duration_sec = _material_duration(
                    binding.memory_root,
                    binding.source_path,
                    material.material_type,
                    primary_memory,
                )
                source = _material_source_metadata(
                    binding.memory_root,
                    binding.source_path,
                    material,
                    primary_memory,
                )
                memory_summary = _material_memory_summary(
                    binding.memory_root,
                    material.material_type,
                    primary_memory,
                    secondary_memory,
                )
                has_preview = bool(
                    material.condition is MaterialCondition.READY
                    and preview_available(
                        binding.memory_root,
                        material.material_type,
                        primary_memory,
                    )
                )
                analysis_cost_yuan = (
                    _analysis_cost_yuan(binding.memory_root, material.material_type)
                    if material.condition is MaterialCondition.READY
                    else None
                )
        except FileNotFoundError:
            # Inspection never blocks deletion. If delete wins before the
            # projection has pinned any bytes, the resource simply disappears.
            return None
        return MaterialDetailView(
            material=material,
            references=references,
            analysis_available=material.condition is MaterialCondition.READY,
            duration_sec=duration_sec,
            source=frozen_payload(source),
            memory_summary=frozen_payload(memory_summary),
            preview_available=has_preview,
            analysis_cost_yuan=analysis_cost_yuan,
        )

    @root_shared_operation
    def references(self, material_id: MaterialId) -> tuple[str, ...]:
        if not isinstance(material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        if self._reference_checker is None:
            return ()
        # Active Analysis Attempts are deletion-exclusion blockers, not user
        # content references.  The Catalog still sees them through the same
        # checker during deletion, while cards report only Project/Run owners.
        return tuple(
            value
            for value in self._reference_checker.references(material_id)
            if not str(value).startswith("attempt:")
        )

    @root_shared_operation
    def memory(
        self,
        material_id: MaterialId,
        tab: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> MaterialMemoryView:
        """Read one sanitized Material Memory tab through a browsing lease.

        Fingerprints, source paths, managed paths, prompt payloads, and checkpoint
        files never enter this Application read model.
        """

        material = self.get(material_id)
        if material is None:
            raise KeyError(str(material_id))
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
        ):
            raise ValueError("limit must be between 1 and 500")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        normalized_tab = str(tab).strip().lower()
        allowed = {
            MaterialType.VIDEO: {"timeline", "story", "dialogue", "technical"},
            MaterialType.MUSIC: {"structure", "technical"},
        }[material.material_type]
        if normalized_tab not in allowed:
            raise MaterialMemoryTabNotFoundError(
                f"{material.material_type.value} Material Memory has no "
                f"{normalized_tab!r} tab"
            )
        if material.condition is not MaterialCondition.READY:
            raise MaterialMemoryUnavailableError(f"Material {material_id} is not ready")

        with self.read_lease(material_id) as binding:
            if material.material_type is MaterialType.VIDEO:
                payload = _video_memory_payload(
                    binding.memory_root,
                    normalized_tab,
                    limit=limit,
                    offset=offset,
                )
            else:
                payload = _music_memory_payload(
                    binding.memory_root,
                    normalized_tab,
                    limit=limit,
                    offset=offset,
                )
        return MaterialMemoryView(
            material_id=material_id,
            material_type=material.material_type,
            tab=normalized_tab,
            limit=limit,
            offset=offset,
            payload=frozen_payload(payload),
        )

    @root_shared_operation
    def preview(self, material_id: MaterialId) -> MaterialPreviewView:
        """Return a bounded card image without exposing or opening source media."""

        material = self.get(material_id)
        if material is None:
            raise KeyError(str(material_id))
        if material.condition is not MaterialCondition.READY:
            raise MaterialPreviewUnavailableError(
                f"Material {material_id} has no ready preview"
            )
        with self.read_lease(material_id) as binding:
            projection = build_preview(
                binding.memory_root,
                material.material_type,
            )
        if projection is None:
            raise MaterialPreviewUnavailableError(
                f"Material {material_id} has no safe preview"
            )
        media_type, content = projection
        return MaterialPreviewView(
            material_id=material_id,
            material_type=material.material_type,
            media_type=media_type,
            content=content,
        )

    @root_shared_operation
    def verify(self, material_id: MaterialId) -> bool:
        return self._catalog().verify(material_id)

    @root_shared_operation
    def delete(self, material_id: MaterialId) -> None:
        self._catalog().delete(material_id)

    @root_shared_operation
    def publish_analysis_result(
        self,
        binding: MaterialBinding,
        staged_result_path: Path | str,
    ) -> MaterialView:
        """Atomically publish a validated result through an active lease."""

        return self._view(
            self._catalog().publish_analysis_result(binding, staged_result_path)
        )

    @root_shared_operation
    def validate_analysis_result(self, material_id: MaterialId) -> bool:
        return self._catalog().validate_analysis_result(material_id)

    @root_shared_operation
    def ensure_subtitle(
        self,
        binding: MaterialBinding,
        source_path: Path | str,
    ) -> Path:
        """Privately bind one immutable subtitle to a leased video Material."""

        return self._catalog().ensure_subtitle(binding, source_path)

    @root_shared_operation
    def resolve_subtitle(self, binding: MaterialBinding) -> Path | None:
        """Privately resolve a leased video Material's verified subtitle."""

        return self._catalog().resolve_subtitle(binding)

    @root_shared_context
    def lease(
        self,
        material_id: MaterialId,
    ) -> AbstractContextManager[MaterialBinding]:
        """Internal Application collaborator used by managed workflow executors.

        Inbound adapters receive :class:`MaterialView` objects and must not expose
        the binding's fingerprint or local paths as Material identity.
        """

        return self._catalog().lease(material_id)

    @root_shared_context
    def read_lease(
        self,
        material_id: MaterialId,
    ) -> AbstractContextManager[MaterialBinding]:
        """Inspect Material paths without acquiring the workflow/delete lock.

        Unlike the verified Workflow lease, this does not recompute the source
        SHA-256.  It cannot be used to publish analysis results or sidecars.
        """

        return self._catalog().read_lease(material_id)

    @root_shared_context
    def consume_lease(
        self,
        material_id: MaterialId,
    ) -> AbstractContextManager[MaterialBinding]:
        """Share a verified Material binding with concurrent workflow consumers."""

        return self._catalog().consume_lease(material_id)

    def _catalog(self) -> MaterialCatalog:
        with self._catalog_lock:
            if self._catalog_instance is None:
                # The concrete adapter is imported lazily so opening the
                # Application or accessing app.materials performs no filesystem
                # writes. The first real Material operation creates the catalog.
                from cutmaster.infrastructure.storage.local.material_catalog import (
                    MaterialCatalog as LocalMaterialCatalog,
                )

                self._catalog_instance = LocalMaterialCatalog(
                    self._effective_configuration.data_root / "media",
                    reference_checker=self._reference_checker,
                )
            return self._catalog_instance

    @staticmethod
    def _view(record: MaterialRecord) -> MaterialView:
        material = record.material
        return MaterialView(
            material_id=material.material_id,
            material_type=material.material_type,
            name=material.name,
            condition=material.condition,
            reused=record.reused,
        )


def _read_memory_document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise MaterialMemoryUnavailableError(
            f"Material Memory document is unavailable: {path.name}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterialMemoryUnavailableError(
            f"Material Memory document is unreadable: {path.name}"
        ) from exc
    if not isinstance(value, dict):
        raise MaterialMemoryUnavailableError(
            f"Material Memory document is invalid: {path.name}"
        )
    return value


def _analysis_cost_yuan(root: Path, kind: MaterialType) -> float | None:
    """Read only the safe cumulative API cost from a current analysis result."""

    if kind is MaterialType.MUSIC:
        # Music analysis is local signal processing and makes no model API calls.
        return 0.0
    result = _optional_memory_document(root / "analysis_result.json")
    if result is None:
        return None
    summary = result.get("model_usage_cumulative_summary")
    if not isinstance(summary, Mapping):
        return None
    value = summary.get("total_cost_yuan")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def _public_video_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = ("title", "duration_sec", "fps", "width", "height")
    return {key: value[key] for key in allowed if key in value}


def _public_segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = (
        "segment_id",
        "time_range",
        "has_dialogue",
        "speech_mode",
        "content_type",
        "timeline_role",
        "shots",
        "dialogue_items",
        "segment_summary",
        "narrative_function",
        "emotional_tone",
        "emotional_intensity",
        "appearing_characters",
    )
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        result.append({key: item[key] for key in allowed if key in item})
    return result


def _page(items: Any, *, limit: int, offset: int) -> dict[str, Any]:
    sequence = items if isinstance(items, list) else []
    return {
        "items": sequence[offset : offset + limit],
        "total": len(sequence),
        "limit": limit,
        "offset": offset,
    }


def _video_memory_payload(
    root: Path,
    tab: str,
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    description = _read_memory_document(root / "video_description.json")
    if tab == "timeline":
        return {
            "schema_version": description.get("schema_version"),
            "source": _public_video_source(description.get("source")),
            "segments": _page(
                _public_segments(description.get("segments")),
                limit=limit,
                offset=offset,
            ),
        }
    if tab == "story":
        return _read_memory_document(root / "video_summary.json")
    dialogues = _read_memory_document(root / "dialogues.json")
    if tab == "dialogue":
        return {
            "schema_version": dialogues.get("schema_version"),
            "statistics": dialogues.get("statistics", {}),
            "sentences": _page(
                dialogues.get("sentences", []),
                limit=limit,
                offset=offset,
            ),
        }

    segments = description.get("segments", [])
    if not isinstance(segments, list):
        segments = []
    shots = [
        shot
        for segment in segments
        if isinstance(segment, Mapping)
        for shot in segment.get("shots", [])
        if isinstance(shot, Mapping)
    ]
    rejected = sum(
        1
        for shot in shots
        if shot.get("visual_annotation_status") == "provider_rejected"
    )
    sentences = dialogues.get("sentences", [])
    return {
        "schema_version": description.get("schema_version"),
        "source": _public_video_source(description.get("source")),
        "scene_detection": description.get("scene_detection", {}),
        "models": {
            "asr": description.get("asr_model"),
            "scene_boundary": description.get("scene_boundary_model"),
            "visual_description": description.get("visual_description_model"),
        },
        "segment_count": len(segments),
        "shot_count": len(shots),
        "provider_rejected_shot_count": rejected,
        "dialogue_count": len(sentences) if isinstance(sentences, list) else 0,
    }


def _music_memory_payload(
    root: Path,
    tab: str,
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    memory = _read_memory_document(root / "music_memory.json")
    if tab == "structure":
        return {
            "schema_version": memory.get("schema_version"),
            "source_duration_sec": memory.get("source_duration_sec"),
            "tempo_bpm": memory.get("tempo_bpm"),
            "energy_step_sec": memory.get("energy_step_sec"),
            "beats_sec": _page(memory.get("beats_sec", []), limit=limit, offset=offset),
            "accents_sec": _page(
                memory.get("accents_sec", []), limit=limit, offset=offset
            ),
            "energy_curve": _page(
                memory.get("energy_curve", []), limit=limit, offset=offset
            ),
            "sections": _page(memory.get("sections", []), limit=limit, offset=offset),
        }
    beats = memory.get("beats_sec", [])
    accents = memory.get("accents_sec", [])
    sections = memory.get("sections", [])
    return {
        "schema_version": memory.get("schema_version"),
        "source_duration_sec": memory.get("source_duration_sec"),
        "tempo_bpm": memory.get("tempo_bpm"),
        "beat_count": len(beats) if isinstance(beats, list) else 0,
        "accent_count": len(accents) if isinstance(accents, list) else 0,
        "section_count": len(sections) if isinstance(sections, list) else 0,
        "energy_step_sec": memory.get("energy_step_sec"),
    }


def _optional_memory_document(path: Path) -> dict[str, Any] | None:
    try:
        return _read_memory_document(path)
    except MaterialMemoryUnavailableError:
        return None


def _projection_documents(
    root: Path,
    kind: MaterialType,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if kind is MaterialType.MUSIC:
        return _optional_memory_document(root / "music_memory.json"), None
    return (
        _optional_memory_document(root / "video_description.json"),
        _optional_memory_document(root / "dialogues.json"),
    )


def _material_duration(
    root: Path,
    source_path: Path,
    kind: MaterialType,
    memory: Mapping[str, Any] | None = None,
) -> float:
    try:
        if kind is MaterialType.VIDEO:
            value = (
                memory
                if memory is not None
                else _read_memory_document(root / "video_description.json")
            )
            source = value.get("source", {})
            if isinstance(source, Mapping):
                duration = float(source.get("duration_sec", 0.0))
                if duration > 0:
                    return duration
        else:
            value = (
                memory
                if memory is not None
                else _read_memory_document(root / "music_memory.json")
            )
            duration = float(value.get("source_duration_sec", 0.0))
            if duration > 0:
                return duration
    except (MaterialMemoryUnavailableError, TypeError, ValueError):
        pass

    from cutmaster.infrastructure.media.ffprobe import media_duration

    duration = float(media_duration(source_path))
    if duration <= 0:
        raise ValueError("Material source has no positive duration")
    return duration


def _material_source_metadata(
    root: Path,
    source_path: Path,
    material: MaterialView,
    memory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    suffix = source_path.suffix.lower()
    filename = (
        material.name
        if suffix and material.name.casefold().endswith(suffix.casefold())
        else f"{material.name}{suffix}"
    )
    metadata: dict[str, Any] = {
        "filename": filename,
        "size_bytes": source_path.stat().st_size,
        "media_type": mimetypes.guess_type(source_path.name)[0]
        or "application/octet-stream",
    }
    if material.material_type is not MaterialType.VIDEO:
        return metadata
    try:
        description = (
            memory
            if memory is not None
            else _read_memory_document(root / "video_description.json")
        )
    except MaterialMemoryUnavailableError:
        return metadata
    source = _public_video_source(description.get("source"))
    for source_key, public_key in (
        ("width", "width"),
        ("height", "height"),
        ("fps", "frame_rate"),
    ):
        if source_key in source:
            metadata[public_key] = source[source_key]
    return metadata


def _material_memory_summary(
    root: Path,
    kind: MaterialType,
    memory: Mapping[str, Any] | None = None,
    secondary_memory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if kind is MaterialType.MUSIC:
            document = (
                memory
                if memory is not None
                else _read_memory_document(root / "music_memory.json")
            )
            return {
                "tempo_bpm": document.get("tempo_bpm"),
                "beat_count": _list_length(document.get("beats_sec")),
                "accent_count": _list_length(document.get("accents_sec")),
                "section_count": _list_length(document.get("sections")),
            }

        description = (
            memory
            if memory is not None
            else _read_memory_document(root / "video_description.json")
        )
        dialogue = (
            secondary_memory
            if secondary_memory is not None
            else _read_memory_document(root / "dialogues.json")
        )
        segments = description.get("segments", [])
        if not isinstance(segments, list):
            segments = []
        shots = [
            shot
            for segment in segments
            if isinstance(segment, Mapping)
            for shot in segment.get("shots", [])
            if isinstance(shot, Mapping)
        ]
        sentences = dialogue.get("sentences", [])
        return {
            "segment_count": len(segments),
            "shot_count": len(shots),
            "dialogue_count": len(sentences) if isinstance(sentences, list) else 0,
        }
    except (MaterialMemoryUnavailableError, TypeError):
        return {}


def _list_length(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


__all__ = ["MaterialView", "MaterialsService"]
