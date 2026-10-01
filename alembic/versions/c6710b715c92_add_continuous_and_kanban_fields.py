"""add_continuous_and_kanban_fields

Revision ID: c6710b715c92
Revises: 54d48e41b5b3
Create Date: 2026-09-22 16:16:50.854155

Gerada via `alembic revision --autogenerate` e ajustada: mantém somente as
novas colunas de `notes` (+ índices). server_default garante backfill seguro
nas colunas NOT NULL em bancos já populados.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c6710b715c92'
down_revision = '54d48e41b5b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('notes', sa.Column('title', sa.String(length=300), nullable=False, server_default=''))
    op.add_column('notes', sa.Column('kanban_status', sa.String(length=20), nullable=True))
    op.add_column('notes', sa.Column('due_date', sa.DateTime(timezone=True), nullable=True))
    op.add_column('notes', sa.Column('reminder_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('notes', sa.Column('reminder_sent', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('notes', sa.Column('capture_mode', sa.String(length=20), nullable=False, server_default='intentional'))
    op.add_column('notes', sa.Column('suggested_action', sa.String(length=200), nullable=True))
    op.create_index('ix_notes_kanban_status', 'notes', ['kanban_status'])
    op.create_index('ix_notes_reminder_at', 'notes', ['reminder_at'])


def downgrade() -> None:
    op.drop_index('ix_notes_reminder_at', table_name='notes')
    op.drop_index('ix_notes_kanban_status', table_name='notes')
    op.drop_column('notes', 'suggested_action')
    op.drop_column('notes', 'capture_mode')
    op.drop_column('notes', 'reminder_sent')
    op.drop_column('notes', 'reminder_at')
    op.drop_column('notes', 'due_date')
    op.drop_column('notes', 'kanban_status')
    op.drop_column('notes', 'title')
