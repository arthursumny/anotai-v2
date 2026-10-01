"""AnotAI — schemas de Template (Pydantic v2, tipagem estrita)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

ActionType = Literal["telegram", "whatsapp", "webhook", "mcp_tool", "email"]


class ActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: StrictBool = True


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=1000)
    semantic_trigger: str = Field(min_length=1, max_length=4000)
    example_output: str = Field(default="", max_length=8000)
    output_schema: dict[str, Any]
    actions: list[ActionConfig] = Field(default_factory=list)
    category: str | None = Field(default=None, max_length=80)
    use_cases: list[str] = Field(default_factory=list, max_length=20)
    routing_description: str | None = Field(default=None, max_length=2000)
    is_public: bool = False

    @field_validator("output_schema")
    @classmethod
    def _validate_json_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(v, dict) or v.get("type") != "object":
            raise ValueError("output_schema deve ser um JSON Schema de type=object")
        Draft202012Validator.check_schema(v)
        return v

    @field_validator("category", "use_cases")
    @classmethod
    def _validate_routing_metadata(cls, value):
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        if isinstance(value, list):
            normalized = [item.strip() for item in value]
            if any(not item or len(item) > 160 for item in normalized):
                raise ValueError("use_cases deve conter textos de 1 a 160 caracteres")
            return normalized
        return value


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: str | None = Field(default=None, min_length=1, max_length=1000)
    semantic_trigger: str | None = Field(default=None, min_length=1, max_length=4000)
    example_output: str | None = Field(default=None, max_length=8000)
    output_schema: dict[str, Any] | None = None
    actions: list[ActionConfig] | None = None
    category: str | None = Field(default=None, max_length=80)
    use_cases: list[str] | None = Field(default=None, max_length=20)
    routing_description: str | None = Field(default=None, max_length=2000)
    is_public: bool | None = None

    @field_validator("output_schema")
    @classmethod
    def _validate_json_schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        if not isinstance(v, dict) or v.get("type") != "object":
            raise ValueError("output_schema deve ser um JSON Schema de type=object")
        Draft202012Validator.check_schema(v)
        return v

    @field_validator("category", "use_cases")
    @classmethod
    def _validate_routing_metadata(cls, value):
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        if isinstance(value, list):
            normalized = [item.strip() for item in value]
            if any(not item or len(item) > 160 for item in normalized):
                raise ValueError("use_cases deve conter textos de 1 a 160 caracteres")
            return normalized
        return value


class TemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    title: str
    purpose: str = ""
    semantic_trigger: str
    example_output: str = ""
    output_schema: dict[str, Any]
    actions: list[dict[str, Any]]
    category: str | None
    use_cases: list[str]
    routing_description: str | None
    version: int
    is_public: bool
    created_at: datetime


CaptureMode = Literal["intentional", "continuous"]


class TemplateRoutingRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str | None = Field(default=None, max_length=120)
    capture_mode: CaptureMode | None = None
    priority: int = Field(default=100, ge=0, le=1000)
    enabled: bool = True

    @field_validator("context")
    @classmethod
    def _normalize_context(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized


class TemplateRoutingRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str | None = Field(default=None, max_length=120)
    capture_mode: CaptureMode | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    enabled: bool | None = None

    @field_validator("context")
    @classmethod
    def _normalize_context(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized


class TemplateRoutingRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    context: str | None
    capture_mode: str | None
    priority: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


DraftKind = Literal["texto", "número", "data", "lista", "sim/não", "opções"]


class DraftField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    kind: DraftKind
    required: bool = True
    options: list[str] = Field(default_factory=list)


class TemplateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=10, max_length=2000)


class TemplateDraft(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=1000)
    semantic_trigger: str = Field(min_length=1, max_length=4000)
    fields: list[DraftField] = Field(min_length=1, max_length=10)
    suggested_message: str = Field(max_length=2000)
    suggested_example: str = Field(default="", max_length=8000)


class DeriveRequest(BaseModel):
    """Deriva gatilho+campos a partir do exemplo (fonte da verdade do usuário)."""

    model_config = ConfigDict(extra="forbid")

    example_output: str = Field(min_length=20, max_length=8000)


class DeriveResponse(BaseModel):
    purpose: str
    semantic_trigger: str
    fields: list[DraftField]
    suggested_message: str
