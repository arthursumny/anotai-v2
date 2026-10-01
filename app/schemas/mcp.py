"""AnotAI — schemas dos servidores MCP do usuário (Pydantic v2)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError("server_url deve começar com http:// ou https://")
    return cleaned


def _clean_secret_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


class MCPServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    server_url: str = Field(
        min_length=1,
        max_length=2000,
        description="URL do servidor MCP. Aceita ${SECRETO} (ex: https://mcp.exemplo.com/${TENANT}/mcp).",
    )
    auth_header_key: str | None = Field(
        default=None,
        max_length=100,
        description="Header de auth do servidor (ex: Authorization, X-API-Key). "
        "Cada servidor define o seu — sem header, nenhuma auth é enviada. Nulo = sem auth.",
    )
    auth_secret_name: str | None = Field(
        default=None,
        max_length=100,
        description="Nome do segredo (UserSecret) com o VALOR COMPLETO do header "
        "(ex: 'Bearer abc123' ou só a chave, conforme o servidor exigir).",
    )

    @field_validator("server_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        return _clean_url(v)

    @field_validator("auth_header_key", "auth_secret_name")
    @classmethod
    def _clean_secret(cls, v: str | None) -> str | None:
        return _clean_secret_name(v)


class MCPServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    server_url: str | None = Field(default=None, min_length=1, max_length=2000)
    auth_header_key: str | None = Field(default=None, max_length=100)
    auth_secret_name: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None

    @field_validator("server_url")
    @classmethod
    def _valid_url(cls, v: str | None) -> str | None:
        return _clean_url(v) if v is not None else None

    @field_validator("auth_header_key", "auth_secret_name")
    @classmethod
    def _clean_secret(cls, v: str | None) -> str | None:
        return _clean_secret_name(v)


class MCPServerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    server_url: str
    auth_header_key: str | None
    auth_secret_name: str | None
    provider_id: str | None = None
    is_active: bool
    created_at: datetime


class MCPServerTestResult(BaseModel):
    ok: bool
    server_id: uuid.UUID
    tools: list[dict[str, Any]]
