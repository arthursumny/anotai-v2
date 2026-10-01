"""add_user_mcp_servers

Revision ID: 54d48e41b5b3
Revises: d86cdd3ca016
Create Date: 2026-09-22 16:10:26.567354

Gerada via `alembic revision --autogenerate` e ajustada: mantém somente a
criação de `user_mcp_servers` (o restante eram diffs espúrios de renderização
TEXT/VARCHAR e FKs sem nome das migrações anteriores).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '54d48e41b5b3'
down_revision = 'd86cdd3ca016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_mcp_servers',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('server_url', sa.String(length=2000), nullable=False),
        sa.Column('auth_header_key', sa.String(length=100), nullable=True),
        sa.Column('auth_secret_name', sa.String(length=100), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_user_mcp_servers_user_id', 'user_mcp_servers', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_user_mcp_servers_user_id', table_name='user_mcp_servers')
    op.drop_table('user_mcp_servers')
