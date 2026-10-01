"""Read-only authenticated pendency feed."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.pendency import (
    PendencyAcceptRequest,
    PendencyActionResult,
    PendencyEditRequest,
    PendencyFeed,
    PendencySnoozeRequest,
)
from app.services.pendency_actions import (
    PendencyActionError,
    accept_pendency,
    edit_pendency,
    ignore_pendency,
    resolve_followup,
    snooze_pendency,
)
from app.services.pendency_feed import get_pendency_feed
from app.services.reminder_scheduler import schedule_followup, schedule_reminder


router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("", response_model=PendencyFeed)
async def list_pendencies(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PendencyFeed:
    items, total = await get_pendency_feed(session, current, limit=limit, offset=offset)
    return PendencyFeed(items=items, limit=limit, offset=offset, total=total)


@router.post("/{kind}/{item_id}/accept", response_model=PendencyActionResult)
async def accept_pendency_endpoint(
    kind: str,
    item_id: uuid.UUID,
    body: PendencyAcceptRequest | None = None,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PendencyActionResult:
    payload = body or PendencyAcceptRequest()
    try:
        result = await accept_pendency(
            session, current, kind, item_id,
            title=payload.title,
            due_at=payload.due_at,
            remind_at=payload.remind_at,
        )
    except PendencyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if kind == "reminder" and result.get("status") == "scheduled" and "remind_at" in result:
        try:
            await schedule_reminder(item_id, result["remind_at"])
        except Exception as exc:
            # Durable SQL state is authoritative; periodic reconciliation recovers.
            logger.warning("Reminder wake-up scheduling failed (%s)", type(exc).__name__)
    if kind == "follow_up" and result.get("status") == "waiting" and "remind_at" in result:
        try:
            await schedule_followup(result["id"], result["remind_at"])
        except Exception as exc:
            logger.warning("Follow-up wake-up scheduling failed (%s)", type(exc).__name__)
    return PendencyActionResult(kind=result["kind"], id=result["id"], status=result["status"])


@router.post("/{kind}/{item_id}/ignore", response_model=PendencyActionResult)
async def ignore_pendency_endpoint(
    kind: str,
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PendencyActionResult:
    try:
        result = await ignore_pendency(session, current, kind, item_id)
    except PendencyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return PendencyActionResult(kind=result["kind"], id=result["id"], status=result["status"])


@router.post("/{kind}/{item_id}/snooze", response_model=PendencyActionResult)
async def snooze_pendency_endpoint(
    kind: str,
    item_id: uuid.UUID,
    body: PendencySnoozeRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PendencyActionResult:
    try:
        result = await snooze_pendency(session, current, kind, item_id, body.until)
    except PendencyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return PendencyActionResult(kind=result["kind"], id=result["id"], status=result["status"])


@router.patch("/{kind}/{item_id}/edit", response_model=PendencyActionResult)
async def edit_pendency_endpoint(
    kind: str,
    item_id: uuid.UUID,
    body: PendencyEditRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PendencyActionResult:
    try:
        result = await edit_pendency(
            session, current, kind, item_id,
            title=body.title,
            due_at=body.due_at,
            remind_at=body.remind_at,
        )
    except PendencyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return PendencyActionResult(kind=result["kind"], id=result["id"], status=result["status"])


@router.post("/follow_up/{item_id}/resolve", response_model=PendencyActionResult)
async def resolve_followup_endpoint(
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PendencyActionResult:
    try:
        result = await resolve_followup(session, current, item_id)
    except PendencyActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return PendencyActionResult(kind="follow_up", id=result["id"], status=result["status"])
