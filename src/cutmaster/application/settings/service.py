"""Effective Configuration editing and local storage reporting."""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cutmaster.application.settings.commands import SaveSettingsCommand
from cutmaster.application.settings.views import (
    SavedSettingsView,
    SecretConfigurationView,
    SettingsView,
    StorageCategoryView,
    StorageReportView,
    frozen_mapping,
)
from cutmaster.configuration.effective import (
    EffectiveConfiguration,
    load_effective_configuration,
    sibling_overlay_path,
)
from cutmaster.infrastructure.persistence.sqlite import SQLiteApplicationStore


class SettingsService:
    """Expose the current immutable Effective Configuration to adapters."""

    __slots__ = ("_effective_configuration", "_store_instance")

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        store: SQLiteApplicationStore | None = None,
    ) -> None:
        self._effective_configuration = effective_configuration
        self._store_instance = store

    @property
    def _store(self) -> SQLiteApplicationStore:
        store = self._store_instance
        if store is None:
            store = SQLiteApplicationStore(self._effective_configuration.data_root)
            self._store_instance = store
        return store

    @property
    def effective_configuration(self) -> EffectiveConfiguration:
        """Return the secret-free configuration loaded for this Application."""

        return self._effective_configuration

    def get(self) -> SettingsView:
        return _settings_view(self._effective_configuration)

    def reload(self) -> SettingsView:
        """Reload the exact selected base and sibling overlay from disk."""

        self._effective_configuration = load_effective_configuration(
            self._effective_configuration.sources.base_path
        )
        return self.get()

    def save(self, command: SaveSettingsCommand) -> SavedSettingsView:
        overlay = _copy_json_mapping(command.overlay)
        content = _toml_document(overlay)
        overlay_path = self._effective_configuration.sources.overlay_path
        lock_path = overlay_path.with_name(f".{overlay_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            receipt_exists = self._store.command_receipt_exists(
                command.command_id,
                "settings.save",
                {"overlay": overlay},
            )
            previous = _read_existing_regular_file(overlay_path)
            wrote_candidate = False

            def apply() -> Mapping[str, Any]:
                nonlocal wrote_candidate
                _validate_candidate_overlay(
                    self._effective_configuration.sources.base_path,
                    content,
                )
                _atomic_write(overlay_path, content)
                wrote_candidate = True
                loaded = load_effective_configuration(
                    self._effective_configuration.sources.base_path
                )
                return {
                    "effective_configuration": loaded.to_dict(),
                    "overlay": overlay,
                }

            try:
                result = self._store.execute_external_command(
                    command.command_id,
                    "settings.save",
                    {"overlay": overlay},
                    apply,
                )
            except Exception:
                if wrote_candidate:
                    _restore_file(overlay_path, previous)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        if receipt_exists:
            expected_overlay = result.value.get("overlay")
            if expected_overlay != overlay:
                raise RuntimeError("Settings receipt does not match its canonical overlay")
            current = _read_existing_regular_file(overlay_path)
            if current is None or current.decode("utf-8") != content:
                raise RuntimeError(
                    "Settings overlay no longer matches the completed command receipt"
                )

        # Reloading verifies that the selected source files remain complete.
        self._effective_configuration = load_effective_configuration(
            self._effective_configuration.sources.base_path
        )
        return SavedSettingsView(settings=self.get(), restart_required=True)

    def storage_report(self) -> StorageReportView:
        root = self._effective_configuration.data_root
        categories: list[StorageCategoryView] = []
        known_paths = {
            "database": (root / "cutmaster.db",),
            "materials": (root / "media",),
            "projects": (root / "projects",),
            "direct": (root / "direct",),
            "logs": (root / "logs",),
        }
        for name, paths in known_paths.items():
            file_count = 0
            size_bytes = 0
            for path in paths:
                count, size = _path_usage(path)
                file_count += count
                size_bytes += size
                if name == "database":
                    for suffix in ("-wal", "-shm"):
                        extra_count, extra_size = _path_usage(Path(f"{path}{suffix}"))
                        file_count += extra_count
                        size_bytes += extra_size
            categories.append(
                StorageCategoryView(
                    name=name,
                    file_count=file_count,
                    size_bytes=size_bytes,
                )
            )
        direct_root = root / "direct"
        direct_bundle_count = 0
        if direct_root.is_dir() and not direct_root.is_symlink():
            direct_bundle_count = sum(
                1
                for item in direct_root.iterdir()
                if item.is_dir()
                and not item.is_symlink()
                and item.name.startswith("bundle_")
            )
        return StorageReportView(
            data_root=root,
            categories=tuple(categories),
            direct_bundle_count=direct_bundle_count,
            total_size_bytes=sum(item.size_bytes for item in categories),
        )


