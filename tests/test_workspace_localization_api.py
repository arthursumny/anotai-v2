"""Workspace-scoped localization settings API coverage."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.models.action_item import Reminder
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


@pytest.mark.asyncio
async def test_localization_get_and_owner_patch(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)

    response = await client.get("/api/v1/settings/localization", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"timezone": "UTC", "locale": "pt-BR"}
    assert "workspaceId" not in response.json()

    for payload, expected in [
        ({"timezone": "America/Sao_Paulo"}, {"timezone": "America/Sao_Paulo", "locale": "pt-BR"}),
        ({"timezone": "Europe/London", "locale": "en-US"}, {"timezone": "Europe/London", "locale": "en-US"}),
    ]:
        updated = await client.patch("/api/v1/settings/localization", headers=headers, json=payload)
        assert updated.status_code == 200, updated.text
        assert updated.json() == expected

    assert workspace.timezone == "Europe/London" and workspace.locale == "en-US"


@pytest.mark.asyncio
async def test_localization_rejects_invalid_fields_and_empty_patch(client, auth_headers):
    headers, _ = auth_headers
    invalid_timezone = await client.patch(
        "/api/v1/settings/localization", headers=headers, json={"timezone": "Mars/Olympus"},
    )
    invalid_locale = await client.patch(
        "/api/v1/settings/localization", headers=headers, json={"locale": "fr-FR"},
    )
    empty = await client.patch("/api/v1/settings/localization", headers=headers, json={})
    extra = await client.patch(
        "/api/v1/settings/localization", headers=headers, json={"timezone": "UTC", "workspaceId": "other"},
    )
    assert invalid_timezone.status_code == invalid_locale.status_code == empty.status_code == extra.status_code == 422


@pytest.mark.asyncio
async def test_localization_requires_membership_and_owner_for_mutation(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    member = (await db_session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == user.workspace_id,
        WorkspaceMembership.user_id == user.id,
    ))).one()

    member.role = "MEMBER"
    await db_session.commit()
    denied_patch = await client.patch(
        "/api/v1/settings/localization", headers=headers, json={"timezone": "Europe/London"},
    )
    assert denied_patch.status_code == 403
    assert (await client.get("/api/v1/settings/localization", headers=headers)).status_code == 200

    await db_session.delete(member)
    await db_session.commit()
    denied_get = await client.get("/api/v1/settings/localization", headers=headers)
    denied_patch = await client.patch(
        "/api/v1/settings/localization", headers=headers, json={"locale": "es-ES"},
    )
    assert denied_get.status_code == denied_patch.status_code == 403


@pytest.mark.asyncio
async def test_localization_patch_does_not_rewrite_existing_reminder_times(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    due_at = datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc)
    remind_at = datetime(2030, 1, 1, 3, 4, tzinfo=timezone.utc)
    reminder = Reminder(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="localization-test",
        title="private reminder", due_at=due_at, remind_at=remind_at,
    )
    db_session.add(reminder)
    await db_session.commit()

    response = await client.patch(
        "/api/v1/settings/localization", headers=headers,
        json={"timezone": "America/Sao_Paulo", "locale": "es-ES"},
    )
    assert response.status_code == 200
    stored = await db_session.get(Reminder, reminder.id)
    assert stored.due_at.replace(tzinfo=timezone.utc) == due_at
    assert stored.remind_at.replace(tzinfo=timezone.utc) == remind_at
