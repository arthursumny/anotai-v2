"""Authenticated encryption helpers for user-owned secrets."""

import json
import uuid

from cryptography.fernet import Fernet


_PREFIX = "enc:v1:"
_MAX_PLAINTEXT_CHARS = 4000


class UserSecretCryptoError(RuntimeError):
    """A user-secret operation failed without exposing credential material."""


def _fernet(key: str) -> Fernet:
    try:
        if not isinstance(key, str) or not key:
            raise ValueError
        return Fernet(key.encode("ascii"))
    except Exception:
        raise UserSecretCryptoError("User secret encryption key is invalid or missing.") from None


def _identifier(value: uuid.UUID | str) -> str:
    return str(uuid.UUID(str(value)))


def encrypt_user_secret(
    user_id: uuid.UUID | str,
    secret_id: uuid.UUID | str,
    value: str,
    *,
    key: str,
) -> str:
    """Encrypt a secret and bind the ciphertext to its user and secret UUIDs."""
    try:
        if not isinstance(value, str) or len(value) > _MAX_PLAINTEXT_CHARS:
            raise ValueError
        payload = json.dumps(
            {
                "user_id": _identifier(user_id),
                "secret_id": _identifier(secret_id),
                "value": value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return _PREFIX + _fernet(key).encrypt(payload).decode("ascii")
    except UserSecretCryptoError:
        raise
    except Exception:
        raise UserSecretCryptoError("Could not protect user secret.") from None


def decrypt_user_secret(
    user_id: uuid.UUID | str,
    secret_id: uuid.UUID | str,
    value: str,
    *,
    key: str,
) -> str:
    """Decrypt only when ciphertext matches its version and both bound UUIDs."""
    try:
        if not isinstance(value, str) or not value.startswith(_PREFIX):
            raise ValueError
        token = value[len(_PREFIX) :]
        payload = json.loads(_fernet(key).decrypt(token.encode("ascii")))
        expected_user_id = _identifier(user_id)
        expected_secret_id = _identifier(secret_id)
        if (
            not isinstance(payload, dict)
            or payload.get("user_id") != expected_user_id
            or payload.get("secret_id") != expected_secret_id
            or not isinstance(payload.get("value"), str)
            or len(payload["value"]) > _MAX_PLAINTEXT_CHARS
        ):
            raise ValueError
        return payload["value"]
    except Exception:
        raise UserSecretCryptoError("Protected user secret could not be read.") from None


def is_encrypted_user_secret(value: str) -> bool:
    """Return whether a value uses the reserved encrypted-secret prefix."""
    return isinstance(value, str) and value.startswith("enc:")
