"""Small, strict HTTP adapter for Nango Connect and Google proxy calls.

Connect-session creation reads the documented ``{"data": {...}}`` envelope;
connection lookup reads the documented top-level connection object. Only
connection ID, provider config key, and the two ownership tags are returned;
provider credentials and unrelated tags in the upstream payload are discarded.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONFIG_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_USER_TAG_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")
_PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,49}$")
# `send` is its own alternative so a POST never rides on the message-id route;
# everything else under messages/ stays a read-only id lookup.
_GMAIL_PATH_RE = re.compile(
    r"^gmail/v1/users/me/(?:profile|messages(?:/send|/[A-Za-z0-9_-]+)?|labels(?:/[A-Za-z0-9_-]+)?|history|watch)$"
)
_CALENDAR_PATH_RE = re.compile(
    r"^calendar/v3/calendars/(?:primary|[A-Za-z0-9@._-]{1,200})/events(?:/[A-Za-z0-9_-]{1,200})?$"
)
# Cada app não-Google entra com uma regex JUSTA do que a ação usa de fato.
# Nunca um curinga: a allowlist existe para impedir que um caller aponte o proxy
# para um endpoint arbitrário da conta do usuário. Adicionar app = uma linha aqui
# e uma política de método explícita em `proxy_request`.
_NOTION_PATH_RE = re.compile(r"^v1/pages$")
_SLACK_PATH_RE = re.compile(r"^chat\.postMessage$")
# Apps não-Google são somente POST: são endpoints de escrita, e uma variante de
# leitura entra deliberadamente quando (e se) uma ação precisar dela.
_POST_ONLY_PATH_RES = (_NOTION_PATH_RE, _SLACK_PATH_RE)
_ALLOWED_PROXY_PATH_RES = (_GMAIL_PATH_RE, _CALENDAR_PATH_RE) + _POST_ONLY_PATH_RES
_SENSITIVE_KEY_RE = re.compile(
    r"token|secret|password|credential|authorization|api[_-]?key|workspace.?id|user.?id|nango.?connection.?id",
    re.I,
)
_FORBIDDEN_PROXY_KEY_RE = re.compile(
    r"base.?url.?override|connection.?id|provider.?config.?key|chat.?id|" + _SENSITIVE_KEY_RE.pattern,
    re.I,
)
_CREDENTIAL_VALUE_RE = re.compile(
    r"\b(?:Bearer\s+\S+|(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S+)", re.I
)
_HISTORY_TYPES = {"messageAdded", "messageDeleted", "labelAdded", "labelRemoved"}
ProxyParam = str | int | bool | Sequence[str]


class NangoClientError(RuntimeError):
    """Sanitized Nango/HTTP error carrying no provider body or credentials."""

    def __init__(self, status_code: int | None, error_type: str) -> None:
        self.status_code = status_code
        self.error_type = error_type if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", error_type) else "request_error"
        label = f"HTTP {status_code}" if status_code is not None else "transport"
        super().__init__(f"Nango {label} ({self.error_type})")


@dataclass(frozen=True, slots=True)
class ConnectSession:
    token: str = field(repr=False)
    connect_link: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectionMetadata:
    connection_id: str
    provider_config_key: str
    tags: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProxyResponse:
    status_code: int
    body: Any


class NangoClient:
    """Nango adapter with injected HTTP client and server-owned endpoint config."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.nango.dev",
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        allow_insecure_localhost: bool = False,
    ) -> None:
        parsed = urlsplit(base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        secure = parsed.scheme == "https"
        explicit_local_dev = (
            parsed.scheme == "http"
            and parsed.hostname in local_hosts
            and allow_insecure_localhost is True
        )
        if (
            not (secure or explicit_local_dev)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Nango base_url deve ser HTTPS (localhost HTTP exige permissão explícita)")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout inválido")
        key = api_key
        if not isinstance(key, str) or not key.strip() or "\r" in key or "\n" in key:
            raise ValueError("api_key ausente ou inválida")
        self._api_key = key.strip()
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = float(timeout)

    @staticmethod
    def _connection_id(value: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise ValueError("connection_id inválido")
        return value

    @staticmethod
    def _provider_config_key(value: str) -> str:
        if not isinstance(value, str) or not _CONFIG_KEY_RE.fullmatch(value):
            raise ValueError("provider_config_key inválida")
        return value

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            if self._client is not None:
                response = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=headers,
                    timeout=self._timeout,
                    **kwargs,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        f"{self._base_url}{path}",
                        headers=headers,
                        **kwargs,
                    )
        except httpx.HTTPError as exc:
            raise NangoClientError(None, type(exc).__name__) from None
        if not 200 <= response.status_code < 300:
            raise NangoClientError(response.status_code, "http_error")
        return response

    @staticmethod
    def _data_object(response: httpx.Response) -> dict[str, Any]:
        try:
            envelope = response.json()
        except (ValueError, json.JSONDecodeError):
            raise NangoClientError(response.status_code, "invalid_json") from None
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise NangoClientError(response.status_code, "invalid_response_shape")
        return data

    async def create_connect_session(
        self,
        *,
        end_user_id: str,
        organization_id: str,
        provider_config_key: str,
    ) -> ConnectSession:
        provider_key = self._provider_config_key(provider_config_key)
        for tag_value in (end_user_id, organization_id):
            if not isinstance(tag_value, str) or not _USER_TAG_RE.fullmatch(tag_value):
                raise ValueError("Connect session tags inválidas")
        response = await self._request(
            "POST",
            "/connect/sessions",
            json={
                "tags": {"end_user_id": end_user_id, "organization_id": organization_id},
                "allowed_integrations": [provider_key],
            },
        )
        data = self._data_object(response)
        token = data.get("token")
        connect_link = data.get("connect_link")
        expires_at = data.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(connect_link, str):
            raise NangoClientError(response.status_code, "invalid_connect_session")
        try:
            parsed_link = urlsplit(connect_link)
            if not isinstance(expires_at, str):
                raise ValueError("expires_at inválido")
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            raise NangoClientError(response.status_code, "invalid_connect_session") from None
        if parsed_link.scheme != "https" or not parsed_link.hostname or expires.tzinfo is None:
            raise NangoClientError(response.status_code, "invalid_connect_session")
        return ConnectSession(token=token, connect_link=connect_link, expires_at=expires)

    async def get_connection_metadata(
        self,
        connection_id: str,
        provider_config_key: str,
    ) -> ConnectionMetadata:
        safe_id = self._connection_id(connection_id)
        provider_key = self._provider_config_key(provider_config_key)
        response = await self._request(
            "GET",
            f"/connections/{safe_id}",
            params={"provider_config_key": provider_key},
        )
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            raise NangoClientError(response.status_code, "invalid_json") from None
        if not isinstance(data, dict):
            raise NangoClientError(response.status_code, "invalid_response_shape")
        raw_connection_id = data.get("connection_id")
        raw_provider_key = data.get("provider_config_key")
        tags = data.get("tags")
        required_tags = {"end_user_id", "organization_id"}
        if (
            not isinstance(raw_connection_id, str)
            or not isinstance(raw_provider_key, str)
            or not isinstance(tags, dict)
            or not required_tags.issubset(tags)
            or any(
                not isinstance(tags[key], str) or not _USER_TAG_RE.fullmatch(tags[key])
                for key in required_tags if isinstance(tags, dict) and key in tags
            )
        ):
            raise NangoClientError(response.status_code, "invalid_connection_metadata")
        try:
            raw_connection_id = self._connection_id(raw_connection_id)
            raw_provider_key = self._provider_config_key(raw_provider_key)
        except ValueError:
            raise NangoClientError(response.status_code, "invalid_connection_metadata") from None
        if raw_connection_id != safe_id or raw_provider_key != provider_key:
            raise NangoClientError(response.status_code, "connection_scope_mismatch")
        return ConnectionMetadata(
            connection_id=raw_connection_id,
            provider_config_key=raw_provider_key,
            tags={"end_user_id": tags["end_user_id"], "organization_id": tags["organization_id"]},
        )

    async def delete_connection(self, connection_id: str) -> None:
        safe_id = self._connection_id(connection_id)
        await self._request("DELETE", f"/connections/{safe_id}")

    async def proxy_request(
        self,
        method: str,
        path: str,
        *,
        connection_id: str,
        provider_config_key: str,
        params: Mapping[str, ProxyParam] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> ProxyResponse:
        safe_method = method.upper() if isinstance(method, str) else ""
        if safe_method not in {"GET", "POST"}:
            raise ValueError("Proxy permite somente GET/POST")
        if (
            not isinstance(path, str)
            or path.startswith("/")
            or ".." in path
            or "\\" in path
            or any(char in path for char in (":", "?", "#", "%", "//"))
            or not any(regex.fullmatch(path) for regex in _ALLOWED_PROXY_PATH_RES)
        ):
            raise ValueError("Proxy path não permitida")
        safe_id = self._connection_id(connection_id)
        provider_key = self._provider_config_key(provider_config_key)
        is_gmail_watch = path == "gmail/v1/users/me/watch"
        is_gmail_send = path == "gmail/v1/users/me/messages/send"
        is_gmail_history = path == "gmail/v1/users/me/history"
        if path.startswith("gmail/"):
            gmail_post_route = is_gmail_watch or is_gmail_send
            if (gmail_post_route and safe_method != "POST") or (
                not gmail_post_route and safe_method != "GET"
            ):
                raise ValueError("Método não permitido para este recurso Google")
        elif safe_method != "POST":
            # Notion, Slack e qualquer app não-Google: somente escrita via POST.
            raise ValueError("Método não permitido para este recurso")
        safe_params: dict[str, str | int | bool | Sequence[str]] = {}
        for key, value in (params or {}).items():
            is_page_token = isinstance(key, str) and key == "pageToken" and is_gmail_history
            valid_value = isinstance(value, (str, int, bool)) or (
                is_gmail_history
                and key == "historyTypes"
                and isinstance(value, (list, tuple))
                and 1 <= len(value) <= 4
                and all(isinstance(item, str) for item in value)
            )
            if (
                not isinstance(key, str)
                or not _PARAM_RE.fullmatch(key)
                or (_FORBIDDEN_PROXY_KEY_RE.search(key) and not is_page_token)
                or key.lower() in {"base-url-override", "host", "connection-id", "provider-config-key"}
                or not valid_value
            ):
                raise ValueError("Proxy params inválidos")
            if is_gmail_history and key not in {"startHistoryId", "pageToken", "historyTypes"}:
                raise ValueError("Gmail history params inválidos")
            if is_gmail_history and key == "startHistoryId" and not re.fullmatch(r"[0-9]+", str(value)):
                raise ValueError("startHistoryId inválido")
            if is_gmail_history and key == "historyTypes":
                history_types = value if isinstance(value, (list, tuple)) else [value]
                if any(item not in _HISTORY_TYPES for item in history_types):
                    raise ValueError("historyTypes inválido")
            safe_params[key] = value
        if json_body is not None:
            if not isinstance(json_body, Mapping):
                raise ValueError("Proxy JSON body inválido")
            self._validate_body(json_body)

        response = await self._request(
            safe_method,
            f"/proxy/{path}",
            headers={"Connection-Id": safe_id, "Provider-Config-Key": provider_key},
            params=safe_params or None,
            json=dict(json_body) if json_body is not None else None,
        )
        try:
            body = response.json()
        except ValueError:
            raise NangoClientError(response.status_code, "invalid_proxy_response") from None
        return ProxyResponse(status_code=response.status_code, body=body)

    @classmethod
    def _validate_body(cls, value: Any) -> None:
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) or _FORBIDDEN_PROXY_KEY_RE.search(key) for key in value):
                raise ValueError("Proxy body contém campo de credencial ou escopo")
            for nested in value.values():
                cls._validate_body(nested)
        elif isinstance(value, list):
            for nested in value:
                cls._validate_body(nested)
        elif isinstance(value, str) and _CREDENTIAL_VALUE_RE.search(value):
            raise ValueError("Proxy body contém possível credencial")
