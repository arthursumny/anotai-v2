"""AnotAI — inbox de triagem das sugestões do modo contínuo."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.v1.notes import _get_owned_note, _note_with_logs
from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.note import KanbanColumn, Note, NoteStatus
from app.models.user import User
from app.schemas.kanban import InboxAccept
from app.schemas.note import NoteRead

router = APIRouter()


@router.get("/suggested", response_model=list[NoteRead])
async def list_suggested(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[NoteRead]:
    result = await session.exec(
        select(Note)
        .where(Note.user_id == current.id, Note.status == NoteStatus.SUGGESTED)
        .order_by(Note.created_at.asc())  # type: ignore[arg-type]
        .offset(offset)
        .limit(limit)
    )
    out: list[NoteRead] = []
    for note in result.all():
        out.append(await _note_with_logs(session, note.id, current.id))
    return out


@router.post("/{note_id}/accept", response_model=NoteRead)
async def accept_suggestion(
    note_id: uuid.UUID,
    data: InboxAccept | None = None,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    """Aceita a sugestão: vira `processed` e entra no Kanban (default `todo`).

    `reminder_at` informado passa a ser consumido pelo cron de lembretes.
    """
    note = await _get_owned_note(session, note_id, current.id)
    if note.status != NoteStatus.SUGGESTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Somente sugestões (status=suggested) podem ser aceitas",
        )
    accept = data or InboxAccept()
    note.status = NoteStatus.PROCESSED
    note.kanban_status = accept.kanban_status or KanbanColumn.TODO.value
    if accept.due_date is not None:
        note.due_date = accept.due_date
    if accept.reminder_at is not None:
        note.reminder_at = accept.reminder_at
        note.reminder_sent = False
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return await _note_with_logs(session, note.id, current.id)


@router.post("/{note_id}/discard", response_model=NoteRead)
async def discard_suggestion(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    """Arquiva a sugestão (`discarded`): sai da inbox sem ir ao Kanban."""
    note = await _get_owned_note(session, note_id, current.id)
    if note.status != NoteStatus.SUGGESTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Somente sugestões (status=suggested) podem ser descartadas",
        )
    note.status = NoteStatus.DISCARDED
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return await _note_with_logs(session, note.id, current.id)
