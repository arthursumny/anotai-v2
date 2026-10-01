"""Create personal workspaces and OWNER memberships for existing users."""

import uuid

from alembic import op
import sqlalchemy as sa


revision = "2e9f3c7a1b64"
down_revision = "7b4a5ca13f21"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_table("workspaces"):
        op.create_table(
            "workspaces",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("is_personal", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_table("workspace_memberships"):
        op.create_table(
            "workspace_memberships",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("workspace_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False, server_default="OWNER"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership_user"),
        )
        op.create_index("ix_workspace_memberships_workspace_id", "workspace_memberships", ["workspace_id"])
        op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"])
    if not _has_column("users", "workspace_id"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column(
                "workspace_id", sa.Uuid(),
                sa.ForeignKey("workspaces.id", name="fk_users_workspace_id_workspaces"),
                nullable=True,
            ))
        op.create_index(
            "ix_users_workspace_id", "users", ["workspace_id"], if_not_exists=True,
        )

    bind = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
    )
    workspaces = sa.table(
        "workspaces",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("is_personal", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    memberships = sa.table(
        "workspace_memberships",
        sa.column("id", sa.Uuid()),
        sa.column("workspace_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("role", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = sa.func.current_timestamp()
    for user in bind.execute(sa.select(users.c.id, users.c.workspace_id)).mappings():
        workspace_id = user["workspace_id"]
        if workspace_id is None:
            workspace_id = uuid.uuid4()
            bind.execute(sa.insert(workspaces).values(
                id=workspace_id, name="Pessoal", is_personal=True, created_at=now,
            ))
            bind.execute(sa.update(users).where(users.c.id == user["id"]).values(workspace_id=workspace_id))
        existing = bind.execute(sa.select(memberships.c.id).where(
            memberships.c.workspace_id == workspace_id,
            memberships.c.user_id == user["id"],
        )).first()
        if existing is None:
            bind.execute(sa.insert(memberships).values(
                id=uuid.uuid4(), workspace_id=workspace_id, user_id=user["id"],
                role="OWNER", created_at=now,
            ))


def downgrade() -> None:
    # O downgrade apaga as relações/workspaces desta revision, mas preserva os
    # registros legados de usuários e os demais dados associados a eles.
    if _has_column("users", "workspace_id"):
        op.drop_index("ix_users_workspace_id", table_name="users", if_exists=True)
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("workspace_id")
    if _has_table("workspace_memberships"):
        op.drop_index("ix_workspace_memberships_user_id", table_name="workspace_memberships")
        op.drop_index("ix_workspace_memberships_workspace_id", table_name="workspace_memberships")
        op.drop_table("workspace_memberships")
    if _has_table("workspaces"):
        op.drop_table("workspaces")
