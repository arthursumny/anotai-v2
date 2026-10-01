"""AnotAI — testes do "Construir com IA" (POST /templates/draft)."""

import json
from types import SimpleNamespace

import app.services.template_drafter as drafter

DRAFT_PAYLOAD = {
    "title": "Ata de reunião",
    "purpose": "Transformar áudio de reunião em ata com pontos e data.",
    "semantic_trigger": "Use para reuniões de trabalho. Não use para conversas casuais.",
    "fields": [
        {"name": "titulo", "kind": "texto", "required": True, "options": []},
        {"name": "pontos", "kind": "lista", "required": True, "options": []},
    ],
    "suggested_message": "Ata: {titulo} — {pontos}",
}


def _fake_chat(payload: dict):
    async def _chat(client, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
        )

    return _chat


async def test_draft_ok(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(drafter, "chat_create", _fake_chat(DRAFT_PAYLOAD))
    monkeypatch.setattr(drafter.settings, "OPENAI_API_KEY", "test-key")
    r = await client.post(
        "/api/v1/templates/draft",
        headers=headers,
        json={"description": "quero registrar atas das minhas reuniões com título e pontos"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Ata de reunião"
    assert body["purpose"].startswith("Transformar áudio de reunião")
    assert [f["name"] for f in body["fields"]] == ["titulo", "pontos"]
    assert body["suggested_message"] == "Ata: {titulo} — {pontos}"
    # nada salvo: continua sem templates
    r = await client.get("/api/v1/templates", headers=headers)
    assert r.json() == []


async def test_draft_short_description_422(client, auth_headers):
    headers, _uid = auth_headers
    r = await client.post("/api/v1/templates/draft", headers=headers, json={"description": "curto"})
    assert r.status_code == 422


async def test_draft_no_key_502(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(drafter.settings, "OPENAI_API_KEY", "")
    r = await client.post(
        "/api/v1/templates/draft", headers=headers, json={"description": "quero atas de reunião"}
    )
    assert r.status_code == 502 and "OPENAI_API_KEY" in r.json()["detail"]


async def test_draft_llm_garbage_502(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(drafter, "chat_create", _fake_chat({"titulo": "??"}))
    monkeypatch.setattr(drafter.settings, "OPENAI_API_KEY", "test-key")
    r = await client.post(
        "/api/v1/templates/draft", headers=headers, json={"description": "quero atas de reunião com título"}
    )
    assert r.status_code == 502


DERIVE_PAYLOAD = {
    "purpose": "Transformar áudio de consulta em prontuário do animal.",
    "semantic_trigger": "Use para consultas veterinárias. Não use para banho.",
    "fields": [
        {"name": "animal_nome", "kind": "texto", "required": True, "options": []},
        {"name": "especie", "kind": "opções", "required": True, "options": ["canino", "felino"]},
    ],
    "suggested_message": "Consulta de {animal_nome} ({especie})",
}


async def test_derive_ok(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(drafter, "chat_create", _fake_chat(DERIVE_PAYLOAD))
    monkeypatch.setattr(drafter.settings, "OPENAI_API_KEY", "test-key")
    r = await client.post(
        "/api/v1/templates/derive",
        headers=headers,
        json={"example_output": "Consulta do Thor:\n- Animal: Thor\n- Espécie: canino\n- Sintomas: tosse"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "veterinária" in body["semantic_trigger"]
    assert body["purpose"].startswith("Transformar áudio de consulta")
    assert [f["name"] for f in body["fields"]] == ["animal_nome", "especie"]
    assert body["fields"][1]["options"] == ["canino", "felino"]


async def test_derive_short_example_422(client, auth_headers):
    headers, _uid = auth_headers
    r = await client.post("/api/v1/templates/derive", headers=headers, json={"example_output": "curto"})
    assert r.status_code == 422


async def test_derive_no_key_502(client, auth_headers, monkeypatch):
    headers, _uid = auth_headers
    monkeypatch.setattr(drafter.settings, "OPENAI_API_KEY", "")
    r = await client.post(
        "/api/v1/templates/derive",
        headers=headers,
        json={"example_output": "Consulta do Thor com todos os detalhes relevantes aqui"},
    )
    assert r.status_code == 502 and "OPENAI_API_KEY" in r.json()["detail"]


async def test_create_template_with_example(client, auth_headers):
    headers, _uid = auth_headers
    r = await client.post("/api/v1/templates", headers=headers, json={
        "title": "Por exemplo",
        "purpose": "Para que serve este teste.",
        "semantic_trigger": "Use para testes.",
        "example_output": "Nota exemplo:\n- Campo: valor",
        "output_schema": {"type": "object", "properties": {"campo": {"type": "string"}}},
        "actions": [],
        "is_public": False,
    })
    assert r.status_code == 201, r.text
    assert r.json()["example_output"].startswith("Nota exemplo")


async def test_create_template_without_purpose_422(client, auth_headers):
    headers, _uid = auth_headers
    r = await client.post("/api/v1/templates", headers=headers, json={
        "title": "Sem purpose",
        "semantic_trigger": "Use para testes.",
        "output_schema": {"type": "object"},
        "actions": [],
        "is_public": False,
    })
    assert r.status_code == 422
