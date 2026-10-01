"""add_user_secrets

Revision ID: d86cdd3ca016
Revises: 0001_initial
Create Date: 2026-09-22 14:14:39.979162

Gerada via `alembic revision --autogenerate` e ajustada: mantém somente a
criação de `user_secrets` (o restante eram diffs espúrios de renderização
TEXT/VARCHAR e FKs sem nome da 0001_initial).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd86cdd3ca016'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_secrets',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('key', sa.String(length=100), nullable=False),
        sa.Column('value', sa.String(length=4000), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'key', name='uq_user_secrets_user_key'),
    )
    op.create_index('ix_user_secrets_user_id', 'user_secrets', ['user_id'])
    op.create_index('ix_user_secrets_key', 'user_secrets', ['key'])


def downgrade() -> None:
    op.drop_index('ix_user_secrets_key', table_name='user_secrets')
    op.drop_index('ix_user_secrets_user_id', table_name='user_secrets')
    op.drop_table('user_secrets')
