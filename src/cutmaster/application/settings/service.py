"""Effective Configuration editing and local storage reporting."""

from __future__ import annotations

import fcntl
import hmac
import io
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from cutmaster.application.ports.data_root import (
    DataRootCoordinator,
    root_shared_operation,
)
from cutmaster.application.settings.data_root_migration import (
    DataRootMigrationService,
)
from cutmaster.application.errors import (
    ProviderConnectionFailedError,
    StorageRevealFailedError,
    StorageRevealUnavailableError,
)
from cutmaster.application.settings.commands import (
    ProbeProviderConnectionCommand,
    SaveProviderSettingsCommand,
    SaveSettingsCommand,
)
from cutmaster.application.settings.provider_connections import (
    ProviderConnectionFailure,
    ProviderConnectionTester,
    ProviderProbe,
    test_provider_connection,
)
from cutmaster.application.settings.providers import (
    ProviderCapability,
    canonical_providers,
    derive_profile,
    provider_presets,
    provider_projection,
    validate_custom_providers,
)
from cutmaster.application.settings.views import (
    CredentialStatusView,
    ProviderConnectionView,
    ProviderSettingsView,
    SavedProviderSettingsView,
    SavedSettingsView,
    SecretConfigurationView,
    SettingsView,
    StorageCategoryView,
    StorageReportView,
    StorageRevealView,
    frozen_mapping,
)
from cutmaster.configuration.effective import (
    EffectiveConfiguration,
    load_effective_configuration,
    sibling_overlay_path,
)
from cutmaster.infrastructure.persistence.sqlite import (
    IdempotencyConflict,
    SQLiteApplicationStore,
)


