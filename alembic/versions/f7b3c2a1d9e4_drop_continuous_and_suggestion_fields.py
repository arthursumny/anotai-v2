"""Drop dead continuous-capture and suggestion fields from notes."""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7b3c2a1d9e4'
down_revision = 'e7c8a2d4f610'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index('ix_notes_kanban_status', table_name='notes')
    op.drop_column('notes', 'kanban_status')
    op.drop_column('notes', 'capture_mode')
    op.drop_column('notes', 'suggested_action')


def downgrade() -> None:
    op.add_column('notes', sa.Column('suggested_action', sa.String(length=200), nullable=True))
    op.add_column('notes', sa.Column('capture_mode', sa.String(length=20), nullable=False, server_default='intentional'))
    op.add_column('notes', sa.Column('kanban_status', sa.String(length=20), nullable=True))
    op.create_index('ix_notes_kanban_status', 'notes', ['kanban_status'])
