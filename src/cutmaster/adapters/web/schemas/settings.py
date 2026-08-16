"""Settings command request bodies."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class DataRootMigrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    destination_root: str = Field(min_length=1)


class SaveSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    overlay: dict[str, Any]


class ModelProviderBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    enable_thinking: bool
    temperature: float = Field(ge=0)
    max_tokens: int = Field(ge=1)
    timeout_sec: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_concurrency: int = Field(ge=1)
    input_price_yuan_per_million_tokens: float = Field(ge=0)
    cached_input_price_yuan_per_million_tokens: float = Field(ge=0)
    output_price_yuan_per_million_tokens: float = Field(ge=0)


class ASRProviderBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    backend: Literal["bailian"]
    api_key_env: str = Field(min_length=1)
    reuse: bool
    timeout_sec: float = Field(gt=0)
    poll_interval_sec: float = Field(gt=0)
    max_chars: int = Field(ge=1)
    max_subtitle_duration_sec: float = Field(gt=0)


class ProviderBundleBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    llm: ModelProviderBody
    vlm: ModelProviderBody
    asr: ASRProviderBody


class CredentialUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["keep", "set", "clear"]
    value: SecretStr | None = None

    @model_validator(mode="after")
    def validate_action_value(self) -> CredentialUpdateBody:
        if self.action == "set" and self.value is None:
            raise ValueError("set requires value")
        if self.action != "set" and self.value is not None:
            raise ValueError(f"{self.action} cannot include value")
        return self


class CredentialUpdatesBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    llm: CredentialUpdateBody
    vlm: CredentialUpdateBody
    asr: CredentialUpdateBody


class SaveProviderSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile: Literal["cost_saving", "simple", "custom"]
    providers: ProviderBundleBody | None = None
    credentials: CredentialUpdatesBody

    @model_validator(mode="after")
    def validate_profile_payload(self) -> SaveProviderSettingsBody:
        if self.profile == "custom" and self.providers is None:
            raise ValueError("Custom profile requires providers")
        if self.profile != "custom" and self.providers is not None:
            raise ValueError("Preset profiles cannot include providers")
        return self


class ModelConnectionTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    configuration: ModelProviderBody
    api_key: SecretStr | None = None


class ASRConnectionTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    configuration: ASRProviderBody
    api_key: SecretStr | None = None


__all__ = [
    "ASRConnectionTestBody",
    "ASRProviderBody",
    "CredentialUpdateBody",
    "CredentialUpdatesBody",
    "DataRootMigrationBody",
    "ModelConnectionTestBody",
    "ModelProviderBody",
    "ProviderBundleBody",
    "SaveProviderSettingsBody",
    "SaveSettingsBody",
]
