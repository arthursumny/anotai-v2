"""AnotAI — marketplace de templates públicos + clone."""

import copy
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.template import Template
from app.models.user import User
from app.schemas.template import TemplateRead

router = APIRouter()


@router.get("", response_model=list[TemplateRead])
async def list_public(
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[Template]:
    stmt = select(Template).where(Template.is_public == True)  # noqa: E712
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Template.title.ilike(like), Template.purpose.ilike(like)))  # type: ignore[attr-defined]
    result = await session.exec(stmt.order_by(Template.created_at.desc()).offset(offset).limit(limit))  # type: ignore[arg-type]
    return list(result.all())


@router.post("/{template_id}/clone", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
async def clone_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> Template:
    source = await session.get(Template, template_id)
    if source is None or not source.is_public:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template público não encontrado")
    cloned = Template(
        user_id=current.id,
        title=f"{source.title} (cópia)",
        purpose=source.purpose,
        semantic_trigger=source.semantic_trigger,
        example_output=source.example_output,
        output_schema=dict(source.output_schema),
        category=source.category,
        use_cases=copy.deepcopy(source.use_cases),
        routing_description=source.routing_description,
        # Version tracks edits to this user's copy, not the source snapshot.
        version=1,
        actions=[
            {**action, "enabled": False} if isinstance(action, dict) else copy.deepcopy(action)
            for action in copy.deepcopy(source.actions)
        ],
        is_public=False,
    )
    session.add(cloned)
    await session.commit()
    await session.refresh(cloned)
    return cloned
