"""Focused tests for versioned user-secret encryption."""

import uuid

import pytest
from cryptography.fernet import Fernet

from app.core.user_secret_crypto import (
    UserSecretCryptoError,
    decrypt_user_secret,
    encrypt_user_secret,
    is_encrypted_user_secret,
)


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def identifiers() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


def test_round_trip_is_versioned_and_does_not_expose_plaintext(key, identifiers):
    user_id, secret_id = identifiers
    plaintext = "private user credential"

    encrypted = encrypt_user_secret(user_id, secret_id, plaintext, key=key)

    assert encrypted.startswith("enc:v1:")
    assert plaintext not in encrypted
    assert is_encrypted_user_secret(encrypted)
    assert decrypt_user_secret(user_id, secret_id, encrypted, key=key) == plaintext
    assert not is_encrypted_user_secret(plaintext)


@pytest.mark.parametrize("wrong_identity", ["user", "secret"])
def test_ciphertext_is_bound_to_user_and_secret(key, identifiers, wrong_identity):
    user_id, secret_id = identifiers
    encrypted = encrypt_user_secret(user_id, secret_id, "credential", key=key)

    if wrong_identity == "user":
        user_id = uuid.uuid4()
    else:
        secret_id = uuid.uuid4()

    with pytest.raises(UserSecretCryptoError):
        decrypt_user_secret(user_id, secret_id, encrypted, key=key)


def test_wrong_key_fails_without_disclosing_key_or_plaintext(key, identifiers):
    user_id, secret_id = identifiers
    plaintext = "credential-not-for-logs"
    encrypted = encrypt_user_secret(user_id, secret_id, plaintext, key=key)
    wrong_key = Fernet.generate_key().decode("ascii")

    with pytest.raises(UserSecretCryptoError) as error:
        decrypt_user_secret(user_id, secret_id, encrypted, key=wrong_key)

    assert plaintext not in str(error.value)
    assert wrong_key not in repr(error.value)


@pytest.mark.parametrize("ciphertext", ["plaintext", "enc:v2:token", "enc:v1:", "enc:v1:bad"])
def test_malformed_or_unknown_ciphertext_fails_closed(key, identifiers, ciphertext):
    user_id, secret_id = identifiers
    with pytest.raises(UserSecretCryptoError):
        decrypt_user_secret(user_id, secret_id, ciphertext, key=key)


def test_tampered_ciphertext_fails_closed(key, identifiers):
    user_id, secret_id = identifiers
    encrypted = encrypt_user_secret(user_id, secret_id, "credential", key=key)
    tampered = encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B")

    with pytest.raises(UserSecretCryptoError):
        decrypt_user_secret(user_id, secret_id, tampered, key=key)


@pytest.mark.parametrize("bad_key", ["", "not-a-fernet-key", "invalid-secret-key-material"])
def test_invalid_or_missing_key_error_is_sanitized(bad_key, identifiers):
    user_id, secret_id = identifiers
    with pytest.raises(UserSecretCryptoError) as error:
        encrypt_user_secret(user_id, secret_id, "sensitive plaintext", key=bad_key)

    assert "sensitive plaintext" not in str(error.value)
    if bad_key:
        assert bad_key not in repr(error.value)


def test_plaintext_limit_is_enforced(key, identifiers):
    user_id, secret_id = identifiers
    with pytest.raises(UserSecretCryptoError):
        encrypt_user_secret(user_id, secret_id, "x" * 4001, key=key)
