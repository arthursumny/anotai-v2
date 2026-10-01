from datetime import datetime, timezone
import uuid

import pytest
import httpx
from sqlmodel import select

from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.gmail_ingress import GmailSourceProvider, _plain_text
from app.services.nango_client import NangoClient, NangoClientError, ProxyResponse
from app.core.config import settings
from app.models.action_item import Task
from app.models.normalized_event import NormalizedEvent
from app.schemas.intelligence import IntelligenceResult, IntelligenceType
from app.services import gmail_ingress


async def _setup(session, *, policy=False):
    user = User(name="Gmail", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    workspace = Workspace(name="Gmail test", timezone="UTC")
    session.add(workspace)
    await session.flush()
    user.workspace_id = workspace.id
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    connection = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail",
        status="active", provider_config_key="gmail-test", nango_connection_id="nango-1",
        account_email=user.email,
    )
    session.add(connection)
    await session.flush()
    state = GmailWatchState(
        integration_connection_id=connection.id, status="active", history_id="10",
        expiration_at=datetime.now(timezone.utc),
    )
    session.add(state)
    if policy:
        session.add(SourceMonitoringPolicy(
            workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
            gmail_label_ids=["INBOX"], gmail_allowed_senders=["good@example.com"],
        ))
    await session.flush()
    return user, workspace, connection, state


class _Nango:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def proxy_request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ProxyResponse(200, response)


@pytest.mark.asyncio
async def test_missing_opt_in_makes_no_gmail_proxy_calls(db_session):
    _, _, connection, state = await _setup(db_session)
    nango = _Nango([])
    assert await GmailSourceProvider(nango).process_history(db_session, connection) == 0
    assert nango.calls == []


@pytest.mark.asyncio
async def test_renew_watch_parses_milliseconds_without_replacing_cursor(db_session, monkeypatch):
    _, _, connection, state = await _setup(db_session, policy=True)
    state.status = "paused"
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "projects/real/topics/ingress")
    await db_session.flush()
    nango = _Nango([{"historyId": "99", "expiration": "1760000000000"}])
    renewed = await GmailSourceProvider(nango).renew_watch(db_session, connection)
    assert renewed.history_id == "10"
    assert renewed.status == "active"
    assert renewed.expiration_at == datetime.fromtimestamp(1760000000, timezone.utc)
    assert nango.calls[0][2]["json_body"]["topicName"] == "projects/real/topics/ingress"
    assert nango.calls[0][2]["json_body"]["labelIds"] == ["INBOX"]
    assert nango.calls[0][2]["json_body"]["labelFilterBehavior"] == "include"


@pytest.mark.asyncio
async def test_renew_watch_uses_policy_labels_without_spam_or_trash(db_session, monkeypatch):
    _, _, connection, _ = await _setup(db_session, policy=True)
    policy = (await db_session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.user_id == connection.user_id,
        SourceMonitoringPolicy.source == "gmail",
    ))).one()
    policy.gmail_label_ids = ["Label_custom", "SPAM", "TRASH"]
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "projects/p/topics/t")
    nango = _Nango([{"historyId": "99", "expiration": "1760000000000"}])
    await GmailSourceProvider(nango).renew_watch(db_session, connection)
    assert nango.calls[0][2]["json_body"]["labelIds"] == ["Label_custom"]


@pytest.mark.asyncio
async def test_watch_missing_policy_or_topic_fails_before_proxy(db_session, monkeypatch):
    _, _, connection, _ = await _setup(db_session)
    nango = _Nango([])
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "projects/p/topics/t")
    with pytest.raises(PermissionError):
        await GmailSourceProvider(nango).renew_watch(db_session, connection)
    assert nango.calls == []

    await db_session.rollback()
    _, _, connection, _ = await _setup(db_session, policy=True)
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "  ")
    with pytest.raises(PermissionError):
        await GmailSourceProvider(nango).renew_watch(db_session, connection)
    assert nango.calls == []


@pytest.mark.asyncio
async def test_needs_resync_cursor_cannot_be_rebaselined_by_watch(db_session, monkeypatch):
    _, _, connection, state = await _setup(db_session, policy=True)
    state.status = "needs_resync"
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "projects/p/topics/t")
    nango = _Nango([])
    with pytest.raises(PermissionError, match="rebaseline"):
        await GmailSourceProvider(nango).renew_watch(db_session, connection)
    assert state.history_id == "10"
    assert nango.calls == []


@pytest.mark.asyncio
async def test_blocked_metadata_sender_never_fetches_full_message(db_session):
    _, _, connection, _ = await _setup(db_session, policy=True)
    nango = _Nango([
        {"history": [{"id": "11", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}]},
        {"id": "m1", "threadId": "t1", "internalDate": "1735689600000", "labelIds": ["INBOX"],
         "payload": {"headers": [{"name": "From", "value": "Blocked <bad@example.com>"},
                                  {"name": "Subject", "value": "Hi"}]}},
    ])
    assert await GmailSourceProvider(nango).process_history(db_session, connection) == 1
    assert len(nango.calls) == 2
    assert nango.calls[1][2]["params"] == {"format": "metadata"}


