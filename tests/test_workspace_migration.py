"""Backfill and signup coverage for personal workspaces."""

import importlib.util
import importlib.metadata
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

# The repository's `alembic/` scripts directory shadows the installed package
# on sys.path. Load the installed package explicitly for migration operations.
_alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", _alembic_package / "__init__.py", submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import select

from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


def test_upgrade_backfills_personal_workspaces_idempotently():
    migration_path = Path(__file__).parents[1] / "alembic/versions/2e9f3c7a1b64_personal_workspaces.py"
    spec = importlib.util.spec_from_file_location("workspace_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    users = sa.Table(
        "users", sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    user_ids = [uuid.uuid4(), uuid.uuid4()]
    with engine.begin() as connection:
        users.create(connection)
        connection.execute(sa.insert(users), [
            {"id": user_id, "name": "Legacy", "email": f"{user_id}@test", "hashed_password": "x",
             "created_at": datetime.now(timezone.utc)}
            for user_id in user_ids
        ])
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()

        workspace_rows = connection.execute(sa.text("SELECT id FROM workspaces")).all()
        membership_rows = connection.execute(
            sa.text("SELECT workspace_id, user_id, role FROM workspace_memberships")
        ).all()
        assigned = connection.execute(sa.text("SELECT id, workspace_id FROM users")).all()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        remaining_users = connection.execute(sa.text("SELECT id FROM users")).all()
        remaining_tables = set(sa.inspect(connection).get_table_names())
        remaining_columns = {column["name"] for column in sa.inspect(connection).get_columns("users")}

    assert len(workspace_rows) == len(user_ids)
    assert len(membership_rows) == len(user_ids)
    assert {uuid.UUID(hex=row.user_id) for row in membership_rows} == set(user_ids)
    assert all(row.role == "OWNER" for row in membership_rows)
    assert {uuid.UUID(hex=row.id): uuid.UUID(hex=row.workspace_id) for row in assigned} == {
        uuid.UUID(hex=row.user_id): uuid.UUID(hex=row.workspace_id) for row in membership_rows
    }
    assert {uuid.UUID(hex=row.id) for row in remaining_users} == set(user_ids)
    assert "workspaces" not in remaining_tables
    assert "workspace_memberships" not in remaining_tables
    assert "workspace_id" not in remaining_columns


async def test_signup_creates_personal_workspace_and_owner_membership(client, db_session):
    response = await client.post("/api/v1/auth/register", json={
        "name": "New user", "email": f"{uuid.uuid4()}@example.com", "password": "senha-forte-123",
    })
    assert response.status_code == 201
    user = (await db_session.exec(select(User).where(User.id == uuid.UUID(response.json()["id"])))).one()
    workspace = (await db_session.exec(select(Workspace).where(Workspace.id == user.workspace_id))).one()
    membership = (await db_session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.workspace_id == workspace.id,
    ))).one()
    assert workspace.is_personal
    assert workspace.name == "Pessoal"
    assert membership.role == "OWNER"
