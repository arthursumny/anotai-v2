"""Durable action-input provenance and nullable step-FK migration coverage."""

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
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlmodel import SQLModel

from app.models.action_execution import ActionExecution
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace
from app.services.action_registry import validate_action


def test_deleting_workflow_step_keeps_canonical_action_snapshot():
    engine = sa.create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        workspace = Workspace(name="Snapshot")
        session.add(workspace)
        session.flush()
        user = User(
            name="Snapshot owner", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
            workspace_id=workspace.id,
        )
        workflow = Workflow(
            workspace_id=workspace.id, user_id=user.id, title="Snapshot workflow",
            trigger_type="MANUAL",
        )
        session.add_all([user, workflow])
        session.flush()
        step = WorkflowStep(
            workflow_id=workflow.id, position=0, action_type="CREATE_TASK",
            input_template={"title": "Review"},
        )
        workflow_execution = WorkflowExecution(
            workspace_id=workspace.id, user_id=user.id, workflow_id=workflow.id,
            trigger_type="MANUAL", idempotency_key="workflow-run-1",
        )
        session.add_all([step, workflow_execution])
        session.flush()
        action_input = validate_action({"type": "CREATE_TASK", "title": "Review"}).model_dump(mode="json")
        action = ActionExecution(
            workspace_id=workspace.id,
            user_id=user.id,
            workflow_execution_id=workflow_execution.id,
            workflow_step_id=step.id,
            trigger_type="MANUAL",
            action_type="CREATE_TASK",
            idempotency_key="action-run-1",
            policy_snapshot={"automation_mode": "suggestive"},
            action_input=action_input,
            workflow_version=7,
        )
        session.add(action)
        session.commit()

        session.delete(step)
        session.commit()
        session.refresh(action)
        assert action.workflow_step_id is None
        assert action.action_input == action_input
        assert action.workflow_version == 7
        assert "token" not in str(action.action_input).lower()

    step_fk = next(iter(ActionExecution.__table__.c.workflow_step_id.foreign_keys))
    assert step_fk.target_fullname == "workflow_steps.id"
    assert step_fk.ondelete == "SET NULL"
    assert ActionExecution.__table__.c.action_input.nullable is True
    assert ActionExecution.__table__.c.workflow_version.nullable is True
    assert "ix_action_executions_workflow_step_id" in {
        index.name for index in ActionExecution.__table__.indexes
    }


def test_snapshot_migration_up_down_keeps_legacy_execution_rows():
    path = Path(__file__).parents[1] / "alembic/versions/d93b6f10a5c4_action_execution_step_snapshot.py"
    spec = importlib.util.spec_from_file_location("action_snapshot_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "b6c41e9d72a0"

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workspaces = sa.Table("workspaces", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workflows = sa.Table("workflows", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workflow_steps = sa.Table("workflow_steps", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workflow_executions = sa.Table(
        "workflow_executions", metadata, sa.Column("id", sa.Uuid(), primary_key=True),
    )
    executions = sa.Table(
        "action_executions", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_type", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(320), nullable=True),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(320), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    workspace_id, user_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(sa.insert(users).values(id=user_id))
        connection.execute(sa.insert(workspaces).values(id=workspace_id))
        connection.execute(sa.insert(executions).values(
            id=execution_id, workspace_id=workspace_id, user_id=user_id,
            workflow_execution_id=None, trigger_type="MANUAL", source_id="legacy-source",
            action_type="CREATE_TASK", idempotency_key="legacy-key", policy_snapshot={},
            status="pending", lease_expires_at=None, attempts=0, last_error=None,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        ))

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            columns = {column["name"]: column for column in sa.inspect(connection).get_columns("action_executions")}
            assert columns["workflow_step_id"]["nullable"] is True
            assert columns["action_input"]["nullable"] is True
            assert columns["workflow_version"]["nullable"] is True
            foreign_keys = sa.inspect(connection).get_foreign_keys("action_executions")
            step_fk = next(fk for fk in foreign_keys if fk["referred_table"] == "workflow_steps")
            assert step_fk.get("options", {}).get("ondelete") == "SET NULL"
            indexes = {index["name"] for index in sa.inspect(connection).get_indexes("action_executions")}
            assert "ix_action_executions_workflow_step_id" in indexes
            legacy = connection.execute(sa.text(
                "SELECT workflow_step_id, action_input, workflow_version, source_id "
                "FROM action_executions WHERE id=:id"
            ), {"id": execution_id.hex}).one()
            assert legacy.workflow_step_id is None
            assert legacy.action_input is None and legacy.workflow_version is None
            assert legacy.source_id == "legacy-source"
            migration.downgrade()

        remaining = {column["name"] for column in sa.inspect(connection).get_columns("action_executions")}
        assert not {"workflow_step_id", "action_input", "workflow_version"}.intersection(remaining)
        legacy_source = connection.execute(sa.text(
            "SELECT source_id FROM action_executions WHERE id=:id"
        ), {"id": execution_id.hex}).scalar_one()
        assert legacy_source == "legacy-source"
