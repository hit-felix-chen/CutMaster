"""Secret-free Settings and storage read models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cutmaster.application.settings.providers import (
    ProviderCapability,
    ProviderProfile,
)


@dataclass(frozen=True)
class SecretConfigurationView:
    llm_configured: bool
    vlm_configured: bool
    asr_configured: bool


@dataclass(frozen=True)
class CredentialStatusView:
    configured: bool
    suffix: str | None
    source: str
    writable: bool


@dataclass(frozen=True)
class ProviderSettingsView:
    profile: ProviderProfile
    providers: Mapping[str, Any]
    presets: Mapping[str, Any]
    credentials: Mapping[ProviderCapability, CredentialStatusView]


@dataclass(frozen=True)
class SettingsView:
    values: Mapping[str, Any]
    base_path: Path
    overlay_path: Path
    data_root: Path
    secrets: SecretConfigurationView
    connections: ProviderSettingsView


@dataclass(frozen=True)
class SavedSettingsView:
    settings: SettingsView
    restart_required: bool


@dataclass(frozen=True)
class SavedProviderSettingsView:
    settings: SettingsView
    restart_required: bool
    credential_results: Mapping[ProviderCapability, str]


@dataclass(frozen=True)
class ProviderConnectionView:
    capability: ProviderCapability
    status: str
    latency_ms: float


@dataclass(frozen=True)
class StorageCategoryView:
    name: str
    file_count: int
    size_bytes: int


@dataclass(frozen=True)
class StorageReportView:
    data_root: Path
    categories: tuple[StorageCategoryView, ...]
    direct_bundle_count: int
    total_size_bytes: int
    reveal_supported: bool


@dataclass(frozen=True)
class StorageRevealView:
    opened: bool


def frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType(
                {str(key): freeze(child) for key, child in item.items()}
            )
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    result = freeze(value)
    if not isinstance(result, Mapping):
        raise TypeError("Expected a mapping")
    return result


__all__ = [
    "CredentialStatusView",
    "ProviderConnectionView",
    "ProviderSettingsView",
    "SavedProviderSettingsView",
    "SavedSettingsView",
    "SecretConfigurationView",
    "SettingsView",
    "StorageCategoryView",
    "StorageReportView",
    "StorageRevealView",
    "frozen_mapping",
]
