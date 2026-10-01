"""Schema guarantees for snoozable pending items."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import sqlalchemy as sa

from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.note import Note


def test_snooze_columns_are_nullable_timezone_aware_and_indexed():
    for model in (Note, Task, Reminder, ActionSuggestion, FollowUp):
        column = model.__table__.c.snoozed_until
        assert column.nullable is True
        assert column.type.timezone is True
        assert any(index.columns.keys() == ["snoozed_until"] for index in model.__table__.indexes)


def test_snooze_migration_upgrade_downgrade_preserves_legacy_notes():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "b7c2e8a14d63_add_pendency_snooze.py"
    )
    spec = importlib.util.spec_from_file_location("snooze_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for table in ("notes", "tasks", "reminders", "action_suggestions", "follow_ups"):
            connection.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO notes (id) VALUES (42)"))
        connection.execute(sa.text("INSERT INTO follow_ups (id) VALUES (84)"))

        def add_column(table, column):
            connection.execute(sa.text(
                f"ALTER TABLE {table} ADD COLUMN {column.name} DATETIME"
            ))

        def create_index(name, table, columns):
            connection.execute(sa.text(
                f"CREATE INDEX {name} ON {table} ({', '.join(columns)})"
            ))

        def drop_index(name, table_name):
            connection.execute(sa.text(f"DROP INDEX {name}"))

        def drop_column(table, column):
            connection.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))

        fake_alembic = ModuleType("alembic")
        fake_alembic.op = SimpleNamespace(
            add_column=add_column,
            create_index=create_index,
            drop_index=drop_index,
            drop_column=drop_column,
        )
        prior_alembic = sys.modules.get("alembic")
        sys.modules["alembic"] = fake_alembic
        try:
            spec.loader.exec_module(migration)
        finally:
            if prior_alembic is None:
                sys.modules.pop("alembic", None)
            else:
                sys.modules["alembic"] = prior_alembic

        migration.op = fake_alembic.op
        migration.upgrade()
        inspector = sa.inspect(connection)
        for table in ("notes", "tasks", "reminders", "action_suggestions", "follow_ups"):
            columns = {column["name"]: column for column in inspector.get_columns(table)}
            assert columns["snoozed_until"]["nullable"] is True
            assert "ix_" + table + "_snoozed_until" in {
                index["name"] for index in inspector.get_indexes(table)
            }
        assert connection.execute(sa.text("SELECT id FROM notes")).scalar_one() == 42
        assert connection.execute(sa.text("SELECT id FROM follow_ups")).scalar_one() == 84

        migration.downgrade()
        inspector = sa.inspect(connection)
        for table in ("notes", "tasks", "reminders", "action_suggestions", "follow_ups"):
            assert "snoozed_until" not in {
                column["name"] for column in inspector.get_columns(table)
            }
            assert f"ix_{table}_snoozed_until" not in {
                index["name"] for index in inspector.get_indexes(table)
            }
        assert connection.execute(sa.text("SELECT id FROM notes")).scalar_one() == 42
        assert connection.execute(sa.text("SELECT id FROM follow_ups")).scalar_one() == 84
