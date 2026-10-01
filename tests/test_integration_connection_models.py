"""Schema constraints and reversible migration coverage for Nango references."""

import importlib.metadata
import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

# The repository's alembic/ directory shadows the installed package.
_alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", str(_alembic_package / "__init__.py"), submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.user import User
from app.models.workspace import Workspace


def test_integration_connection_constraints_and_minimal_metadata():
    connection_fks = {
        (foreign_key.parent.name, foreign_key.target_fullname): foreign_key.ondelete
        for foreign_key in IntegrationConnection.__table__.foreign_keys
    }
    assert connection_fks == {
        ("workspace_id", "workspaces.id"): "CASCADE",
        ("user_id", "users.id"): "CASCADE",
    }
    watch_fk = next(iter(GmailWatchState.__table__.foreign_keys))
    assert (watch_fk.parent.name, watch_fk.target_fullname, watch_fk.ondelete) == (
        "integration_connection_id", "integration_connections.id", "CASCADE",
    )

    engine = sa.create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, Workspace.__table__, IntegrationConnection.__table__, GmailWatchState.__table__,
    ])
    now = datetime.now(timezone.utc)
    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    connection_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(sa.insert(User.__table__).values(
            id=user_id, name="Test", email="test@example.com", hashed_password="hash", created_at=now,
        ))
        connection.execute(sa.insert(Workspace.__table__).values(
            id=workspace_id, name="Test", is_personal=True, timezone="UTC", locale="pt-BR", created_at=now,
        ))
        insert = sa.insert(IntegrationConnection.__table__)
        connection.execute(insert.values(
            id=connection_id, workspace_id=workspace_id, user_id=user_id, provider="google_gmail",
            provider_config_key="gmail-config", nango_connection_id="connection-1", status="pending",
            metadata={"source": "setup"}, created_at=now, updated_at=now,
        ))
        assert connection.execute(sa.select(IntegrationConnection.__table__.c.account_email)).scalar_one() is None
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(insert.values(
                    id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id, provider="google_gmail",
                    provider_config_key="gmail-config", nango_connection_id="connection-2", status="pending",
                    metadata={}, created_at=now, updated_at=now,
                ))
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(insert.values(
                    id=uuid.uuid4(), workspace_id=workspace_id, user_id=user_id, provider="google_calendar",
                    provider_config_key="gmail-config", nango_connection_id="connection-1", status="pending",
                    metadata={}, created_at=now, updated_at=now,
                ))
        watch_insert = sa.insert(GmailWatchState.__table__)
        watch = dict(id=uuid.uuid4(), integration_connection_id=connection_id, history_id="9007199254740993",
                     status="paused", label_ids=["INBOX"], retry_count=0, updated_at=now)
        connection.execute(watch_insert.values(**watch))
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(watch_insert.values(**{**watch, "id": uuid.uuid4()}))

    column_names = set(IntegrationConnection.__table__.c.keys())
    assert "metadata" in column_names
    assert not {"access_token", "refresh_token", "token", "secret"} & column_names
    assert IntegrationConnection.__table__.c.metadata.nullable is False
    stored_metadata = engine.connect().execute(
        sa.select(IntegrationConnection.__table__.c.metadata)
    ).scalar_one()
    assert stored_metadata == {"source": "setup"}


def test_connection_migration_upgrade_downgrade_preserves_legacy_mcp_table():
    migration_path = Path(__file__).parents[1] / "alembic/versions/f4a1c9e7d2b6_nango_gmail_connection_state.py"
    spec = importlib.util.spec_from_file_location("integration_connection_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    legacy_mcp = sa.Table("user_mcp_servers", sa.MetaData(),
                          sa.Column("id", sa.String(), primary_key=True),
                          sa.Column("provider_id", sa.String()))
    legacy_mcp.create(engine)
    with engine.begin() as connection:
        connection.execute(legacy_mcp.insert().values(id="legacy", provider_id="mcp-google"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        inspector = sa.inspect(connection)
        integration_indexes = {index["name"] for index in inspector.get_indexes("integration_connections")}
        assert {
            "ix_integration_connections_workspace_id",
            "ix_integration_connections_user_id",
            "ix_integration_connections_account_email",
        } <= integration_indexes
        integration_uniques = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("integration_connections")
        }
        assert integration_uniques == {
            ("workspace_id", "user_id", "provider"),
            ("provider_config_key", "nango_connection_id"),
        }
        watch_indexes = {index["name"] for index in inspector.get_indexes("gmail_watch_states")}
        assert "ix_gmail_watch_states_integration_connection_id" in watch_indexes
        watch_uniques = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("gmail_watch_states")
        }
        assert ("integration_connection_id",) in watch_uniques
        assert {"user_mcp_servers", "integration_connections", "gmail_watch_states"} <= set(
            sa.inspect(connection).get_table_names()
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert {"user_mcp_servers"} <= set(sa.inspect(connection).get_table_names())
        assert connection.execute(sa.select(legacy_mcp.c.id)).scalar_one() == "legacy"
