"""Encrypt user-owned secrets at rest.

Downgrading decrypts these values back to plaintext and should only be done
with explicit acknowledgement of that security regression.
"""

from alembic import op
import sqlalchemy as sa

from app.core.config import settings
from app.core.user_secret_crypto import (
    UserSecretCryptoError,
    decrypt_user_secret,
    encrypt_user_secret,
    is_encrypted_user_secret,
)


revision = "9c2e7f4a1b63"
down_revision = "f6a1b2c3d4e5"
branch_labels = None
depends_on = None


def _validate_key() -> str:
    key = settings.USER_SECRET_ENCRYPTION_KEY
    # Encrypt a dummy payload to validate that this is a usable Fernet key.
    encrypt_user_secret(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "validation",
        key=key,
    )
    return key


def upgrade() -> None:
    # Must be the first operation: missing/invalid key aborts before schema/data changes.
    key = _validate_key()
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, user_id, value FROM user_secrets")).mappings().all()

    # Validate all reserved-format values before changing schema or any row.
    for row in rows:
        value = row["value"]
        if is_encrypted_user_secret(value):
            if not value.startswith("enc:v1:"):
                raise UserSecretCryptoError("Protected user secret could not be read.")
            decrypt_user_secret(row["user_id"], row["id"], value, key=key)

    with op.batch_alter_table("user_secrets") as batch_op:
        batch_op.alter_column(
            "value", existing_type=sa.String(length=4000), type_=sa.Text(), existing_nullable=False
        )

    for row in rows:
        value = row["value"]
        if not is_encrypted_user_secret(value):
            encrypted = encrypt_user_secret(row["user_id"], row["id"], value, key=key)
            connection.execute(
                sa.text("UPDATE user_secrets SET value = :value WHERE id = :id"),
                {"value": encrypted, "id": row["id"]},
            )


def downgrade() -> None:
    # WARNING: this downgrade restores plaintext values in the database.
    key = _validate_key()
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, user_id, value FROM user_secrets")).mappings().all()
    plaintext_rows: list[tuple[object, str]] = []
    for row in rows:
        value = row["value"]
        if not isinstance(value, str) or not value.startswith("enc:v1:"):
            raise UserSecretCryptoError("Protected user secret could not be read.")
        plaintext = decrypt_user_secret(row["user_id"], row["id"], value, key=key)
        if len(plaintext) > 4000:
            raise UserSecretCryptoError("Protected user secret exceeds downgrade limit.")
        plaintext_rows.append((row["id"], plaintext))

    for secret_id, plaintext in plaintext_rows:
        connection.execute(
            sa.text("UPDATE user_secrets SET value = :value WHERE id = :id"),
            {"value": plaintext, "id": secret_id},
        )
    with op.batch_alter_table("user_secrets") as batch_op:
        batch_op.alter_column(
            "value", existing_type=sa.Text(), type_=sa.String(length=4000), existing_nullable=False
        )
