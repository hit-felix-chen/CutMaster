from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from cutmaster.application.errors import StorageRevealUnavailableError
from cutmaster.application.settings import (
    CredentialUpdate,
    ProbeProviderConnectionCommand,
    SaveProviderSettingsCommand,
    SaveSettingsCommand,
    SettingsService,
)
from cutmaster.application.settings.provider_connections import ProviderProbe
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.infrastructure.persistence.sqlite import IdempotencyConflict


def command_id() -> str:
    return str(uuid4())


def test_settings_save_updates_config_toml_atomically_and_reloads(
    managed_configuration: EffectiveConfiguration,
) -> None:
    config = managed_configuration.sources.base_path
    before = config.read_text(encoding="utf-8")
    mode = config.stat().st_mode & 0o777
    service = SettingsService(managed_configuration)

    saved = service.save(
        SaveSettingsCommand(
            command_id(),
            {"renderer": {"width": 1280, "height": 720, "fps": 24}},
        )
    )

    assert saved.restart_required
    assert saved.settings.values["renderer"]["width"] == 1280
    content = config.read_text(encoding="utf-8")
    assert ("# Stage 0a" in content) == ("# Stage 0a" in before)
    assert "width = 1280" in content
    assert "height = 720" in content
    assert "fps = 24" in content
    assert config.stat().st_mode & 0o777 == mode
    assert not list(config.parent.glob(f".{config.name}.*.tmp"))


def test_invalid_settings_candidate_preserves_previous_bytes(
    managed_configuration: EffectiveConfiguration,
) -> None:
    config = managed_configuration.sources.base_path
    before = config.read_bytes()
    service = SettingsService(managed_configuration)

    with pytest.raises((TypeError, ValueError)):
        service.save(
            SaveSettingsCommand(
                command_id(),
                {"renderer": {"width": "not-an-integer"}},
            )
        )

    assert config.read_bytes() == before


def test_settings_replay_detects_config_changed_outside_application(
    managed_configuration: EffectiveConfiguration,
) -> None:
    service = SettingsService(managed_configuration)
    identifier = command_id()
    command = SaveSettingsCommand(identifier, {"renderer": {"width": 1280}})
    service.save(command)
    config = managed_configuration.sources.base_path
    config.write_text(
        config.read_text(encoding="utf-8").replace("width = 1280", "width = 1920"),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="no longer matches"):
        service.save(command)


def test_storage_report_counts_real_managed_categories(
    managed_configuration: EffectiveConfiguration,
) -> None:
    root = managed_configuration.data_root
    (root / "media/video").mkdir(parents=True)
    (root / "media/video/source.mp4").write_bytes(b"video")
    (root / "projects/p").mkdir(parents=True)
    (root / "projects/p/plan.json").write_bytes(b"{}")
    (root / "logs").mkdir(parents=True)
    (root / "logs/app.log").write_bytes(b"log")
    service = SettingsService(managed_configuration)

    report = service.storage_report()
    categories = {item.name: item for item in report.categories}

    assert categories["materials"].size_bytes == 5
    assert categories["projects"].file_count == 1
    assert categories["logs"].size_bytes == 3
    assert categories["database"].file_count == 0
    assert report.total_size_bytes == sum(item.size_bytes for item in report.categories)


def test_storage_reveal_opens_only_the_fixed_data_root_once_per_command(
    managed_configuration: EffectiveConfiguration,
) -> None:
    opened: list[Path] = []
    service = SettingsService(
        managed_configuration,
        storage_revealer=opened.append,
        reveal_supported=True,
    )
    identifier = command_id()

    first = service.reveal_data_root(identifier)
    replay = service.reveal_data_root(identifier)

    assert first.opened and replay.opened
    assert opened == [managed_configuration.data_root]


def test_storage_reveal_is_explicitly_unavailable_on_unsupported_hosts(
    managed_configuration: EffectiveConfiguration,
) -> None:
    service = SettingsService(managed_configuration, reveal_supported=False)

    with pytest.raises(StorageRevealUnavailableError):
        service.reveal_data_root(command_id())


