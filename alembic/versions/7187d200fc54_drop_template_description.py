"""drop_template_description

Revision ID: 7187d200fc54
Revises: a595544f6802
Create Date: 2026-09-23 17:03:33.374590

Gerada via `alembic revision --autogenerate` e ajustada: mantém somente o
drop da coluna `templates.description` (o restante eram diffs espúrios de
renderização TEXT/VARCHAR e FKs sem nome das migrações anteriores).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7187d200fc54'
down_revision = 'a595544f6802'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('templates', 'description')


def downgrade() -> None:
    op.add_column(
        'templates',
        sa.Column('description', sa.String(length=2000), nullable=False, server_default=''),
    )
