"""Template authorization, generated payloads, and real executor regressions."""

import base64
import json
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.integration_service as integration_service
from app.core.config import settings
from app.models.action_execution import ActionExecution
from app.models.action_item import Task
from app.models.automation_settings import AutomationSettings
from app.models.integration_connection import IntegrationConnection
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.schemas.template import ActionConfig
from app.services.action_executor import process_pending_actions, run_action_execution
from app.services.nango_client import NangoClientError
from app.services.template_actions import TemplateActionError, prepare_template_actions
from tests.automation_helpers import persist_generation


async def _generation(session, actions, *, user_id=None, title="Meeting", body="Generated document", summary="Decisions"):
    note = Note(
        user_id=user_id or uuid.uuid4(), title=title, body=body, summary=summary,
        status=NoteStatus.PROCESSED, raw_transcription="Raw recording",
        structured_data={"client": {"name": "Ana"}, "items": ["one", "two"]},
    )
    template = Template(
        user_id=note.user_id, title=title, semantic_trigger=title,
        actions=actions, output_schema={"type": "object"},
    )
    user, document = await persist_generation(session, note, template)
    return note, template, user, document


def _factory(session):
    return async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def gmail_send(monkeypatch):
    """Stub the Nango Gmail proxy: no real Gmail/Nango API call ever happens."""
    captured = {"messages": [], "error": None}
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(settings, "NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY", "gmail-test-key")

    class FakeNangoClient:
        def __init__(self, *, api_key, base_url):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        async def proxy_request(self, method, path, **kwargs):
            if captured["error"] is not None:
                raise captured["error"]
            captured["messages"].append({
                "method": method,
                "path": path,
                "connection_id": kwargs["connection_id"],
                "provider_config_key": kwargs["provider_config_key"],
                "raw": kwargs["json_body"]["raw"],
            })
            return SimpleNamespace(
                status_code=200, body={"id": "gmail_msg_1", "threadId": "thread_1"},
            )

    monkeypatch.setattr(integration_service, "NangoClient", FakeNangoClient)

    async def connect(session, user):
        session.add(IntegrationConnection(
            workspace_id=user.workspace_id,
            user_id=user.id,
            provider="google_gmail",
            provider_config_key="gmail-test-key",
            nango_connection_id="nango_conn_1",
            status="active",
        ))
        await session.flush()

    captured["connect"] = connect
    return captured


def _decode_mime(raw: str):
    """Decode the Gmail base64url (unpadded) payload back into an email message."""
    assert raw and "=" not in raw
    padded = raw + "=" * (-len(raw) % 4)
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(padded))


async def test_two_selected_templates_execute_only_their_own_actions(db_session, gmail_send):
    # Given two templates owned by the same user but with different actions.
    email_note, email_template, user, _ = await _generation(db_session, [
        {"type": "email", "enabled": True, "config": {"subject": "{title}", "body": "{summary}\n{body}\n{client.name}"}},
    ], title="Email note", body="Email-only generated body")
    await gmail_send["connect"](db_session, user)
    await db_session.commit()
    task_note, task_template, _, _ = await _generation(db_session, [
        {"type": "create_task", "enabled": True, "config": {"title": "{title}", "summary": "{summary}"}},
    ], user_id=user.id, title="Task note")
    first = await prepare_template_actions(db_session, email_note, user.id)
    second = await prepare_template_actions(db_session, task_note, user.id)
    await db_session.commit()

    # When the shared executor consumes both independent ledgers.
    result = await process_pending_actions(_factory(db_session))

    # Then generated content and provider types stay bound to each selection.
    assert result == {"succeeded": 2, "retry_scheduled": 0, "failed": 0, "skipped": 0}
    assert first[0].policy_snapshot["template_id"] == str(email_template.id)
    assert second[0].policy_snapshot["template_id"] == str(task_template.id)
    assert len(gmail_send["messages"]) == 1
    sent = gmail_send["messages"][0]
    assert sent["method"] == "POST"
    assert sent["path"] == "gmail/v1/users/me/messages/send"
    assert sent["connection_id"] == "nango_conn_1"
    assert sent["provider_config_key"] == "gmail-test-key"
    message = _decode_mime(sent["raw"])
    assert message["Subject"] == email_note.title
    assert message["To"] == user.email
    # The From header is never forged: the connected Gmail account is the sender.
    assert message.get("From") is None
    assert message.get_content().splitlines() == ["Decisions", "Email-only generated body", "Ana"]
    assert [task.title for task in (await db_session.exec(select(Task))).all()] == ["Task note"]
    assert len((await db_session.exec(select(Note))).all()) == 2


