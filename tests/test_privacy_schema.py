"""Privacy schema, migration, and retention setting coverage."""

import importlib.metadata
import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlmodel import SQLModel

# The repository's alembic/ folder shadows the installed package on sys.path.
_alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", _alembic_package / "__init__.py", submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.core.config import Settings
from app.models import AuditEvent, AutomationSettings, SourceMonitoringPolicy


def _load_migration():
    path = Path(__file__).parents[1] / "alembic/versions/f6a1b2c3d4e5_privacy_schema.py"
    spec = importlib.util.spec_from_file_location("privacy_schema_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_privacy_model_metadata_contracts():
    policy = SQLModel.metadata.tables["source_monitoring_policies"]
    automation = SQLModel.metadata.tables["automation_settings"]
    audit = SQLModel.metadata.tables["audit_events"]

    assert policy.c.retention_days.nullable
    assert any(check.name == "ck_source_policy_retention_days" for check in policy.constraints)
    assert automation.c.notifications_enabled.default.arg is True
    assert automation.c.notifications_enabled.server_default is not None
    assert automation.c.quiet_start_minute.nullable and automation.c.quiet_end_minute.nullable
    assert audit.c.workspace_id.foreign_keys.pop().ondelete == "CASCADE"
    assert next(iter(audit.c.user_id.foreign_keys)).ondelete == "SET NULL"
    assert {"workspace_id", "created_at"} <= {
        column.name for index in audit.indexes if index.name == "ix_audit_events_workspace_created_at"
        for column in index.columns
    }
    assert audit.c.created_at.type.timezone
    assert not {"content", "title", "secret"} & set(audit.c.keys())

    required_policy = {"workspace_id": uuid.uuid4(), "user_id": uuid.uuid4(), "source": "gmail"}
    assert SourceMonitoringPolicy(**required_policy, retention_days=None).retention_days is None
    assert SourceMonitoringPolicy(**required_policy, retention_days=365).retention_days == 365
    assert AutomationSettings(workspace_id=uuid.uuid4(), user_id=uuid.uuid4()).notifications_enabled is True
    assert AuditEvent(
        workspace_id=uuid.uuid4(), actor="system", event="privacy", action="read", status="ok",
        source="api", target_kind="note",
    )


def test_privacy_migration_preserves_legacy_data_and_downgrades():
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    workspaces = sa.Table("workspaces", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    users = sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    policies = sa.Table(
        "source_monitoring_policies", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("selected_ids", sa.JSON(), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("allow_groups", sa.Boolean(), nullable=False),
        sa.Column("allow_individuals", sa.Boolean(), nullable=False),
        sa.Column("gmail_label_ids", sa.JSON(), nullable=False),
        sa.Column("gmail_allowed_senders", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    automation = sa.Table(
        "automation_settings", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("automation_mode", sa.String(20), nullable=False),
        sa.Column("action_thresholds", sa.JSON(), nullable=False),
        sa.Column("action_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    legacy = {
        name: sa.Table(name, metadata, sa.Column("id", sa.Integer(), primary_key=True), sa.Column("payload", sa.String()))
        for name in ("notes", "normalized_events", "workflows", "whatsapp_instances")
    }
    metadata.create_all(engine)
    workspace_id, user_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(workspaces.insert(), {"id": workspace_id})
        connection.execute(users.insert(), {"id": user_id})
        connection.execute(policies.insert(), {
            "id": uuid.uuid4(), "workspace_id": workspace_id, "user_id": user_id, "source": "gmail",
            "mode": "ALL", "selected_ids": [], "direction": "incoming", "allow_groups": True,
            "allow_individuals": True, "gmail_label_ids": [], "gmail_allowed_senders": [],
            "created_at": now, "updated_at": now,
        })
        connection.execute(automation.insert(), {
            "id": uuid.uuid4(), "workspace_id": workspace_id, "user_id": user_id,
            "automation_mode": "suggestive", "action_thresholds": {}, "action_config": {},
            "created_at": now, "updated_at": now,
        })
        for table in legacy.values():
            connection.execute(table.insert(), {"id": 1, "payload": "preserve-me"})

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.text("SELECT notifications_enabled FROM automation_settings")).scalar_one() in (1, True)
        assert connection.execute(sa.text("SELECT retention_days FROM source_monitoring_policies")).scalar_one() is None
        for name in legacy:
            assert connection.execute(sa.text(f"SELECT payload FROM {name}")).scalar_one() == "preserve-me"

        audit_fks = sa.inspect(connection).get_foreign_keys("audit_events")
        assert {tuple(fk["constrained_columns"]): fk["options"].get("ondelete") for fk in audit_fks} == {
            ("workspace_id",): "CASCADE", ("user_id",): "SET NULL",
        }
        with pytest.raises(sa.exc.IntegrityError):
            with connection.begin_nested():
                connection.execute(sa.text(
                    "INSERT INTO source_monitoring_policies "
                    "(id, workspace_id, user_id, source, mode, selected_ids, direction, allow_groups, "
                    "allow_individuals, gmail_label_ids, gmail_allowed_senders, created_at, updated_at, retention_days) "
                    "VALUES (:id, :workspace, :user, 'gmail', 'ALL', '[]', 'incoming', 1, 1, '[]', '[]', :now, :now, 366)"
                ), {"id": str(uuid.uuid4()), "workspace": str(workspace_id), "user": str(user_id), "now": now.isoformat()})

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "audit_events" not in sa.inspect(connection).get_table_names()
        assert "retention_days" not in {column["name"] for column in sa.inspect(connection).get_columns("source_monitoring_policies")}
        assert "notifications_enabled" not in {column["name"] for column in sa.inspect(connection).get_columns("automation_settings")}
        for name in legacy:
            assert connection.execute(sa.text(f"SELECT payload FROM {name}")).scalar_one() == "preserve-me"


@pytest.mark.parametrize("name", ["DEFAULT_EVENT_RETENTION_DAYS", "AUDIT_RETENTION_DAYS"])
@pytest.mark.parametrize("value", [0, 366])
def test_invalid_retention_config_is_rejected(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_retention_config_defaults_and_boundaries():
    settings = Settings(_env_file=None)
    assert settings.DEFAULT_EVENT_RETENTION_DAYS == 30
    assert settings.AUDIT_RETENTION_DAYS == 90
    assert Settings(_env_file=None, DEFAULT_EVENT_RETENTION_DAYS=1, AUDIT_RETENTION_DAYS=365)
