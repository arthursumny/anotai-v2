"""AnotAI — testes da engine de variáveis e segredos do dispatcher."""

import uuid
from types import SimpleNamespace

import pytest

import app.services.dispatcher as dispatcher
from app.core.config import settings
from app.core.user_secret_crypto import encrypt_user_secret
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user_secret import UserSecret
from app.services.dispatcher import (
    dispatch_actions,
    load_user_secrets,
    render_template_string,
    resolve_secrets_in_config,
)


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


def test_render_missing_falls_back_gracefully():
    assert render_template_string("Dx: {diagnostico}", {}) == "Dx: N/A"
    assert render_template_string("{a.b.c}", {"a": {"x": 1}}) == "N/A"
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
    monkeypatch.setattr(dispatcher, "Bot", FakeBot)
    uid = uuid.uuid4()
    token_secret = UserSecret(user_id=uid, key="TELEGRAM_BOT_TOKEN", value="")
    chat_secret = UserSecret(user_id=uid, key="TELEGRAM_CHAT_ID", value="")
    token_secret.value = encrypt_user_secret(uid, token_secret.id, "bot-token-real", key=settings.USER_SECRET_ENCRYPTION_KEY)
    chat_secret.value = encrypt_user_secret(uid, chat_secret.id, "12345", key=settings.USER_SECRET_ENCRYPTION_KEY)
    db_session.add(token_secret)
    db_session.add(chat_secret)
    await db_session.commit()

    note, tpl = _note_and_template(uid, [
        {"type": "telegram", "config": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "chat_id": "${TELEGRAM_CHAT_ID}",
            "template": "Atendimento de {animal_nome} ({ausente})",
        }}
    ])
    logs = await dispatch_actions(note, tpl, db_session)
    assert len(logs) == 1 and logs[0].status.value == "success"
    assert FakeBot.last_token == "bot-token-real"  # segredo resolvido, não o placeholder
    assert FakeBot.last_sent is not None
    assert "Thor" in FakeBot.last_sent["text"]
    assert "N/A" in FakeBot.last_sent["text"]  # variável ausente não quebra
    assert "bot-token-real" not in str(logs[0].payload)  # segredo não vaza no log


async def test_dispatch_missing_secret_fails_gracefully(db_session):
    uid = uuid.uuid4()
    note, tpl = _note_and_template(uid, [
        {"type": "telegram", "config": {"bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "1"}},
        {"type": "fax", "config": {}},
    ])
    logs = await dispatch_actions(note, tpl, db_session)  # nunca levanta
    assert len(logs) == 2
    assert all(lg.status.value == "failed" for lg in logs)
    assert "Segredo ${TELEGRAM_BOT_TOKEN} não configurado pelo usuário" in (logs[0].error_message or "")


async def test_copied_secret_ciphertext_cannot_be_read_by_another_user(db_session):
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    secret = UserSecret(user_id=owner_id, key="TOKEN", value="")
    secret.value = encrypt_user_secret(owner_id, secret.id, "private-token", key=settings.USER_SECRET_ENCRYPTION_KEY)
    secret.user_id = other_id
    db_session.add(secret)
    await db_session.commit()
    with pytest.raises(Exception):
        await load_user_secrets(db_session, other_id)