def test_macos_storage_reveal_invokes_open_without_a_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cutmaster.application.settings.service as service_module

    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **options: object) -> None:
        calls.append((command, options))

    monkeypatch.setattr(service_module.sys, "platform", "darwin")
    monkeypatch.setattr(service_module.subprocess, "run", run)

    service_module._reveal_in_file_manager(tmp_path)

    assert calls[0][0] == ["open", str(tmp_path)]
    assert "shell" not in calls[0][1]
    assert calls[0][1]["check"] is True
    assert calls[0][1]["close_fds"] is True


def _credentials(
    *,
    llm: CredentialUpdate | None = None,
    vlm: CredentialUpdate | None = None,
    asr: CredentialUpdate | None = None,
) -> dict[str, CredentialUpdate]:
    return {
        "llm": llm or CredentialUpdate("keep"),
        "vlm": vlm or CredentialUpdate("keep"),
        "asr": asr or CredentialUpdate("keep"),
    }


def test_provider_projection_derives_preset_without_exposing_secret(
    managed_configuration: EffectiveConfiguration,
) -> None:
    view = SettingsService(managed_configuration).get()

    assert view.connections.profile == "simple"
    assert view.connections.providers["llm"]["model"] == "qwen3.7-max"
    assert view.connections.credentials["llm"].source == "none"
    assert view.connections.credentials["llm"].suffix is None


def test_provider_save_writes_one_shared_dotenv_secret_with_mode_0600(
    managed_configuration: EffectiveConfiguration,
) -> None:
    service = SettingsService(managed_configuration)
    secret = "dashscope-secret-value"

    saved = service.save_providers(
        SaveProviderSettingsCommand(
            command_id(),
            "simple",
            None,
            _credentials(
                llm=CredentialUpdate("set", secret),
                vlm=CredentialUpdate("set", secret),
                asr=CredentialUpdate("set", secret),
            ),
        )
    )

    dotenv = managed_configuration.sources.dotenv_path
    assert dotenv.read_text(encoding="utf-8") == (
        'DASHSCOPE_API_KEY="dashscope-secret-value"\n'
    )
    assert dotenv.stat().st_mode & 0o777 == 0o600
    assert saved.settings.connections.profile == "simple"
    assert saved.settings.connections.credentials["llm"].suffix == "alue"
    assert set(saved.credential_results.values()) == {"set"}
    assert 'model = "qwen3.7-max"' in managed_configuration.sources.base_path.read_text(
        encoding="utf-8"
    )


def test_provider_receipt_has_no_secret_verifier_and_repairs_dotenv_mode(
    managed_configuration: EffectiveConfiguration,
) -> None:
    service = SettingsService(managed_configuration)
    dotenv = managed_configuration.sources.dotenv_path
    secret = "same-secret"
    dotenv.write_text(f'DEEPSEEK_API_KEY="{secret}"\n', encoding="utf-8")
    dotenv.chmod(0o644)

    service.save_providers(
        SaveProviderSettingsCommand(
            command_id(),
            "cost_saving",
            None,
            _credentials(llm=CredentialUpdate("set", secret)),
        )
    )

    assert dotenv.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(
        managed_configuration.data_root / "cutmaster.db"
    ) as connection:
        request_digest, result_json = connection.execute(
            """
            SELECT request_digest, result_json
            FROM idempotency_receipts
            WHERE command_kind = 'settings.providers.save'
            """
        ).fetchone()
    assert secret not in result_json
    assert hashlib.sha256(secret.encode("utf-8")).hexdigest() not in result_json
    assert request_digest != hashlib.sha256(secret.encode("utf-8")).hexdigest()


