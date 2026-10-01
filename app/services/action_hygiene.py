"""Bounded maintenance for action execution ledgers."""

from datetime import datetime, timedelta, timezone
import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_execution import ActionExecution
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep


async def cleanup_action_executions(
    session: AsyncSession,
    *,
    now: datetime,
    batch_size: int = 100,
    input_retention_days: int = 7,
) -> int:
    """Clean stale workflow actions and old succeeded payloads without committing.

    The return value is the number of ledger rows changed. A standalone action
    is deliberately outside workflow invalidation cleanup.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if isinstance(input_retention_days, bool) or not isinstance(input_retention_days, int) or input_retention_days < 0:
        raise ValueError("input_retention_days must be a non-negative integer")

    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(days=input_retention_days)
    rows = (await session.exec(
        select(ActionExecution)
        .where(
            ((ActionExecution.status.in_(("pending", "awaiting_confirmation"))))
            | ((ActionExecution.status == "succeeded") & (ActionExecution.action_input.is_not(None))
               & (ActionExecution.updated_at < cutoff))
        )
        .order_by(ActionExecution.updated_at, ActionExecution.id)
        .limit(batch_size)
    )).all()

    changed = 0
    for action in rows:
        if action.status == "succeeded":
            # The candidate query has already enforced both age and payload.
            action.action_input = None
            action.updated_at = now
            changed += 1
            continue

        snapshot = action.policy_snapshot
        # Explicit standalone actions (for example due reminders) remain governed
        # by their own policy and are not workflow-origin actions.
        if isinstance(snapshot, dict) and snapshot.get("standalone") is True:
            continue

        workflow_id = _uuid_or_none(snapshot.get("workflow_id") if isinstance(snapshot, dict) else None)
        workflow = await session.get(Workflow, workflow_id) if workflow_id is not None else None
        snapshot_version = snapshot.get("workflow_version") if isinstance(snapshot, dict) else None
        valid_version = isinstance(snapshot_version, int) and not isinstance(snapshot_version, bool)

        stale = (
            workflow is None
            or workflow.enabled is not True
            or workflow.workspace_id != action.workspace_id
            or workflow.user_id != action.user_id
            or action.workflow_version is None
            or workflow.version != action.workflow_version
            or not valid_version
            or snapshot_version != action.workflow_version
        )

        if not stale and action.workflow_execution_id is not None:
            parent = await session.get(WorkflowExecution, action.workflow_execution_id)
            stale = (
                parent is None
                or parent.workflow_id != workflow.id
                or parent.workspace_id != action.workspace_id
                or parent.user_id != action.user_id
            )

        if not stale and action.workflow_step_id is not None:
            step = await session.get(WorkflowStep, action.workflow_step_id)
            stale = step is None or step.workflow_id != workflow.id
        else:
            step = None

        if stale:
            action.status = "failed"
            action.last_error = "stale_workflow"
            action.lease_expires_at = None
            action.updated_at = now
            changed += 1
            continue

        if action.workflow_execution_id is None or step is None:
            continue
        failed_predecessor = (await session.exec(
            select(ActionExecution.id)
            .join(WorkflowStep, ActionExecution.workflow_step_id == WorkflowStep.id)
            .where(
                ActionExecution.workflow_execution_id == action.workflow_execution_id,
                ActionExecution.status == "failed",
                ActionExecution.workspace_id == action.workspace_id,
                ActionExecution.user_id == action.user_id,
                WorkflowStep.workflow_id == step.workflow_id,
                WorkflowStep.position < step.position,
            )
            .limit(1)
        )).first()
        if failed_predecessor is not None:
            action.status = "failed"
            action.last_error = "predecessor_failed"
            action.lease_expires_at = None
            action.updated_at = now
            changed += 1

    return changed


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
