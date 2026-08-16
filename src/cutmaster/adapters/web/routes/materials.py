"""Read-only Material Library HTTP routes.

Import, analysis submission, retry, and deletion are intentionally absent until
their durable Web command boundaries are complete.
"""

from __future__ import annotations

from enum import StrEnum
import mimetypes
import re
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from cutmaster.adapters.web.dependencies import ApplicationDependency
from cutmaster.adapters.web.presenters import (
    material_detail_view,
    material_memory_view,
)
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialType


router = APIRouter(prefix="/materials", tags=["materials"])


class _LeasedFileResponse(FileResponse):
    """Release a Material lease on success, Range errors, or disconnects."""

    def __init__(self, *args, release: Callable[[], None], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._release = release

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


class MaterialSort(StrEnum):
    NAME_ASC = "name_asc"
    NAME_DESC = "name_desc"
    TYPE_NAME = "type_name"


@router.get("")
def list_materials(
    application: ApplicationDependency,
    material_type: MaterialType | None = Query(default=None, alias="type"),
    search: str = Query(default="", max_length=200),
    sort: MaterialSort = MaterialSort.NAME_ASC,
) -> dict[str, object]:
    materials = application.materials.list(
        material_type,
        search=search,
        sort=sort.value,
    )
    items = []
    for material in materials:
        detail = application.materials.detail(material.material_id)
        if detail is not None:
            items.append(material_detail_view(detail))
    return {
        "items": items,
        "total": len(items),
        "type": None if material_type is None else material_type.value,
        "search": search,
        "sort": sort.value,
    }


@router.get("/{material_id}")
def get_material(
    material_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    identifier = MaterialId.parse(material_id)
    value = application.materials.detail(identifier)
    if value is None:
        raise HTTPException(status_code=404, detail="Material not found")
    attempts = application.jobs.activity(
        owner_type="material",
        owner_id=str(identifier),
        limit=50,
    )
    return material_detail_view(value, attempts=attempts)


@router.get("/{material_id}/memory/{tab}")
def get_material_memory(
    material_id: str,
    tab: str,
    application: ApplicationDependency,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    identifier = MaterialId.parse(material_id)
    if application.materials.get(identifier) is None:
        raise HTTPException(status_code=404, detail="Material not found")
    return material_memory_view(
        application.materials.memory(
            identifier,
            tab,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/{material_id}/source")
def get_material_source(
    material_id: str,
    application: ApplicationDependency,
) -> FileResponse:
    """Stream managed source bytes while retaining deletion exclusion."""

    identifier = MaterialId.parse(material_id)
    material = application.materials.get(identifier)
    if material is None:
        raise HTTPException(status_code=404, detail="Material not found")
    lease = application.materials.read_lease(identifier)
    entered = False
    try:
        binding = lease.__enter__()
        entered = True
        path = binding.source_path
        metadata = path.stat()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = _inline_filename(material.name, path.suffix)
    except Exception:
        if entered:
            lease.__exit__(None, None, None)
        raise

    return _LeasedFileResponse(
        path,
        release=lambda: lease.__exit__(None, None, None),
        stat_result=metadata,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


_UNSAFE_FILENAME = re.compile(r"[^\w .()\-]+", flags=re.UNICODE)


def _inline_filename(name: str, suffix: str) -> str:
    safe_name = _UNSAFE_FILENAME.sub("_", name).strip(" .")[:120]
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:16]
    if not safe_suffix.startswith("."):
        safe_suffix = ""
    return f"{safe_name or 'material'}{safe_suffix}"


__all__ = ["router"]
