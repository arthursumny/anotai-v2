"""Tests for safe marketplace action cloning and dispatch."""

import copy
import uuid
from unittest.mock import AsyncMock

import pytest

from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.services.integration_service import IntegrationService
from app.services.template_actions import TemplateActionError, prepare_template_actions
from tests.automation_helpers import execute_actions, persist_generation


async def test_clone_disables_every_action_without_mutating_public_template(client, auth_headers, db_session):
    headers, user_id = auth_headers
    source_actions = [
        {"type": "webhook", "config": {"url": "https://example.test/hook", "headers": {"X-Test": "yes"}}},
        {"type": "telegram", "config": {"template": "Olá {animal}"}, "enabled": True},
    ]
    original = copy.deepcopy(source_actions)
    source = Template(
        user_id=uuid.UUID(user_id),
        title="Marketplace",
        purpose="Public template",
        semantic_trigger="Trigger",
        output_schema={"type": "object"},
        actions=source_actions,
        is_public=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    response = await client.post(f"/api/v1/marketplace/{source.id}/clone", headers=headers)

    assert response.status_code == 201, response.text
    cloned_actions = response.json()["actions"]
    assert [action["enabled"] for action in cloned_actions] == [False, False]
    assert cloned_actions[0]["config"] == original[0]["config"]
    assert cloned_actions[1]["type"] == original[1]["type"]
    assert source.actions == original


def _note_and_template(uid: uuid.UUID, actions: list[dict]) -> tuple[Note, Template]:
    note = Note(
        user_id=uid,
        raw_transcription="Transcrição",
        structured_data={"animal": "Thor"},
        status=NoteStatus.PROCESSED,
    )
    template = Template(
        user_id=uid,
        title="Actions",
        semantic_trigger="Trigger",
        output_schema={"type": "object"},
        actions=actions,
        is_public=False,
    )
    return note, template


async def test_dispatch_skips_disabled_and_legacy_actions(monkeypatch, db_session):
    post_webhook = AsyncMock(return_value={"status_code": 200})
    monkeypatch.setattr(IntegrationService, "_send_webhook", post_webhook)
    uid = uuid.uuid4()
    note, template = _note_and_template(uid, [
        {"type": "webhook", "enabled": False, "config": {"url": "https://example.test/disabled"}},
        {"type": "mcp_tool", "enabled": False, "config": {}},
        {"type": "webhook", "config": {"url": "https://example.test/legacy"}},
        {"type": "webhook", "enabled": True, "config": {"url": "https://example.test/enabled"}},
    ])

    await persist_generation(db_session, note, template)
    logs = await execute_actions(db_session, note)

    post_webhook.assert_awaited_once()
    assert post_webhook.await_args.args[3].config["url"] == "https://example.test/enabled"
    assert len(logs) == 1
    assert logs[0].status == "succeeded"
    assert logs[0].action_type == "SEND_WEBHOOK"


async def test_dispatch_all_disabled_actions_produce_no_logs_or_external_calls(monkeypatch, db_session):
    post_webhook = AsyncMock()
    monkeypatch.setattr(IntegrationService, "_send_webhook", post_webhook)
    note, template = _note_and_template(uuid.uuid4(), [
        {"type": "webhook", "enabled": False, "config": {}},
        {"type": "mcp_tool", "enabled": False, "config": {}},
    ])

    await persist_generation(db_session, note, template)
    logs = await execute_actions(db_session, note)

    assert logs == []
    post_webhook.assert_not_awaited()


async def test_create_and_patch_preserve_explicit_enabled_value(client, auth_headers):
    headers, _ = auth_headers
    create_response = await client.post(
        "/api/v1/templates",
        headers=headers,
        json={
            "title": "Enabled action",
            "purpose": "Test",
            "semantic_trigger": "Trigger",
            "output_schema": {"type": "object"},
            "actions": [{"type": "webhook", "enabled": True, "config": {"url": "https://example.test/hook"}}],
        },
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    assert created["actions"][0]["enabled"] is True

    patch_response = await client.patch(
        f"/api/v1/templates/{created['id']}",
        headers=headers,
        json={"actions": [{"type": "webhook", "enabled": False, "config": {"url": "https://example.test/hook"}}]},
    )
    assert patch_response.status_code == 200, patch_response.text
    assert patch_response.json()["actions"][0]["enabled"] is False

    legacy_patch = await client.patch(
        f"/api/v1/templates/{created['id']}",
        headers=headers,
        json={"actions": [{"type": "webhook", "config": {"url": "https://example.test/legacy"}}]},
    )
    assert legacy_patch.status_code == 200, legacy_patch.text
    assert "enabled" not in legacy_patch.json()["actions"][0]

    invalid_response = await client.post(
        "/api/v1/templates",
        headers=headers,
        json={
            "title": "Invalid enabled",
            "purpose": "Test",
            "semantic_trigger": "Trigger",
            "output_schema": {"type": "object"},
            "actions": [{"type": "webhook", "enabled": "yes", "config": {}}],
        },
    )
    assert invalid_response.status_code == 422


async def test_cloned_action_runs_only_after_patch_enables_it(client, auth_headers, db_session, monkeypatch):
    headers, user_id = auth_headers
    source = Template(
        user_id=uuid.UUID(user_id),
        title="Marketplace",
        purpose="Public template",
        semantic_trigger="Trigger",
        output_schema={"type": "object"},
        actions=[{"type": "webhook", "config": {"url": "https://example.test/hook"}}],
        is_public=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    clone_response = await client.post(f"/api/v1/marketplace/{source.id}/clone", headers=headers)
    assert clone_response.status_code == 201, clone_response.text
    clone_id = uuid.UUID(clone_response.json()["id"])

    post_webhook = AsyncMock(return_value={"status_code": 200})
    monkeypatch.setattr(IntegrationService, "_send_webhook", post_webhook)
    clone = await db_session.get(Template, clone_id)
    assert clone is not None
    note, _ = _note_and_template(uuid.UUID(user_id), [])
    await persist_generation(db_session, note, clone)
    assert await execute_actions(db_session, note) == []
    post_webhook.assert_not_awaited()

    patch_response = await client.patch(
        f"/api/v1/templates/{clone_id}",
        headers=headers,
        json={"actions": [{"type": "webhook", "enabled": True, "config": {"url": "https://example.test/hook"}}]},
    )
    assert patch_response.status_code == 200, patch_response.text
    await db_session.refresh(clone)
    # Existing documents retain their generation version; enabling a template
    # authorizes the next generation, never a stale previous document.
    with pytest.raises(TemplateActionError, match="version"):
        await prepare_template_actions(db_session, note, note.user_id)
    next_note, _ = _note_and_template(uuid.UUID(user_id), [])
    await persist_generation(db_session, next_note, clone)
    logs = await execute_actions(db_session, next_note)
    assert len(logs) == 1 and logs[0].status == "succeeded"
    post_webhook.assert_awaited_once()


async def test_public_templates_never_execute_even_when_enabled_or_legacy(monkeypatch, db_session):
    post_webhook = AsyncMock()
    monkeypatch.setattr(IntegrationService, "_send_webhook", post_webhook)
    uid = uuid.uuid4()
    note, template = _note_and_template(uid, [
        {"type": "webhook", "enabled": True, "config": {"url": "https://example.test/enabled"}},
        {"type": "webhook", "config": {"url": "https://example.test/legacy"}},
    ])
    template.user_id = None

    await persist_generation(db_session, note, template)
    with pytest.raises(TemplateActionError, match="template_scope"):
        await prepare_template_actions(db_session, note, note.user_id)
    post_webhook.assert_not_awaited()


async def test_clone_copies_routing_metadata_and_resets_copy_version(client, auth_headers, db_session):
    headers, user_id = auth_headers
    source = Template(
        user_id=uuid.UUID(user_id),
        title="Routing metadata",
        purpose="Public use",
        semantic_trigger="Trigger",
        output_schema={"type": "object"},
        actions=[{"type": "webhook", "config": {"url": "https://example.test"}}],
        category="operations",
        use_cases=["planning", "follow-up"],
        routing_description="Route business discussions",
        version=4,
        is_public=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    response = await client.post(f"/api/v1/marketplace/{source.id}/clone", headers=headers)

    assert response.status_code == 201, response.text
    clone = response.json()
    assert clone["category"] == source.category
    assert clone["use_cases"] == source.use_cases
    assert clone["routing_description"] == source.routing_description
    assert clone["version"] == 1
    assert clone["actions"][0]["enabled"] is False
    assert source.actions[0].get("enabled") is None