@pytest.mark.asyncio
async def test_history_404_requires_resync_without_full_sync(db_session):
    _, _, connection, state = await _setup(db_session, policy=True)
    nango = _Nango([NangoClientError(404, "http_error")])
    assert await GmailSourceProvider(nango).process_history(db_session, connection) == 0
    refreshed = await db_session.get(GmailWatchState, state.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "needs_resync"
    assert len(nango.calls) == 1


@pytest.mark.asyncio
async def test_history_pagination_keeps_original_cursor_and_string_history_type(db_session):
    _, _, connection, state = await _setup(db_session, policy=True)
    nango = _Nango([
        {"history": [], "nextPageToken": "next", "historyId": "99"},
        {"history": [], "historyId": "100"},
    ])
    assert await GmailSourceProvider(nango).process_history(db_session, connection) == 0
    first, second = [call[2]["params"] for call in nango.calls]
    assert first == {"startHistoryId": "10", "historyTypes": "messageAdded"}
    assert second == {"startHistoryId": "10", "historyTypes": "messageAdded", "pageToken": "next"}
    assert state.history_id == "100"


@pytest.mark.asyncio
async def test_history_types_string_is_accepted_by_real_nango_adapter():
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"history": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = NangoClient("test-key", client=http)
        await client.proxy_request(
            "GET", "gmail/v1/users/me/history", connection_id="nango-1",
            provider_config_key="gmail-test",
            params={"startHistoryId": "10", "historyTypes": "messageAdded"},
        )
    assert requests[0].url.params["historyTypes"] == "messageAdded"


@pytest.mark.asyncio
async def test_message_cap_pauses_at_complete_record_without_resync(db_session, monkeypatch):
    _, _, connection, state = await _setup(db_session, policy=True)
    monkeypatch.setattr(gmail_ingress, "MAX_MESSAGES", 2)
    nango = _Nango([
        {"history": [{"id": "11", "messagesAdded": [
            {"message": {"id": "a"}}, {"message": {"id": "b"}},
        ]}], "nextPageToken": "next"},
        {"history": [{"id": "12", "messagesAdded": [{"message": {"id": "c"}}]}]},
    ])
    provider = GmailSourceProvider(nango)

    async def no_process(*args, **kwargs):
        return None

    monkeypatch.setattr(provider, "_process_message", no_process)
    assert await provider.process_history(db_session, connection) == 2
    assert state.history_id == "11"
    assert state.status == "active"


@pytest.mark.asyncio
async def test_oversized_single_history_record_requires_resync(db_session, monkeypatch):
    _, _, connection, state = await _setup(db_session, policy=True)
    monkeypatch.setattr(gmail_ingress, "MAX_MESSAGES", 1)
    nango = _Nango([{"history": [{"id": "11", "messagesAdded": [
        {"message": {"id": "a"}}, {"message": {"id": "b"}},
    ]}]}])
    assert await GmailSourceProvider(nango).process_history(db_session, connection) == 0
    await db_session.refresh(state)
    assert state.status == "needs_resync"
    assert state.history_id == "10"


@pytest.mark.asyncio
async def test_allowed_email_ingests_once_and_materializes_suggestion(db_session, monkeypatch):
    _, workspace, connection, _ = await _setup(db_session, policy=True)
    calls = []

    async def mocked_classify(content, **kwargs):
        calls.append((content, kwargs))
        return [IntelligenceResult(type=IntelligenceType.TASK, title="Responder", summary="Responder email", confidence=0.9)]

    monkeypatch.setattr(gmail_ingress, "classify_event", mocked_classify)
    nango = _Nango([
        {"history": [{"id": "11", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}]},
        {"id": "m1", "threadId": "t1", "internalDate": "1735689600000", "labelIds": ["INBOX"],
         "payload": {"headers": [{"name": "From", "value": "Alice <good@example.com>"},
                                  {"name": "Subject", "value": "Follow-up"}]}},
        {"id": "m1", "threadId": "t1", "snippet": "fallback", "payload": {
            "mimeType": "multipart/mixed", "parts": [
                {"mimeType": "text/plain", "body": {"data": "SGVsbG8gZnJvbSBtYWls"}},
                {"mimeType": "text/plain", "filename": "attachment.txt", "body": {"data": "c2VjcmV0"}},
            ],
        }},
    ])
    await GmailSourceProvider(nango).process_history(db_session, connection)
    await db_session.flush()
    events = (await db_session.exec(select(NormalizedEvent))).all()
    tasks = (await db_session.exec(select(Task))).all()
    assert len(events) == 1
    assert events[0].content == "Hello from mail"
    assert events[0].external_id == f"{connection.id}:m1"
    assert len(tasks) == 1 and tasks[0].status == "suggested"
    assert calls[0][0] == "Hello from mail"
    assert calls[0][1]["timezone"] == workspace.timezone




def test_mime_plain_text_excludes_attachment_parts():
    import base64

    enc = lambda text: base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": enc("hello")}},
            {"mimeType": "text/plain", "filename": "secret.txt", "body": {"data": enc("attachment")}},
        ],
    }
    assert _plain_text(payload) == "hello"
