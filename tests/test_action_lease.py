"""Atomic action lease/retry transition tests."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.action_execution import ActionExecution, ActionExecutionStatus
from app.models.user import User
from app.models.workspace import Workspace
from app.services.action_lease import ActionLeaseToken, claim_action, complete_action


async def _action(
    session,
    *,
    status: ActionExecutionStatus = "pending",
    attempts: int = 0,
    lease_expires_at: datetime | None = None,
):
    workspace = Workspace(name="Lease workspace")
    user = User(
        name="Lease user",
        email=f"lease-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    action = ActionExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        trigger_type="MANUAL",
        source_id=str(uuid.uuid4()),
        action_type="CREATE_TASK",
        idempotency_key=str(uuid.uuid4()),
        status=status,
        attempts=attempts,
        lease_expires_at=lease_expires_at,
    )
    session.add(action)
    await session.commit()
    await session.refresh(action)
    return action


async def test_claim_commits_and_excludes_a_second_sequential_claim(db_session):
    action = await _action(db_session)
    now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)

    token = await claim_action(db_session, action.id, now)
    competing = await claim_action(db_session, action.id, now + timedelta(seconds=1))

    assert isinstance(token, ActionLeaseToken)
    assert token.attempts == 1
    assert token.lease_expires_at == now + timedelta(seconds=300)
    assert token.lease_expires_at.utcoffset() == timedelta(0)
    assert competing is None
    await db_session.refresh(action)
    assert action.status == "claimed" and action.attempts == 1
    assert action.lease_expires_at is not None


async def test_expired_lease_reclaim_and_stale_completion_noop(db_session):
    action = await _action(db_session)
    now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
    first = await claim_action(db_session, action.id, now, lease_seconds=30)
    assert first is not None

    second = await claim_action(db_session, action.id, now + timedelta(seconds=31), lease_seconds=60)

    assert second is not None and second.attempts == 2
    assert await complete_action(db_session, first, success=True) is False
    await db_session.refresh(action)
    assert action.status == "claimed" and action.attempts == 2
    assert await complete_action(db_session, second, success=True) is True
    await db_session.refresh(action)
    assert action.status == "succeeded"
    assert action.lease_expires_at is None


async def test_failed_attempt_retries_until_max_then_fails_terminally(db_session):
    action = await _action(db_session)
    now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)

    first = await claim_action(db_session, action.id, now, max_attempts=2)
    assert first is not None
    assert await complete_action(db_session, first, success=False, error_kind="network_timeout") is True
    await db_session.refresh(action)
    assert action.status == "pending"
    assert action.last_error == "network_timeout"

    second = await claim_action(db_session, action.id, now + timedelta(seconds=1), max_attempts=2)
    assert second is not None and second.attempts == 2
    assert await complete_action(db_session, second, success=False, error_kind="secret=must-not-leak") is True
    await db_session.refresh(action)
    assert action.status == "failed"
    assert action.last_error == "unknown_error"
    assert await claim_action(db_session, action.id, now + timedelta(seconds=2), max_attempts=2) is None


async def test_expired_claim_already_at_attempt_limit_is_marked_failed(db_session):
    expired_at = datetime(2026, 9, 15, 11, tzinfo=timezone.utc)
    action = await _action(db_session, status="claimed", attempts=3, lease_expires_at=expired_at)

    assert await claim_action(
        db_session,
        action.id,
        datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
        max_attempts=3,
    ) is None
    await db_session.refresh(action)
    assert action.status == "failed"
    assert action.last_error == "max_attempts_exhausted"
    assert action.lease_expires_at is None


@pytest.mark.parametrize("status", ["awaiting_confirmation", "succeeded", "failed"])
async def test_non_claimable_statuses_are_never_claimed(db_session, status):
    action = await _action(db_session, status=status)

    assert await claim_action(
        db_session,
        action.id,
        datetime(2026, 9, 15, 12, tzinfo=timezone.utc),
    ) is None
    await db_session.refresh(action)
    assert action.status == status
    assert action.attempts == 0


async def test_naive_now_is_rejected(db_session):
    action = await _action(db_session)

    with pytest.raises(ValueError, match="timezone"):
        await claim_action(db_session, action.id, datetime(2026, 9, 15, 12))