async def test_email_explicit_recipient_reaches_the_mime_destination(db_session, gmail_send):
    # Given an explicit user-configured recipient.
    note, _, user, _ = await _generation(db_session, [
        {"type": "email", "enabled": True, "config": {"to": "chosen@example.test"}},
    ])
    await gmail_send["connect"](db_session, user)
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    # When the executor sends through the user's own Gmail connection.
    result = await run_action_execution(_factory(db_session), rows[0].id)
    # Then the proxied message addresses only the configured destination.
    assert result == "succeeded"
    assert len(gmail_send["messages"]) == 1
    assert _decode_mime(gmail_send["messages"][0]["raw"])["To"] == "chosen@example.test"


async def test_task_retries_and_repeated_preparation_are_idempotent(db_session):
    # Given one generated document with an explicitly configured task.
    note, _, user, _ = await _generation(db_session, [
        {"type": "create_task", "enabled": True, "config": {"title": "{title}", "summary": "{body}"}},
    ])
    first = await prepare_template_actions(db_session, note, user.id)
    second = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    factory = _factory(db_session)
    assert await run_action_execution(factory, first[0].id) == "succeeded"
    await db_session.refresh(first[0])
    first[0].status = "claimed"
    first[0].lease_expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    await db_session.commit()

    # When an expired lease is retried after the task already exists.
    result = await run_action_execution(factory, first[0].id)

    # Then the same durable Task is reused without a Note card.
    assert result == "succeeded" and first[0].id == second[0].id
    tasks = (await db_session.exec(select(Task))).all()
    assert len(tasks) == 1 and tasks[0].summary == note.body
    assert tasks[0].source_note_id == note.id and tasks[0].kanban_status == "todo"
    assert len((await db_session.exec(select(Note))).all()) == 1


@pytest.mark.parametrize("change", [
    "selection", "template_owner", "version", "config", "enabled", "document_owner",
    "document_template", "document_content", "note_owner", "workspace", "global_disabled", "payload",
])
async def test_changed_provenance_cannot_execute(db_session, change):
    # Given a prepared ledger for a selected owned generation.
    note, template, user, document = await _generation(db_session, [
        {"type": "create_task", "enabled": True, "config": {"title": "{title}"}},
    ])
    rows = await prepare_template_actions(db_session, note, user.id)
    _other_note, other_template, other_user, _ = await _generation(db_session, [])
    match change:
        case "selection":
            note.template_id = other_template.id
        case "template_owner":
            template.user_id = other_user.id
        case "version":
            template.version += 1
        case "config":
            template.actions = [{"type": "create_task", "enabled": True, "config": {"title": "Changed"}}]
        case "enabled":
            template.actions = [{**template.actions[0], "enabled": False}]
        case "document_owner":
            document.user_id = other_user.id
        case "document_template":
            document.template_id = other_template.id
        case "document_content":
            document.rendered_content = "Changed body"
        case "note_owner":
            note.user_id = other_user.id
        case "workspace":
            rows[0].workspace_id = other_user.workspace_id
        case "global_disabled":
            db_session.add(AutomationSettings(
                workspace_id=user.workspace_id, user_id=user.id, automation_mode="disabled",
            ))
        case "payload":
            rows[0].action_input = {"title": "Injected task"}
    await db_session.commit()

    # When execution revalidates the saved provenance.
    result = await run_action_execution(_factory(db_session), rows[0].id)

    # Then no task is created and the ledger records a sanitized failure.
    assert result == "retry_scheduled"
    await db_session.refresh(rows[0])
    assert rows[0].last_error == "integrationexecutionerror"
    assert (await db_session.exec(select(Task))).all() == []


