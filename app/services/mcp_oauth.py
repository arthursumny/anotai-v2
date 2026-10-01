"""Catálogo de integrações e OAuth seguro para MCPs remotos conhecidos."""

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.mcp_encryption import MCPEncryptionError, decrypt_payload, encrypt_payload
from app.models.mcp_oauth import MCPOAuthFlow, UserMCPOAuthCredential
from app.models.mcp_server import UserMCPServer


class MCPOAuthError(RuntimeError):
    pass


PROVIDERS: dict[str, dict[str, Any]] = {
    "google_gmail": {
        "name": "Gmail",
        "brand": "Google",
        "url": "https://gmailmcp.googleapis.com/mcp/v1",
        "kind": "google",
        "description": "Encontre mensagens e prepare rascunhos a partir das suas notas.",
        "scope_text": "Ler mensagens e criar rascunhos. O Anotaí não envia e-mails sem uma automação sua.",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"],
        "allowed_hosts": ["gmailmcp.googleapis.com"],
    },
    "google_calendar": {
        "name": "Google Agenda",
        "brand": "Google",
        "url": "https://calendarmcp.googleapis.com/mcp/v1",
        "kind": "google",
        "description": "Organize compromissos e prepare eventos com base no que você gravar.",
        "scope_text": "Ver e editar eventos da sua agenda.",
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
        "allowed_hosts": ["calendarmcp.googleapis.com"],
    },
    "google_sheets": {
        "name": "Google Planilhas",
        "brand": "Google",
        "url": "https://sheetsmcp.googleapis.com/mcp/v1",
        "kind": "google",
        "description": "Registre informações estruturadas em planilhas.",
        "scope_text": "Ver e atualizar planilhas e arquivos criados ou abertos no Anotaí.",
        "scopes": ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/spreadsheets"],
        "allowed_hosts": ["sheetsmcp.googleapis.com"],
    },
    "notion": {
        "name": "Notion",
        "brand": "Notion",
        "url": "https://mcp.notion.com/mcp",
        "kind": "mcp_oauth",
        "description": "Guarde e encontre notas e tarefas no seu espaço de trabalho.",
        "scope_text": "Acesso autorizado pelo seu espaço Notion.",
        "allowed_hosts": ["mcp.notion.com"],
        "auth_hosts": ["mcp.notion.com"],
    },
    "evernote": {
        "name": "Evernote",
        "brand": "Evernote",
        "url": "https://mcp.evernote.com/mcp",
        "kind": "mcp_oauth",
        "description": "Salve e pesquise notas, cadernos, marcadores e tarefas.",
        "scope_text": "Ler, criar e atualizar conteúdo. Acesso para apagar não é solicitado.",
        "scopes": ["read", "create", "write"],
        "allowed_hosts": ["mcp.evernote.com"],
        "auth_hosts": ["accounts.evernote.com"],
    },
}

_GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_ISSUER = "https://accounts.google.com"
_CLIENT_NAME = "Anotaí"


def _has_public_urls() -> bool:
    return bool(settings.MCP_PUBLIC_BASE_URL.strip() and settings.MCP_WEB_APP_URL.strip())


def catalog() -> list[dict[str, Any]]:
    try:
        encrypt_payload({"readiness_check": True})
        encryption_ready = True
    except MCPEncryptionError:
        encryption_ready = False
    try:
        _validate_app_urls()
        app_urls_ready = _has_public_urls()
    except MCPOAuthError:
        app_urls_ready = False
    result: list[dict[str, Any]] = []
    for provider_id, provider in PROVIDERS.items():
        needs_google_client = provider["kind"] == "google"
        has_google_client = bool(settings.GOOGLE_OAUTH_CLIENT_ID.strip() and settings.GOOGLE_OAUTH_CLIENT_SECRET.strip())
        missing: list[str] = []
        if not encryption_ready:
            missing.append("chave de proteção de credenciais")
        if not app_urls_ready:
            missing.append("URLs de retorno HTTPS do aplicativo")
        if needs_google_client and not has_google_client:
            missing.append("cliente OAuth do Google Cloud")
        result.append({
            "id": provider_id,
            "name": provider["name"],
            "brand": provider["brand"],
            "description": provider["description"],
            "scope_text": provider["scope_text"],
            "server_url": provider["url"],
            "is_ready": not missing,
            "setup_hint": "Configuração da plataforma pendente: " + ", ".join(missing) if missing else None,
            "beta": provider_id == "evernote",
        })
    return result


