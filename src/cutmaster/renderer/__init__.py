"""Deterministic rendering-stage public API."""

from cutmaster.contracts.renderer import RenderRequest, RenderResult
from cutmaster.renderer.renderer import Renderer

__all__ = ["Renderer", "RenderRequest", "RenderResult"]
