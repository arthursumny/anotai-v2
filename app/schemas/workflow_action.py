"""Strict, ORM-independent contracts for finite workflow actions."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)


class WorkflowActionType(StrEnum):
    CREATE_TASK = "CREATE_TASK"
    CREATE_REMINDER = "CREATE_REMINDER"
    SEND_TELEGRAM = "SEND_TELEGRAM"
    CREATE_CALENDAR_EVENT = "CREATE_CALENDAR_EVENT"
    PIPELINE_TRIGGER = "PIPELINE_TRIGGER"
    SEND_EMAIL = "SEND_EMAIL"
    SEND_WEBHOOK = "SEND_WEBHOOK"
    CALL_MCP_TOOL = "CALL_MCP_TOOL"


class ActionValidationError(ValueError):
    """Raised when an action is unsupported, disallowed, or malformed."""


_URL_PATTERN = re.compile(
    r"(?:https?://|ftp://|www\.)|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|co|edu|gov)\b",
    re.I,
)
_CREDENTIAL_PATTERN = re.compile(
    r"\b(?:bearer\s+\S+|(?:password|secret|token|api[_-]?key|authorization)\s*[:=]\s*\S+)",
    re.I,
)
_PROVIDER_ID_PATTERN = re.compile(r"\b(?:workspaceId|userId|nangoConnectionId)\b", re.I)


class _ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    confidence: StrictFloat | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _reject_sensitive_text(self, info: ValidationInfo):
        # Generated document content is explicitly authorized by an owned
        # template, not an LLM-generated workflow instruction.
        if info.context and info.context.get("template_authorized") is True:
            return self
        for name in ("title", "summary", "text"):
            value = getattr(self, name, None)
            if isinstance(value, str) and (
                _URL_PATTERN.search(value)
                or _CREDENTIAL_PATTERN.search(value)
                or _PROVIDER_ID_PATTERN.search(value)
            ):
                raise ValueError("Texto da ação contém URL, credencial ou identificador de provider")
        return self


class CreateTaskAction(_ActionBase):
    type: Literal[WorkflowActionType.CREATE_TASK] = WorkflowActionType.CREATE_TASK
    title: StrictStr = Field(min_length=1, max_length=300)
    summary: StrictStr = Field(default="", max_length=2000)
    due_at: AwareDatetime | None = None

    @field_validator("due_at", mode="before")
    @classmethod
    def _parse_due_at(cls, value):
        return _parse_datetime(value)


class CreateReminderAction(_ActionBase):
    type: Literal[WorkflowActionType.CREATE_REMINDER] = WorkflowActionType.CREATE_REMINDER
    title: StrictStr = Field(min_length=1, max_length=300)
    remind_at: AwareDatetime

    @field_validator("remind_at", mode="before")
    @classmethod
    def _parse_remind_at(cls, value):
        return _parse_datetime(value)


class SendTelegramAction(_ActionBase):
    type: Literal[WorkflowActionType.SEND_TELEGRAM] = WorkflowActionType.SEND_TELEGRAM
    text: StrictStr = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _workflow_text_limit(cls, value: str, info: ValidationInfo) -> str:
        if len(value) > 2000 and not (info.context and info.context.get("template_authorized") is True):
            raise ValueError("Texto do workflow excede 2000 caracteres")
        return value


class SendEmailAction(_ActionBase):
    type: Literal[WorkflowActionType.SEND_EMAIL] = WorkflowActionType.SEND_EMAIL
    subject: StrictStr = Field(min_length=1, max_length=998)
    body: StrictStr = Field(min_length=1, max_length=200000)
    to: StrictStr | None = Field(default=None, max_length=320)

    @field_validator("subject")
    @classmethod
    def _single_line_subject(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Assunto deve ter uma linha")
        return value

    @field_validator("to")
    @classmethod
    def _single_mailbox(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[^@\s,;<>\r\n]+@[^@\s,;<>\r\n]+\.[^@\s,;<>\r\n]+", value):
            raise ValueError("Destinatário inválido")
        return value


class SendWebhookAction(_ActionBase):
    """Provider location and credentials stay in the owned template."""

    type: Literal[WorkflowActionType.SEND_WEBHOOK] = WorkflowActionType.SEND_WEBHOOK
    body: dict[str, JsonValue]


class CallMCPToolAction(_ActionBase):
    type: Literal[WorkflowActionType.CALL_MCP_TOOL] = WorkflowActionType.CALL_MCP_TOOL
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class CreateCalendarEventAction(_ActionBase):
    type: Literal[WorkflowActionType.CREATE_CALENDAR_EVENT] = WorkflowActionType.CREATE_CALENDAR_EVENT
    summary: StrictStr = Field(min_length=1, max_length=300)
    start_at: AwareDatetime
    end_at: AwareDatetime
    timezone: StrictStr | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("start_at", "end_at", mode="before")
    @classmethod
    def _parse_times(cls, value):
        return _parse_datetime(value)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone deve ser um identificador IANA válido") from exc
        return value

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end_at <= self.start_at:
            raise ValueError("end_at deve ser posterior a start_at")
        return self


class PipelineTriggerAction(_ActionBase):
    """Internal-only workflow signal; it cannot address an external provider."""

    type: Literal[WorkflowActionType.PIPELINE_TRIGGER] = WorkflowActionType.PIPELINE_TRIGGER
    pipeline: Literal["CLASSIFY_EVENT", "GENERATE_DOCUMENT"]


def _parse_datetime(value):
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return value
    return value


StructuredAction: TypeAlias = Annotated[
    CreateTaskAction | CreateReminderAction | SendTelegramAction | CreateCalendarEventAction | PipelineTriggerAction
    | SendEmailAction | SendWebhookAction | CallMCPToolAction,
    Field(discriminator="type"),
]