async def test_invalid_config_and_missing_variables_fail_without_blocking_siblings(db_session):
    # Given invalid legacy configurations beside a valid task.
    note, _, user, _ = await _generation(db_session, [
        {"type": "email", "enabled": True, "config": {"body": "{missing}", "subject": "{title}"}},
        {"type": "email", "enabled": True, "config": {"to": "not an email"}},
        {"type": "whatsapp", "enabled": True, "config": {}},
        {"type": "create_task", "enabled": True, "config": {}},
    ])
    # When all configured actions are prepared and the executor runs.
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    result = await process_pending_actions(_factory(db_session))
    # Then definition errors are durable and only the valid sibling executes.
    for row in rows:
        await db_session.refresh(row)
    assert [row.status for row in rows] == ["failed", "failed", "failed", "succeeded"]
    assert rows[0].last_error == "missing_template_variable"
    assert rows[1].last_error == rows[2].last_error == "invalid_template_action"
    assert result["succeeded"] == 1


async def test_gmail_proxy_failure_retries_without_regeneration_or_blocking_task(
    db_session, gmail_send,
):
    # Given a failing Gmail proxy call and an independent task action.
    note, _, user, document = await _generation(db_session, [
        {"type": "email", "enabled": True, "config": {}},
        {"type": "create_task", "enabled": True, "config": {}},
    ])
    await gmail_send["connect"](db_session, user)
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()

    gmail_send["error"] = NangoClientError(502, "http_error")
    # When the executor handles provider failure in its own transaction.
    result = await process_pending_actions(_factory(db_session))
    # Then the task succeeds, the email can retry, and generation stays unchanged.
    assert result["retry_scheduled"] == result["succeeded"] == 1
    await db_session.refresh(rows[0])
    await db_session.refresh(document)
    assert rows[0].last_error == "integrationexecutionerror" and rows[0].attempts == 1
    assert "http_error" not in (rows[0].last_error or "")
    assert document.rendered_content == note.body
    assert gmail_send["messages"] == []
    assert len((await db_session.exec(select(Task))).all()) == 1


async def test_template_enabled_is_authorization_and_confirmation_is_opt_in(db_session):
    # Given suggestive global defaults and a mix of explicit template flags.
    note, _, user, _ = await _generation(db_session, [
        {"type": "create_task", "config": {}},
        {"type": "create_task", "enabled": False, "config": {}},
        {"type": "create_task", "enabled": True, "config": {}},
        {"type": "create_task", "enabled": True, "require_confirmation": True, "config": {}},
    ])
    # When actions are prepared.
    rows = await prepare_template_actions(db_session, note, user.id)
    # Then only explicit enablement authorizes actions and confirmation is opt-in.
    assert [row.status for row in rows] == ["pending", "awaiting_confirmation"]
    assert all(row.policy_snapshot["authorized"] is True for row in rows)


async def test_global_disabled_prevents_preparation(db_session):
    # Given global automation disabled.
    note, _, user, _ = await _generation(db_session, [{"type": "create_task", "enabled": True, "config": {}}])
    db_session.add(AutomationSettings(workspace_id=user.workspace_id, user_id=user.id, automation_mode="disabled"))
    await db_session.flush()
    # When the selected template is prepared.
    rows = await prepare_template_actions(db_session, note, user.id)
    # Then no ledger or action is created.
    assert rows == []


