"""Focused WhatsApp crypto, model constraints, and migration coverage."""

import importlib.util
import importlib.metadata
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The repository's migration directory shadows the installed Alembic package.
_alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", str(_alembic_package / "__init__.py"), submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.config import settings
from app.core.whatsapp_crypto import (
    WhatsAppCryptoError,
    decrypt_instance_token,
    encrypt_instance_token,
    hash_webhook_secret,
    verify_webhook_secret,
)
from app.models import ConversationState, PendingConversationItem, WhatsAppInstance
from app.models.user import User
from app.models.workspace import Workspace


def test_whatsapp_token_roundtrip_and_instance_binding(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    instance_id = uuid.uuid4()
    token = "private-evolution-token"
    ciphertext = encrypt_instance_token(instance_id, token)
    assert token not in ciphertext
    assert decrypt_instance_token(instance_id, ciphertext) == token
    with pytest.raises(WhatsAppCryptoError):
        decrypt_instance_token(uuid.uuid4(), ciphertext)


def test_whatsapp_crypto_fails_closed_without_key_and_on_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(WhatsAppCryptoError):
        encrypt_instance_token(uuid.uuid4(), "secret")
    with pytest.raises(WhatsAppCryptoError):
        decrypt_instance_token(uuid.uuid4(), "ciphertext")

    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    ciphertext = encrypt_instance_token(uuid.uuid4(), "secret")
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(WhatsAppCryptoError) as error:
        decrypt_instance_token(uuid.uuid4(), ciphertext)
    assert "secret" not in str(error.value)


def test_webhook_secret_hash_and_constant_time_verification(monkeypatch):
    secret = "a webhook secret"
    digest = hash_webhook_secret(secret)
    assert digest == hash_webhook_secret(secret)
    assert secret not in digest
    assert verify_webhook_secret(secret, digest)
    assert not verify_webhook_secret("wrong", digest)


def test_model_repr_never_exposes_credentials_or_webhook_hash():
    instance = WhatsAppInstance(
        workspace_id=uuid.uuid4(), user_id=uuid.uuid4(), go_instance_name="anotai-1",
        token_ciphertext="ciphertext-private", webhook_secret_hash="hash-private",
    )
    representation = repr(instance)
    assert "ciphertext-private" not in representation
    assert "hash-private" not in representation


async def test_whatsapp_models_enforce_scoped_uniqueness(db_session):
    user = User(name="WhatsApp", email=f"{uuid.uuid4()}@example.test", hashed_password="x")
    workspace = Workspace(name="WhatsApp workspace")
    db_session.add(user)
    db_session.add(workspace)
    await db_session.commit()
    workspace_id, user_id = workspace.id, user.id
    instance = WhatsAppInstance(
        workspace_id=workspace.id, user_id=user.id, go_instance_name=f"go-{uuid.uuid4()}",
        token_ciphertext="encrypted", webhook_secret_hash="0" * 64,
    )
    db_session.add(instance)
    await db_session.commit()
    instance_id = instance.id

    state = ConversationState(
        workspace_id=workspace_id, user_id=user_id, instance_id=instance_id,
        conversation_id="person@s.whatsapp.net",
    )
    db_session.add(state)
    await db_session.commit()
    db_session.add(ConversationState(
        workspace_id=workspace_id, user_id=user_id, instance_id=instance_id,
        conversation_id=state.conversation_id,
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    now = datetime.now(timezone.utc)
    pending = PendingConversationItem(
        workspace_id=workspace_id, user_id=user_id, instance_id=instance_id,
        conversation_id="person@s.whatsapp.net", source_event="event-1", message_id="message-1",
        summary="A bounded question summary", asked_at=now, confidence=0.9,
    )
    db_session.add(pending)
    await db_session.commit()
    db_session.add(PendingConversationItem(
        workspace_id=workspace_id, user_id=user_id, instance_id=instance_id,
        conversation_id="person@s.whatsapp.net", source_event="event-1", message_id="message-2",
        summary="Another summary", asked_at=now, confidence=0.5,
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    assert (await db_session.exec(select(WhatsAppInstance))).first() is not None


def test_migration_upgrade_downgrade_preserves_legacy_rows():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration_path = Path(__file__).parents[1] / "alembic/versions/c81d6e5a9f20_whatsapp_foundation.py"
    spec = importlib.util.spec_from_file_location("whatsapp_foundation_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workspaces = sa.Table("workspaces", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    legacy = sa.Table("legacy_records", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)
    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(sa.insert(users), {"id": user_id})
        connection.execute(sa.insert(workspaces), {"id": workspace_id})
        connection.execute(sa.insert(legacy), {"id": 1})
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.select(legacy.c.id)).scalar_one() == 1
        assert {"whatsapp_instances", "conversation_states", "pending_conversation_items"}.issubset(
            set(sa.inspect(connection).get_table_names())
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.execute(sa.select(legacy.c.id)).scalar_one() == 1
        assert "legacy_records" in sa.inspect(connection).get_table_names()
        assert not {"whatsapp_instances", "conversation_states", "pending_conversation_items"}.intersection(
            sa.inspect(connection).get_table_names()
        )
