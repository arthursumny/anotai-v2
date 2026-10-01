"""add_template_example_output

Revision ID: ca4d4e901f45
Revises: c6710b715c92
Create Date: 2026-09-23 14:31:40.151168

Gerada via `alembic revision --autogenerate` e ajustada: mantém somente a
coluna `templates.example_output` (o restante eram diffs espúrios de
renderização TEXT/VARCHAR e FKs sem nome das migrações anteriores).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ca4d4e901f45'
down_revision = 'c6710b715c92'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'templates',
        sa.Column('example_output', sa.String(length=8000), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_column('templates', 'example_output')
