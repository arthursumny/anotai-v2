"""Prevent duplicate generated documents per workspace note."""

from alembic import op


revision = "ae52d0193bc7"
down_revision = "c3a91f2d7e40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generated_documents") as batch_op:
        batch_op.create_unique_constraint(
            "uq_generated_documents_workspace_note", ["workspace_id", "note_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("generated_documents") as batch_op:
        batch_op.drop_constraint("uq_generated_documents_workspace_note", type_="unique")
