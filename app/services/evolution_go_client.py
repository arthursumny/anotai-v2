"""Strict HTTP adapter for the Evolution Go 0.7.2 instance API.

Only documented instance endpoints are exposed. Credentials are sent in the
``apikey`` header and upstream response bodies are reduced to typed, safe data.
"""

from dataclasses import dataclass, field
import ipaddress
import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx


_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")


class EvolutionGoError(RuntimeError):
    """Sanitized upstream error; never contains response body or credentials."""

    def __init__(self, status_code: int | None, kind: str) -> None:
        self.status_code = status_code
        self.kind = kind if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", kind) else "request_error"
        label = f"HTTP {status_code}" if status_code is not None else "transport"
        super().__init__(f"Evolution Go {label} ({self.kind})")


@dataclass(frozen=True, slots=True)
class EvolutionGoQr:
    # Both values are short-lived pairing material and should not leak to logs.
    qrcode: str = field(repr=False)
    code: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class EvolutionGoStatus:
    connected: bool
    logged_in: bool
    name: str
    my_jid: str | None


@dataclass(frozen=True, slots=True)
class EvolutionGoMetadata:
    jid: str
    display_name: str
    is_group: bool

    def __repr__(self) -> str:
        return f"EvolutionGoMetadata(is_group={self.is_group})"


class WhatsAppGateway(Protocol):
    """Small product-facing boundary for the WhatsApp gateway."""

    async def get_status(self, instance_token: str) -> EvolutionGoStatus: ...


