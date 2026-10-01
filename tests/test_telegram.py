"""AnotAI — testes do Telegram da plataforma (info do bot + mensagem de teste)."""

from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

import app.api.v1.telegram as telegram_api


class FakeBot:
    username_to_return = "AnotAiOficialBot"
    fail_with: Exception | None = None
    last_sent: dict | None = None

    def __init__(self, token: str) -> None:
        self.token = token

    async def get_me(self):
        if FakeBot.fail_with is not None:
            raise FakeBot.fail_with
        return SimpleNamespace(username=FakeBot.username_to_return)

    async def send_message(self, chat_id, text):
        if FakeBot.fail_with is not None:
            raise FakeBot.fail_with
        FakeBot.last_sent = {"chat_id": chat_id, "text": text}
        return SimpleNamespace(message_id=1)


@pytest.fixture(autouse=True)
def _patch_bot(monkeypatch):
    monkeypatch.setattr(telegram_api, "Bot", FakeBot)
    FakeBot.fail_with = None
    FakeBot.last_sent = None
    yield


async def test_bot_info_without_token_returns_not_configured(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(telegram_api.settings, "TELEGRAM_BOT_TOKEN", "")
    r = await client.get("/api/v1/telegram/bot", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"configured": False, "username": None}


async def test_bot_info_returns_username(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(telegram_api.settings, "TELEGRAM_BOT_TOKEN", "tok-plataforma")
    r = await client.get("/api/v1/telegram/bot", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"configured": True, "username": "AnotAiOficialBot"}


async def test_bot_info_telegram_error_becomes_502(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(telegram_api.settings, "TELEGRAM_BOT_TOKEN", "tok-plataforma")
    FakeBot.fail_with = TelegramError("boom")
    r = await client.get("/api/v1/telegram/bot", headers=headers)
    assert r.status_code == 502


async def test_send_test_without_chat_returns_400(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(telegram_api.settings, "TELEGRAM_BOT_TOKEN", "tok-plataforma")
    r = await client.post("/api/v1/telegram/test", headers=headers)
    assert r.status_code == 400
    assert "Conecte seu Telegram" in r.json()["detail"]


async def test_send_test_success(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(telegram_api.settings, "TELEGRAM_BOT_TOKEN", "tok-plataforma")
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TELEGRAM_CHAT_ID", "value": "12345"})
    r = await client.post("/api/v1/telegram/test", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "chat_id": "12345"}
    assert FakeBot.last_sent is not None
    assert FakeBot.last_sent["chat_id"] == "12345"
    assert "AnotAI conectado" in FakeBot.last_sent["text"]


async def test_send_test_without_start_hint_on_telegram_error(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(telegram_api.settings, "TELEGRAM_BOT_TOKEN", "tok-plataforma")
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TELEGRAM_CHAT_ID", "value": "12345"})
    FakeBot.fail_with = TelegramError("bot can't initiate conversation")
    r = await client.post("/api/v1/telegram/test", headers=headers)
    assert r.status_code == 502
    assert "/start" in r.json()["detail"]


async def test_send_test_without_platform_token_returns_503(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(telegram_api.settings, "TELEGRAM_BOT_TOKEN", "")
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TELEGRAM_CHAT_ID", "value": "12345"})
    r = await client.post("/api/v1/telegram/test", headers=headers)
    assert r.status_code == 503
