"""Focused policy, ownership, persistence and migration tests for event ingestion."""

import importlib.metadata
import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The repository's Alembic scripts directory shadows the installed package.
_alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", str(_alembic_package / "__init__.py"),
    submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

import sqlalchemy as sa
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlmodel import select

from app.models.normalized_event import NormalizedEvent
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.event_ingestion import (
    EventCandidate,
    UnauthorizedWorkspace,
    UnsupportedEventSource,
    ingest,
    whatsapp_policy_allows,
)


async def _identity(session):
    user = User(
        name="Event tester", email=f"{uuid.uuid4()}@example.com", hashed_password="not-used",
    )
    workspace = Workspace(name="Test", is_personal=True)
    session.add(workspace)
    await session.flush()
    user.workspace_id = workspace.id
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    await session.flush()
    return user, workspace


def _candidate(user, workspace, **overrides):
    values = {
        "workspace_id": workspace.id,
        "user_id": user.id,
        "source": "whatsapp",
        "type": "message",
        "occurred_at": datetime(2025, 1, 1, 12, tzinfo=timezone.utc),
        "content": "sensitive message body",
        "external_id": f"external-{uuid.uuid4()}",
        "conversation_id": "chat-1",
    }
    values.update(overrides)
    return EventCandidate(**values)


async def _policy(session, user, workspace, source="whatsapp", **overrides):
    values = {
        "workspace_id": workspace.id,
        "user_id": user.id,
        "source": source,
        "mode": "ALL",
        "selected_ids": [],
        "direction": "both",
    }
    values.update(overrides)
    policy = SourceMonitoringPolicy(**values)
    session.add(policy)
    await session.flush()
    return policy


async def test_allowlist_and_blocklist_filter_conversations(db_session):
    user, workspace = await _identity(db_session)
    await _policy(db_session, user, workspace, mode="ALLOWLIST", selected_ids=["allowed-chat"])
    allowed = await ingest(_candidate(user, workspace, conversation_id="allowed-chat"), db_session)
    blocked = await ingest(_candidate(user, workspace, conversation_id="other-chat"), db_session)
    assert allowed is not None
    assert blocked is None
    assert len((await db_session.exec(select(NormalizedEvent))).all()) == 1

    policy = (await db_session.exec(select(SourceMonitoringPolicy))).one()
    policy.mode = "BLOCKLIST"
    policy.selected_ids = ["blocked-chat"]
    await db_session.flush()
    assert await ingest(_candidate(user, workspace, conversation_id="blocked-chat"), db_session) is None
    assert await ingest(_candidate(user, workspace, conversation_id="allowed-chat"), db_session) is not None


async def test_direction_group_and_individual_filters_honor_from_me(db_session):
    user, workspace = await _identity(db_session)
    await _policy(
        db_session, user, workspace, direction="incoming", allow_groups=False, allow_individuals=True,
    )
    assert await ingest(_candidate(user, workspace, from_me=False, is_group=False), db_session) is not None
    assert await ingest(_candidate(user, workspace, from_me=True, is_group=False), db_session) is None
    assert await ingest(_candidate(user, workspace, from_me=False, is_group=True), db_session) is None
    assert len((await db_session.exec(select(NormalizedEvent))).all()) == 1


async def test_whatsapp_without_conversation_id_is_rejected_even_in_all_mode(db_session):
    user, workspace = await _identity(db_session)
    await _policy(db_session, user, workspace, mode="ALL")
    candidate = _candidate(user, workspace, conversation_id=None, content="must not persist")

    assert await ingest(candidate, db_session) is None
    assert (await db_session.exec(select(NormalizedEvent))).all() == []


async def test_naive_occurred_at_is_rejected_without_persisting(db_session):
    user, workspace = await _identity(db_session)
    candidate = _candidate(
        user, workspace, source="audio", external_id=None,
        occurred_at=datetime(2025, 1, 1, 12), content="must not persist",
    )

    with pytest.raises(ValueError, match="timezone explícito"):
        await ingest(candidate, db_session)
    assert (await db_session.exec(select(NormalizedEvent))).all() == []


async def test_no_external_policy_is_opt_in_and_blocked_content_is_not_stored(db_session):
    user, workspace = await _identity(db_session)
    blocked = _candidate(user, workspace, content="must never persist")
    assert await ingest(blocked, db_session) is None
    assert (await db_session.exec(select(NormalizedEvent))).all() == []

    audio = await ingest(_candidate(user, workspace, source="audio", external_id=None), db_session)
    assert audio is not None
    assert audio.content == "sensitive message body"
    assert audio.metadata_json["capture_mode"] is None
    await _policy(db_session, user, workspace)
    non_audio = await ingest(_candidate(user, workspace), db_session)
    assert non_audio is not None and "capture_mode" not in non_audio.metadata_json


async def test_capture_mode_is_validated_and_only_stored_for_audio(db_session):
    user, workspace = await _identity(db_session)
    valid = await ingest(
        _candidate(user, workspace, source="audio", external_id=None, capture_mode="continuous"),
        db_session,
    )
    assert valid is not None and valid.metadata_json["capture_mode"] == "continuous"

    with pytest.raises(ValueError, match="capture_mode"):
        await ingest(
            _candidate(user, workspace, source="audio", external_id=None, capture_mode="automatic"),
            db_session,
        )
    with pytest.raises(ValueError, match="só é permitido"):
        await ingest(_candidate(user, workspace, capture_mode="intentional"), db_session)


