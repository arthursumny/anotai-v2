"""MCP OAuth state and encrypted credentials."""

from alembic import op
import sqlalchemy as sa


revision = "7b4a5ca13f21"
down_revision = "7187d200fc54"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_mcp_servers", sa.Column("provider_id", sa.String(length=80), nullable=True))
    op.create_index("ix_user_mcp_servers_provider_id", "user_mcp_servers", ["provider_id"])
    op.create_unique_constraint(
        "uq_user_mcp_provider", "user_mcp_servers", ["user_id", "provider_id"]
    )

    op.create_table(
        "user_mcp_oauth_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.String(length=80), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["user_mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", name="uq_mcp_oauth_server"),
    )
    op.create_index(
        "ix_user_mcp_oauth_credentials_server_id", "user_mcp_oauth_credentials", ["server_id"]
    )
    op.create_index(
        "ix_user_mcp_oauth_credentials_provider_id", "user_mcp_oauth_credentials", ["provider_id"]
    )

    op.create_table(
        "mcp_oauth_flows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.String(length=80), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_ciphertext", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_mcp_oauth_flows_user_id", "mcp_oauth_flows", ["user_id"])
    op.create_index("ix_mcp_oauth_flows_provider_id", "mcp_oauth_flows", ["provider_id"])
    op.create_index("ix_mcp_oauth_flows_state_hash", "mcp_oauth_flows", ["state_hash"])
    op.create_index("ix_mcp_oauth_flows_expires_at", "mcp_oauth_flows", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_flows_expires_at", table_name="mcp_oauth_flows")
    op.drop_index("ix_mcp_oauth_flows_state_hash", table_name="mcp_oauth_flows")
    op.drop_index("ix_mcp_oauth_flows_provider_id", table_name="mcp_oauth_flows")
    op.drop_index("ix_mcp_oauth_flows_user_id", table_name="mcp_oauth_flows")
    op.drop_table("mcp_oauth_flows")
    op.drop_index("ix_user_mcp_oauth_credentials_provider_id", table_name="user_mcp_oauth_credentials")
    op.drop_index("ix_user_mcp_oauth_credentials_server_id", table_name="user_mcp_oauth_credentials")
    op.drop_table("user_mcp_oauth_credentials")
    op.drop_constraint("uq_user_mcp_provider", "user_mcp_servers", type_="unique")
    op.drop_index("ix_user_mcp_servers_provider_id", table_name="user_mcp_servers")
    op.drop_column("user_mcp_servers", "provider_id")
