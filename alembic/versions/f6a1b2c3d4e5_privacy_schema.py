"""Add privacy retention settings and minimal audit events."""

from alembic import op
import sqlalchemy as sa


revision = "f6a1b2c3d4e5"
down_revision = "c81d6e5a9f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("source_monitoring_policies") as batch_op:
        batch_op.add_column(sa.Column("retention_days", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_source_policy_retention_days",
            "retention_days IS NULL OR retention_days BETWEEN 1 AND 365",
        )

    with op.batch_alter_table("automation_settings") as batch_op:
        batch_op.add_column(sa.Column(
            "notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ))
        batch_op.add_column(sa.Column("quiet_start_minute", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("quiet_end_minute", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_automation_settings_quiet_start_minute",
            "quiet_start_minute IS NULL OR quiet_start_minute BETWEEN 0 AND 1439",
        )
        batch_op.create_check_constraint(
            "ck_automation_settings_quiet_end_minute",
            "quiet_end_minute IS NULL OR quiet_end_minute BETWEEN 0 AND 1439",
        )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("actor", sa.String(length=10), nullable=False),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("target_kind", sa.String(length=40), nullable=False),
        sa.Column("target_ref", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("actor IN ('user', 'system')", name="ck_audit_event_actor"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_workspace_id", "audit_events", ["workspace_id"])
    op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index(
        "ix_audit_events_workspace_created_at", "audit_events", ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_workspace_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_id", table_name="audit_events")
    op.drop_table("audit_events")

    with op.batch_alter_table("automation_settings") as batch_op:
        batch_op.drop_constraint("ck_automation_settings_quiet_end_minute", type_="check")
        batch_op.drop_constraint("ck_automation_settings_quiet_start_minute", type_="check")
        batch_op.drop_column("quiet_end_minute")
        batch_op.drop_column("quiet_start_minute")
        batch_op.drop_column("notifications_enabled")

    with op.batch_alter_table("source_monitoring_policies") as batch_op:
        batch_op.drop_constraint("ck_source_policy_retention_days", type_="check")
        batch_op.drop_column("retention_days")
