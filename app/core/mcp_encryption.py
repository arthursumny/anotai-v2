"""Cifra tokens OAuth MCP em repouso usando uma chave da plataforma."""

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class MCPEncryptionError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = settings.MCP_TOKEN_ENCRYPTION_KEY.strip()
    if not key:
        raise MCPEncryptionError("Conexões ainda não estão configuradas neste ambiente.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise MCPEncryptionError("A chave de segurança das conexões está inválida.") from exc


def encrypt_payload(value: dict[str, Any]) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return _fernet().encrypt(payload).decode("ascii")
    except MCPEncryptionError:
        raise
    except Exception as exc:
        raise MCPEncryptionError("Não foi possível proteger as credenciais da conexão.") from exc


def decrypt_payload(value: str) -> dict[str, Any]:
    try:
        decoded = _fernet().decrypt(value.encode("ascii"))
        result = json.loads(decoded)
    except MCPEncryptionError:
        raise
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise MCPEncryptionError("A credencial protegida não pôde ser lida. Reconecte o aplicativo.") from exc
    if not isinstance(result, dict):
        raise MCPEncryptionError("A credencial protegida está inválida. Reconecte o aplicativo.")
    return result
