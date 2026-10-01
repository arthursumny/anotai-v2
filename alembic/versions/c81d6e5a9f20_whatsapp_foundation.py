"""Create WhatsApp instance and conversation foundation tables."""

from alembic import op
import sqlalchemy as sa


revision = "c81d6e5a9f20"
down_revision = "b7c2e8a14d63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("go_instance_name", sa.String(120), nullable=False),
        sa.Column("go_instance_id", sa.String(255), nullable=True),
        sa.Column("token_ciphertext", sa.String(), nullable=False),
        sa.Column("webhook_secret_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("phone_jid", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending_qr', 'connected', 'disconnected', 'error')", name="ck_whatsapp_instance_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("go_instance_name", name="uq_whatsapp_instance_go_name"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_whatsapp_instance_workspace_user"),
    )
    op.create_index("ix_whatsapp_instances_workspace_id", "whatsapp_instances", ["workspace_id"])
    op.create_index("ix_whatsapp_instances_user_id", "whatsapp_instances", ["user_id"])

    op.create_table(
        "conversation_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("last_incoming_message_id", sa.String(255), nullable=True),
        sa.Column("last_outgoing_message_id", sa.String(255), nullable=True),
        sa.Column("last_processed_message_id", sa.String(255), nullable=True),
        sa.Column("last_incoming_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outgoing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source = 'whatsapp'", name="ck_conversation_state_source"),
        sa.ForeignKeyConstraint(["instance_id"], ["whatsapp_instances.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", "instance_id", "conversation_id", name="uq_conversation_state_owner_instance_conversation"),
    )
    for column in ("workspace_id", "user_id", "instance_id"):
        op.create_index(f"ix_conversation_states_{column}", "conversation_states", [column])

    op.create_table(
        "pending_conversation_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("source_event", sa.String(255), nullable=False),
        sa.Column("message_id", sa.String(255), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source = 'whatsapp'", name="ck_pending_conversation_source"),
        sa.CheckConstraint("status IN ('WAITING_FOR_RESPONSE', 'RESOLVED', 'DUE')", name="ck_pending_conversation_status"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_pending_conversation_confidence"),
        sa.ForeignKeyConstraint(["instance_id"], ["whatsapp_instances.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "source_event", name="uq_pending_conversation_workspace_event"),
    )
    for column in ("workspace_id", "user_id", "instance_id", "remind_at"):
        op.create_index(f"ix_pending_conversation_items_{column}", "pending_conversation_items", [column])


def downgrade() -> None:
    for column in ("workspace_id", "user_id", "instance_id", "remind_at"):
        op.drop_index(f"ix_pending_conversation_items_{column}", table_name="pending_conversation_items")
    op.drop_table("pending_conversation_items")
    for column in ("workspace_id", "user_id", "instance_id"):
        op.drop_index(f"ix_conversation_states_{column}", table_name="conversation_states")
    op.drop_table("conversation_states")
    op.drop_index("ix_whatsapp_instances_user_id", table_name="whatsapp_instances")
    op.drop_index("ix_whatsapp_instances_workspace_id", table_name="whatsapp_instances")
    op.drop_table("whatsapp_instances")
