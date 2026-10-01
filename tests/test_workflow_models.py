"""Workflow/action-execution schema and additive migration tests."""

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
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationSettings
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace


async def _workspace_owner(session):
    workspace = Workspace(name="Workflow workspace")
    session.add(workspace)
    await session.flush()
    user = User(
        name="Workflow user", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=workspace.id,
    )
    session.add(user)
    await session.flush()
    return workspace, user


async def test_workflow_entities_defaults_foreign_keys_and_timezone_columns(db_session):
    workspace, user = await _workspace_owner(db_session)
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Receipt workflow",
        trigger_type="event", conditions={"source": "gmail"},
    )
    db_session.add(workflow)
    await db_session.flush()
    step = WorkflowStep(
        workflow_id=workflow.id, position=0, action_type="create_task",
        input_template={"title": "Review receipt"}, require_confirmation=True,
    )
    execution = WorkflowExecution(
        workspace_id=workspace.id, user_id=user.id, workflow_id=workflow.id,
        trigger_type="event", source_id="mail-1", idempotency_key="workflow-1",
        started_at=datetime.now(timezone.utc), metadata_json={"source": "gmail"},
    )
    db_session.add_all([step, execution])
    await db_session.flush()
    action = ActionExecution(
        workspace_id=workspace.id, user_id=user.id, workflow_execution_id=execution.id,
        trigger_type="event", source_id="mail-1", action_type="create_task",
        idempotency_key="action-1", policy_snapshot={"automation_mode": "suggestive"},
        lease_expires_at=datetime.now(timezone.utc),
    )
    settings = AutomationSettings(workspace_id=workspace.id, user_id=user.id)
    db_session.add_all([action, settings])
    await db_session.flush()

    assert workflow.enabled and workflow.version == 1
    assert step.require_confirmation and step.enabled
    assert execution.status == "pending"
    assert action.status == "pending" and action.attempts == 0
    assert action.created_at.tzinfo is not None
    assert settings.automation_mode == "suggestive"
    assert settings.action_thresholds == {} and settings.action_config == {}
    for model, columns in (
        (Workflow, ("created_at", "updated_at")),
        (WorkflowStep, ("created_at", "updated_at")),
        (WorkflowExecution, ("started_at", "finished_at", "created_at", "updated_at")),
        (ActionExecution, ("lease_expires_at", "created_at", "updated_at")),
        (AutomationSettings, ("created_at", "updated_at")),
    ):
        assert all(getattr(model, "__table__").c[column].type.timezone for column in columns)
    for table, column in (
        (Workflow, "workspace_id"), (Workflow, "user_id"),
        (WorkflowStep, "workflow_id"), (WorkflowExecution, "workflow_id"),
        (ActionExecution, "workspace_id"), (ActionExecution, "workflow_execution_id"),
        (AutomationSettings, "user_id"),
    ):
        assert getattr(table, "__table__").c[column].foreign_keys


async def test_workflow_steps_executions_settings_and_action_keys_are_scoped(db_session):
    workspace_a, user_a = await _workspace_owner(db_session)
    workspace_b, user_b = await _workspace_owner(db_session)
    workflow_a = Workflow(
        workspace_id=workspace_a.id, user_id=user_a.id, title="A", trigger_type="event",
    )
    workflow_b = Workflow(
        workspace_id=workspace_b.id, user_id=user_b.id, title="B", trigger_type="event",
    )
    db_session.add_all([workflow_a, workflow_b])
    await db_session.flush()

    db_session.add_all([
        WorkflowExecution(
            workspace_id=workspace_a.id, user_id=user_a.id, workflow_id=workflow_a.id,
            trigger_type="event", idempotency_key="same-key",
        ),
        WorkflowExecution(
            workspace_id=workspace_b.id, user_id=user_b.id, workflow_id=workflow_b.id,
            trigger_type="event", idempotency_key="same-key",
        ),
        ActionExecution(
            workspace_id=workspace_a.id, user_id=user_a.id, trigger_type="event",
            action_type="notify", idempotency_key="same-action-key",
        ),
        ActionExecution(
            workspace_id=workspace_b.id, user_id=user_b.id, trigger_type="event",
            action_type="notify", idempotency_key="same-action-key",
        ),
        AutomationSettings(workspace_id=workspace_a.id, user_id=user_a.id),
        AutomationSettings(workspace_id=workspace_b.id, user_id=user_b.id),
    ])
    await db_session.flush()

    db_session.add(WorkflowStep(workflow_id=workflow_a.id, position=0, action_type="notify"))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(WorkflowStep(workflow_id=workflow_a.id, position=0, action_type="other"))
            await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(WorkflowExecution(
                workspace_id=workspace_a.id, user_id=user_a.id, workflow_id=workflow_a.id,
                trigger_type="event", idempotency_key="same-key",
            ))
            await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ActionExecution(
                workspace_id=workspace_a.id, user_id=user_a.id, trigger_type="event",
                action_type="notify", idempotency_key="same-action-key",
            ))
            await db_session.flush()

    assert len((await db_session.exec(select(WorkflowExecution))).all()) == 2
    assert len((await db_session.exec(select(ActionExecution))).all()) == 2
    assert len((await db_session.exec(select(AutomationSettings))).all()) == 2


async def test_automation_mode_persists_semi_automatic_and_rejects_invalid_db_value(db_session):
    workspace, user = await _workspace_owner(db_session)
    settings = AutomationSettings(
        workspace_id=workspace.id,
        user_id=user.id,
        automation_mode="semi_automatic",
    )
    db_session.add(settings)
    await db_session.flush()
    await db_session.refresh(settings)
    assert settings.automation_mode == "semi_automatic"

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.exec(sa.text(
                "UPDATE automation_settings SET automation_mode = :mode WHERE id = :id"
            ), params={"mode": "unsafe_mode", "id": settings.id.hex})
    await db_session.refresh(settings)
    assert settings.automation_mode == "semi_automatic"


def test_migration_upgrade_downgrade_preserves_legacy_rows_and_has_fk_indexes():
    path = Path(__file__).parents[1] / "alembic/versions/b6c41e9d72a0_workflow_execution_schema.py"
    spec = importlib.util.spec_from_file_location("workflow_schema_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workspaces = sa.Table("workspaces", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    legacy_notes = sa.Table("notes", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    user_id, workspace_id, note_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(sa.insert(users).values(id=user_id))
        connection.execute(sa.insert(workspaces).values(id=workspace_id))
        connection.execute(sa.insert(legacy_notes).values(id=note_id))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            inspector = sa.inspect(connection)
            action_indexes = {index["name"] for index in inspector.get_indexes("action_executions")}
            assert {"ix_action_executions_status", "ix_action_executions_lease_expires_at"} <= action_indexes
            workflow_fks = inspector.get_foreign_keys("workflow_executions")
            assert {fk["referred_table"] for fk in workflow_fks} >= {"workflows", "workspaces", "users"}
            assert {column["name"] for column in inspector.get_columns("workflow_executions")} >= {
                "started_at", "finished_at",
            }
            assert "lease_expires_at" in {
                column["name"] for column in inspector.get_columns("action_executions")
            }
            migration.downgrade()
        table_names = set(sa.inspect(connection).get_table_names())
        assert connection.scalar(sa.select(sa.func.count()).select_from(legacy_notes)) == 1
    assert not {
        "workflows", "workflow_steps", "workflow_executions", "action_executions", "automation_settings",
    }.intersection(table_names)
