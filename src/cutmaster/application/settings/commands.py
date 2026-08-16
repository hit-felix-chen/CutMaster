"""Transport-neutral settings commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from cutmaster.application.settings.providers import (
    ProviderCapability,
    ProviderProfile,
)


@dataclass(frozen=True)
class SaveSettingsCommand:
    command_id: str
    overlay: Mapping[str, Any]


@dataclass(frozen=True)
class CredentialUpdate:
    """One explicit secret mutation; values are never placed in durable receipts."""

    action: Literal["keep", "set", "clear"]
    value: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SaveProviderSettingsCommand:
    command_id: str
    profile: ProviderProfile
    providers: Mapping[str, Any] | None
    credentials: Mapping[ProviderCapability, CredentialUpdate]


@dataclass(frozen=True)
class ProbeProviderConnectionCommand:
    capability: ProviderCapability
    configuration: Mapping[str, Any]
    api_key: str | None = field(default=None, repr=False, compare=False)


__all__ = [
    "CredentialUpdate",
    "ProbeProviderConnectionCommand",
    "SaveProviderSettingsCommand",
    "SaveSettingsCommand",
]
