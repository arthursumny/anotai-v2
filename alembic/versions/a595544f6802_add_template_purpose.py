"""add_template_purpose

Revision ID: a595544f6802
Revises: ca4d4e901f45
Create Date: 2026-09-23 16:49:31.293909

Gerada via `alembic revision --autogenerate` e ajustada: mantém somente a
coluna `templates.purpose` (o restante eram diffs espúrios de renderização
TEXT/VARCHAR e FKs sem nome das migrações anteriores).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a595544f6802'
down_revision = 'ca4d4e901f45'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'templates',
        sa.Column('purpose', sa.String(length=1000), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_column('templates', 'purpose')