async def test_gmail_policy_filters_labels_and_senders(db_session):
    user, workspace = await _identity(db_session)
    await _policy(
        db_session, user, workspace, source="gmail", gmail_label_ids=["important"],
        gmail_allowed_senders=["alice@example.com"],
    )
    allowed = await ingest(_candidate(
        user, workspace, source="gmail", sender="Alice@Example.com", gmail_label_ids=("important",),
    ), db_session)
    assert allowed is not None
    assert await ingest(_candidate(
        user, workspace, source="gmail", sender="bob@example.com", gmail_label_ids=("important",),
    ), db_session) is None
    assert await ingest(_candidate(
        user, workspace, source="gmail", sender="alice@example.com", gmail_label_ids=("inbox",),
    ), db_session) is None
    assert len((await db_session.exec(select(NormalizedEvent))).all()) == 1


async def test_gmail_selected_ids_are_sender_addresses_and_spam_trash_never_persist(db_session):
    user, workspace = await _identity(db_session)
    await _policy(
        db_session, user, workspace, source="gmail", mode="ALLOWLIST",
        selected_ids=["alice@example.com"], direction="incoming", gmail_label_ids=["INBOX"],
    )
    accepted = await ingest(_candidate(
        user, workspace, source="gmail", sender="Alice Example <ALICE@example.com>",
        gmail_label_ids=("INBOX",), conversation_id="unrelated-thread", from_me=True,
    ), db_session)
    assert accepted is not None  # Gmail sent/thread semantics do not reuse WhatsApp from_me/conversation filters.
    assert await ingest(_candidate(
        user, workspace, source="gmail", sender="bob@example.com", gmail_label_ids=("INBOX",),
    ), db_session) is None
    assert await ingest(_candidate(
        user, workspace, source="gmail", sender="alice@example.com", gmail_label_ids=("SPAM", "INBOX"),
    ), db_session) is None
    assert await ingest(_candidate(
        user, workspace, source="gmail", sender="alice@example.com", gmail_label_ids=("TRASH",),
    ), db_session) is None
    assert len((await db_session.exec(select(NormalizedEvent))).all()) == 1

    policy = (await db_session.exec(select(SourceMonitoringPolicy))).one()
    policy.mode = "BLOCKLIST"
    await db_session.flush()
    assert await ingest(_candidate(
        user, workspace, source="gmail", sender="alice@example.com", gmail_label_ids=("INBOX",),
    ), db_session) is None
    assert await ingest(_candidate(
        user, workspace, source="gmail", sender="bob@example.com", gmail_label_ids=("INBOX",),
    ), db_session) is not None


async def test_workspace_authorization_duplicate_and_source_allowlist(db_session):
    user, workspace = await _identity(db_session)
    await _policy(db_session, user, workspace)
    candidate = _candidate(user, workspace)
    first = await ingest(candidate, db_session)
    again = await ingest(candidate, db_session)
    assert first is not None and again is not None
    assert first.id == again.id
    assert len((await db_session.exec(select(NormalizedEvent))).all()) == 1

    unauthorized = _candidate(user, workspace, workspace_id=uuid.uuid4())
    with pytest.raises(UnauthorizedWorkspace):
        await ingest(unauthorized, db_session)
    with pytest.raises(UnsupportedEventSource):
        await ingest(_candidate(user, workspace, source="made-up"), db_session)


async def test_whatsapp_policy_revalidation_observes_current_policy_and_membership(db_session):
    user, workspace = await _identity(db_session)
    candidate = _candidate(user, workspace, from_me=False, is_group=False)
    policy = await _policy(db_session, user, workspace, direction="both")
    assert await whatsapp_policy_allows(candidate, db_session) is True

    policy.direction = "outgoing"
    await db_session.flush()
    assert await whatsapp_policy_allows(candidate, db_session) is False

    policy.direction = "both"
    policy.mode = "BLOCKLIST"
    policy.selected_ids = [candidate.conversation_id or ""]
    await db_session.flush()
    assert await whatsapp_policy_allows(candidate, db_session) is False

    await db_session.delete(policy)
    await db_session.flush()
    assert await whatsapp_policy_allows(candidate, db_session) is False


def test_migration_adds_and_drops_only_ingestion_tables():
    migration_path = Path(__file__).parents[1] / "alembic/versions/8f31c2a6d940_normalized_event_ingestion.py"
    spec = importlib.util.spec_from_file_location("ingestion_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workspaces = sa.Table("workspaces", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    memberships = sa.Table(
        "workspace_memberships", metadata, sa.Column("id", sa.Uuid(), primary_key=True),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
        connection.execute(sa.insert(users).values(id=user_id))
        connection.execute(sa.insert(workspaces).values(id=workspace_id))
        connection.execute(sa.insert(memberships).values(id=uuid.uuid4()))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "normalized_events" in sa.inspect(connection).get_table_names()
            assert "source_monitoring_policies" in sa.inspect(connection).get_table_names()
            migration.downgrade()
        names = set(sa.inspect(connection).get_table_names())
        assert connection.scalar(sa.select(sa.func.count()).select_from(users)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(workspaces)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(memberships)) == 1
    assert "normalized_events" not in names
    assert "source_monitoring_policies" not in names
