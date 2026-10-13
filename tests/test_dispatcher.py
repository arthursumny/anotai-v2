"""AnotAI — testes da engine de variáveis e segredos do dispatcher."""

import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.user_secret_crypto import encrypt_user_secret
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user_secret import UserSecret
from app.services import integration_service
from app.services.dispatcher import (
    load_user_secrets,
    render_template_string,
    resolve_secrets_in_config,
)
from tests.automation_helpers import execute_actions, persist_generation


class FakeBot:
    last_token: str | None = None
    last_sent: dict | None = None

    def __init__(self, token: str) -> None:
        FakeBot.last_token = token

    async def send_message(self, chat_id, text):
        FakeBot.last_sent = {"chat_id": chat_id, "text": text}
        return SimpleNamespace(message_id=7)


@pytest.fixture(autouse=True)
def _reset_fake_bot():
    FakeBot.last_token = None
    FakeBot.last_sent = None


def test_render_simple_and_nested():
    ctx = {"animal_nome": "Thor", "tutor": {"nome": "Carlos"}, "peso": 12.5}
    assert render_template_string("Olá {animal_nome}!", ctx) == "Olá Thor!"
    assert render_template_string("{tutor.nome} e {animal_nome}", ctx) == "Carlos e Thor"
    assert render_template_string("Peso: {peso}kg", ctx) == "Peso: 12.5kg"


def test_render_missing_fails_explicitly():
    with pytest.raises(ValueError, match="ausente"):
        render_template_string("Dx: {diagnostico}", {})
    with pytest.raises(ValueError, match="ausente"):
        render_template_string("{a.b.c}", {"a": {"x": 1}})
    assert render_template_string("S: {sintomas}", {"sintomas": ["tosse", "febre"]}) == "S: tosse, febre"


def test_resolve_secrets_recursive():
    config = {
        "url": "https://api.exemplo.com/${TENANT}/hook",
        "headers": {"Authorization": "Bearer ${WEBHOOK_AUTH_TOKEN}", "X-N": 1},
        "nested": [{"t": "${TELEGRAM_BOT_TOKEN}"}],
    }
    resolved, missing = resolve_secrets_in_config(config, {"TENANT": "acme", "WEBHOOK_AUTH_TOKEN": "w"})
    assert missing == ["TELEGRAM_BOT_TOKEN"]
    assert resolved["url"] == "https://api.exemplo.com/acme/hook"
    assert resolved["headers"]["Authorization"] == "Bearer w"
    assert resolved["nested"][0]["t"] == "${TELEGRAM_BOT_TOKEN}"  # ausente: preservado


def _note_and_template(uid: uuid.UUID, actions: list[dict]) -> tuple[Note, Template]:
    note = Note(
        user_id=uid,
        raw_transcription="Consulta do Thor",
        structured_data={"animal_nome": "Thor", "sintomas": ["tosse"]},
        status=NoteStatus.PROCESSED,
    )
    tpl = Template(
        user_id=uid,
        title="Vet",
        semantic_trigger="consulta veterinária",
        output_schema={"type": "object"},
        actions=actions,
        is_public=False,
    )
    return note, tpl


async def test_dispatch_telegram_uses_secret_and_interpolates(monkeypatch, db_session):
    monkeypatch.setattr(integration_service, "Bot", FakeBot)
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "bot-token-real")
    uid = uuid.uuid4()
    token_secret = UserSecret(user_id=uid, key="TELEGRAM_BOT_TOKEN", value="")
    chat_secret = UserSecret(user_id=uid, key="TELEGRAM_CHAT_ID", value="")
    token_secret.value = encrypt_user_secret(uid, token_secret.id, "bot-token-real", key=settings.USER_SECRET_ENCRYPTION_KEY)
    chat_secret.value = encrypt_user_secret(uid, chat_secret.id, "12345", key=settings.USER_SECRET_ENCRYPTION_KEY)
    db_session.add(token_secret)
    db_session.add(chat_secret)
    await db_session.commit()

    note, tpl = _note_and_template(uid, [
        {"type": "telegram", "enabled": True, "config": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_CHAT_ID}",
            "template": "Atendimento de {animal_nome}",
        }}
    ])
    await persist_generation(db_session, note, tpl)
    logs = await execute_actions(db_session, note)
    assert len(logs) == 1 and logs[0].status == "succeeded"
    assert FakeBot.last_token == "bot-token-real"  # segredo resolvido, não o placeholder
    assert FakeBot.last_sent is not None
    assert "Thor" in FakeBot.last_sent["text"]
    assert FakeBot.last_sent["text"] == "Atendimento de Thor"
    assert "bot-token-real" not in str(logs[0].action_input)


async def test_dispatch_missing_secret_fails_gracefully(db_session, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    uid = uuid.uuid4()
    note, tpl = _note_and_template(uid, [
        {"type": "telegram", "enabled": True, "config": {"bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "1"}},
        {"type": "fax", "enabled": True, "config": {}},
    ])
    await persist_generation(db_session, note, tpl)
    logs = await execute_actions(db_session, note)
    assert len(logs) == 2
    assert [row.status for row in logs] == ["pending", "failed"]
    assert logs[0].last_error == "integrationexecutionerror"


async def test_copied_secret_ciphertext_cannot_be_read_by_another_user(db_session):
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    secret = UserSecret(user_id=owner_id, key="TOKEN", value="")
    secret.value = encrypt_user_secret(owner_id, secret.id, "private-token", key=settings.USER_SECRET_ENCRYPTION_KEY)
    secret.user_id = other_id
    db_session.add(secret)
    await db_session.commit()
    with pytest.raises(Exception):
        await load_user_secrets(db_session, other_id)
