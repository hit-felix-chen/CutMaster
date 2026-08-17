"""Secret-free effective configuration for the Application composition root."""

from __future__ import annotations

import copy
import json
import math
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

_SECRET_KEYS = {"api_key"}
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CANONICAL_TOP_LEVEL_KEYS = frozenset(
    {"llm", "vlm", "analyser", "planners", "renderer"}
)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Config root must be a table: {path}")
    return value


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_secrets(item)
            for key, item in value.items()
            if str(key) not in _SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_strip_secrets(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ConfigurationSources:
    base_path: Path
    dotenv_path: Path
    data_root_pointer_path: Path

    def __post_init__(self) -> None:
        for field_name in (
            "base_path",
            "dotenv_path",
            "data_root_pointer_path",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Path):
                raise TypeError(f"{field_name} must be a pathlib.Path")
            if not value.is_absolute():
                raise ValueError(f"{field_name} must be an absolute path")


@dataclass(frozen=True)
class SecretReferences:
    """Environment-variable names used for per-Attempt secret resolution."""

    llm_api_key_env: str | None
    vlm_api_key_env: str | None
    asr_api_key_env: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "llm_api_key_env",
            "vlm_api_key_env",
            "asr_api_key_env",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str)
                or not _ENVIRONMENT_NAME_PATTERN.fullmatch(value)
            ):
                raise ValueError(
                    f"{field_name} must be a valid environment variable name"
                )


@dataclass(frozen=True)
class EffectiveConfiguration:
    """One immutable, secret-free configuration snapshot and its source metadata."""

    sources: ConfigurationSources
    data_root: Path
    secret_references: SecretReferences
    _values: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.sources, ConfigurationSources):
            raise TypeError("sources must be ConfigurationSources")
        if not isinstance(self.data_root, Path):
            raise TypeError("data_root must be a pathlib.Path")
        if not self.data_root.is_absolute():
            raise ValueError("data_root must be an absolute path")
        _validate_data_root_candidate(self.data_root)
        if not isinstance(self.secret_references, SecretReferences):
            raise TypeError("secret_references must be SecretReferences")
        if not isinstance(self._values, Mapping):
            raise TypeError("Effective Configuration values must be a mapping")
        if set(self._values) != _CANONICAL_TOP_LEVEL_KEYS:
            raise ValueError(
                "Effective Configuration must contain the canonical top-level "
                f"sections: {sorted(_CANONICAL_TOP_LEVEL_KEYS)}"
            )
        if "material_analysis" in self._values.get("analyser", {}):
            raise ValueError(
                "Effective Configuration must not contain a Material storage root"
            )
        object.__setattr__(self, "_values", _freeze(_strip_secrets(self._values)))

    @property
    def values(self) -> Mapping[str, Any]:
        return self._values

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self._values)

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _validate_data_root_candidate(candidate: Path) -> Path:
    resolved = candidate.resolve()
    if resolved == Path(resolved.anchor):
        raise ValueError("Filesystem root cannot be used as the Application Data Root")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("Application Data Root must be a directory when it exists")
    return resolved


