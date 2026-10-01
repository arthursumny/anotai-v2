"""Bounded, idempotent action ledger hygiene tests."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.action_execution import ActionExecution, ActionExecutionStatus
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace
from app.services.action_hygiene import cleanup_action_executions


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


async def _owner(session, name="hygiene"):
    workspace = Workspace(name=f"{name} workspace")
    session.add(workspace)
    await session.flush()
    user = User(
        name=f"{name} user", email=f"{uuid.uuid4()}@example.test",
        hashed_password="unused", workspace_id=workspace.id,
    )
    session.add(user)
    await session.flush()
    return workspace, user


async def _workflow(session, workspace, user, *, version=1, enabled=True):
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Test workflow",
        trigger_type="event", version=version, enabled=enabled,
    )
    session.add(workflow)
    await session.flush()
    execution = WorkflowExecution(
        workspace_id=workspace.id, user_id=user.id, workflow_id=workflow.id,
        trigger_type="event", idempotency_key=str(uuid.uuid4()),
    )
    first = WorkflowStep(workflow_id=workflow.id, position=0, action_type="first")
    second = WorkflowStep(workflow_id=workflow.id, position=1, action_type="second")
    session.add_all([execution, first, second])
    await session.flush()
    return workflow, execution, first, second


def _action(workspace, user, *, workflow=None, execution=None, step=None,
            status: ActionExecutionStatus = "pending", updated_at=None,
            action_input=None, snapshot=True):
    policy_snapshot = {"workflow_id": str(workflow.id), "workflow_version": workflow.version} if workflow and snapshot else {}
    return ActionExecution(
        workspace_id=workspace.id, user_id=user.id,
        workflow_execution_id=execution.id if execution else None,
        workflow_step_id=step.id if step else None,
        trigger_type="event", action_type="notify", idempotency_key=str(uuid.uuid4()),
        policy_snapshot=policy_snapshot, workflow_version=workflow.version if workflow else None,
        status=status, updated_at=updated_at or NOW, action_input=action_input,
    )


async def test_stale_workflow_edit_and_deleted_workflow_are_failed(db_session):
    workspace, user = await _owner(db_session)
    edited, edited_execution, edited_step, _ = await _workflow(db_session, workspace, user)
    edited_action = _action(
        workspace, user, workflow=edited, execution=edited_execution,
        step=edited_step, status="awaiting_confirmation",
    )
    deleted, deleted_execution, deleted_step, _ = await _workflow(db_session, workspace, user)
    deleted_action = _action(workspace, user, workflow=deleted, execution=deleted_execution, step=deleted_step)
    db_session.add_all([edited_action, deleted_action])
    await db_session.commit()

    edited.version += 1
    await db_session.delete(deleted)
    await db_session.flush()
    assert await cleanup_action_executions(db_session, now=NOW) == 2
    assert edited_action.status == deleted_action.status == "failed"
    assert edited_action.last_error == deleted_action.last_error == "stale_workflow"


async def test_failed_predecessor_blocks_only_later_pending_step(db_session):
    workspace, user = await _owner(db_session)
    workflow, execution, first_step, second_step = await _workflow(db_session, workspace, user)
    predecessor = _action(workspace, user, workflow=workflow, execution=execution, step=first_step, status="failed")
    downstream = _action(workspace, user, workflow=workflow, execution=execution, step=second_step)
    db_session.add_all([predecessor, downstream])
    await db_session.flush()

    assert await cleanup_action_executions(db_session, now=NOW) == 1
    assert downstream.status == "failed" and downstream.last_error == "predecessor_failed"
    assert predecessor.last_error is None


async def test_standalone_unrelated_workspace_and_claimed_actions_are_untouched(db_session):
    workspace, user = await _owner(db_session, "one")
    other_workspace, other_user = await _owner(db_session, "two")
    workflow, execution, step, _ = await _workflow(db_session, workspace, user)
    other_workflow, other_execution, other_step, _ = await _workflow(
        db_session, other_workspace, other_user,
    )
    standalone = _action(workspace, user, status="pending")
    standalone.policy_snapshot = {"standalone": True}
    unrelated = _action(
        other_workspace, other_user, workflow=other_workflow,
        execution=other_execution, step=other_step,
    )
    claimed = _action(workspace, user, workflow=workflow, execution=execution, step=step, status="claimed")
    db_session.add_all([standalone, unrelated, claimed])
    await db_session.flush()

    assert await cleanup_action_executions(db_session, now=NOW) == 0
    assert [item.status for item in (standalone, unrelated, claimed)] == ["pending", "pending", "claimed"]


async def test_succeeded_payload_scrub_retains_ledger_and_repeat_is_noop(db_session):
    workspace, user = await _owner(db_session)
    succeeded = _action(
        workspace, user, status="succeeded", action_input={"private": "do not log"},
        updated_at=NOW - timedelta(days=8),
    )
    db_session.add(succeeded)
    await db_session.flush()
    ledger = (succeeded.id, succeeded.idempotency_key, succeeded.status, succeeded.attempts)

    assert await cleanup_action_executions(db_session, now=NOW) == 1
    assert succeeded.action_input is None
    assert (succeeded.id, succeeded.idempotency_key, succeeded.status, succeeded.attempts) == ledger
    assert await cleanup_action_executions(db_session, now=NOW) == 0


async def test_missing_or_invalid_workflow_provenance_is_stale_and_caps_batch(db_session):
    workspace, user = await _owner(db_session)
    actions = [_action(workspace, user) for _ in range(102)]
    for index, action in enumerate(actions):
        action.policy_snapshot = {"workflow_id": "invalid"} if index else {}
    db_session.add_all(actions)
    await db_session.flush()

    assert await cleanup_action_executions(db_session, now=NOW, batch_size=100) == 100
    assert sum(action.status == "failed" for action in actions) == 100
    assert await cleanup_action_executions(db_session, now=NOW, batch_size=100) == 2
    assert all(action.last_error == "stale_workflow" for action in actions)


async def test_timezone_awareness_required(db_session):
    with pytest.raises(ValueError, match="timezone-aware"):
        await cleanup_action_executions(db_session, now=datetime(2026, 9, 30, 12))
