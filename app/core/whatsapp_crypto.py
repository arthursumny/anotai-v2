"""Encryption and verification helpers for WhatsApp instance credentials."""

import hashlib
import hmac
import json
import uuid

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class WhatsAppCryptoError(RuntimeError):
    """Credential operation failed without exposing credential material."""


def _fernet() -> Fernet:
    key = settings.WHATSAPP_TOKEN_ENCRYPTION_KEY.strip()
    if not key:
        raise WhatsAppCryptoError("WhatsApp credential encryption is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise WhatsAppCryptoError("WhatsApp credential encryption key is invalid.") from exc


def encrypt_instance_token(instance_id: uuid.UUID | str, token: str) -> str:
    """Encrypt a token, binding it cryptographically to its instance identifier."""
    try:
        instance_key = str(uuid.UUID(str(instance_id)))
        if not isinstance(token, str) or not token:
            raise ValueError
        payload = json.dumps({"instance_id": instance_key, "token": token}, separators=(",", ":")).encode()
        return _fernet().encrypt(payload).decode("ascii")
    except WhatsAppCryptoError:
        raise
    except Exception as exc:
        raise WhatsAppCryptoError("Could not protect WhatsApp credentials.") from exc


def decrypt_instance_token(instance_id: uuid.UUID | str, ciphertext: str) -> str:
    """Decrypt only for the instance to which the ciphertext was originally bound."""
    try:
        instance_key = str(uuid.UUID(str(instance_id)))
        payload = json.loads(_fernet().decrypt(ciphertext.encode("ascii")))
        if payload.get("instance_id") != instance_key or not isinstance(payload.get("token"), str):
            raise ValueError
        return payload["token"]
    except WhatsAppCryptoError:
        raise
    except Exception as exc:
        raise WhatsAppCryptoError("Protected WhatsApp credential could not be read.") from exc


def hash_webhook_secret(secret: str) -> str:
    """Return a lowercase SHA-256 hex digest; never store the webhook secret itself."""
    if not isinstance(secret, str) or not secret:
        raise WhatsAppCryptoError("Webhook secret must not be empty.")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_webhook_secret(secret: str, expected_hash: str) -> bool:
    """Compare the secret's digest in constant time."""
    if not isinstance(secret, str) or not isinstance(expected_hash, str):
        return False
    return hmac.compare_digest(hash_webhook_secret(secret), expected_hash)