class SettingsService:
    """Expose the current immutable Effective Configuration to adapters."""

    __slots__ = (
        "_connection_tester",
        "_data_root_coordinator",
        "_effective_configuration",
        "_migration_service",
        "_process_environment_names",
        "_reveal_supported",
        "_storage_revealer",
        "_store_instance",
    )

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        store: SQLiteApplicationStore | None = None,
        *,
        process_environment_names: frozenset[str] | None = None,
        connection_tester: ProviderConnectionTester | None = None,
        storage_revealer: Callable[[Path], None] | None = None,
        reveal_supported: bool | None = None,
        data_root_coordinator: DataRootCoordinator | None = None,
    ) -> None:
        self._effective_configuration = effective_configuration
        self._store_instance = store
        self._data_root_coordinator = data_root_coordinator
        self._migration_service: DataRootMigrationService | None = None
        self._process_environment_names = (
            frozenset()
            if process_environment_names is None
            else process_environment_names
        )
        self._connection_tester = connection_tester or test_provider_connection
        self._storage_revealer = storage_revealer or _reveal_in_file_manager
        self._reveal_supported = (
            sys.platform == "darwin" if reveal_supported is None else reveal_supported
        )

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

    @property
    def migrations(self) -> DataRootMigrationService:
        coordinator = self._data_root_coordinator
        if coordinator is None:
            raise RuntimeError("Data Root Migration requires Application composition")
        service = self._migration_service
        if service is None:
            service = DataRootMigrationService(
                self._effective_configuration,
                coordinator,
            )
            self._migration_service = service
        return service

    def get(self) -> SettingsView:
        return _settings_view(
            self._effective_configuration,
            self._process_environment_names,
        )

    @root_shared_operation
    def reload(self) -> SettingsView:
        """Reload the exact selected base and sibling overlay from disk."""

        self._effective_configuration = load_effective_configuration(
            self._effective_configuration.sources.base_path
        )
        return self.get()

    @root_shared_operation
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
                raise RuntimeError(
                    "Settings receipt does not match its canonical overlay"
                )
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

    @root_shared_operation
    def save_providers(
        self,
        command: SaveProviderSettingsCommand,
    ) -> SavedProviderSettingsView:
        """Atomically save public provider settings and sibling dotenv updates."""

        if command.profile == "custom":
            if command.providers is None:
                raise ValueError("Custom provider settings require all providers")
            providers = validate_custom_providers(command.providers)
        else:
            if command.providers is not None:
                raise ValueError("Preset provider settings cannot include providers")
            providers = canonical_providers(command.profile)
        if set(command.credentials) != {"llm", "vlm", "asr"}:
            raise ValueError("Credential updates must contain llm, vlm, and asr")

        overlay_path = self._effective_configuration.sources.overlay_path
        dotenv_path = self._effective_configuration.sources.dotenv_path
        previous_overlay = _read_existing_regular_file(overlay_path)
        current_overlay = _decode_overlay(previous_overlay)
        next_overlay = _provider_overlay(current_overlay, providers)
        overlay_content = _toml_document(next_overlay)
        _validate_candidate_overlay(
            self._effective_configuration.sources.base_path,
            overlay_content,
        )

        previous_dotenv = _read_existing_regular_file(dotenv_path)
        dotenv_updates, credential_results = _credential_file_updates(
            providers,
            command.credentials,
            self._process_environment_names,
        )
        dotenv_content = _updated_dotenv(previous_dotenv, dotenv_updates)
        dotenv_changed = bool(dotenv_updates) and dotenv_content.encode("utf-8") != (
            previous_dotenv or b""
        )
        if (
            not dotenv_changed
            and dotenv_updates
            and previous_dotenv is not None
            and stat.S_IMODE(dotenv_path.stat().st_mode) != 0o600
        ):
            dotenv_changed = True

        lock_path = overlay_path.with_name(f".{overlay_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_request = _provider_receipt_request(command, providers)
        wrote_dotenv = False
        wrote_overlay = False
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            receipt_exists = self._store.command_receipt_exists(
                command.command_id,
                "settings.providers.save",
                receipt_request,
            )

            def apply() -> Mapping[str, Any]:
                nonlocal wrote_dotenv, wrote_overlay
                # Re-read after acquiring the lock so concurrent saves cannot be lost.
                locked_overlay = _read_existing_regular_file(overlay_path)
                locked_dotenv = _read_existing_regular_file(dotenv_path)
                if (
                    locked_overlay != previous_overlay
                    or locked_dotenv != previous_dotenv
                ):
                    raise RuntimeError(
                        "Settings changed while this save was being prepared"
                    )
                if dotenv_changed:
                    _atomic_write(dotenv_path, dotenv_content)
                    wrote_dotenv = True
                _atomic_write(overlay_path, overlay_content)
                wrote_overlay = True
                loaded = load_effective_configuration(
                    self._effective_configuration.sources.base_path
                )
                return {
                    "effective_configuration": loaded.to_dict(),
                    "overlay": next_overlay,
                    "credential_results": credential_results,
                }

            try:
                result = self._store.execute_external_command(
                    command.command_id,
                    "settings.providers.save",
                    receipt_request,
                    apply,
                )
            except Exception:
                if wrote_overlay:
                    _restore_file(overlay_path, previous_overlay)
                if wrote_dotenv:
                    _restore_file(dotenv_path, previous_dotenv)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

        if receipt_exists:
            current_overlay = _read_existing_regular_file(overlay_path)
            if (
                current_overlay is None
                or current_overlay.decode("utf-8") != overlay_content
            ):
                raise RuntimeError(
                    "Provider Settings overlay no longer matches the completed command receipt"
                )
            _verify_credential_replay(
                command,
                providers,
                result.value.get("credential_results"),
                _dotenv_mapping(_read_existing_regular_file(dotenv_path)),
                self._process_environment_names,
            )

        self._effective_configuration = load_effective_configuration(
            self._effective_configuration.sources.base_path
        )
        result_credentials = result.value.get("credential_results")
        if not isinstance(result_credentials, Mapping):
            raise TypeError("Provider Settings receipt has invalid credentials")
        return SavedProviderSettingsView(
            settings=self.get(),
            restart_required=True,
            credential_results=frozen_mapping(result_credentials),
        )

    def test_provider(
        self,
        command: ProbeProviderConnectionCommand,
    ) -> ProviderConnectionView:
        """Probe a proposed provider without persisting its configuration or secret."""

        candidate = canonical_providers("cost_saving")
        candidate[command.capability] = _copy_json_mapping(command.configuration)
        normalized = validate_custom_providers(candidate)[command.capability]
        secret = command.api_key
        if secret is not None:
            secret = _validated_secret(secret)
        else:
            reference = str(normalized["api_key_env"])
            secret = self._credential_value(reference)
            if not secret:
                raise ValueError(
                    f"{command.capability.upper()} API key is not configured"
                )
        try:
            latency_ms = self._connection_tester(
                ProviderProbe(command.capability, normalized, secret)
            )
        except ProviderConnectionFailure as exc:
            raise ProviderConnectionFailedError(
                f"{command.capability.upper()} connection test failed"
            ) from exc
        return ProviderConnectionView(
            capability=command.capability,
            status="connected",
            latency_ms=round(latency_ms, 1),
        )

    def _credential_value(self, reference: str) -> str | None:
        if reference in self._process_environment_names:
            value = os.environ.get(reference)
            return value if value and value.strip() else None
        values = _dotenv_mapping(
            _read_existing_regular_file(
                self._effective_configuration.sources.dotenv_path
            )
        )
        value = values.get(reference)
        return value if value and value.strip() else None

    @root_shared_operation
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
            reveal_supported=self._reveal_supported,
        )

    @root_shared_operation
    def reveal_data_root(self, command_id: str) -> StorageRevealView:
        """Open the one fixed Application Data Root in the host file manager."""

        if not self._reveal_supported:
            raise StorageRevealUnavailableError(
                "Opening the Application Data Root is unavailable on this host"
            )
        root = self._effective_configuration.data_root

        def apply() -> Mapping[str, Any]:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.is_symlink() or not root.is_dir():
                raise StorageRevealFailedError(
                    "The Application Data Root is not a safe directory"
                )
            try:
                self._storage_revealer(root)
            except (OSError, subprocess.SubprocessError) as exc:
                raise StorageRevealFailedError(
                    "The system file manager could not open the Application Data Root"
                ) from exc
            return {"opened": True}

        result = self._store.execute_external_command(
            command_id,
            "settings.storage.reveal",
            {"target": "data_root"},
            apply,
        )
        return StorageRevealView(opened=result.value.get("opened") is True)


