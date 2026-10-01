"""Schema and migration coverage for generated documents and action records."""

import importlib.metadata
import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The repository's Alembic scripts directory shadows the installed package.
_alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", str(_alembic_package / "__init__.py"),
    submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

import sqlalchemy as sa
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.generated_document import GeneratedDocument
from app.models.user import User
from app.models.workspace import Workspace


async def _owner(session):
    workspace = Workspace(name="Actions", is_personal=True)
    session.add(workspace)
    await session.flush()
    user = User(
        name="Action tester", email=f"{uuid.uuid4()}@example.com", hashed_password="not-used",
        workspace_id=workspace.id,
    )
    session.add(user)
    await session.flush()
    return user, workspace


def _actions(user, workspace, key):
    return [
        Task(workspace_id=workspace.id, user_id=user.id, idempotency_key=key,
             title="Task", due_at=datetime(2025, 1, 2, tzinfo=timezone.utc)),
        Reminder(workspace_id=workspace.id, user_id=user.id, idempotency_key=key,
                 title="Reminder", remind_at=datetime(2025, 1, 3, tzinfo=timezone.utc)),
        FollowUp(workspace_id=workspace.id, user_id=user.id, idempotency_key=key,
                 title="Follow up", conversation_id="conversation", contact="person",
                 reason="Need a response", response_status="pending"),
        ActionSuggestion(workspace_id=workspace.id, user_id=user.id, idempotency_key=key,
                         title="Suggested action", confidence=0.9, suggestion_type="task"),
    ]


async def test_generated_document_and_distinct_action_models_create(db_session):
    user, workspace = await _owner(db_session)
    document = GeneratedDocument(
        workspace_id=workspace.id,
        user_id=user.id,
        template_version="v1",
        structured_data={"summary": "structured"},
        rendered_content="Rendered document",
    )
    actions = _actions(user, workspace, "stable-action-key")
    db_session.add_all([document, *actions])
    await db_session.flush()

    assert (await db_session.exec(select(GeneratedDocument))).one().rendered_content == "Rendered document"
    assert len((await db_session.exec(select(Task))).all()) == 1
    assert len((await db_session.exec(select(Reminder))).all()) == 1
    follow_up = (await db_session.exec(select(FollowUp))).one()
    assert follow_up.reason == "Need a response"
    assert follow_up.response_status == "pending"
    suggestion = (await db_session.exec(select(ActionSuggestion))).one()
    assert suggestion.confidence == pytest.approx(0.9)
    assert suggestion.suggestion_type == "task"
    assert actions[0].due_at.tzinfo is not None
    assert actions[1].remind_at.tzinfo is not None
    assert all(action.timezone == "UTC" for action in actions)


async def test_each_action_table_enforces_workspace_idempotency(db_session):
    user, workspace = await _owner(db_session)
    for action in _actions(user, workspace, "same-key-per-table"):
        db_session.add(action)
        await db_session.flush()
        duplicate = _actions(user, workspace, "same-key-per-table")[
            [Task, Reminder, FollowUp, ActionSuggestion].index(type(action))
        ]
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(duplicate)
                await db_session.flush()


def test_migration_upgrade_downgrade_preserves_legacy_notes():
    migration_path = Path(__file__).parents[1] / "alembic/versions/4a2d9e71c6b3_documents_and_action_records.py"
    spec = importlib.util.spec_from_file_location("action_models_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workspaces = sa.Table("workspaces", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    events = sa.Table("normalized_events", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    notes = sa.Table("notes", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    templates = sa.Table("templates", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    with engine.begin() as connection:
        metadata.create_all(connection)
        legacy_note_id = uuid.uuid4()
        connection.execute(sa.insert(notes).values(id=legacy_note_id))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            added = set(sa.inspect(connection).get_table_names())
            assert {
                "generated_documents", "tasks", "reminders", "follow_ups", "action_suggestions",
            }.issubset(added)
            migration.downgrade()
        tables_after = set(sa.inspect(connection).get_table_names())
        assert connection.scalar(sa.select(sa.func.count()).select_from(notes)) == 1
        assert connection.scalar(sa.select(notes.c.id)) == legacy_note_id

    assert not {
        "generated_documents", "tasks", "reminders", "follow_ups", "action_suggestions",
    }.intersection(tables_after)
