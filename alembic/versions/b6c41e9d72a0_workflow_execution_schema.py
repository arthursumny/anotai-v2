"""Add declarative workflows and idempotent execution state."""

from alembic import op
import sqlalchemy as sa


revision = "b6c41e9d72a0"
down_revision = "ae52d0193bc7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.String(length=50), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("workspace_id", "user_id", "trigger_type", "enabled"):
        op.create_index(f"ix_workflows_{column}", "workflows", [column])

    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("input_template", sa.JSON(), nullable=False),
        sa.Column("require_confirmation", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "position", name="uq_workflow_steps_position"),
    )
    op.create_index("ix_workflow_steps_workflow_id", "workflow_steps", ["workflow_id"])
    op.create_index("ix_workflow_steps_enabled", "workflow_steps", ["enabled"])

    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_type", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=320), nullable=True),
        sa.Column("idempotency_key", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "workflow_id", "idempotency_key",
            name="uq_workflow_execution_idempotency",
        ),
    )
    for column in ("workspace_id", "user_id", "workflow_id", "trigger_type", "status", "finished_at"):
        op.create_index(f"ix_workflow_executions_{column}", "workflow_executions", [column])

    op.create_table(
        "action_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_execution_id", sa.Uuid(), nullable=True),
        sa.Column("trigger_type", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=320), nullable=True),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=320), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'succeeded', 'failed', 'awaiting_confirmation')",
            name="ck_action_execution_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_action_execution_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workflow_execution_id"], ["workflow_executions.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_action_execution_workspace_key"),
    )
    for column in (
        "workspace_id", "user_id", "workflow_execution_id", "trigger_type", "action_type",
        "status", "lease_expires_at",
    ):
        op.create_index(f"ix_action_executions_{column}", "action_executions", [column])

    op.create_table(
        "automation_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("automation_mode", sa.String(length=20), nullable=False),
        sa.Column("action_thresholds", sa.JSON(), nullable=False),
        sa.Column("action_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "automation_mode IN ('suggestive', 'semi_automatic', 'automatic', 'disabled')",
            name="ck_automation_settings_mode",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name="uq_automation_settings_workspace_user",
        ),
    )
    op.create_index("ix_automation_settings_workspace_id", "automation_settings", ["workspace_id"])
    op.create_index("ix_automation_settings_user_id", "automation_settings", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_automation_settings_user_id", table_name="automation_settings")
    op.drop_index("ix_automation_settings_workspace_id", table_name="automation_settings")
    op.drop_table("automation_settings")
    for column in (
        "lease_expires_at", "status", "action_type", "trigger_type", "workflow_execution_id",
        "user_id", "workspace_id",
    ):
        op.drop_index(f"ix_action_executions_{column}", table_name="action_executions")
    op.drop_table("action_executions")
    for column in ("finished_at", "status", "trigger_type", "workflow_id", "user_id", "workspace_id"):
        op.drop_index(f"ix_workflow_executions_{column}", table_name="workflow_executions")
    op.drop_table("workflow_executions")
    op.drop_index("ix_workflow_steps_enabled", table_name="workflow_steps")
    op.drop_index("ix_workflow_steps_workflow_id", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    for column in ("enabled", "trigger_type", "user_id", "workspace_id"):
        op.drop_index(f"ix_workflows_{column}", table_name="workflows")
    op.drop_table("workflows")
