"""AnotAI — quadro Kanban nativo (colunas, prazos)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.v1.notes import _get_owned_note, _note_with_logs
from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.note import KanbanColumn, Note, NoteStatus
from app.models.user import User
from app.schemas.kanban import KanbanBoard, KanbanMove
from app.schemas.note import NoteRead

router = APIRouter()


@router.get("", response_model=KanbanBoard)
async def get_board(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> KanbanBoard:
    result = await session.exec(
        select(Note)
        .where(Note.user_id == current.id, Note.kanban_status.is_not(None))
        .order_by(Note.due_date.asc(), Note.created_at.desc())  # type: ignore[arg-type]
    )
    board = KanbanBoard()
    for note in result.all():
        item = await _note_with_logs(session, note.id, current.id)
        if note.kanban_status == KanbanColumn.IN_PROGRESS.value:
            board.in_progress.append(item)
        elif note.kanban_status == KanbanColumn.DONE.value:
            board.done.append(item)
        else:
            board.todo.append(item)
    return board


@router.patch("/{note_id}/move", response_model=NoteRead)
async def move_card(
    note_id: uuid.UUID,
    data: KanbanMove,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    """Move o card de coluna e/ou ajusta o prazo (null limpa o prazo).

    Também aceita a entrada no Kanban: nota `processed` (inclusive nota
    geral) ainda fora do quadro entra direto na coluna pedida.
    """
    note = await _get_owned_note(session, note_id, current.id)
    if note.kanban_status is None:
        if note.status not in (NoteStatus.PROCESSED, NoteStatus.NO_MATCH):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Nota não está no Kanban (somente processadas podem entrar)",
            )
        # Entrada no quadro: usa a coluna pedida.
    note.kanban_status = data.kanban_status
    if "due_date" in data.model_fields_set:
        note.due_date = data.due_date
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return await _note_with_logs(session, note.id, current.id)