async def test_confirmation_uses_the_existing_ledger_and_executor(db_session):
    # Given a template action waiting for explicit confirmation.
    note, _, user, _ = await _generation(db_session, [{
        "type": "create_task", "enabled": True, "require_confirmation": True, "config": {},
    }])
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    assert await run_action_execution(_factory(db_session), rows[0].id) == "not_claimed"
    await db_session.refresh(rows[0])
    rows[0].policy_snapshot = {**rows[0].policy_snapshot, "confirmed_by_user": True}
    rows[0].status = "pending"
    await db_session.commit()
    # When the already-existing execution receives user confirmation.
    result = await run_action_execution(_factory(db_session), rows[0].id)
    # Then only this action executes without changing the template default.
    assert result == "succeeded"
    assert len((await db_session.exec(select(Task))).all()) == 1


async def test_provider_receives_generated_braces_without_second_interpolation(db_session, gmail_send):
    # Given generated text that itself contains placeholder-like text.
    note, _, user, _ = await _generation(db_session, [
        {"type": "email", "enabled": True, "config": {"body": "{body}"}},
    ], body="Literal {not_a_variable} in generated content")
    await gmail_send["connect"](db_session, user)
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    # When sending the prepared content.
    result = await run_action_execution(_factory(db_session), rows[0].id)
    # Then generated braces stay literal, not missing-variable substitutions.
    assert result == "succeeded"
    message = _decode_mime(gmail_send["messages"][0]["raw"])
    assert message.get_content().strip() == note.body


async def test_schema_validation_keeps_template_content_urls_but_workflows_restricted(db_session):
    # Given a user-configured task whose generated summary contains a URL.
    note, _, user, _ = await _generation(db_session, [
        {"type": "create_task", "enabled": True, "config": {"summary": "{body}"}},
    ], body="Read https://example.test/report")
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    # When this explicitly authorized template action executes.
    result = await run_action_execution(_factory(db_session), rows[0].id)
    # Then its original generated content is retained.
    assert result == "succeeded"
    assert (await db_session.exec(select(Task))).one().summary == note.body


async def test_missing_or_foreign_saved_document_is_rejected(db_session):
    # Given a processed note without its persisted generated document.
    note, _, user, document = await _generation(db_session, [{"type": "create_task", "enabled": True, "config": {}}])
    await db_session.delete(document)
    await db_session.commit()
    # When preparation is attempted.
    with pytest.raises(TemplateActionError, match="generation_template_version"):
        await prepare_template_actions(db_session, note, user.id)
    # Then no action can escape the generation prerequisite.
    assert (await db_session.exec(select(ActionExecution))).all() == []


async def test_webhook_preserves_json_types_and_generated_content(db_session, monkeypatch):
    # Given a webhook configured with both nested and whole-field interpolation.
    note, _, user, _ = await _generation(db_session, [{
        "type": "webhook", "enabled": True, "config": {
            "url": "https://hooks.test/generated", "method": "PUT",
            "body": {"title": "{title}", "items": "{items}", "owner": "{structured.client.name}", "body": "{body}"},
        },
    }])
    requests = []
    transport = httpx.MockTransport(lambda request: (requests.append(request), httpx.Response(204))[1])
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.integration_service.httpx.AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    # When the same executor invokes the HTTP provider.
    result = await run_action_execution(_factory(db_session), rows[0].id)
    # Then the wire payload is the configured generated document, not raw audio.
    assert result == "succeeded"
    assert requests[0].method == "PUT"
    assert json.loads(requests[0].content) == {
        "title": note.title, "items": ["one", "two"], "owner": "Ana", "body": note.body,
    }
    assert requests[0].headers["Idempotency-Key"] == rows[0].idempotency_key


def test_unsupported_whatsapp_and_unknown_provider_configuration_are_rejected():
    # Given unsupported or malformed explicitly enabled action definitions.
    for raw in [
        {"type": "whatsapp", "enabled": True, "config": {}},
        {"type": "webhook", "enabled": True, "config": {"url": "not-http"}},
        {"type": "email", "enabled": True, "config": {"unknown": True}},
    ]:
        # When parsed at the API boundary, then the invalid contract is rejected.
        with pytest.raises(ValidationError):
            ActionConfig.model_validate(raw)
