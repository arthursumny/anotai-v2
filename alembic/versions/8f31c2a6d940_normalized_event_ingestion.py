"""Add normalized events and source monitoring policies."""

from alembic import op
import sqlalchemy as sa


revision = "8f31c2a6d940"
down_revision = "2e9f3c7a1b64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "normalized_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("external_id", sa.String(length=320), nullable=True),
        sa.Column("conversation_id", sa.String(length=320), nullable=True),
        sa.Column("sender", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source NOT IN ('whatsapp', 'gmail') OR external_id IS NOT NULL",
            name="ck_normalized_event_external_id",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "source", "external_id", name="uq_normalized_event_external"),
    )
    op.create_index("ix_normalized_events_workspace_id", "normalized_events", ["workspace_id"])
    op.create_index("ix_normalized_events_user_id", "normalized_events", ["user_id"])
    op.create_index("ix_normalized_events_source", "normalized_events", ["source"])
    op.create_index("ix_normalized_events_conversation_id", "normalized_events", ["conversation_id"])

    op.create_table(
        "source_monitoring_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("selected_ids", sa.JSON(), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("allow_groups", sa.Boolean(), nullable=False),
        sa.Column("allow_individuals", sa.Boolean(), nullable=False),
        sa.Column("gmail_label_ids", sa.JSON(), nullable=False),
        sa.Column("gmail_allowed_senders", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "user_id", "source", name="uq_source_policy_workspace_user_source",
        ),
    )
    op.create_index("ix_source_monitoring_policies_workspace_id", "source_monitoring_policies", ["workspace_id"])
    op.create_index("ix_source_monitoring_policies_user_id", "source_monitoring_policies", ["user_id"])
    op.create_index("ix_source_monitoring_policies_source", "source_monitoring_policies", ["source"])


def downgrade() -> None:
    op.drop_index("ix_source_monitoring_policies_source", table_name="source_monitoring_policies")
    op.drop_index("ix_source_monitoring_policies_user_id", table_name="source_monitoring_policies")
    op.drop_index("ix_source_monitoring_policies_workspace_id", table_name="source_monitoring_policies")
    op.drop_table("source_monitoring_policies")
    op.drop_index("ix_normalized_events_conversation_id", table_name="normalized_events")
    op.drop_index("ix_normalized_events_source", table_name="normalized_events")
    op.drop_index("ix_normalized_events_user_id", table_name="normalized_events")
    op.drop_index("ix_normalized_events_workspace_id", table_name="normalized_events")
    op.drop_table("normalized_events")
