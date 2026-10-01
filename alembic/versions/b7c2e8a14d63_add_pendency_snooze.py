"""Add snooze timestamps to pending-item tables."""

from alembic import op
import sqlalchemy as sa


revision = "b7c2e8a14d63"
down_revision = "f4a1c9e7d2b6"
branch_labels = None
depends_on = None


_TABLES = ("notes", "tasks", "reminders", "action_suggestions", "follow_ups")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True))
        op.create_index(f"ix_{table}_snoozed_until", table, ["snoozed_until"])


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_index(f"ix_{table}_snoozed_until", table_name=table)
        op.drop_column(table, "snoozed_until")