def _settings_view(configuration: EffectiveConfiguration) -> SettingsView:
    references = configuration.secret_references

    def configured(name: str | None) -> bool:
        return bool(name and os.getenv(name, "").strip())

    return SettingsView(
        values=frozen_mapping(configuration.to_dict()),
        base_path=configuration.sources.base_path,
        overlay_path=configuration.sources.overlay_path,
        data_root=configuration.data_root,
        secrets=SecretConfigurationView(
            llm_configured=configured(references.llm_api_key_env),
            vlm_configured=configured(references.vlm_api_key_env),
            asr_configured=configured(references.asr_api_key_env),
        ),
    )


def _copy_json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("Settings overlay must be a mapping")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        result = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("Settings overlay must be JSON-compatible") from exc
    if not isinstance(result, dict):
        raise TypeError("Settings overlay must be an object")
    return result


def _toml_key(value: str) -> str:
    if value and all(character.isalnum() or character in "_-" for character in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Settings values must be finite")
        return repr(value)
    if isinstance(value, list):
        if any(isinstance(item, Mapping) for item in value):
            raise TypeError("Settings arrays cannot contain tables")
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise TypeError(f"Unsupported Settings value type: {type(value).__name__}")


def _toml_document(value: Mapping[str, Any]) -> str:
    lines: list[str] = []

    def write_table(path: tuple[str, ...], table: Mapping[str, Any]) -> None:
        scalars = {key: item for key, item in table.items() if not isinstance(item, Mapping)}
        children = {key: item for key, item in table.items() if isinstance(item, Mapping)}
        if path and (scalars or not children):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_toml_key(item) for item in path) + "]")
        for key in sorted(scalars):
            lines.append(f"{_toml_key(str(key))} = {_toml_scalar(scalars[key])}")
        for key in sorted(children):
            write_table((*path, str(key)), children[key])

    top_scalars = {key: item for key, item in value.items() if not isinstance(item, Mapping)}
    if top_scalars:
        raise ValueError("Settings overlay top-level values must be TOML tables")
    for key in sorted(value):
        child = value[key]
        if not isinstance(child, Mapping):
            raise AssertionError("Top-level scalar validation failed")
        write_table((str(key),), child)
    return "\n".join(lines).lstrip() + ("\n" if lines else "")


def _validate_candidate_overlay(base_path: Path, content: str) -> None:
    descriptor, temporary_base_name = tempfile.mkstemp(
        dir=base_path.parent,
        prefix=f".{base_path.stem}.settings-validation.",
        suffix=".toml",
    )
    temporary_base = Path(temporary_base_name)
    temporary_overlay = sibling_overlay_path(temporary_base)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(base_path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_write(temporary_overlay, content)
        load_effective_configuration(temporary_base)
    finally:
        temporary_overlay.unlink(missing_ok=True)
        temporary_base.unlink(missing_ok=True)


def _read_existing_regular_file(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Settings overlay must be a regular file: {path}")
    return path.read_bytes()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.restore.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _path_usage(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0, 0
    if stat.S_ISLNK(metadata.st_mode):
        return 0, 0
    if stat.S_ISREG(metadata.st_mode):
        return 1, metadata.st_size
    if not stat.S_ISDIR(metadata.st_mode):
        return 0, 0
    count = 0
    size = 0
    for child in path.iterdir():
        child_count, child_size = _path_usage(child)
        count += child_count
        size += child_size
    return count, size


__all__ = ["SettingsService"]