def _origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise MCPOAuthError("O provedor retornou um endereço de autorização inseguro.")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _validate_endpoint(url: str, allowed_hosts: list[str]) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed = any(host == item or host.endswith("." + item) for item in allowed_hosts)
    if parts.scheme != "https" or not allowed or parts.username or parts.password:
        raise MCPOAuthError("O provedor retornou um endereço de autorização não reconhecido.")
    return url


async def _get_json(client: httpx.AsyncClient, url: str, allowed_hosts: list[str]) -> dict[str, Any]:
    _validate_endpoint(url, allowed_hosts)
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MCPOAuthError("Não foi possível iniciar a autorização com esse aplicativo.") from exc
    if not isinstance(value, dict):
        raise MCPOAuthError("O provedor retornou dados de autorização inválidos.")
    return value


async def _discover_oauth(provider: dict[str, Any]) -> tuple[str, dict[str, Any], str, list[str]]:
    server_url = str(provider["url"])
    resource_origin = _origin(server_url)
    hosts = list(provider["auth_hosts"])
    resource_url = f"{resource_origin}/.well-known/oauth-protected-resource"
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        resource_metadata = await _get_json(client, resource_url, list(provider["allowed_hosts"]))
        servers = resource_metadata.get("authorization_servers")
        if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
            raise MCPOAuthError("O provedor ainda não oferece autorização automática compatível.")
        auth_server = str(servers[0])
        auth_origin = _origin(auth_server)
        if not any(urlsplit(auth_origin).hostname == h for h in hosts):
            raise MCPOAuthError("O provedor apontou para um servidor de autorização desconhecido.")
        metadata_url = f"{auth_origin}/.well-known/oauth-authorization-server"
        metadata = await _get_json(client, metadata_url, hosts)
    if not metadata.get("authorization_endpoint") or not metadata.get("token_endpoint"):
        raise MCPOAuthError("O provedor não publicou os endereços OAuth necessários.")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if metadata.get(key):
            _validate_endpoint(str(metadata[key]), hosts)
    scopes = provider.get("scopes") or resource_metadata.get("scopes_supported") or []
    scopes = [str(scope) for scope in scopes if isinstance(scope, str)]
    issuer = str(metadata.get("issuer") or auth_origin)
    try:
        if _origin(issuer) != auth_origin:
            raise MCPOAuthError("O provedor publicou uma origem OAuth diferente da autorizada.")
    except MCPOAuthError:
        raise
    return str(resource_metadata.get("resource") or resource_origin), metadata, issuer, scopes


def _callback_url() -> str:
    return f"{settings.MCP_PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_PREFIX}/mcp/oauth/callback"


def _validate_app_urls() -> None:
    for configured in (settings.MCP_PUBLIC_BASE_URL.strip(), settings.MCP_WEB_APP_URL.strip()):
        parts = urlsplit(configured)
        local_http = parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}
        if not parts.hostname or (parts.scheme != "https" and not local_http):
            raise MCPOAuthError("Os endereços OAuth da plataforma precisam usar HTTPS.")


