"""Bounded, policy-first Gmail history ingestion. Invoked by push/cron jobs only."""

import base64
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.normalized_event import NormalizedEvent
from app.models.source_policy import SourceMonitoringPolicy
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.event_ingestion import EventCandidate, gmail_policy_allows, ingest
from app.services.intelligence import classify_event
from app.services.intelligence_materializer import materialize_results
from app.services.nango_client import NangoClient, NangoClientError

MAX_MESSAGES = 25
MAX_PAGES = 5
MAX_BODY_BYTES = 20_000
_WATCH_PATH = "gmail/v1/users/me/watch"
_HISTORY_PATH = "gmail/v1/users/me/history"


def _expiration(value: Any) -> datetime:
    if isinstance(value, bool):
        raise ValueError("Invalid Gmail watch expiration")
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValueError("Invalid Gmail watch expiration") from None


def _headers(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    headers = payload.get("headers", []) if isinstance(payload, dict) else []
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in headers if isinstance(item, dict)
    }


def _decode_body(data: Any) -> str:
    if not isinstance(data, str):
        return ""
    try:
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        if len(decoded) > MAX_BODY_BYTES:
            return ""
        return decoded.decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _plain_text(payload: Any) -> str:
    """Extract bounded text/plain MIME parts; never descend into attachments."""
    pieces: list[str] = []
    budget = MAX_BODY_BYTES

    def visit(part: Any) -> None:
        nonlocal budget
        if not isinstance(part, dict) or budget <= 0:
            return
        if part.get("filename"):
            return
        mime = str(part.get("mimeType", "")).lower()
        raw_body = part.get("body")
        body = raw_body if isinstance(raw_body, dict) else {}
        data = body.get("data")
        if mime == "text/plain" and data:
            decoded = _decode_body(data)
            encoded_size = len(decoded.encode("utf-8"))
            if encoded_size <= budget:
                pieces.append(decoded)
                budget -= encoded_size
            return
        children = part.get("parts", [])
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(payload)
    return "\n".join(pieces)[:MAX_BODY_BYTES]


