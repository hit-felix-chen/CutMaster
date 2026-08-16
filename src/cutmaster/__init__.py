"""CutMaster public API.

Public classes are loaded on first access so importing the package does not
initialize media libraries, model clients, or workflow stages.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cutmaster.workflow.analyser import Analyser
    from cutmaster.application import CutMasterApplication
    from cutmaster.workflow.planners import Planners
    from cutmaster.workflow.renderer import Renderer

__version__ = "0.1.0"

_PUBLIC_EXPORTS = {
    "Analyser": ("cutmaster.workflow.analyser", "Analyser"),
    "CutMasterApplication": ("cutmaster.application", "CutMasterApplication"),
    "Planners": ("cutmaster.workflow.planners", "Planners"),
    "Renderer": ("cutmaster.workflow.renderer", "Renderer"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _PUBLIC_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_PUBLIC_EXPORTS})


__all__ = [
    "Analyser",
    "CutMasterApplication",
    "Planners",
    "Renderer",
]