class EvolutionGoAdapter:
    """Evolution Go API client with fixed paths and an injected HTTPX client."""

    def __init__(
        self,
        global_api_key: str,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        timeout: float = 15.0,
        allow_insecure_private: bool = False,
    ) -> None:
        parsed = urlsplit(base_url)
        secure = parsed.scheme == "https"
        host = parsed.hostname
        local_dev = False
        if parsed.scheme == "http" and host and allow_insecure_private is True:
            if host == "localhost" or host.endswith(".localhost"):
                local_dev = True
            else:
                try:
                    local_dev = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local_dev = False
        if (
            not (secure or local_dev)
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Evolution Go base_url deve ser HTTPS (HTTP privado exige permissão explícita)")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout inválido")
        self._global_api_key = self._credential(global_api_key, "global_api_key")
        if not isinstance(client, httpx.AsyncClient):
            raise ValueError("client deve ser um httpx.AsyncClient injetado")
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = float(timeout)

    def __repr__(self) -> str:
        return "EvolutionGoAdapter(base_url=<redacted>)"

    @staticmethod
    def _credential(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
            raise ValueError(f"{label} ausente ou inválida")
        return value.strip()

    @classmethod
    def _instance_token(cls, value: str) -> str:
        return cls._credential(value, "instance_token")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        apikey: str,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        # Callers can only select fixed paths below; don't expose a path-taking public API.
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={"apikey": apikey},
                json=json_body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            kind = type(exc).__name__
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", kind):
                kind = "transport_error"
            raise EvolutionGoError(None, kind) from None
        if not 200 <= response.status_code < 300:
            raise EvolutionGoError(response.status_code, "http_error")
        return response

    async def create_instance(self, name: str, token: str, *, webhook: str | None = None) -> None:
        """Create an instance using the global API key; discard raw create data."""
        if not isinstance(name, str) or not _INSTANCE_NAME_RE.fullmatch(name):
            raise ValueError("instance name inválido")
        safe_token = self._instance_token(token)
        body: dict[str, object] = {"name": name, "token": safe_token}
        if webhook is not None:
            if not isinstance(webhook, str) or "\r" in webhook or "\n" in webhook:
                raise ValueError("webhook inválido")
            parsed = urlsplit(webhook)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("webhook inválido")
            body["webhook"] = webhook
        await self._request("POST", "/instance/create", apikey=self._global_api_key, json_body=body)

    async def get_qr(self, instance_token: str) -> EvolutionGoQr:
        response = await self._request(
            "GET", "/instance/qr", apikey=self._instance_token(instance_token)
        )
        data = self._response_data(response)
        qrcode, code = data.get("qrcode"), data.get("code")
        if not isinstance(qrcode, str) or not qrcode or not isinstance(code, str) or not code.startswith("data:image/png;base64,"):
            raise EvolutionGoError(response.status_code, "invalid_qr_response")
        return EvolutionGoQr(qrcode=qrcode, code=code)

    async def get_status(self, instance_token: str) -> EvolutionGoStatus:
        response = await self._request(
            "GET", "/instance/status", apikey=self._instance_token(instance_token)
        )
        data = self._response_data(response)
        connected, logged_in, name, my_jid = (
            data.get("connected"), data.get("loggedIn"), data.get("name"), data.get("myJid")
        )
        if (
            not isinstance(connected, bool)
            or not isinstance(logged_in, bool)
            or not isinstance(name, str)
            or not name
            or (my_jid is not None and not isinstance(my_jid, str))
        ):
            raise EvolutionGoError(response.status_code, "invalid_status_response")
        return EvolutionGoStatus(connected, logged_in, name, my_jid)

    async def delete_instance(self, instance_id: str) -> None:
        """Delete using a separately supplied documented instance ID."""
        if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.fullmatch(instance_id):
            raise ValueError("instance_id inválido")
        await self._request(
            "DELETE", f"/instance/delete/{instance_id}", apikey=self._global_api_key
        )

    async def logout(self, instance_token: str) -> None:
        await self._request("DELETE", "/instance/logout", apikey=self._instance_token(instance_token))

    async def list_contacts(self, instance_token: str) -> list[EvolutionGoMetadata]:
        response = await self._request(
            "GET", "/user/contacts", apikey=self._instance_token(instance_token)
        )
        rows = self._list_data(response, allow_raw=False)
        return self._metadata_rows(rows, is_group=False)

    async def list_groups(self, instance_token: str) -> list[EvolutionGoMetadata]:
        response = await self._request(
            "GET", "/group/list", apikey=self._instance_token(instance_token)
        )
        rows = self._list_data(response, allow_raw=True)
        return self._metadata_rows(rows, is_group=True)

    @staticmethod
    def _list_data(response: httpx.Response, *, allow_raw: bool) -> list[object]:
        try:
            payload = response.json()
        except ValueError:
            raise EvolutionGoError(response.status_code, "invalid_json") from None
        data = payload.get("data") if isinstance(payload, dict) else payload if allow_raw else None
        if not isinstance(data, list):
            raise EvolutionGoError(response.status_code, "invalid_response_shape")
        return data

    @staticmethod
    def _metadata_rows(rows: list[object], *, is_group: bool) -> list[EvolutionGoMetadata]:
        result: list[EvolutionGoMetadata] = []
        for row in rows:
            if len(result) >= 200:
                break
            if not isinstance(row, dict):
                continue
            jid = row.get("JID" if is_group else "Jid")
            expected_suffix = "@g.us" if is_group else "@s.whatsapp.net"
            if (
                not isinstance(jid, str)
                or len(jid) > 255
                or not jid.endswith(expected_suffix)
                or not jid[: -len(expected_suffix)]
                or any(ord(char) < 32 or char.isspace() for char in jid)
            ):
                continue
            candidates = (row.get("Name"),) if is_group else (
                row.get("FullName"), row.get("FirstName"), row.get("PushName")
            )
            name = next((value.strip() for value in candidates if isinstance(value, str) and value.strip()), None)
            if is_group and name is None:
                name = jid.removesuffix(expected_suffix).replace("-", " ")
            if name is None:
                name = jid.removesuffix(expected_suffix)
            display_name = "".join(char for char in name if char.isprintable()).strip()[:120]
            if not display_name:
                display_name = jid.removesuffix(expected_suffix)[:120]
            result.append(EvolutionGoMetadata(jid=jid, display_name=display_name, is_group=is_group))
        return result

    @staticmethod
    def _response_data(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError:
            raise EvolutionGoError(response.status_code, "invalid_json") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise EvolutionGoError(response.status_code, "invalid_response_shape")
        return data
