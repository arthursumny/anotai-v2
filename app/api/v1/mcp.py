"""AnotAI — CRUD de conexões MCP do usuário + teste de conexão."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.mcp_server import UserMCPServer
from app.models.user import User
from app.schemas.mcp import MCPServerCreate, MCPServerRead, MCPServerTestResult, MCPServerUpdate
from app.services import mcp_client
from app.services.dispatcher import load_user_secrets
from app.services import mcp_oauth

router = APIRouter()


@router.get("/providers")
async def list_providers(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[dict]:
    """Catálogo guiado de conexões conhecidas e seu estado para esta conta."""
    result = await session.exec(select(UserMCPServer).where(UserMCPServer.user_id == current.id))
    connected = {server.provider_id for server in result.all() if server.provider_id}
    return [{**item, "is_connected": item["id"] in connected} for item in mcp_oauth.catalog()]


@router.post("/providers/{provider_id}/oauth/start")
async def start_provider_oauth(
    provider_id: str,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> dict[str, str]:
    try:
        return await mcp_oauth.start_oauth(session, current.id, provider_id)
    except mcp_oauth.MCPOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/oauth/callback", include_in_schema=False)
async def provider_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    iss: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if error:
        return RedirectResponse(mcp_oauth.oauth_redirect(), status_code=status.HTTP_303_SEE_OTHER)
    target = await mcp_oauth.finish_oauth(session, code, state, iss)
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


async def _get_owned(session: AsyncSession, server_id: uuid.UUID, user_id: uuid.UUID) -> UserMCPServer:
    server = await session.get(UserMCPServer, server_id)
    if server is None or server.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Servidor MCP não encontrado")
    return server


@router.get("", response_model=list[MCPServerRead])
async def list_servers(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[UserMCPServer]:
    result = await session.exec(
        select(UserMCPServer).where(UserMCPServer.user_id == current.id).order_by(UserMCPServer.name.asc()).offset(offset).limit(limit)  # type: ignore[arg-type]
    )
    return list(result.all())


@router.post("", response_model=MCPServerRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    data: MCPServerCreate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> UserMCPServer:
    server = UserMCPServer(
        user_id=current.id,
        name=data.name.strip(),
        server_url=data.server_url,
        auth_header_key=data.auth_header_key,
        auth_secret_name=data.auth_secret_name,
    )
    session.add(server)
    await session.commit()
    await session.refresh(server)
    return server


@router.post("/{server_id}/test", response_model=MCPServerTestResult)
async def test_connection(
    server_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> MCPServerTestResult:
    """Lista as tools do servidor (valida URL, rede, auth e protocolo)."""
    server = await _get_owned(session, server_id, current.id)
    secrets = await load_user_secrets(session, current.id)
    try:
        access_token = await mcp_oauth.get_access_token(session, server, current.id)
    except mcp_oauth.MCPOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if access_token:
        headers = {"Authorization": f"Bearer {access_token}"}
        missing: list[str] = []
    else:
        headers, missing = mcp_client.resolve_mcp_auth(server.auth_header_key, server.auth_secret_name, secrets)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Segredo ${missing[0]} não configurado pelo usuário",
            )
    server_url, missing_url = mcp_client.resolve_server_url(server.server_url, secrets)
    if missing_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Segredo ${missing_url[0]} não configurado pelo usuário",
        )
    try:
        tools = await mcp_client.list_tools(server_url, headers)
    except mcp_client.MCPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return MCPServerTestResult(ok=True, server_id=server.id, tools=tools)


@router.patch("/{server_id}", response_model=MCPServerRead)
async def update_server(
    server_id: uuid.UUID,
    data: MCPServerUpdate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> UserMCPServer:
    server = await _get_owned(session, server_id, current.id)
    patch = data.model_dump(exclude_unset=True)
    if "name" in patch and patch["name"] is not None:
        patch["name"] = patch["name"].strip()
    for key, value in patch.items():
        setattr(server, key, value)
    session.add(server)
    await session.commit()
    await session.refresh(server)
    return server


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> None:
    server = await _get_owned(session, server_id, current.id)
    await session.delete(server)
    await session.commit()
