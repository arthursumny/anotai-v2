"""AnotAI — quadro Kanban nativo (colunas, prazos)."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import asc, desc
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.action_item import Task
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.kanban import KanbanBoard, KanbanMove
from app.schemas.task import TaskRead

router = APIRouter()


@router.get("", response_model=KanbanBoard)
async def get_board(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> KanbanBoard:
    result = await session.exec(
        select(Task)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Task.workspace_id)
        .where(
            Task.user_id == current.id,
            Task.workspace_id == current.workspace_id,
            WorkspaceMembership.user_id == current.id,
            Task.status.in_(["open", "in_progress", "done"]),
        )
        .order_by(asc(Task.due_at), desc(Task.created_at))
    )
    board = KanbanBoard()
    columns = {"todo": board.todo, "in_progress": board.in_progress, "done": board.done}
    for task in result.all():
        item = TaskRead.model_validate(task)
        columns[item.kanban_status].append(item)
    return board


@router.patch("/{task_id}/move", response_model=TaskRead)
async def move_card(
    task_id: uuid.UUID,
    data: KanbanMove,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> TaskRead:
    """Move an active native task; an explicit null clears its deadline."""
    task = (await session.exec(
        select(Task)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Task.workspace_id)
        .where(
            Task.id == task_id,
            Task.user_id == current.id,
            Task.workspace_id == current.workspace_id,
            WorkspaceMembership.user_id == current.id,
        )
    )).first()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status not in {"open", "in_progress", "done"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is not on the Kanban board")
    task.kanban_status = data.kanban_status
    task.status = {"todo": "open", "in_progress": "in_progress", "done": "done"}[data.kanban_status]
    if "due_at" in data.model_fields_set:
        task.due_at = data.due_at
    task.updated_at = datetime.now(timezone.utc)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return TaskRead.model_validate(task)
