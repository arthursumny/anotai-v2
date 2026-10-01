"""Finite, pure action validation registry for workflow engines."""

from types import MappingProxyType
from typing import Collection

from app.schemas.workflow_action import (
    ActionValidationError,
    CreateCalendarEventAction,
    CreateReminderAction,
    CreateTaskAction,
    PipelineTriggerAction,
    SendTelegramAction,
    WorkflowActionType,
)

ACTION_REGISTRY = MappingProxyType({
    WorkflowActionType.CREATE_TASK.value: CreateTaskAction,
    WorkflowActionType.CREATE_REMINDER.value: CreateReminderAction,
    WorkflowActionType.SEND_TELEGRAM.value: SendTelegramAction,
    WorkflowActionType.CREATE_CALENDAR_EVENT.value: CreateCalendarEventAction,
    WorkflowActionType.PIPELINE_TRIGGER.value: PipelineTriggerAction,
})


def validate_action(
    action_dict: object,
    *,
    permitted_actions: Collection[str | WorkflowActionType] | None = None,
) -> CreateTaskAction | CreateReminderAction | SendTelegramAction | CreateCalendarEventAction | PipelineTriggerAction:
    """Return a typed action payload or raise an explicit validation error."""
    if not isinstance(action_dict, dict):
        raise ActionValidationError("A ação deve ser um objeto")
    action_type = action_dict.get("type")
    if not isinstance(action_type, str) or action_type not in ACTION_REGISTRY:
        raise ActionValidationError("Tipo de ação não suportado")
    if permitted_actions is not None:
        allowed = {
            value.value if isinstance(value, WorkflowActionType) else value
            for value in permitted_actions
        }
        if action_type not in allowed:
            raise ActionValidationError("Ação não permitida pela whitelist")
    try:
        return ACTION_REGISTRY[action_type].model_validate(action_dict)
    except Exception as exc:
        # Do not echo untrusted payload values into the exception text.
        raise ActionValidationError("Payload de ação inválido") from exc
