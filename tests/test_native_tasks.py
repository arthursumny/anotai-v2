"""Native Task board scoping and column contracts."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete
from sqlmodel import select

from app.models.action_item import Task
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.kanban import KanbanMove
from app.schemas.task import TaskRead


async def test_board_only_exposes_active_owned_workspace_tasks(client, auth_headers, db_session):
    # Given: active tasks, hidden task statuses, and legacy Note cards.
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    other = User(name="Other", email=f"{uuid.uuid4()}@example.test", hashed_password="unused")
    workspace = Workspace(name="Other workspace")
    db_session.add_all([other, workspace])
    await db_session.flush()
    db_session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id))
    active = [
        Task(workspace_id=user.workspace_id, user_id=user.id, title=column,
             idempotency_key=column, status=status, kanban_status=column)
        for column, status in [("todo", "open"), ("in_progress", "in_progress"), ("done", "done")]
    ]
    hidden = [
        Task(workspace_id=user.workspace_id, user_id=user.id, title=status,
             idempotency_key=status, status=status)
        for status in ["suggested", "ignored", "dismissed", "consumed"]
    ]
    foreign = Task(workspace_id=workspace.id, user_id=user.id, title="Different workspace",
                   idempotency_key="foreign-workspace")
    other_task = Task(workspace_id=user.workspace_id, user_id=other.id, title="Other owner",
                      idempotency_key="foreign-owner")
    note = Note(user_id=user.id, title="Legacy card", status=NoteStatus.PROCESSED, kanban_status="todo")
    db_session.add_all([*active, *hidden, foreign, other_task, note])
    await db_session.commit()

    # When: loading the native board and attempting foreign moves.
    response = await client.get("/api/v1/kanban", headers=headers)
    moves = [
        await client.patch(f"/api/v1/kanban/{task.id}/move", headers=headers,
                           json={"kanban_status": "done"})
        for task in [foreign, other_task]
    ]

    # Then: only active current-workspace owned Tasks are visible and movable.
    assert response.status_code == 200, response.text
    board = response.json()
    assert set(board) == {"todo", "in_progress", "done"}
    for task in active:
        assert [row["id"] for row in board[task.kanban_status]] == [str(task.id)]
        assert set(board[task.kanban_status][0]) == {
            "id", "title", "summary", "status", "kanban_status", "due_at", "remind_at",
            "source_note_id", "created_at", "updated_at",
        }
    assert [response.status_code for response in moves] == [404, 404]


async def test_board_and_moves_require_workspace_membership(client, auth_headers, db_session):
    # Given: ownership without membership does not grant board access.
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    task = Task(workspace_id=user.workspace_id, user_id=user.id, title="Private", idempotency_key="private")
    db_session.add(task)
    await db_session.exec(delete(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == user.workspace_id,
        WorkspaceMembership.user_id == user.id,
    ))
    await db_session.commit()

    # When: loading the board and moving a task after membership removal.
    board = await client.get("/api/v1/kanban", headers=headers)
    move = await client.patch(f"/api/v1/kanban/{task.id}/move", headers=headers,
                              json={"kanban_status": "done"})

    # Then: no task data is exposed or changed.
    assert board.status_code == 200, board.text
    assert board.json() == {"todo": [], "in_progress": [], "done": []}
    assert move.status_code == 404
    await db_session.refresh(task)
    assert task.status == "open" and task.kanban_status == "todo"


@pytest.mark.parametrize("status", ["suggested", "ignored", "consumed"])
async def test_move_does_not_activate_hidden_tasks(status, client, auth_headers, db_session):
    # Given: a task not accepted onto the board.
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    task = Task(workspace_id=user.workspace_id, user_id=user.id, title="Hidden",
                idempotency_key=status, status=status)
    db_session.add(task)
    await db_session.commit()

    # When: trying to move that task directly.
    response = await client.patch(f"/api/v1/kanban/{task.id}/move", headers=headers,
                                  json={"kanban_status": "done"})

    # Then: acceptance cannot be bypassed by the board endpoint.
    assert response.status_code == 409
    await db_session.refresh(task)
    assert task.status == status


async def test_accept_retry_after_move_preserves_task_progress(client, auth_headers, db_session):
    # Given: an accepted task already moved to done.
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    task = Task(workspace_id=user.workspace_id, user_id=user.id, title="Completed",
                idempotency_key="completed", status="done", kanban_status="done")
    db_session.add(task)
    await db_session.commit()

    # When: an acceptance request is retried after that move.
    response = await client.post(f"/api/v1/pendencies/task/{task.id}/accept", headers=headers,
                                 json={"title": "Do not overwrite"})

    # Then: the same identity and completed state are returned.
    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "task", "id": str(task.id), "status": "done"}
    await db_session.refresh(task)
    assert task.title == "Completed" and task.kanban_status == "done"
    assert len((await db_session.exec(select(Task))).all()) == 1


def test_native_task_defaults_and_move_deadline_alias():
    # Given: a native Task without a source note or custom column.
    task = Task(workspace_id=uuid.uuid4(), user_id=uuid.uuid4(), title="Native", idempotency_key="native")
    target = datetime(2030, 1, 2, 9, tzinfo=timezone.utc)

    # When: serializing it and parsing the legacy deadline spelling.
    read = TaskRead.model_validate(task)
    move = KanbanMove.model_validate({"kanban_status": "todo", "due_date": target.isoformat()})

    # Then: responses remain canonical and field presence tracks the alias.
    assert read.kanban_status == "todo" and read.source_note_id is None
    assert move.due_at == target and "due_at" in move.model_fields_set
    assert "due_date" not in move.model_dump()