def _resolve_data_root(base_directory: Path, pointer_path: Path) -> Path:
    try:
        metadata = pointer_path.lstat()
    except FileNotFoundError:
        return _validate_data_root_candidate(base_directory / ".cutmaster")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Data Root pointer must be a regular file: {pointer_path}")
    raw = pointer_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"Data Root pointer is empty: {pointer_path}")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(
            f"Data Root pointer contains control characters: {pointer_path}"
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError(
            f"Data Root pointer must contain an absolute path: {pointer_path}"
        )
    return _validate_data_root_candidate(candidate)


def _reject_inline_secrets(
    value: Mapping[str, Any],
    source: Path,
    prefix: tuple[str, ...] = (),
) -> None:
    for key, item in value.items():
        path = (*prefix, str(key))
        if str(key) in _SECRET_KEYS:
            raise ValueError(
                f"Inline secret {'.'.join(path)} is not allowed in {source}; "
                "use api_key_env with the sibling .env or process environment"
            )
        if isinstance(item, Mapping):
            _reject_inline_secrets(item, source, path)


def _secret_reference(
    data: Mapping[str, Any],
    *section_names: str,
) -> str | None:
    value: Any = data
    for name in section_names:
        if not isinstance(value, Mapping):
            return None
        value = value.get(name, {})
    if not isinstance(value, Mapping):
        return None
    raw = value.get("api_key_env")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(f"{'.'.join(section_names)}.api_key_env must be a string")
    normalized = raw.strip()
    return normalized or None


def _validate_provided_types(
    provided: Mapping[str, Any],
    decoded: Mapping[str, Any],
    prefix: tuple[str, ...] = (),
) -> None:
    for key, value in provided.items():
        path = (*prefix, str(key))
        dotted = ".".join(path)
        if key in {"api_key", "api_key_env"}:
            continue
        if path == ("analyser", "material_analysis", "material_library_dir"):
            if not isinstance(value, str):
                raise TypeError(f"{dotted} must be a string")
            continue
        expected = decoded[key]
        if isinstance(value, Mapping):
            if not isinstance(expected, Mapping):
                raise TypeError(f"{dotted} must not be a table")
            _validate_provided_types(value, expected, path)
            continue
        if isinstance(expected, bool):
            valid = isinstance(value, bool)
        elif isinstance(expected, int):
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(expected, float):
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        elif isinstance(expected, str):
            valid = isinstance(value, str)
        else:
            valid = isinstance(value, type(expected))
        if not valid:
            raise TypeError(
                f"{dotted} has invalid type {type(value).__name__}"
            )


def _canonical_values(
    merged: dict[str, Any],
    base_path: Path,
    references: SecretReferences,
) -> dict[str, Any]:
    # The legacy decoder materializes all defaults and performs value checks.
    # Strict comparison with the source then prevents its compatibility
    # coercions from entering the Application snapshot.
    from cutmaster.configuration.loader import _decode_effective_config

    decoded = asdict(_decode_effective_config(merged, base_path))
    _validate_provided_types(merged, decoded)
    canonical = _strip_secrets(decoded)
    material_analysis = canonical["analyser"].pop("material_analysis")
    if set(material_analysis) != {"material_library_dir"}:
        raise AssertionError("Unexpected Material Analysis configuration shape")
    reference_paths = (
        (("llm",), references.llm_api_key_env),
        (("vlm",), references.vlm_api_key_env),
        (("analyser", "asr"), references.asr_api_key_env),
    )
    for path, reference in reference_paths:
        if reference is None:
            continue
        section = canonical
        for name in path:
            section = section[name]
        section["api_key_env"] = reference
    return canonical


def load_effective_configuration(path: Path | str) -> EffectiveConfiguration:
    """Load one authoritative TOML configuration without resolving API keys."""

    base_path = Path(path).expanduser().resolve()
    if base_path.suffix.lower() != ".toml":
        raise ValueError("CutMaster configuration files must use the .toml suffix")
    merged = _read_toml(base_path)
    _reject_inline_secrets(merged, base_path)
    references = SecretReferences(
        llm_api_key_env=_secret_reference(merged, "llm"),
        vlm_api_key_env=_secret_reference(merged, "vlm"),
        asr_api_key_env=_secret_reference(merged, "analyser", "asr"),
    )
    canonical = _canonical_values(merged, base_path, references)
    base_directory = base_path.parent
    pointer_path = base_directory / ".cutmaster-location"
    sources = ConfigurationSources(
        base_path=base_path,
        dotenv_path=base_directory / ".env",
        data_root_pointer_path=pointer_path,
    )
    return EffectiveConfiguration(
        sources=sources,
        data_root=_resolve_data_root(base_directory, pointer_path),
        secret_references=references,
        _values=canonical,
    )


__all__ = [
    "ConfigurationSources",
    "EffectiveConfiguration",
    "SecretReferences",
    "load_effective_configuration",
]
