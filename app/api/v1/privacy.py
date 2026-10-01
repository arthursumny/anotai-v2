"""Owner-scoped privacy summary and explicit source-data deletion."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.action_item import FollowUp, Reminder
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.privacy import PrivacyDeleteRequest, PrivacyDeleteResponse, PrivacySource, PrivacySummary
from app.services.audit import write_audit
from app.services.privacy_retention import purge_user_source_events

router = APIRouter()
_NOTICE = "notas preservadas; exclua-as em Notas se desejar"


async def _scope(session: AsyncSession, current: User):
    if current.workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace indisponível")
    membership = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == current.workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso ao workspace negado")
    return current.workspace_id


async def _count(session: AsyncSession, statement) -> int:
    return int((await session.exec(statement)).one() or 0)


def _event_scope(workspace_id, user_id, source: PrivacySource):
    predicates = [NormalizedEvent.workspace_id == workspace_id, NormalizedEvent.user_id == user_id]
    if source != "all":
        predicates.append(NormalizedEvent.source == source)
    return predicates


@router.get("/summary", response_model=PrivacySummary)
async def privacy_summary(
    source: PrivacySource = "all",
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PrivacySummary:
    workspace_id = await _scope(session, current)
    predicates = _event_scope(workspace_id, current.id, source)
    event_ids = select(NormalizedEvent.id).where(*predicates)
    events = await _count(session, select(func.count()).select_from(NormalizedEvent).where(*predicates))
    documents = await _count(session, select(func.count()).select_from(GeneratedDocument).where(
        GeneratedDocument.workspace_id == workspace_id,
        GeneratedDocument.user_id == current.id,
        GeneratedDocument.source_event_id.in_(event_ids),
    ))
    notes = await _count(session, select(func.count()).select_from(Note).where(Note.user_id == current.id))
    reminders = await _count(session, select(func.count()).select_from(Reminder).where(
        Reminder.workspace_id == workspace_id, Reminder.user_id == current.id,
        Reminder.source_event_id.in_(event_ids), Reminder.status.in_(("scheduled", "due")),
    ))
    follow_ups = await _count(session, select(func.count()).select_from(FollowUp).where(
        FollowUp.workspace_id == workspace_id, FollowUp.user_id == current.id,
        FollowUp.source_event_id.in_(event_ids), FollowUp.status.in_(("waiting", "due")),
    ))
    return PrivacySummary(source=source, events=events, generated_documents=documents,
                          notes_retained=notes, reminders_to_cancel=reminders,
                          follow_ups_to_cancel=follow_ups)


@router.post("/delete-data", response_model=PrivacyDeleteResponse)
async def delete_privacy_data(
    payload: PrivacyDeleteRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> PrivacyDeleteResponse:
    workspace_id = await _scope(session, current)
    counts = await purge_user_source_events(
        session, workspace_id, current.id, source=payload.source, cancel_active=True,
    )
    complete = counts["failed"] == 0
    await write_audit(
        session, workspace_id=workspace_id, user_id=current.id, actor="user",
        event="USER_DELETED_SOURCE_DATA", action="delete", status="success" if complete else "partial",
        source=payload.source if payload.source != "all" else "internal",
        target_kind="source_data",
        metadata={"count": counts["events"], "reason_code": "user_delete"},
    )
    if counts["failed"] > 0:
        await write_audit(
            session, workspace_id=workspace_id, user_id=current.id, actor="user",
            event="USER_DELETED_SOURCE_DATA", action="delete_failed_rows", status="partial",
            source=payload.source if payload.source != "all" else "internal",
            target_kind="source_data",
            metadata={"count": counts["failed"], "reason_code": "user_delete"},
        )
    await session.commit()
    return PrivacyDeleteResponse(
        source=payload.source, counts=counts, notes_retained=await _count(
            session, select(func.count()).select_from(Note).where(Note.user_id == current.id),
        ), complete=complete, notice=_NOTICE,
    )
