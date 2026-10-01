"""Run claimed workflow actions with isolated claim/provider transactions."""

import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_execution import ActionExecution
from app.models.action_item import Reminder
from app.models.automation_settings import AutomationSettings
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.models.workflow import WorkflowStep
from app.services.action_lease import claim_action, complete_action
from app.services.integration_service import IntegrationService
from app.services.notification_prefs import should_notify
from app.services.reminder_scheduler import schedule_reminder

logger = logging.getLogger("anotai.action_executor")


async def _telegram_notifications_allowed(
    session: AsyncSession, execution_id: uuid.UUID, now: datetime,
) -> bool:
    execution = await session.get(ActionExecution, execution_id)
    if execution is None or execution.action_type != "SEND_TELEGRAM":
        return True
    user = await session.get(User, execution.user_id)
    if user is None or user.workspace_id != execution.workspace_id:
        return False
    membership_id = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == execution.workspace_id,
        WorkspaceMembership.user_id == execution.user_id,
    ))).first()
    if membership_id is None:
        return False
    workspace = await session.get(Workspace, execution.workspace_id)
    if workspace is None:
        return False
    prefs = (await session.exec(select(AutomationSettings).where(
        AutomationSettings.workspace_id == execution.workspace_id,
        AutomationSettings.user_id == execution.user_id,
    ))).first()
    return should_notify(
        now,
        workspace.timezone,
        notifications_enabled=prefs.notifications_enabled if prefs else True,
        quiet_start_minute=prefs.quiet_start_minute if prefs else None,
        quiet_end_minute=prefs.quiet_end_minute if prefs else None,
    )


async def _step_is_ready(session: AsyncSession, execution_id: uuid.UUID) -> bool:
    execution = await session.get(ActionExecution, execution_id)
    if execution is None:
        return False
    if execution.workflow_step_id is None and execution.workflow_execution_id is None:
        snapshot = execution.policy_snapshot
        return isinstance(snapshot, dict) and snapshot.get("standalone") is True
    if execution.workflow_step_id is None or execution.workflow_execution_id is None:
        return False
    step = await session.get(WorkflowStep, execution.workflow_step_id)
    if step is None:
        return False
    previous = (await session.exec(
        select(ActionExecution.status)
        .join(WorkflowStep, WorkflowStep.id == ActionExecution.workflow_step_id)
        .where(
            ActionExecution.workflow_execution_id == execution.workflow_execution_id,
            ActionExecution.workspace_id == execution.workspace_id,
            WorkflowStep.position < step.position,
        )
    )).all()
    return all(status == "succeeded" for status in previous)


async def _schedule_committed_reminder_best_effort(
    session_factory: Callable[[], Any],
    execution_id: uuid.UUID,
    result: Any,
) -> None:
    if not isinstance(result, dict) or result.get("entity") != "reminder":
        return
    try:
        reminder_id = uuid.UUID(str(result.get("id")))
        async with session_factory() as session:
            execution = await session.get(ActionExecution, execution_id)
            reminder = await session.get(Reminder, reminder_id)
            if (
                execution is None
                or execution.status != "succeeded"
                or execution.action_type != "CREATE_REMINDER"
                or reminder is None
                or reminder.user_id != execution.user_id
                or reminder.workspace_id != execution.workspace_id
                or reminder.status != "scheduled"
            ):
                return
            remind_at = reminder.remind_at
            if remind_at.tzinfo is None or remind_at.utcoffset() is None:
                # SQLite drops tzinfo on DateTime(timezone=True); persisted values are UTC.
                remind_at = remind_at.replace(tzinfo=timezone.utc)
            else:
                remind_at = remind_at.astimezone(timezone.utc)
            await schedule_reminder(reminder.id, remind_at)
    except Exception:
        # Reminder SQL reconciliation remains authoritative if Redis is unavailable.
        logger.warning("Wake-up de reminder não foi agendado para action %s", execution_id)


async def run_action_execution(
    session_factory: Callable[[], Any],
    execution_id: uuid.UUID,
) -> str:
    """Claim once, execute in a separate transaction, then complete conditionally."""
    now = datetime.now(timezone.utc)
    async with session_factory() as claim_session:
        if not await _step_is_ready(claim_session, execution_id):
            await claim_session.rollback()
            return "blocked_by_previous_step"
        if not await _telegram_notifications_allowed(claim_session, execution_id, now):
            await claim_session.rollback()
            return "muted"
        token = await claim_action(claim_session, execution_id, now, max_attempts=5)
    if token is None:
        return "not_claimed"

    failure_kind: str | None = None
    provider_result: Any = None
    completed = False
    async with session_factory() as provider_session:
        try:
            provider_result = await IntegrationService().execute_claimed(provider_session, execution_id)
            completed = await complete_action(provider_session, token, success=True)
        except Exception as exc:  # noqa: BLE001 — never expose provider error text
            await provider_session.rollback()
            failure_kind = type(exc).__name__.lower()
    if failure_kind is None:
        if not completed:
            return "stale_completion"
        await _schedule_committed_reminder_best_effort(session_factory, execution_id, provider_result)
        return "succeeded"

    # Provider writes are rolled back before this new transaction requeues or
    # terminalizes the lease. No exception message or payload is persisted.
    async with session_factory() as completion_session:
        completed = await complete_action(
            completion_session,
            token,
            success=False,
            error_kind=failure_kind,
        )
    if not completed:
        return "stale_failure"
    return "failed" if token.attempts >= token.max_attempts else "retry_scheduled"


async def process_pending_actions(
    session_factory: Callable[[], Any],
    limit: int = 100,
) -> dict[str, int]:
    """Process pending and expired claims up to the bounded batch size."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit deve ser um inteiro positivo")
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        candidates = list((await session.exec(
            select(ActionExecution.id)
            .where(
                or_(
                    ActionExecution.status == "pending",
                    and_(
                        ActionExecution.status == "claimed",
                        ActionExecution.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(ActionExecution.created_at.asc(), ActionExecution.id.asc())
            .limit(limit)
        )).all())
    counts = {"succeeded": 0, "retry_scheduled": 0, "failed": 0, "skipped": 0}
    for execution_id in candidates:
        result = await run_action_execution(session_factory, execution_id)
        if result == "succeeded":
            counts["succeeded"] += 1
        elif result == "retry_scheduled":
            counts["retry_scheduled"] += 1
        elif result in {"not_claimed", "muted"}:
            counts["skipped"] += 1
        elif result == "blocked_by_previous_step":
            counts["skipped"] += 1
        elif result in {"stale_completion", "stale_failure"}:
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
    return counts