def test_provider_save_is_idempotent_and_detects_secret_request_drift(
    managed_configuration: EffectiveConfiguration,
) -> None:
    service = SettingsService(managed_configuration)
    identifier = command_id()
    command = SaveProviderSettingsCommand(
        identifier,
        "cost_saving",
        None,
        _credentials(llm=CredentialUpdate("set", "first-secret")),
    )

    first = service.save_providers(command)
    replay = service.save_providers(command)

    assert replay.credential_results == first.credential_results
    with pytest.raises(IdempotencyConflict):
        service.save_providers(
            SaveProviderSettingsCommand(
                identifier,
                "cost_saving",
                None,
                _credentials(llm=CredentialUpdate("set", "different-secret")),
            )
        )


def test_process_secret_is_locked_without_blocking_public_provider_save(
    managed_configuration: EffectiveConfiguration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "process-secret")
    service = SettingsService(
        managed_configuration,
        process_environment_names=frozenset({"DASHSCOPE_API_KEY"}),
    )
    providers = {
        name: dict(value) for name, value in service.get().connections.providers.items()
    }
    providers["llm"]["model"] = "deepseek-custom"

    saved = service.save_providers(
        SaveProviderSettingsCommand(
            command_id(),
            "custom",
            providers,
            _credentials(llm=CredentialUpdate("set", "ignored-secret")),
        )
    )

    assert saved.settings.connections.providers["llm"]["model"] == "deepseek-custom"
    assert saved.credential_results["llm"] == "process_locked"
    assert not managed_configuration.sources.dotenv_path.exists()
    assert saved.settings.connections.credentials["llm"].source == "process"
    assert saved.settings.connections.credentials["llm"].writable is False


def test_provider_save_rolls_back_dotenv_when_config_write_fails(
    managed_configuration: EffectiveConfiguration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cutmaster.application.settings.service as service_module

    dotenv = managed_configuration.sources.dotenv_path
    dotenv.write_text("KEEP=value\n", encoding="utf-8")
    before = dotenv.read_bytes()
    config = managed_configuration.sources.base_path
    config_before = config.read_bytes()
    real_atomic_write = service_module._atomic_write

    def fail_config(path: Path, content: str, *, mode: int | None = None) -> None:
        if path == config:
            raise OSError("injected config failure")
        real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(service_module, "_atomic_write", fail_config)
    service = SettingsService(managed_configuration)

    with pytest.raises(OSError, match="injected"):
        service.save_providers(
            SaveProviderSettingsCommand(
                command_id(),
                "cost_saving",
                None,
                _credentials(llm=CredentialUpdate("set", "new-secret")),
            )
        )

    assert dotenv.read_bytes() == before
    assert config.read_bytes() == config_before


def test_atomic_write_and_both_restore_paths_fsync_the_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cutmaster.application.settings.service as service_module

    target = tmp_path / ".env"
    synced: list[Path] = []
    monkeypatch.setattr(service_module, "_fsync_directory", synced.append)

    service_module._atomic_write(target, "API_KEY=updated\n")
    service_module._restore_file(target, b"API_KEY=previous\n")
    service_module._restore_file(target, None)

    assert synced == [tmp_path, tmp_path, tmp_path]
    assert not target.exists()


def test_connection_test_uses_ephemeral_candidate_without_persisting(
    managed_configuration: EffectiveConfiguration,
) -> None:
    probes: list[ProviderProbe] = []

    def tester(probe: ProviderProbe) -> float:
        probes.append(probe)
        return 12.34

    service = SettingsService(managed_configuration, connection_tester=tester)
    config_before = managed_configuration.sources.base_path.read_bytes()
    configuration = dict(service.get().connections.providers["llm"])
    configuration["model"] = "ephemeral-model"

    result = service.test_provider(
        ProbeProviderConnectionCommand(
            "llm",
            configuration,
            "ephemeral-secret",
        )
    )

    assert result.status == "connected"
    assert result.latency_ms == 12.3
    assert probes[0].api_key == "ephemeral-secret"
    assert probes[0].configuration["model"] == "ephemeral-model"
    assert managed_configuration.sources.base_path.read_bytes() == config_before
    assert not managed_configuration.sources.dotenv_path.exists()
