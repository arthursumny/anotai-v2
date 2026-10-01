"""Add Nango connection references and Gmail watch state."""

from alembic import op
import sqlalchemy as sa


revision = "f4a1c9e7d2b6"
down_revision = "d93b6f10a5c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_config_key", sa.String(length=120), nullable=False),
        sa.Column("nango_connection_id", sa.String(length=255), nullable=False),
        sa.Column("account_email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('google_gmail', 'google_calendar')", name="ck_integration_connection_provider"),
        sa.CheckConstraint("status IN ('pending', 'active', 'error', 'disconnected')", name="ck_integration_connection_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_config_key", "nango_connection_id", name="uq_integration_connection_nango_binding"),
        sa.UniqueConstraint("workspace_id", "user_id", "provider", name="uq_integration_connection_owner_provider"),
    )
    op.create_index("ix_integration_connections_workspace_id", "integration_connections", ["workspace_id"])
    op.create_index("ix_integration_connections_user_id", "integration_connections", ["user_id"])
    op.create_index("ix_integration_connections_account_email", "integration_connections", ["account_email"])

    op.create_table(
        "gmail_watch_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("integration_connection_id", sa.Uuid(), nullable=False),
        sa.Column("history_id", sa.String(length=32), nullable=True),
        sa.Column("expiration_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="paused", nullable=False),
        sa.Column("label_ids", sa.JSON(), server_default=sa.text("'[\"INBOX\"]'"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_kind", sa.String(length=80), nullable=True),
        sa.Column("page_token", sa.String(length=2048), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'needs_resync', 'paused')", name="ck_gmail_watch_state_status"),
        sa.ForeignKeyConstraint(["integration_connection_id"], ["integration_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("integration_connection_id"),
    )
    op.create_index("ix_gmail_watch_states_integration_connection_id", "gmail_watch_states", ["integration_connection_id"])


def downgrade() -> None:
    op.drop_index("ix_gmail_watch_states_integration_connection_id", table_name="gmail_watch_states")
    op.drop_table("gmail_watch_states")
    op.drop_index("ix_integration_connections_account_email", table_name="integration_connections")
    op.drop_index("ix_integration_connections_user_id", table_name="integration_connections")
    op.drop_index("ix_integration_connections_workspace_id", table_name="integration_connections")
    op.drop_table("integration_connections")
