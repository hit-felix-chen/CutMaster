"""Lightweight managed-execution projections shared by Web routes."""

from __future__ import annotations

from cutmaster.adapters.web.presenters import execution_view
from cutmaster.application import CutMasterApplication
from cutmaster.domain.ids import MaterialId, RenderVariantId, RunId


def latest_run_execution(
    application: CutMasterApplication,
    run_id: RunId,
) -> dict[str, object] | None:
    """Return the latest Run Attempt and its Job without touching Materials."""

    attempts = application.jobs.activity(
        owner_type="run",
        owner_id=str(run_id),
        limit=500,
    )
    if not attempts:
        return None
    attempt = max(attempts, key=lambda item: item.sequence)
    job = application.jobs.get_job_for_attempt(attempt.attempt_id)
    return execution_view(attempt, job)


def latest_material_execution(
    application: CutMasterApplication,
    material_id: MaterialId,
) -> dict[str, object] | None:
    """Return the latest Material Analysis Attempt and its Job."""

    attempts = application.jobs.activity(
        owner_type="material",
        owner_id=str(material_id),
        limit=500,
    )
    if not attempts:
        return None
    attempt = max(attempts, key=lambda item: item.sequence)
    job = application.jobs.get_job_for_attempt(attempt.attempt_id)
    return execution_view(attempt, job)


def latest_render_execution(
    application: CutMasterApplication,
    render_variant_id: RenderVariantId,
) -> dict[str, object] | None:
    """Return the latest Renderer Attempt and Job."""

    attempts = application.jobs.activity(
        owner_type="render_variant",
        owner_id=str(render_variant_id),
        limit=500,
    )
    if not attempts:
        return None
    attempt = max(attempts, key=lambda item: item.sequence)
    job = application.jobs.get_job_for_attempt(attempt.attempt_id)
    return execution_view(attempt, job)


__all__ = [
    "latest_material_execution",
    "latest_render_execution",
    "latest_run_execution",
]
