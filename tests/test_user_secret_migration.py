"""Focused coverage for the fail-closed UserSecret encryption migration."""

import importlib.util
import importlib.metadata
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

# The repository's alembic/ scripts directory shadows the installed package.
# Load the installed package explicitly, following test_workspace_migration.py.
try:
    _alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
except importlib.metadata.PackageNotFoundError:
    pytest.skip("Alembic dependency is not installed", allow_module_level=True)
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", str(_alembic_package / "__init__.py"), submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.core.config import settings
from app.core.user_secret_crypto import decrypt_user_secret, is_encrypted_user_secret


MIGRATION_PATH = Path(__file__).parents[1] / "alembic" / "versions" / "9c2e7f4a1b63_encrypt_user_secrets.py"
spec = importlib.util.spec_from_file_location("user_secret_encryption_migration", MIGRATION_PATH)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def _db():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    secrets = sa.Table(
        "user_secrets",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.String(4000), nullable=False),
    )
    metadata.create_all(engine)
    return engine, secrets


def _run(engine, operation):
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            operation()


def test_upgrade_encrypts_and_downgrade_restores_plaintext(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "USER_SECRET_ENCRYPTION_KEY", key)
    engine, secrets = _db()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(secrets.insert().values(id=sid, user_id=uid, value="original plaintext"))
    _run(engine, migration.upgrade)
    with engine.connect() as connection:
        row = connection.execute(sa.select(secrets)).one()
    encrypted = row.value
    assert is_encrypted_user_secret(encrypted)
    assert decrypt_user_secret(uid, sid, encrypted, key=key) == "original plaintext"
    # A repeated invocation validates and preserves an already encrypted row.
    _run(engine, migration.upgrade)
    with engine.connect() as connection:
        assert connection.execute(sa.select(secrets.c.value)).scalar_one() == encrypted
    _run(engine, migration.downgrade)
    with engine.connect() as connection:
        assert connection.execute(sa.select(secrets.c.value)).scalar_one() == "original plaintext"
    engine.dispose()


def test_upgrade_without_key_aborts_before_changes(monkeypatch):
    engine, secrets = _db()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(secrets.insert().values(id=sid, user_id=uid, value="leave as is"))
    original_type = sa.inspect(engine).get_columns("user_secrets")[2]["type"]
    monkeypatch.setattr(settings, "USER_SECRET_ENCRYPTION_KEY", "")
    with pytest.raises(Exception):
        _run(engine, migration.upgrade)
    with engine.connect() as connection:
        assert connection.execute(sa.select(secrets.c.value)).scalar_one() == "leave as is"
    current_type = sa.inspect(engine).get_columns("user_secrets")[2]["type"]
    assert type(current_type) is type(original_type)
    assert getattr(current_type, "length", None) == getattr(original_type, "length", None) == 4000
    engine.dispose()
