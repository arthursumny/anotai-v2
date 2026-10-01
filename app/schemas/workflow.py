"""Strict request/response schemas for user-managed workflow definitions."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.schemas.workflow_action import WorkflowActionType
from app.services.action_registry import validate_action
from app.schemas.workflow_action import ActionValidationError


WorkflowTrigger = Literal[
    "MANUAL", "DOCUMENT_GENERATED", "TASK_CREATED", "REMINDER_DUE", "FOLLOW_UP_DUE",
]
WorkflowStepAction = Literal[
    WorkflowActionType.CREATE_TASK,
    WorkflowActionType.CREATE_REMINDER,
    WorkflowActionType.SEND_TELEGRAM,
    WorkflowActionType.CREATE_CALENDAR_EVENT,
    WorkflowActionType.PIPELINE_TRIGGER,
]
AutomationAction = Literal[
    WorkflowActionType.CREATE_TASK,
    WorkflowActionType.CREATE_REMINDER,
    WorkflowActionType.SEND_TELEGRAM,
    WorkflowActionType.CREATE_CALENDAR_EVENT,
]
AutomationMode = Literal["suggestive", "semi_automatic", "automatic", "disabled"]
ActionDisposition = Literal["require_confirmation", "automatic", "disabled"]
ConfidenceThreshold = Annotated[float, Field(ge=0.0, le=1.0)]


class WorkflowConditions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["audio", "whatsapp", "gmail"] | None = None
    event_type: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    minimum_confidence: ConfidenceThreshold | None = None


class WorkflowStepWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    position: int = Field(ge=0, le=99)
    action_type: WorkflowStepAction
    input_template: dict = Field(default_factory=dict)
    require_confirmation: bool = False
    enabled: bool = False

    @field_validator("input_template")
    @classmethod
    def _validate_action_payload(cls, value: dict, info):
        action_type = info.data.get("action_type")
        if action_type is None:
            return value
        if "type" in value:
            raise ValueError("input_template não deve repetir action_type")
        try:
            validated = validate_action(
                {"type": action_type, **value},
            )
        except ActionValidationError as exc:
            raise ValueError("Configuração de action inválida") from exc
        return validated.model_dump(mode="json", exclude={"type"})

    @model_validator(mode="after")
    def _telegram_requires_confirmation(self):
        if (
            self.action_type in {
                WorkflowActionType.SEND_TELEGRAM,
                WorkflowActionType.CREATE_CALENDAR_EVENT,
            }
            and "require_confirmation" not in self.model_fields_set
        ):
            self.require_confirmation = True
        return self


def _validate_step_positions(steps: list[WorkflowStepWrite] | None) -> list[WorkflowStepWrite] | None:
    if steps is None:
        return None
    positions = [step.position for step in steps]
    if len(positions) != len(set(positions)):
        raise ValueError("Cada step deve ter uma position única no workflow")
    return steps


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: StrictStr = Field(min_length=1, max_length=200)
    description: StrictStr = Field(default="", max_length=2000)
    trigger_type: WorkflowTrigger
    conditions: WorkflowConditions = Field(default_factory=WorkflowConditions)
    enabled: bool = False
    steps: list[WorkflowStepWrite] = Field(default_factory=list, max_length=100)

    @field_validator("steps")
    @classmethod
    def _unique_positions(cls, steps: list[WorkflowStepWrite]) -> list[WorkflowStepWrite]:
        return _validate_step_positions(steps) or []


class WorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    description: StrictStr | None = Field(default=None, max_length=2000)
    trigger_type: WorkflowTrigger | None = None
    conditions: WorkflowConditions | None = None
    enabled: bool | None = None
    steps: list[WorkflowStepWrite] | None = Field(default=None, max_length=100)

    @field_validator("steps")
    @classmethod
    def _unique_positions(cls, steps: list[WorkflowStepWrite] | None) -> list[WorkflowStepWrite] | None:
        return _validate_step_positions(steps)


class WorkflowStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    action_type: str
    input_template: dict
    require_confirmation: bool
    enabled: bool


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    trigger_type: str
    conditions: dict
    enabled: bool
    version: int
    steps: list[WorkflowStepRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AutomationSettingsReplace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    automation_mode: AutomationMode = "suggestive"
    action_thresholds: dict[AutomationAction, ConfidenceThreshold] = Field(default_factory=dict)
    action_config: dict[AutomationAction, ActionDisposition] = Field(default_factory=dict)
    notifications_enabled: bool = True
    quiet_start_minute: int | None = Field(default=None, ge=0, le=1439)
    quiet_end_minute: int | None = Field(default=None, ge=0, le=1439)

    @model_validator(mode="after")
    def _quiet_minutes_are_a_pair(self):
        if (self.quiet_start_minute is None) != (self.quiet_end_minute is None):
            raise ValueError("quiet_start_minute e quiet_end_minute devem ser definidos juntos")
        return self


class AutomationSettingsRead(BaseModel):
    automation_mode: AutomationMode = "suggestive"
    action_thresholds: dict[str, float] = Field(default_factory=dict)
    action_config: dict[str, str] = Field(default_factory=dict)
    notifications_enabled: bool = True
    quiet_start_minute: int | None = Field(default=None, ge=0, le=1439)
    quiet_end_minute: int | None = Field(default=None, ge=0, le=1439)


class ActionExecutionRead(BaseModel):
    """Safe ledger projection; never exposes action input or policy snapshots."""

    id: uuid.UUID
    trigger_type: str
    action_type: str
    status: str
    attempts: int
    created_at: datetime
    last_error: str | None
    source_id: str | None


class ActionExecutionDetailRead(ActionExecutionRead):
    preview_available: bool
    preview: dict[str, Any] | None = None
