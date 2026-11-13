"""DOCUMENT_GENERATED emitido pelo pipeline real de gravação→nota."""

import uuid

from sqlmodel import select

import app.services.note_pipeline as pipeline
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership


async def _scope(session, *, with_template: bool = True):
    workspace = Workspace(name="Trigger workspace")
    user = User(
        name="Trigger user",
        email=f"trigger-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add_all([workspace, user])
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    template = None
    if with_template:
        template = Template(
            user_id=user.id,
            title="Ata de reunião",
            purpose="Registrar decisões",
            semantic_trigger="reunião",
            example_output="## Decisões\n- Exemplo",
            output_schema={"type": "object"},
            actions=[],
            version=1,
        )
        session.add(template)
    await session.flush()
    return workspace, user, template


async def _processed_note(session, user, template) -> Note:
    note = Note(
        user_id=user.id,
        title="Nota pronta",
        body="## Documento\nConteúdo gerado.",
        summary="Resumo.",
        raw_transcription="Transcrição da conversa.",
        structured_data={"tipo": "nota_geral"},
        template_id=template.id if template else None,
        status=NoteStatus.PROCESSED,
    )
    session.add(note)
    await session.commit()
    return note


async def _workflow(session, workspace, user, *, conditions=None, enabled=True):
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=user.id,
        title="Fluxo da nota pronta",
        trigger_type="DOCUMENT_GENERATED",
        conditions=conditions or {},
        enabled=enabled,
    )
    session.add(workflow)
    await session.flush()
    session.add(WorkflowStep(
        workflow_id=workflow.id,
        position=0,
        action_type="CREATE_TASK",
        input_template={"title": "Revisar a nota"},
        require_confirmation=True,
        enabled=True,
    ))
    await session.commit()
    return workflow


async def test_processed_note_emits_document_generated_for_matching_template(db_session):
    workspace, user, template = await _scope(db_session)
    workflow = await _workflow(
        db_session, workspace, user, conditions={"source": str(template.id)}
    )
    note = await _processed_note(db_session, user, template)

    await pipeline.process_transcription(db_session, note)

    executions = list((await db_session.exec(select(WorkflowExecution))).all())
    assert len(executions) == 1
    execution = executions[0]
    assert execution.workflow_id == workflow.id
    assert execution.trigger_type == "DOCUMENT_GENERATED"
    assert execution.source_id == str(note.id)
    assert execution.metadata_json == {
        "source": str(template.id),
        "event_type": "DOCUMENT_GENERATED",
    }
    saved = await db_session.get(Note, note.id)
    assert saved is not None and saved.status == NoteStatus.PROCESSED


async def test_workflow_filtered_to_another_template_is_not_executed(db_session):
    workspace, user, template = await _scope(db_session)
    other = Template(
        user_id=user.id,
        title="Outro modelo",
        purpose="Outro",
        semantic_trigger="outro",
        example_output="## Outro",
        output_schema={"type": "object"},
        actions=[],
        version=1,
    )
    db_session.add(other)
    await db_session.commit()
    await _workflow(db_session, workspace, user, conditions={"source": str(other.id)})
    note = await _processed_note(db_session, user, template)

    await pipeline.process_transcription(db_session, note)

    assert list((await db_session.exec(select(WorkflowExecution))).all()) == []


async def test_general_note_without_template_does_not_break_the_pipeline(db_session):
    workspace, user, _ = await _scope(db_session, with_template=False)
    workflow = await _workflow(db_session, workspace, user)
    note = await _processed_note(db_session, user, None)

    await pipeline.process_transcription(db_session, note)

    saved = await db_session.get(Note, note.id)
    assert saved is not None and saved.status == NoteStatus.PROCESSED
    assert saved.template_id is None
    executions = list((await db_session.exec(select(WorkflowExecution))).all())
    # Nota geral emite sem "source": o fluxo sem condição de modelo corresponde.
    assert len(executions) == 1
    assert executions[0].workflow_id == workflow.id
    assert executions[0].metadata_json == {"event_type": "DOCUMENT_GENERATED"}


async def test_trigger_failure_never_prevents_the_note_from_being_saved(db_session, monkeypatch):
    workspace, user, template = await _scope(db_session)
    await _workflow(db_session, workspace, user)
    note = await _processed_note(db_session, user, template)

    async def exploding(*args, **kwargs):
        raise RuntimeError("falha simulada ao emitir o trigger")

    monkeypatch.setattr(pipeline, "emit_trigger", exploding)

    await pipeline.process_transcription(db_session, note)

    saved = await db_session.get(Note, note.id)
    assert saved is not None
    assert saved.status == NoteStatus.PROCESSED
    assert saved.body == "## Documento\nConteúdo gerado."
    assert list((await db_session.exec(select(WorkflowExecution))).all()) == []
