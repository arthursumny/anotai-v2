"""Persist real automation inputs and execute through the production ledger."""

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.generated_document import GeneratedDocument
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.action_executor import run_action_execution
from app.services.template_actions import prepare_template_actions


async def persist_generation(session, note, template):
    user = await session.get(User, note.user_id)
    if user is None:
        workspace = Workspace(name="Automation tests")
        user = User(
            id=note.user_id, workspace_id=workspace.id, name="Owner",
            email=f"{uuid.uuid4()}@example.test", hashed_password="unused",
        )
        session.add_all([workspace, user])
        await session.flush()
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    note.template_id = template.id
    note.title = note.title or "Generated title"
    note.summary = note.summary or "Generated summary"
    note.body = note.body or "Generated body"
    session.add_all([template, note])
    await session.flush()
    document = GeneratedDocument(
        workspace_id=user.workspace_id, user_id=user.id, note_id=note.id,
        template_id=template.id, template_version=str(template.version),
        structured_data=note.structured_data or {}, rendered_content=note.body,
    )
    session.add(document)
    await session.commit()
    return user, document


async def execute_actions(session, note):
    rows = await prepare_template_actions(session, note, note.user_id)
    await session.commit()
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)
    for row in rows:
        await run_action_execution(factory, row.id)
        await session.refresh(row)
    return rows
