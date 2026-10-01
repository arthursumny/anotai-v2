"""Atomic leases and bounded retry transitions for action executions.

Execution is intentionally at-least-once: an expired claim can be reclaimed,
so an executor must use the action's idempotency key with downstream systems.
This module only changes the database ledger; it never invokes a provider.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import or_, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_execution import ActionExecution

_ERROR_KIND_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ActionLeaseToken:
    execution_id: uuid.UUID
    attempts: int
    lease_expires_at: datetime
    max_attempts: int = 5


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("datetime inválido")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime deve incluir timezone")
    return value.astimezone(timezone.utc)


def _token_values(token: ActionLeaseToken | Mapping[str, Any]) -> ActionLeaseToken:
    if isinstance(token, ActionLeaseToken):
        return token
    if isinstance(token, Mapping):
        try:
            return ActionLeaseToken(
                execution_id=uuid.UUID(str(token["execution_id"])),
                attempts=int(token["attempts"]),
                lease_expires_at=token["lease_expires_at"],
                max_attempts=int(token.get("max_attempts", 5)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("lease token inválido") from exc
    raise ValueError("lease token inválido")


async def claim_action(
    session: AsyncSession,
    execution_id: uuid.UUID,
    now: datetime,
    lease_seconds: int = 300,
    max_attempts: int = 5,
) -> ActionLeaseToken | None:
    """Atomically claim a pending or expired action and commit before returning."""
    if not isinstance(execution_id, uuid.UUID):
        raise ValueError("execution_id inválido")
    current_time = _aware_utc(now)
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or lease_seconds <= 0
        or not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts <= 0
    ):
        raise ValueError("lease_seconds e max_attempts devem ser positivos")
    lease_expires_at = current_time + timedelta(seconds=lease_seconds)
    eligible = or_(
        ActionExecution.status == "pending",
        (ActionExecution.status == "claimed") & (ActionExecution.lease_expires_at <= current_time),
    )
    claimed = (await session.exec(
        update(ActionExecution)
        .where(
            ActionExecution.id == execution_id,
            eligible,
            ActionExecution.attempts < max_attempts,
        )
        .values(
            status="claimed",
            attempts=ActionExecution.attempts + 1,
            lease_expires_at=lease_expires_at,
            last_error=None,
            updated_at=current_time,
        )
        .execution_options(synchronize_session=False)
        .returning(
            ActionExecution.id,
            ActionExecution.attempts,
            ActionExecution.lease_expires_at,
        )
    )).first()
    if claimed is not None:
        await session.commit()
        returned_expiry = claimed.lease_expires_at
        if returned_expiry.tzinfo is None or returned_expiry.utcoffset() is None:
            returned_expiry = returned_expiry.replace(tzinfo=timezone.utc)
        return ActionLeaseToken(
            execution_id=claimed.id,
            attempts=claimed.attempts,
            lease_expires_at=returned_expiry.astimezone(timezone.utc),
            max_attempts=max_attempts,
        )

    # An expired lease that exhausted its retry budget is terminalized under
    # the same conditional guard, never made claimable again.
    terminalized = (await session.exec(
        update(ActionExecution)
        .where(
            ActionExecution.id == execution_id,
            ActionExecution.status == "claimed",
            ActionExecution.lease_expires_at <= current_time,
            ActionExecution.attempts >= max_attempts,
        )
        .values(
            status="failed",
            lease_expires_at=None,
            last_error="max_attempts_exhausted",
            updated_at=current_time,
        )
        .execution_options(synchronize_session=False)
        .returning(ActionExecution.id)
    )).first()
    if terminalized is not None:
        await session.commit()
    else:
        await session.rollback()
    return None


async def complete_action(
    session: AsyncSession,
    token: ActionLeaseToken | Mapping[str, Any],
    success: bool,
    error_kind: str | None = None,
) -> bool:
    """Complete only the current claim; stale tokens are an idempotent no-op."""
    if not isinstance(success, bool):
        raise ValueError("success deve ser booleano")
    lease = _token_values(token)
    expires_at = _aware_utc(lease.lease_expires_at)
    if (
        not isinstance(lease.attempts, int)
        or isinstance(lease.attempts, bool)
        or lease.attempts < 1
        or not isinstance(lease.max_attempts, int)
        or isinstance(lease.max_attempts, bool)
        or lease.max_attempts < 1
    ):
        raise ValueError("lease token inválido")
    if success:
        status = "succeeded"
        safe_error = None
    else:
        status = "pending" if lease.attempts < lease.max_attempts else "failed"
        safe_error = error_kind.lower() if isinstance(error_kind, str) and _ERROR_KIND_RE.fullmatch(error_kind.lower()) else "unknown_error"
    updated = (await session.exec(
        update(ActionExecution)
        .where(
            ActionExecution.id == lease.execution_id,
            ActionExecution.status == "claimed",
            ActionExecution.attempts == lease.attempts,
            ActionExecution.lease_expires_at == expires_at,
        )
        .values(
            status=status,
            lease_expires_at=None,
            last_error=safe_error,
            updated_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
        .returning(ActionExecution.id)
    )).first()
    if updated is None:
        await session.rollback()
        return False
    await session.commit()
    return True
