"""Strict API schemas for explicit per-source monitoring policies."""

from email.utils import parseaddr
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, field_validator


SourceName = Literal["gmail", "whatsapp", "audio"]
PolicyMode = Literal["ALL", "ALLOWLIST", "BLOCKLIST"]
PolicyDirection = Literal["incoming", "outgoing", "both"]
_email_adapter = TypeAdapter(EmailStr)


def validate_gmail_addresses(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        parsed = parseaddr(value.strip())[1]
        if not parsed or parsed != value.strip():
            raise ValueError("deve conter endereços de email válidos")
        normalized.append(str(_email_adapter.validate_python(parsed)).lower())
    return normalized


class SourcePolicyPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required on every PUT: policy activation must be a deliberate opt-in.
    monitor_enabled: bool
    mode: PolicyMode = "ALL"
    selected_ids: list[str] = Field(default_factory=list, max_length=200)
    direction: PolicyDirection = "incoming"
    allow_groups: bool = True
    allow_individuals: bool = True
    gmail_label_ids: list[str] = Field(default_factory=lambda: ["INBOX"], max_length=100)
    gmail_allowed_senders: list[str] = Field(default_factory=list, max_length=200)
    retention_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("selected_ids")
    @classmethod
    def validate_selected_ids(cls, values: list[str]) -> list[str]:
        result = [value.strip() for value in values]
        if any(not value or len(value) > 500 for value in result):
            raise ValueError("IDs devem conter de 1 a 500 caracteres")
        return result

    @field_validator("gmail_allowed_senders")
    @classmethod
    def validate_allowed_senders(cls, values: list[str]) -> list[str]:
        return validate_gmail_addresses(values)

    @field_validator("gmail_label_ids")
    @classmethod
    def validate_label_ids(cls, values: list[str]) -> list[str]:
        labels = [value.strip() for value in values]
        if any(not label or len(label) > 255 for label in labels):
            raise ValueError("labels devem conter de 1 a 255 caracteres")
        return list(dict.fromkeys(labels))


class SourcePolicyRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceName
    monitor_enabled: bool
    mode: PolicyMode = "ALL"
    selected_ids: list[str] = Field(default_factory=list)
    direction: PolicyDirection = "incoming"
    allow_groups: bool = True
    allow_individuals: bool = True
    gmail_label_ids: list[str] = Field(default_factory=lambda: ["INBOX"])
    gmail_allowed_senders: list[str] = Field(default_factory=list)
    retention_days: int | None = Field(default=None, ge=1, le=365)
