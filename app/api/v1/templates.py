"""AnotAI — CRUD de templates do usuário autenticado."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.template import Template
from app.models.user import User
from app.schemas.template import (
    DeriveRequest,
    DeriveResponse,
    TemplateCreate,
    TemplateDraft,
    TemplateDraftRequest,
    TemplateRead,
    TemplateUpdate,
)
from app.services.template_drafter import DraftError, derive_from_example, draft_template

router = APIRouter()


async def _get_owned(session: AsyncSession, template_id: uuid.UUID, user_id: uuid.UUID) -> Template:
    tpl = await session.get(Template, template_id)
    if tpl is None or tpl.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template não encontrado")
    return tpl


@router.get("", response_model=list[TemplateRead])
async def list_templates(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[Template]:
    result = await session.exec(
        select(Template).where(Template.user_id == current.id).order_by(Template.created_at.desc()).offset(offset).limit(limit)  # type: ignore[arg-type]
    )
    return list(result.all())


@router.post("", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
async def create_template(
    data: TemplateCreate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> Template:
    tpl = Template(
        user_id=current.id,
        title=data.title.strip(),
        purpose=data.purpose.strip(),
        semantic_trigger=data.semantic_trigger.strip(),
        example_output=data.example_output,
        output_schema=data.output_schema,
        actions=[a.model_dump(exclude_unset=True) for a in data.actions],
        category=data.category,
        use_cases=data.use_cases,
        routing_description=data.routing_description,
        is_public=data.is_public,
    )
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)
    return tpl


@router.post("/draft", response_model=TemplateDraft)
async def draft_template_endpoint(
    data: TemplateDraftRequest,
    current: User = Depends(get_current_user),
) -> TemplateDraft:
    """"Construir com IA": gera um rascunho a partir de descrição livre.

    Nada é salvo — o frontend carrega o rascunho no construtor para revisão.
    """
    try:
        draft = await draft_template(data.description)
    except DraftError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return TemplateDraft.model_validate(draft)


@router.post("/derive", response_model=DeriveResponse)
async def derive_template_endpoint(
    data: DeriveRequest,
    current: User = Depends(get_current_user),
) -> DeriveResponse:
    """"Como a nota deve sair": deriva gatilho+campos a partir do exemplo.

    O exemplo é a fonte da verdade do usuário; nada é salvo.
    """
    try:
        derived = await derive_from_example(data.example_output)
    except DraftError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return DeriveResponse.model_validate(derived)


@router.get("/{template_id}", response_model=TemplateRead)
async def get_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> Template:
    return await _get_owned(session, template_id, current.id)


@router.patch("/{template_id}", response_model=TemplateRead)
async def update_template(
    template_id: uuid.UUID,
    data: TemplateUpdate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> Template:
    tpl = await _get_owned(session, template_id, current.id)
    patch = data.model_dump(exclude_unset=True)
    if "actions" in patch and patch["actions"] is not None:
        patch["actions"] = [action.model_dump(exclude_unset=True) for action in data.actions or []]
    changes = {key: value for key, value in patch.items() if getattr(tpl, key) != value}
    if changes:
        for key, value in changes.items():
            setattr(tpl, key, value)
        tpl.version += 1
        session.add(tpl)
        await session.commit()
        await session.refresh(tpl)
    return tpl


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> None:
    tpl = await _get_owned(session, template_id, current.id)
    await session.delete(tpl)
    await session.commit()


