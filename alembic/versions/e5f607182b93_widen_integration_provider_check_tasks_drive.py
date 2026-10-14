"""Widen integration_connections provider check to include google_tasks/google_drive.

Idempotent-safe and lossless: this migration only widens the allowed-value set
of the existing CHECK constraint ``ck_integration_connection_provider``. It
reads and writes no data, so no existing row can become invalid (the new set is
a superset of the old one) and nothing is backfilled. Dropping the constraint
by name and recreating it under the same name is deterministic: re-running the
body always yields the same constraint. The unique constraint
``uq_integration_connection_owner_provider`` (one connection per provider per
user) and the status CHECK are intentionally left untouched.
"""

from alembic import op


revision = "e5f607182b93"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


_TABLE = "integration_connections"
_CONSTRAINT = "ck_integration_connection_provider"
_CONDITION = (
    "provider IN ('google_gmail', 'google_calendar', 'google_sheets', "
    "'google_tasks', 'google_drive', 'notion', 'slack')"
)


def upgrade() -> None:
    # Alembic >=1.13 aceita type_="check"; o drop é por nome, o create recria
    # o mesmo nome com o conjunto ampliado de valores permitidos.
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _CONDITION)


def downgrade() -> None:
    # Reversão estrita: volta ao conjunto anterior (sem tasks/drive).
    # Só é segura se não existirem linhas com os provedores google_tasks ou
    # google_drive.
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "provider IN ('google_gmail', 'google_calendar', 'google_sheets', 'notion', 'slack')",
    )
