"""Persist canonical action input and workflow-step/version provenance."""

from alembic import op
import sqlalchemy as sa


revision = "d93b6f10a5c4"
down_revision = "b6c41e9d72a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("action_executions") as batch_op:
        batch_op.add_column(sa.Column(
            "workflow_step_id", sa.Uuid(),
            sa.ForeignKey(
                "workflow_steps.id", name="fk_action_executions_workflow_step_id_workflow_steps",
                ondelete="SET NULL",
            ),
            nullable=True,
        ))
        batch_op.add_column(sa.Column("action_input", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("workflow_version", sa.Integer(), nullable=True))
    op.create_index(
        "ix_action_executions_workflow_step_id", "action_executions", ["workflow_step_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_action_executions_workflow_step_id", table_name="action_executions")
    with op.batch_alter_table("action_executions") as batch_op:
        batch_op.drop_column("workflow_version")
        batch_op.drop_column("action_input")
        batch_op.drop_column("workflow_step_id")
