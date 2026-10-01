"""AnotAI — testes do CRUD /secrets (máscara, upsert, delete)."""

from httpx import AsyncClient
from sqlmodel import select

from app.core.config import settings
from app.core.user_secret_crypto import decrypt_user_secret, is_encrypted_user_secret
from app.models.user_secret import UserSecret


async def test_secret_upsert_list_masked(client: AsyncClient, auth_headers):
    headers, _uid = auth_headers

    r = await client.post("/api/v1/secrets", headers=headers, json={"key": "telegram_bot_token", "value": "tok123456789"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"] == "TELEGRAM_BOT_TOKEN"  # normalizado p/ UPPER
    assert "tok123456789" not in r.text  # valor completo nunca exposto
    assert body["value_masked"] and body["value_masked"] != "tok123456789"

    r = await client.post("/api/v1/secrets", headers=headers, json={"key": "WEBHOOK_AUTH_TOKEN", "value": "web987654"})
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/secrets", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()
    assert {i["key"] for i in items} == {"TELEGRAM_BOT_TOKEN", "WEBHOOK_AUTH_TOKEN"}
    assert "tok123456789" not in r.text and "web987654" not in r.text


async def test_secret_is_encrypted_at_rest(client: AsyncClient, auth_headers, db_session):
    headers, uid = auth_headers
    response = await client.post("/api/v1/secrets", headers=headers, json={"key": "API_KEY", "value": "private-value-123"})
    assert response.status_code == 200
    secret = (await db_session.exec(select(UserSecret).where(UserSecret.key == "API_KEY"))).one()
    assert is_encrypted_user_secret(secret.value)
    assert secret.value != "private-value-123"
    assert decrypt_user_secret(secret.user_id, secret.id, secret.value, key=settings.USER_SECRET_ENCRYPTION_KEY) == "private-value-123"
    listed = await client.get("/api/v1/secrets", headers=headers)
    assert "private-value-123" not in listed.text
    assert "value_masked" in listed.text


async def test_secret_api_fails_closed_without_key(client: AsyncClient, auth_headers, monkeypatch, db_session):
    headers, _uid = auth_headers
    monkeypatch.setattr(settings, "USER_SECRET_ENCRYPTION_KEY", "")
    response = await client.post("/api/v1/secrets", headers=headers, json={"key": "NO_KEY", "value": "secret"})
    assert response.status_code == 503
    assert (await db_session.exec(select(UserSecret).where(UserSecret.key == "NO_KEY"))).first() is None


async def test_secret_upsert_updates_value(client: AsyncClient, auth_headers):
    headers, _uid = auth_headers
    await client.post("/api/v1/secrets", headers=headers, json={"key": "K1", "value": "aaa111"})
    await client.post("/api/v1/secrets", headers=headers, json={"key": "K1", "value": "bbb222"})
    r = await client.get("/api/v1/secrets", headers=headers)
    keys = [i["key"] for i in r.json()]
    assert keys.count("K1") == 1  # upsert, sem duplicar
    assert "bbb222" not in r.text


async def test_secret_delete(client: AsyncClient, auth_headers):
    headers, _uid = auth_headers
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TEMP", "value": "xyz123"})
    r = await client.delete("/api/v1/secrets/TEMP", headers=headers)
    assert r.status_code == 204, r.text
    r = await client.get("/api/v1/secrets", headers=headers)
    assert all(i["key"] != "TEMP" for i in r.json())
    r = await client.delete("/api/v1/secrets/TEMP", headers=headers)
    assert r.status_code == 404


async def test_secret_isolated_per_user(client: AsyncClient, auth_headers, db_session):
    from app.core.security import create_access_token, hash_password
    from app.models.user import User

    headers, _uid = auth_headers
    other = User(name="Outro", email="outro@anotai.com", hashed_password=hash_password("senha-forte-123"))
    db_session.add(other)
    await db_session.commit()
    other_headers = {"Authorization": f"Bearer {create_access_token(str(other.id))}"}

    await client.post("/api/v1/secrets", headers=headers, json={"key": "MINE", "value": "segredo-meu-1"})
    r = await client.get("/api/v1/secrets", headers=other_headers)
    assert r.status_code == 200 and r.json() == []  # outro usuário não vê