async def start_oauth(
    session: AsyncSession, user_id: Any, provider_id: str
) -> dict[str, str]:
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise MCPOAuthError("Esse aplicativo ainda não está no catálogo de conexões.")
    try:
        if not settings.MCP_TOKEN_ENCRYPTION_KEY.strip():
            raise MCPOAuthError("A plataforma ainda precisa configurar a proteção das credenciais.")
    except MCPEncryptionError as exc:
        raise MCPOAuthError(str(exc)) from exc
    if not _has_public_urls():
        raise MCPOAuthError("A plataforma ainda precisa configurar os endereços de retorno OAuth.")
    _validate_app_urls()

    callback = _callback_url()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode()).hexdigest()

    if provider["kind"] == "google":
        if not settings.GOOGLE_OAUTH_CLIENT_ID.strip() or not settings.GOOGLE_OAUTH_CLIENT_SECRET.strip():
            raise MCPOAuthError("A plataforma ainda precisa configurar o cliente OAuth do Google Cloud.")
        client_id = settings.GOOGLE_OAUTH_CLIENT_ID.strip()
        client_secret = None  # Secret stays in environment; it is never stored in flow/token rows.
        authorization_endpoint = _GOOGLE_AUTHORIZATION_ENDPOINT
        token_endpoint = _GOOGLE_TOKEN_ENDPOINT
        issuer = _GOOGLE_ISSUER
        resource = None
        scopes = list(provider["scopes"])
        issuer_supported = False
    else:
        try:
            resource, metadata, issuer, scopes = await _discover_oauth(provider)
            registration_endpoint = metadata.get("registration_endpoint")
            if not registration_endpoint:
                raise MCPOAuthError("O provedor não permite cadastrar o Anotaí como cliente OAuth.")
            registration = {
                "client_name": _CLIENT_NAME,
                "redirect_uris": [callback],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                _validate_endpoint(str(registration_endpoint), list(provider["auth_hosts"]))
                response = await client.post(
                    str(registration_endpoint),
                    json=registration,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                registered = response.json()
            if not isinstance(registered, dict) or not registered.get("client_id"):
                raise MCPOAuthError("O provedor não retornou o identificador OAuth necessário.")
            client_id = str(registered["client_id"])
            client_secret = None
            authorization_endpoint = str(metadata["authorization_endpoint"])
            token_endpoint = str(metadata["token_endpoint"])
            issuer_supported = bool(metadata.get("authorization_response_iss_parameter_supported"))
        except MCPOAuthError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MCPOAuthError("Não foi possível registrar o Anotaí neste provedor.") from exc

    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        auth_params["scope"] = " ".join(scopes)
    if resource:
        auth_params["resource"] = resource
    if provider["kind"] == "google":
        auth_params.update({"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"})
    authorization_url = f"{authorization_endpoint}?{urlencode(auth_params)}"

    flow_payload = {
        "server_url": provider["url"],
        "provider_name": provider["name"],
        "callback_url": callback,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_endpoint": token_endpoint,
        "issuer": issuer,
        "issuer_supported": issuer_supported,
        "resource": resource,
        "scopes": scopes,
        "code_verifier": verifier,
        "kind": provider["kind"],
    }
    try:
        encrypted = encrypt_payload(flow_payload)
    except MCPEncryptionError as exc:
        raise MCPOAuthError(str(exc)) from exc
    session.add(MCPOAuthFlow(
        user_id=user_id,
        provider_id=provider_id,
        state_hash=state_hash,
        payload_ciphertext=encrypted,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    ))
    await session.commit()
    return {"authorization_url": authorization_url, "provider_id": provider_id}


def oauth_redirect(provider_id: str | None = None, success: bool = False) -> str:
    base = settings.MCP_WEB_APP_URL.rstrip("/") + "/mcp"
    params = {"oauth": "success" if success else "error"}
    if provider_id:
        params["provider"] = provider_id
    return f"{base}?{urlencode(params)}"


async def finish_oauth(
    session: AsyncSession,
    code: str | None,
    state: str | None,
    issuer_response: str | None,
) -> str:
    if not code or not state:
        return oauth_redirect()
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    result = await session.exec(select(MCPOAuthFlow).where(MCPOAuthFlow.state_hash == state_hash))
    flow = result.first()
    now = datetime.now(timezone.utc)
    if flow is None or flow.consumed_at is not None or flow.expires_at.replace(tzinfo=flow.expires_at.tzinfo or timezone.utc) <= now:
        return oauth_redirect()
    flow.consumed_at = now
    session.add(flow)
    await session.commit()
    try:
        provider = PROVIDERS[flow.provider_id]
        payload = decrypt_payload(flow.payload_ciphertext)
        if issuer_response and issuer_response != payload["issuer"]:
            raise MCPOAuthError("A resposta de autorização não corresponde ao provedor selecionado.")
        if payload.get("issuer_supported") and not issuer_response:
            raise MCPOAuthError("O provedor não confirmou a origem da autorização.")

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": payload["callback_url"],
            "client_id": payload["client_id"],
            "code_verifier": payload["code_verifier"],
        }
        if payload.get("resource"):
            form["resource"] = payload["resource"]
        if payload.get("kind") == "google":
            form["client_secret"] = settings.GOOGLE_OAUTH_CLIENT_SECRET.strip()
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.post(
                payload["token_endpoint"],
                data=form,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            tokens = response.json()
        if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str):
            raise MCPOAuthError("O provedor não retornou uma credencial utilizável.")

        credentials = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "token_type": str(tokens.get("token_type") or "Bearer"),
            "expires_at": int(now.timestamp()) + int(tokens["expires_in"]) if tokens.get("expires_in") else None,
            "token_endpoint": payload["token_endpoint"],
            "client_id": payload["client_id"],
            "client_secret": (
                settings.GOOGLE_OAUTH_CLIENT_SECRET.strip()
                if payload.get("kind") == "google"
                else payload.get("client_secret")
            ),
            "resource": payload.get("resource"),
            "scopes": payload.get("scopes", []),
        }
        encrypted_credentials = encrypt_payload(credentials)
        server_result = await session.exec(select(UserMCPServer).where(
            UserMCPServer.user_id == flow.user_id,
            UserMCPServer.provider_id == flow.provider_id,
        ))
        server = server_result.first()
        if server is None:
            server = UserMCPServer(
                user_id=flow.user_id,
                name=provider["name"],
                server_url=provider["url"],
                provider_id=flow.provider_id,
                is_active=True,
            )
            session.add(server)
            await session.flush()
        else:
            server.name = provider["name"]
            server.server_url = provider["url"]
            server.is_active = True
            session.add(server)
        credential_result = await session.exec(select(UserMCPOAuthCredential).where(
            UserMCPOAuthCredential.server_id == server.id,
        ))
        credential = credential_result.first()
        if credential is None:
            credential = UserMCPOAuthCredential(
                server_id=server.id,
                provider_id=flow.provider_id,
                token_ciphertext=encrypted_credentials,
                updated_at=now,
            )
        else:
            credential.provider_id = flow.provider_id
            credential.token_ciphertext = encrypted_credentials
            credential.updated_at = now
        session.add(credential)
        await session.commit()
        return oauth_redirect(flow.provider_id, success=True)
    except (MCPOAuthError, MCPEncryptionError, KeyError, ValueError, httpx.HTTPError) as exc:
        await session.rollback()
        # Do not send provider details, authorization codes, or tokens to the URL/log.
        if isinstance(exc, MCPOAuthError) and str(exc) == "A resposta de autorização não corresponde ao provedor selecionado.":
            return oauth_redirect()
        return oauth_redirect(flow.provider_id, success=False)