class GmailSourceProvider:
    def __init__(self, nango: Any) -> None:
        self.nango = nango

    @staticmethod
    def _scope(connection: IntegrationConnection) -> tuple[str, str]:
        if (
            connection.provider != "google_gmail" or connection.status != "active"
            or not connection.nango_connection_id or not connection.provider_config_key
        ):
            raise PermissionError("Gmail connection is unavailable")
        return connection.nango_connection_id, connection.provider_config_key

    async def renew_watch(self, session: AsyncSession, connection: IntegrationConnection) -> GmailWatchState:
        scope = self._scope(connection)
        topic = settings.GMAIL_PUBSUB_TOPIC.strip()
        if not topic:
            raise PermissionError("Gmail Pub/Sub topic is not configured")
        member = (await session.exec(select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == connection.workspace_id,
            WorkspaceMembership.user_id == connection.user_id,
        ))).first()
        policy = (await session.exec(select(SourceMonitoringPolicy).where(
            SourceMonitoringPolicy.workspace_id == connection.workspace_id,
            SourceMonitoringPolicy.user_id == connection.user_id,
            SourceMonitoringPolicy.source == "gmail",
        ))).first()
        if member is None or policy is None:
            raise PermissionError("Gmail monitoring is not authorized")
        state = (await session.exec(select(GmailWatchState).where(
            GmailWatchState.integration_connection_id == connection.id,
        ))).first()
        if state is None:
            state = GmailWatchState(integration_connection_id=connection.id, status="paused")
            session.add(state)
            await session.flush()
        if state.status == "needs_resync":
            raise PermissionError("Gmail cursor requires explicit rebaseline")
        connection_id, provider_config_key = scope
        label_ids = [
            label for label in (policy.gmail_label_ids or ["INBOX"])
            if label not in {"SPAM", "TRASH"}
        ] or ["INBOX"]
        response = await self.nango.proxy_request(
            "POST", _WATCH_PATH, connection_id=connection_id, provider_config_key=provider_config_key,
            json_body={"topicName": topic, "labelIds": label_ids, "labelFilterBehavior": "include"},
        )
        body = response.body if isinstance(response.body, dict) else {}
        expiration_at = _expiration(body.get("expiration"))
        # A renewal's historyId is not a safe replacement for an existing cursor.
        if state.history_id is None and isinstance(body.get("historyId"), (str, int)):
            state.history_id = str(body["historyId"])
        state.expiration_at = expiration_at
        state.last_renewed_at = datetime.now(timezone.utc)
        state.label_ids = label_ids
        state.status = "active"
        await session.flush()
        return state

    async def process_history(self, session: AsyncSession, connection: IntegrationConnection) -> int:
        scope = self._scope(connection)
        connection_id, provider_config_key = scope
        state = (await session.exec(select(GmailWatchState).where(
            GmailWatchState.integration_connection_id == connection.id,
        ))).first()
        if state is None or state.status != "active" or not state.history_id:
            return 0
        policy = (await session.exec(select(SourceMonitoringPolicy.id).where(
            SourceMonitoringPolicy.workspace_id == connection.workspace_id,
            SourceMonitoringPolicy.user_id == connection.user_id,
            SourceMonitoringPolicy.source == "gmail",
        ))).first()
        if policy is None:
            return 0
        count = 0
        base_history_id = state.history_id
        saw_record = False
        page_token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"startHistoryId": base_history_id, "historyTypes": "messageAdded"}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = await self.nango.proxy_request(
                    "GET", _HISTORY_PATH, connection_id=connection_id,
                    provider_config_key=provider_config_key, params=params,
                )
            except NangoClientError as exc:
                if exc.status_code == 404:
                    state.status = "needs_resync"
                    state.page_token = None
                    await session.commit()
                    return count
                raise
            body = response.body if isinstance(response.body, dict) else {}
            records = body.get("history", [])
            if not isinstance(records, list):
                raise ValueError("Invalid Gmail history response")
            for record in records:
                saw_record = True
                if not isinstance(record, dict) or not isinstance(record.get("id"), (str, int)):
                    raise ValueError("Invalid Gmail history record")
                added = record.get("messagesAdded", [])
                if not isinstance(added, list):
                    raise ValueError("Invalid Gmail messagesAdded record")
                valid: list[dict[str, Any]] = []
                for item in added:
                    message = item.get("message") if isinstance(item, dict) else None
                    if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                        raise ValueError("Invalid Gmail messageAdded item")
                    valid.append(message)
                # Never checkpoint a partial record. Oversized records require explicit resync.
                if len(valid) > MAX_MESSAGES:
                    state.status = "needs_resync"
                    state.last_error_kind = "history_record_too_large"
                    state.page_token = None
                    await session.commit()
                    return count
                if count + len(valid) > MAX_MESSAGES:
                    # This record has not been processed; preserve the last complete cursor.
                    return count
                processed_ids: set[str] = set()
                for message in valid:
                    message_id = message["id"]
                    if message_id in processed_ids:
                        continue
                    processed_ids.add(message_id)
                    await self._process_message(session, connection, scope, message)
                state.history_id = str(record["id"])
                state.page_token = None
                await session.commit()
                count += len(processed_ids)
            next_token = body.get("nextPageToken")
            if not next_token:
                final_history_id = body.get("historyId")
                if not saw_record and isinstance(final_history_id, (str, int)) and not isinstance(final_history_id, bool):
                    final_cursor = str(final_history_id)
                    if final_cursor.isdigit():
                        state.history_id = final_cursor
                        await session.commit()
                return count
            if not isinstance(next_token, str) or next_token in seen_tokens:
                state.status = "needs_resync"
                await session.commit()
                return count
            seen_tokens.add(next_token)
            page_token = next_token
        # Resume from the last complete history record, never a partial record/page.
        return count

    async def _process_message(
        self, session: AsyncSession, connection: IntegrationConnection, scope: tuple[str, str],
        history_message: dict[str, Any],
    ) -> None:
        message_id = history_message["id"]
        connection_id, provider_config_key = scope
        external_id = f"{connection.id}:{message_id}"
        existing = (await session.exec(select(NormalizedEvent).where(
            NormalizedEvent.workspace_id == connection.workspace_id,
            NormalizedEvent.source == "gmail", NormalizedEvent.external_id == external_id,
        ))).first()
        if existing is not None:
            return
        meta_response = await self.nango.proxy_request(
            "GET", f"gmail/v1/users/me/messages/{message_id}", connection_id=connection_id,
            provider_config_key=provider_config_key, params={"format": "metadata"},
        )
        metadata = meta_response.body if isinstance(meta_response.body, dict) else {}
        metadata_payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
        headers = _headers(metadata_payload)
        sender = parseaddr(headers.get("from", ""))[1].strip().lower()
        labels_raw = metadata.get("labelIds", [])
        labels = tuple(item for item in labels_raw if isinstance(item, str)) if isinstance(labels_raw, list) else ()
        internal_date = metadata.get("internalDate")
        try:
            if isinstance(internal_date, bool) or not isinstance(internal_date, (str, int)):
                raise ValueError
            occurred = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            raise ValueError("Invalid Gmail internalDate") from None
        candidate = EventCandidate(
            workspace_id=connection.workspace_id, user_id=connection.user_id, source="gmail",
            type="EMAIL_RECEIVED", occurred_at=occurred, content="", external_id=external_id,
            conversation_id=metadata.get("threadId") or history_message.get("threadId"),
            sender=sender, subject=headers.get("subject", "")[:500], gmail_label_ids=labels,
        )
        if not await gmail_policy_allows(candidate, session):
            return
        full_response = await self.nango.proxy_request(
            "GET", f"gmail/v1/users/me/messages/{message_id}", connection_id=connection_id,
            provider_config_key=provider_config_key, params={"format": "full"},
        )
        full = full_response.body if isinstance(full_response.body, dict) else {}
        payload = full.get("payload") if isinstance(full.get("payload"), dict) else {}
        content = _plain_text(payload)
        if not content:
            snippet = full.get("snippet")
            content = snippet[:500] if isinstance(snippet, str) else ""
        candidate = replace(candidate, content=content)
        event = await ingest(candidate, session)
        if event is None:
            return
        workspace = await session.get(Workspace, connection.workspace_id)
        if workspace is None:
            return
        results = await classify_event(
            content, occurred_at=occurred, timezone=workspace.timezone, locale=workspace.locale,
        )
        await materialize_results(session, event, results, timezone=workspace.timezone, automation_mode="SUGGESTIVE")
