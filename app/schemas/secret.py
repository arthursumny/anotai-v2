"""AnotAI — schemas de segredos do usuário (Pydantic v2)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SecretUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_]+$")
    value: str = Field(min_length=1, max_length=4000)


class SecretRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    value_masked: str
    created_at: datetime
    id: uuid.UUID


def mask_secret(value: str) -> str:
    """Máscara p/ listagem: nunca expõe o valor completo."""
    if len(value) <= 4:
        return "••••"
    return f"{value[:2]}••••{value[-2:]}"