def _settings_view(
    configuration: EffectiveConfiguration,
    process_environment_names: frozenset[str],
) -> SettingsView:
    references = configuration.secret_references
    dotenv = _dotenv_mapping(
        _read_existing_regular_file(configuration.sources.dotenv_path)
    )

    provider_values = provider_projection(configuration.values)
    profile = derive_profile(provider_values)

    reference_by_capability = {
        "llm": references.llm_api_key_env,
        "vlm": references.vlm_api_key_env,
        "asr": references.asr_api_key_env,
    }

    def credential_status(name: str | None) -> CredentialStatusView:
        if not name:
            return CredentialStatusView(False, None, "none", True)
        if name in process_environment_names:
            raw = os.environ.get(name)
            configured = bool(raw and raw.strip())
            return CredentialStatusView(
                configured=configured,
                suffix=_secret_suffix(raw) if configured else None,
                source="process",
                writable=False,
            )
        raw = dotenv.get(name)
        configured = bool(raw and raw.strip())
        return CredentialStatusView(
            configured=configured,
            suffix=_secret_suffix(raw) if configured else None,
            source="dotenv" if configured else "none",
            writable=True,
        )

    credentials = {
        capability: credential_status(reference)
        for capability, reference in reference_by_capability.items()
    }

    return SettingsView(
        values=frozen_mapping(configuration.to_dict()),
        base_path=configuration.sources.base_path,
        overlay_path=configuration.sources.overlay_path,
        data_root=configuration.data_root,
        secrets=SecretConfigurationView(
            llm_configured=credentials["llm"].configured,
            vlm_configured=credentials["vlm"].configured,
            asr_configured=credentials["asr"].configured,
        ),
        connections=ProviderSettingsView(
            profile=profile,
            providers=frozen_mapping(provider_values),
            presets=frozen_mapping(provider_presets()),
            credentials=frozen_mapping(credentials),
        ),
    )


