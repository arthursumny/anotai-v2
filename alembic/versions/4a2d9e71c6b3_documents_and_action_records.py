"""Add rendered documents and distinct action records."""

from alembic import op
import sqlalchemy as sa


revision = "4a2d9e71c6b3"
down_revision = "8f31c2a6d940"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=True),
        sa.Column("note_id", sa.Uuid(), nullable=True),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("template_version", sa.String(length=80), nullable=False),
        sa.Column("structured_data", sa.JSON(), nullable=False),
        sa.Column("rendered_content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_event_id"], ["normalized_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["template_id"], ["templates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("workspace_id", "user_id", "source_event_id", "note_id", "template_id"):
        op.create_index(f"ix_generated_documents_{column}", "generated_documents", [column])

    _create_action_table("tasks", "uq_tasks_workspace_key", reminder_required=False)
    _create_action_table("reminders", "uq_reminders_workspace_key", reminder_required=True)
    _create_action_table(
        "follow_ups", "uq_follow_ups_workspace_key", reminder_required=False, follow_up=True,
    )
    _create_action_table(
        "action_suggestions", "uq_action_suggestions_workspace_key", reminder_required=False,
        suggestion=True,
    )


def _create_action_table(
    table: str,
    unique_name: str,
    *,
    reminder_required: bool,
    follow_up: bool = False,
    suggestion: bool = False,
) -> None:
    columns = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=320), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=not reminder_required),
        sa.Column("timezone", sa.String(length=64), nullable=False),
    ]
    if follow_up:
        columns.extend([
            sa.Column("conversation_id", sa.String(length=320), nullable=True),
            sa.Column("contact", sa.String(length=320), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("response_status", sa.String(length=30), nullable=False),
        ])
    if suggestion:
        columns.append(sa.Column("suggestion_type", sa.String(length=40), nullable=False))
    columns.extend([
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_event_id"], ["normalized_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name=unique_name),
    ])
    op.create_table(table, *columns)

    index_columns = ["workspace_id", "user_id", "source_event_id", "due_at", "remind_at"]
    if follow_up:
        index_columns.append("conversation_id")
    for column in index_columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table in ("action_suggestions", "follow_ups", "reminders", "tasks"):
        columns = ["workspace_id", "user_id", "source_event_id", "due_at", "remind_at"]
        if table == "follow_ups":
            columns.append("conversation_id")
        for column in columns:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
    for column in ("template_id", "note_id", "source_event_id", "user_id", "workspace_id"):
        op.drop_index(f"ix_generated_documents_{column}", table_name="generated_documents")
    op.drop_table("generated_documents")
