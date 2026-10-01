"""Preparation-only workflow trigger, authorization, and idempotency tests."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationSettings
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.workflow_engine import (
    WorkflowPreparationError,
    emit_trigger,
    prepare_workflow_executions,
)


async def _identity(session, *, membership: bool = True):
    workspace = Workspace(name="Workflow workspace")
    user = User(
        name="Workflow user",
        email=f"workflow-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    if membership:
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
        await session.flush()
    return workspace, user


async def _workflow_with_steps(session, workspace, user, actions, *, conditions=None):
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=user.id,
        title="Safe workflow",
        trigger_type="DOCUMENT_GENERATED",
        conditions=conditions or {},
        enabled=True,
    )
    session.add(workflow)
    await session.flush()
    steps = []
    for position, (action_type, payload, require_confirmation) in enumerate(actions):
        # API serialization stores only input fields; action_type is a separate column.
        stored_template = {key: value for key, value in payload.items() if key != "type"}
        step = WorkflowStep(
            workflow_id=workflow.id,
            position=position,
            action_type=action_type,
            input_template=stored_template,
            require_confirmation=require_confirmation,
            enabled=True,
        )
        session.add(step)
        steps.append(step)
    await session.flush()
    return workflow, steps


async def test_default_suggestive_mode_prepares_confirmation_ledgers_idempotently(db_session):
    workspace, user = await _identity(db_session)
    workflow, steps = await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Review document"}, False),
        ("SEND_TELEGRAM", {"type": "SEND_TELEGRAM", "text": "Document is ready"}, False),
    ])
    await db_session.commit()

    first = await prepare_workflow_executions(
        db_session,
        workspace.id,
        user.id,
        "DOCUMENT_GENERATED",
        source_id="doc-001",
        context={"source": "audio", "event_type": "AUDIO_TRANSCRIBED", "confidence": 0.9},
    )
    second = await emit_trigger(
        db_session,
        workspace.id,
        user.id,
        "DOCUMENT_GENERATED",
        source_id="doc-001",
        context={"source": "audio", "event_type": "AUDIO_TRANSCRIBED", "confidence": 0.9},
    )

    executions = list((await db_session.exec(select(WorkflowExecution))).all())
    action_rows = list((await db_session.exec(select(ActionExecution))).all())
    assert len(first) == len(second) == 1
    assert first[0].workflow_id == workflow.id
    assert len(executions) == 1
    assert len(action_rows) == len(steps) == 2
    assert all(row.status == "awaiting_confirmation" for row in action_rows)
    assert all("content" not in row.policy_snapshot for row in action_rows)


async def test_automatic_policy_requires_per_action_authorization_and_confirmation_wins(db_session):
    workspace, user = await _identity(db_session)
    settings = AutomationSettings(
        workspace_id=workspace.id,
        user_id=user.id,
        automation_mode="automatic",
        action_config={
            "CREATE_TASK": "automatic",
            "CREATE_REMINDER": "automatic",
            "SEND_TELEGRAM": "automatic",
        },
    )
    db_session.add(settings)
    workflow, _ = await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Create task"}, False),
        ("CREATE_REMINDER", {
            "type": "CREATE_REMINDER",
            "title": "Review later",
            "remind_at": "2030-01-01T09:00:00-03:00",
        }, False),
        ("SEND_TELEGRAM", {"type": "SEND_TELEGRAM", "text": "Send approved message"}, False),
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Needs human"}, True),
        ("PIPELINE_TRIGGER", {
            "type": "PIPELINE_TRIGGER",
            "pipeline": "GENERATE_DOCUMENT",
        }, False),
    ])
    await db_session.commit()

    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="doc-automatic"
    )
    rows = list((await db_session.exec(select(ActionExecution))).all())
    by_type_and_confirmation = {(row.action_type, row.policy_snapshot["require_confirmation"]): row.status for row in rows}

    assert by_type_and_confirmation[("CREATE_TASK", False)] == "pending"
    assert by_type_and_confirmation[("CREATE_REMINDER", False)] == "pending"
    assert by_type_and_confirmation[("SEND_TELEGRAM", False)] == "pending"
    assert by_type_and_confirmation[("CREATE_TASK", True)] == "awaiting_confirmation"
    assert by_type_and_confirmation[("PIPELINE_TRIGGER", False)] == "awaiting_confirmation"
    assert all(row.workflow_execution_id is not None for row in rows)


async def test_conditions_are_finite_and_context_must_match(db_session):
    workspace, user = await _identity(db_session)
    workflow, _ = await _workflow_with_steps(
        db_session,
        workspace,
        user,
        [("CREATE_TASK", {"type": "CREATE_TASK", "title": "Match"}, False)],
        conditions={"source": "audio", "event_type": "AUDIO_TRANSCRIBED", "minimum_confidence": 0.8},
    )
    await db_session.commit()

    none = await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="miss",
        context={"source": "gmail", "event_type": "AUDIO_TRANSCRIBED", "confidence": 0.9},
    )
    matched = await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="match",
        context={"source": "audio", "event_type": "AUDIO_TRANSCRIBED", "confidence": 0.8},
    )
    assert none == []
    assert len(matched) == 1 and matched[0].workflow_id == workflow.id

    with pytest.raises(WorkflowPreparationError, match="Contexto"):
        await prepare_workflow_executions(
            db_session,
            workspace.id,
            user.id,
            "DOCUMENT_GENERATED",
            source_id="bad-context",
            context={"workspaceId": str(workspace.id)},
        )


@pytest.mark.parametrize("invalid_kind", ["action", "conditions"])
async def test_invalid_workflow_does_not_poison_other_workflows(db_session, caplog, invalid_kind):
    workspace, user = await _identity(db_session)
    invalid_conditions = {"expression": "private condition must not be logged"} if invalid_kind == "conditions" else {}
    invalid_definition_action = (
        ("SEND_TELEGRAM", {"type": "SEND_TELEGRAM", "text": "private action content", "chat_id": "secret destination"}, False)
        if invalid_kind == "action"
        else ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Definition condition"}, False)
    )
    invalid_workflow, _ = await _workflow_with_steps(
        db_session,
        workspace,
        user,
        [invalid_definition_action],
        conditions=invalid_conditions,
    )
    valid_workflow, _ = await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Safe"}, False),
    ])
    invalid_workflow.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    valid_workflow.created_at = datetime(2021, 1, 1, tzinfo=timezone.utc)
    await db_session.commit()

    with caplog.at_level("WARNING"):
        executions = await prepare_workflow_executions(
            db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id=f"invalid-{invalid_kind}"
        )
    assert len(executions) == 1 and executions[0].workflow_id == valid_workflow.id
    assert [row.workflow_id for row in (await db_session.exec(select(WorkflowExecution))).all()] == [valid_workflow.id]
    action_rows = list((await db_session.exec(select(ActionExecution))).all())
    assert len(action_rows) == 1 and action_rows[0].workflow_execution_id == executions[0].id
    assert "private" not in caplog.text and "secret destination" not in caplog.text
    assert str(invalid_workflow.id) in caplog.text


async def test_invalid_workflow_alone_is_skipped_without_ledgers(db_session, caplog):
    workspace, user = await _identity(db_session)
    await _workflow_with_steps(db_session, workspace, user, [
        ("SEND_TELEGRAM", {"type": "SEND_TELEGRAM", "text": "safe text", "chat_id": "secret"}, False),
    ])
    await db_session.commit()

    with caplog.at_level("WARNING"):
        executions = await prepare_workflow_executions(
            db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="invalid-only"
        )

    assert executions == []
    assert list((await db_session.exec(select(WorkflowExecution))).all()) == []
    assert list((await db_session.exec(select(ActionExecution))).all()) == []
    assert "secret" not in caplog.text


async def test_cross_workspace_membership_and_workflow_scopes_are_denied(db_session):
    workspace, user = await _identity(db_session)
    foreign_workspace, foreign_user = await _identity(db_session)
    await _workflow_with_steps(db_session, foreign_workspace, foreign_user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Foreign"}, False),
    ])
    await db_session.commit()

    with pytest.raises(PermissionError):
        await prepare_workflow_executions(
            db_session, foreign_workspace.id, user.id, "DOCUMENT_GENERATED", source_id="cross"
        )
    assert await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="empty"
    ) == []
    assert list((await db_session.exec(select(WorkflowExecution))).all()) == []


async def test_telegram_needs_automatic_mode_and_explicit_auth_and_internal_only_never_auto(db_session):
    workspace, user = await _identity(db_session)
    db_session.add(AutomationSettings(
        workspace_id=workspace.id,
        user_id=user.id,
        automation_mode="automatic",
        action_config={"CREATE_TASK": "automatic"},
    ))
    await _workflow_with_steps(db_session, workspace, user, [
        ("SEND_TELEGRAM", {"type": "SEND_TELEGRAM", "text": "Approval required"}, False),
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Task"}, False),
    ])
    await db_session.commit()

    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="policy"
    )
    statuses = {row.action_type: row.status for row in (await db_session.exec(select(ActionExecution))).all()}
    assert statuses == {"SEND_TELEGRAM": "awaiting_confirmation", "CREATE_TASK": "pending"}


async def test_disabled_action_disposition_omits_action_ledger(db_session):
    workspace, user = await _identity(db_session)
    db_session.add(AutomationSettings(
        workspace_id=workspace.id,
        user_id=user.id,
        automation_mode="automatic",
        action_config={"CREATE_TASK": "disabled"},
    ))
    workflow, _ = await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Disabled"}, False),
    ])
    await db_session.commit()

    executions = await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="disabled-action"
    )

    assert len(executions) == 1 and executions[0].workflow_id == workflow.id
    assert len(list((await db_session.exec(select(WorkflowExecution))).all())) == 1
    assert list((await db_session.exec(select(ActionExecution))).all()) == []


async def test_semi_automatic_only_prepares_authorized_internal_actions(db_session):
    workspace, user = await _identity(db_session)
    db_session.add(AutomationSettings(
        workspace_id=workspace.id,
        user_id=user.id,
        automation_mode="semi_automatic",
        action_config={
            "CREATE_TASK": "automatic",
            "CREATE_REMINDER": "automatic",
            "SEND_TELEGRAM": "automatic",
        },
    ))
    await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Task"}, False),
        ("CREATE_REMINDER", {
            "type": "CREATE_REMINDER", "title": "Reminder", "remind_at": "2030-01-01T10:00:00Z",
        }, False),
        ("SEND_TELEGRAM", {"type": "SEND_TELEGRAM", "text": "Message"}, False),
    ])
    await db_session.commit()

    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="semi-automatic"
    )
    statuses = {row.action_type: row.status for row in (await db_session.exec(select(ActionExecution))).all()}
    assert statuses == {
        "CREATE_TASK": "pending",
        "CREATE_REMINDER": "pending",
        "SEND_TELEGRAM": "awaiting_confirmation",
    }


async def test_long_source_id_does_not_collide_across_steps(db_session):
    workspace, user = await _identity(db_session)
    _, steps = await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "First"}, False),
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Second"}, False),
    ])
    await db_session.commit()
    source_id = "source-" + "x" * 300

    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id=source_id
    )

    rows = list((await db_session.exec(select(ActionExecution))).all())
    assert len(rows) == 2
    assert len({row.idempotency_key for row in rows}) == 2
    assert all(len(row.idempotency_key) <= 320 for row in rows)
    assert steps[0].id.hex not in rows[0].idempotency_key


async def test_source_id_is_required_and_db_type_override_is_rejected(db_session):
    workspace, user = await _identity(db_session)
    workflow, steps = await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Will be tampered"}, False),
    ])
    # Simulate a corrupted/legacy DB row; API schemas do not store this key.
    steps[0].input_template = {"type": "SEND_TELEGRAM", "text": "override"}
    await db_session.commit()

    with pytest.raises(WorkflowPreparationError, match="source_id"):
        await prepare_workflow_executions(db_session, workspace.id, user.id, "DOCUMENT_GENERATED")
    assert await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="tampered"
    ) == []
    assert list((await db_session.exec(select(WorkflowExecution))).all()) == []
    assert list((await db_session.exec(select(ActionExecution))).all()) == []


async def test_action_snapshot_survives_workflow_edits_and_step_removal(db_session):
    workspace, user = await _identity(db_session)
    workflow, steps = await _workflow_with_steps(db_session, workspace, user, [
        ("SEND_TELEGRAM", {"type": "SEND_TELEGRAM", "text": "Original safe message"}, False),
    ])
    await db_session.commit()
    step_id = steps[0].id

    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="snapshot-source"
    )
    action = (await db_session.exec(select(ActionExecution))).one()
    assert action.action_input == {"text": "Original safe message", "confidence": None}
    assert action.workflow_step_id == step_id
    assert action.workflow_version == 1
    original_policy = dict(action.policy_snapshot)
    assert original_policy["workflow_id"] == str(workflow.id)
    assert original_policy["workflow_version"] == 1
    assert "Original safe message" not in str(action.policy_snapshot)

    steps[0].input_template = {"text": "Edited message"}
    workflow.version = 2
    await db_session.commit()
    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="snapshot-source"
    )
    await db_session.refresh(action)
    assert action.action_input == {"text": "Original safe message", "confidence": None}
    assert action.workflow_version == 1
    assert action.policy_snapshot == original_policy

    await db_session.delete(steps[0])
    await db_session.commit()
    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="snapshot-source"
    )
    await db_session.refresh(action)
    assert action.action_input == {"text": "Original safe message", "confidence": None}
    assert action.workflow_version == 1
    assert action.policy_snapshot == original_policy
    # SQLite test fixtures don't enforce FK on-delete actions, but the snapshot
    # data remains immutable regardless of whether the FK is nulled by the DB.
    assert action.workflow_step_id in (step_id, None)


async def test_existing_legacy_ledger_without_action_input_is_not_backfilled(db_session):
    workspace, user = await _identity(db_session)
    await _workflow_with_steps(db_session, workspace, user, [
        ("CREATE_TASK", {"type": "CREATE_TASK", "title": "Legacy"}, False),
    ])
    await db_session.commit()
    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="legacy-source"
    )
    action = (await db_session.exec(select(ActionExecution))).one()
    action.action_input = None
    action.workflow_version = None
    await db_session.commit()

    await prepare_workflow_executions(
        db_session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id="legacy-source"
    )
    await db_session.refresh(action)

    assert action.action_input is None
    assert action.workflow_version is None
