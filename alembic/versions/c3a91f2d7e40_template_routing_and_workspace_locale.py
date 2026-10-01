"""Add template routing metadata/rules and workspace locale defaults."""

from alembic import op
import sqlalchemy as sa


revision = "c3a91f2d7e40"
down_revision = "4a2d9e71c6b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "templates",
        sa.Column("category", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "templates",
        sa.Column("use_cases", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "templates",
        sa.Column("routing_description", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "templates",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "workspaces",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
    )
    op.add_column(
        "workspaces",
        sa.Column("locale", sa.String(length=20), nullable=False, server_default="pt-BR"),
    )

    op.create_table(
        "template_routing_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("context", sa.String(length=120), nullable=True),
        sa.Column("capture_mode", sa.String(length=20), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("workspace_id", "user_id", "template_id", "priority", "enabled"):
        op.create_index(f"ix_template_routing_rules_{column}", "template_routing_rules", [column])


def downgrade() -> None:
    for column in ("enabled", "priority", "template_id", "user_id", "workspace_id"):
        op.drop_index(f"ix_template_routing_rules_{column}", table_name="template_routing_rules")
    op.drop_table("template_routing_rules")

    for column in ("version", "routing_description", "use_cases", "category"):
        op.drop_column("templates", column)
    for column in ("locale", "timezone"):
        op.drop_column("workspaces", column)