async def get_access_token(session: AsyncSession, server: UserMCPServer, user_id: Any) -> str | None:
    """Retorna token atual e renova-o quando necessário; segredos nunca vão ao cliente."""
    if not server.provider_id:
        return None
    result = await session.exec(select(UserMCPOAuthCredential).where(
        UserMCPOAuthCredential.server_id == server.id,
        UserMCPOAuthCredential.provider_id == server.provider_id,
    ))
    credential = result.first()
    if credential is None:
        return None
    try:
        data = decrypt_payload(credential.token_ciphertext)
    except MCPEncryptionError as exc:
        raise MCPOAuthError(str(exc)) from exc
    expires_at = data.get("expires_at")
    now = datetime.now(timezone.utc)
    if isinstance(expires_at, (int, float)) and expires_at <= now.timestamp() + 60:
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            raise MCPOAuthError("A autorização expirou. Reconecte o aplicativo.")
        form: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token),
            "client_id": str(data["client_id"]),
        }
        if data.get("client_secret"):
            form["client_secret"] = str(data["client_secret"])
        if data.get("resource"):
            form["resource"] = str(data["resource"])
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.post(data["token_endpoint"], data=form, headers={"Accept": "application/json"})
                response.raise_for_status()
                refreshed = response.json()
            if not isinstance(refreshed, dict) or not isinstance(refreshed.get("access_token"), str):
                raise MCPOAuthError("A autorização expirou. Reconecte o aplicativo.")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise MCPOAuthError("A autorização expirou. Reconecte o aplicativo.") from exc
        data["access_token"] = refreshed["access_token"]
        data["refresh_token"] = refreshed.get("refresh_token") or refresh_token
        data["expires_at"] = int(now.timestamp()) + int(refreshed["expires_in"]) if refreshed.get("expires_in") else None
        data["token_type"] = str(refreshed.get("token_type") or data.get("token_type") or "Bearer")
        credential.token_ciphertext = encrypt_payload(data)
        credential.updated_at = now
        session.add(credential)
        await session.commit()
    return str(data.get("access_token") or "") or None