def _secret_suffix(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()[-4:]


def _decode_overlay(value: bytes | None) -> dict[str, Any]:
    if value is None:
        return {}
    decoded = tomllib.loads(value.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise TypeError("Settings overlay must be a TOML object")
    return decoded


def _provider_overlay(
    current: Mapping[str, Any],
    providers: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    overlay = _copy_json_mapping(current)
    overlay["llm"] = _copy_json_mapping(providers["llm"])
    overlay["vlm"] = _copy_json_mapping(providers["vlm"])
    analyser = overlay.get("analyser", {})
    if not isinstance(analyser, Mapping):
        raise TypeError("analyser Settings overlay must be a table")
    analyser_overlay = _copy_json_mapping(analyser)
    analyser_overlay["asr"] = _copy_json_mapping(providers["asr"])
    overlay["analyser"] = analyser_overlay
    return overlay


def _validated_secret(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("API key must be a string")
    if not value or not value.strip():
        raise ValueError("API key must not be empty")
    if len(value) > 4096:
        raise ValueError("API key is too long")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError("API key must not contain line breaks or NUL")
    return value


def _credential_file_updates(
    providers: Mapping[str, Mapping[str, Any]],
    credentials: Mapping[ProviderCapability, Any],
    process_environment_names: frozenset[str],
) -> tuple[dict[str, str | None], dict[str, str]]:
    references = {
        capability: str(providers[capability]["api_key_env"])
        for capability in ("llm", "vlm", "asr")
    }
    results: dict[str, str] = {}
    requested: dict[str, list[tuple[str, str | None]]] = {}
    for capability in ("llm", "vlm", "asr"):
        update = credentials[capability]
        if update.action not in {"keep", "set", "clear"}:
            raise ValueError(f"Invalid {capability} credential action")
        reference = references[capability]
        if update.action == "keep":
            if update.value is not None:
                raise ValueError("keep cannot include an API key")
            results[capability] = "kept"
            continue
        if reference in process_environment_names:
            results[capability] = "process_locked"
            continue
        if update.action == "set":
            if update.value is None:
                raise ValueError("set requires an API key")
            value = _validated_secret(update.value)
        else:
            if update.value is not None:
                raise ValueError("clear cannot include an API key")
            value = None
        requested.setdefault(reference, []).append((capability, value))
        results[capability] = "set" if value is not None else "cleared"

    updates: dict[str, str | None] = {}
    for reference, values in requested.items():
        distinct = {value for _, value in values}
        if len(distinct) != 1:
            raise ValueError(f"Conflicting credential updates target {reference}")
        value = next(iter(distinct))
        if value is None:
            sharing = {
                capability
                for capability, target in references.items()
                if target == reference
            }
            clearing = {capability for capability, _ in values}
            if sharing != clearing:
                raise ValueError(f"Clear every provider using {reference} in one save")
        updates[reference] = value
    return updates, results


def _dotenv_mapping(value: bytes | None) -> dict[str, str | None]:
    if value is None:
        return {}
    parsed = dotenv_values(
        stream=io.StringIO(value.decode("utf-8")),
        interpolate=False,
    )
    return {str(key): item for key, item in parsed.items()}


def _updated_dotenv(
    previous: bytes | None,
    updates: Mapping[str, str | None],
) -> str:
    content = "" if previous is None else previous.decode("utf-8")
    lines = content.splitlines(keepends=True)
    for name, value in updates.items():
        pattern = re.compile(
            rf"^\s*(?:export\s+)?{re.escape(name)}\s*=",
        )
        replacement = None if value is None else f"{name}={json.dumps(value)}\n"
        rewritten: list[str] = []
        replaced = False
        for line in lines:
            if pattern.match(line):
                if replacement is not None and not replaced:
                    rewritten.append(replacement)
                    replaced = True
                continue
            rewritten.append(line)
        if replacement is not None and not replaced:
            if rewritten and not rewritten[-1].endswith(("\n", "\r")):
                rewritten[-1] += "\n"
            rewritten.append(replacement)
        lines = rewritten
    return "".join(lines)


def _provider_receipt_request(
    command: SaveProviderSettingsCommand,
    providers: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    credential_request: dict[str, dict[str, str]] = {}
    for capability in ("llm", "vlm", "asr"):
        update = command.credentials[capability]
        item = {"action": update.action}
        credential_request[capability] = item
    return {
        "profile": command.profile,
        "providers": _copy_json_mapping(providers),
        "credentials": credential_request,
    }


def _verify_credential_replay(
    command: SaveProviderSettingsCommand,
    providers: Mapping[str, Mapping[str, Any]],
    raw_results: Any,
    dotenv: Mapping[str, str | None],
    process_environment_names: frozenset[str],
) -> None:
    if not isinstance(raw_results, Mapping):
        raise TypeError("Provider Settings receipt has invalid credentials")
    for capability in ("llm", "vlm", "asr"):
        update = command.credentials[capability]
        reference = str(providers[capability]["api_key_env"])
        result = raw_results.get(capability)
        if reference in process_environment_names:
            if update.action != "keep" and result != "process_locked":
                raise RuntimeError("Provider Settings receipt process lock drifted")
            continue
        if update.action == "set":
            actual = dotenv.get(reference)
            expected = update.value
            matches = bool(
                actual is not None
                and expected is not None
                and hmac.compare_digest(
                    actual.encode("utf-8"),
                    expected.encode("utf-8"),
                )
            )
            if not matches or result != "set":
                raise IdempotencyConflict(command.command_id)
        elif update.action == "clear":
            if dotenv.get(reference) not in {None, ""} or result != "cleared":
                raise IdempotencyConflict(command.command_id)


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
        scalars = {
            key: item for key, item in table.items() if not isinstance(item, Mapping)
        }
        children = {
            key: item for key, item in table.items() if isinstance(item, Mapping)
        }
        if path and (scalars or not children):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_toml_key(item) for item in path) + "]")
        for key in sorted(scalars):
            lines.append(f"{_toml_key(str(key))} = {_toml_scalar(scalars[key])}")
        for key in sorted(children):
            write_table((*path, str(key)), children[key])

    top_scalars = {
        key: item for key, item in value.items() if not isinstance(item, Mapping)
    }
    if top_scalars:
        raise ValueError("Settings overlay top-level values must be TOML tables")
    for key in sorted(value):
        child = value[key]
        if not isinstance(child, Mapping):
            raise TypeError("Top-level Settings values must be TOML tables")
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
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
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
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


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


def _reveal_in_file_manager(path: Path) -> None:
    if sys.platform != "darwin":
        raise OSError("Host file-manager reveal is unsupported")
    subprocess.run(
        ["open", str(path)],
        check=True,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


__all__ = ["SettingsService"]
