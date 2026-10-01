"""AnotAI — modelo Template."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Template(SQLModel, table=True):
    __tablename__ = "templates"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    # None = template oficial do marketplace/sistema
    user_id: uuid.UUID | None = Field(default=None, foreign_key="users.id", index=True)
    title: str = Field(min_length=1, max_length=200)
    # Para que serve: identidade do template, OBRIGATÓRIO na API.
    # O roteador lê título + purpose + exemplo para casar.
    purpose: str = Field(default="", max_length=1000)
    semantic_trigger: str = Field(min_length=1, max_length=4000)
    # Fonte da verdade p/ o usuário: UM exemplo de como a nota deve sair.
    # Gatilho e schema são derivados dele automaticamente (ver /derive).
    example_output: str = Field(default="", max_length=8000)
    output_schema: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    actions: list[dict] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    category: str | None = Field(default=None, max_length=80)
    use_cases: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    routing_description: str | None = Field(default=None, max_length=2000)
    version: int = Field(default=1, ge=1)
    is_public: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
