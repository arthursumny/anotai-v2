"""Pure action schema and registry validation tests."""

from datetime import datetime

import pytest

from app.schemas.workflow_action import (
    ActionValidationError,
    CreateReminderAction,
    CreateTaskAction,
    PipelineTriggerAction,
    SendTelegramAction,
    WorkflowActionType,
)
from app.services.action_registry import ACTION_REGISTRY, validate_action


def test_registry_returns_typed_actions_and_parses_aware_local_datetime():
    task = validate_action({
        "type": "CREATE_TASK",
        "title": "Call the client",
        "summary": "Follow up tomorrow",
        "due_at": "2026-11-03T09:30:00-05:00",
        "confidence": 0.9,
    })
    reminder = validate_action({
        "type": "CREATE_REMINDER",
        "title": "Review the draft",
        "remind_at": "2026-11-03T09:30:00+05:30",
    })
    telegram = validate_action({"type": "SEND_TELEGRAM", "text": "The meeting starts at 10."})

    assert isinstance(task, CreateTaskAction)
    assert isinstance(task.due_at, datetime)
    task_offset = task.due_at.utcoffset()
    assert task_offset is not None and task_offset.total_seconds() == -18000
    assert isinstance(reminder, CreateReminderAction)
    reminder_offset = reminder.remind_at.utcoffset()
    assert reminder_offset is not None and reminder_offset.total_seconds() == 19800
    assert isinstance(telegram, SendTelegramAction)
    assert set(ACTION_REGISTRY) == {
        "CREATE_TASK", "CREATE_REMINDER", "SEND_TELEGRAM", "CREATE_CALENDAR_EVENT", "PIPELINE_TRIGGER"
    }


@pytest.mark.parametrize("payload", [
    {"type": "HTTP_REQUEST", "url": "https://example.test"},
    {"type": "CREATE_TASK", "title": "x", "workspaceId": "w"},
    {"type": "CREATE_TASK", "title": "x", "userId": "u"},
    {"type": "CREATE_TASK", "title": "x", "nangoConnectionId": "n"},
    {"type": "SEND_TELEGRAM", "text": "hello", "chat_id": "123"},
    {"type": "SEND_TELEGRAM", "text": "hello", "token": "secret"},
    {"type": "CREATE_TASK", "title": "Call", "due_at": "2026-11-03T09:30:00"},
    {"type": "CREATE_REMINDER", "title": "Review", "remind_at": "2026-11-03T09:30:00"},
    {"type": "SEND_TELEGRAM", "text": "Open https://example.test"},
    {"type": "SEND_TELEGRAM", "text": "Visit example.com"},
    {"type": "SEND_TELEGRAM", "text": "Bearer abc123"},
    {"type": "SEND_TELEGRAM", "text": "token=abc123"},
])
def test_rejects_injected_or_unsafe_payloads(payload):
    with pytest.raises(ActionValidationError):
        validate_action(payload)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, "0.8"])
def test_confidence_is_strict_and_bounded(confidence):
    with pytest.raises(ActionValidationError):
        validate_action({"type": "CREATE_TASK", "title": "Task", "confidence": confidence})


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_confidence_boundaries_are_valid(confidence):
    action = validate_action({"type": "CREATE_TASK", "title": "Task", "confidence": confidence})
    assert isinstance(action, CreateTaskAction)
    assert action.confidence == confidence


def test_whitelist_and_internal_pipeline_trigger():
    payload = {"type": "PIPELINE_TRIGGER", "pipeline": "GENERATE_DOCUMENT"}
    trigger = validate_action(payload, permitted_actions={WorkflowActionType.PIPELINE_TRIGGER})
    assert isinstance(trigger, PipelineTriggerAction)

    with pytest.raises(ActionValidationError, match="whitelist"):
        validate_action(payload, permitted_actions={WorkflowActionType.CREATE_TASK})
    with pytest.raises(ActionValidationError):
        validate_action({"type": "PIPELINE_TRIGGER", "pipeline": "SEND_TELEGRAM"})


def test_telegram_text_and_task_fields_are_required_and_bounded():
    with pytest.raises(ActionValidationError):
        validate_action({"type": "SEND_TELEGRAM", "text": ""})
    with pytest.raises(ActionValidationError):
        validate_action({"type": "SEND_TELEGRAM", "text": "x" * 2001})
    with pytest.raises(ActionValidationError):
        validate_action({"type": "CREATE_TASK", "title": ""})
    with pytest.raises(ActionValidationError):
        validate_action({"type": "CREATE_TASK", "title": "x" * 301})
