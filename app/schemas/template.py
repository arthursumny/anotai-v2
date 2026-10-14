"""AnotAI — schemas de Template (Pydantic v2, tipagem estrita)."""

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

ActionType = Literal["telegram", "webhook", "mcp_tool", "email", "create_task", "slack", "notion"]


class _ActionProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmailConfig(_ActionProviderConfig):
    subject: StrictStr = Field(default="{title}", min_length=1, max_length=998)
    body: StrictStr = Field(default="{body}", min_length=1, max_length=200000)
    to: StrictStr | None = Field(default=None, min_length=1, max_length=320)


class TaskConfig(_ActionProviderConfig):
    title: StrictStr = Field(default="{title}", min_length=1, max_length=300)
    summary: StrictStr = Field(default="{summary}", max_length=2000)
    due_at: StrictStr | None = None


class TelegramConfig(_ActionProviderConfig):
    template: StrictStr = Field(default="{summary}", min_length=1)
    message: StrictStr | None = None
    chat_id: StrictStr | None = None
    bot_token: StrictStr | None = None


class SlackConfig(_ActionProviderConfig):
    # O canal de destino é escolhido pelo dono do template (id do canal no
    # Slack, ex.: C0123456789). Nunca vem do conteúdo gerado nem do LLM — é a
    # barreira que impede uma nota de publicar num canal arbitrário.
    channel: StrictStr = Field(min_length=1, max_length=64)
    message: StrictStr = Field(default="{summary}", min_length=1, max_length=40000)


class NotionConfig(_ActionProviderConfig):
    # O database de destino e o mapeamento de propriedades são configuração do
    # dono do template — nunca vêm do conteúdo gerado nem do LLM. É essa
    # barreira que impede uma nota de gravar em database arbitrário do Notion.
    database_id: StrictStr = Field(min_length=1, max_length=128)
    # Template do título da página (mesmo estilo de EmailConfig.subject).
    title: StrictStr = Field(default="{title}", min_length=1, max_length=300)
    # Nome da propriedade-título no database (ex.: "Name", "Nome", "Título").
    title_property: StrictStr = Field(default="Name", min_length=1, max_length=200)
    # Valores já no formato da API do Notion, com placeholders interpoláveis
    # (ex.: {"rich_text": [{"text": {"content": "{summary}"}}]}).
    properties: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("database_id")
    @classmethod
    def _notion_database_id(cls, value: str) -> str:
        # Somente o id do database (hex ou UUID com traços) — nunca uma URL.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("database_id do Notion inválido")
        return value


class WebhookConfig(_ActionProviderConfig):
    url: StrictStr = Field(min_length=1)
    method: Literal["POST", "PUT", "PATCH"] = "POST"
    headers: dict[str, StrictStr] = Field(default_factory=dict)
    body: dict[str, JsonValue] | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("Webhook exige URL HTTP")
        return value


class MCPToolConfig(_ActionProviderConfig):
    server_id: StrictStr
    tool_name: StrictStr | None = Field(default=None, min_length=1)
    arguments_mapping: dict[str, JsonValue] = Field(default_factory=dict)
    dynamic_llm_call: StrictBool = False

    @field_validator("server_id")
    @classmethod
    def _server_uuid(cls, value: str) -> str:
        uuid.UUID(value)
        return value

    @model_validator(mode="after")
    def _tool_required(self):
        if not self.dynamic_llm_call and self.tool_name is None:
            raise ValueError("MCP exige tool_name ou dynamic_llm_call")
        return self


ACTION_CONFIG_MODELS = {
    "email": EmailConfig,
    "create_task": TaskConfig,
    "telegram": TelegramConfig,
    "webhook": WebhookConfig,
    "mcp_tool": MCPToolConfig,
    "slack": SlackConfig,
    "notion": NotionConfig,
}


class ActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: StrictBool = False
    require_confirmation: StrictBool = False

    @model_validator(mode="after")
    def _validate_enabled_config(self):
        if self.enabled:
            ACTION_CONFIG_MODELS[self.type].model_validate(self.config)
        return self


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
