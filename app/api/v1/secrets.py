"""AnotAI — CRUD de segredos do usuário (valores nunca expostos, só máscara)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.deps import get_current_user
from app.core.user_secret_crypto import (
    UserSecretCryptoError,
    decrypt_user_secret,
    encrypt_user_secret,
)
from app.models.user import User
from app.models.user_secret import UserSecret
from app.schemas.secret import SecretRead, SecretUpsert, mask_secret

router = APIRouter()


def _to_read(secret: UserSecret, plaintext: str) -> SecretRead:
    return SecretRead(
        id=secret.id,
        key=secret.key,
        value_masked=mask_secret(plaintext),
        created_at=secret.created_at,
    )


@router.get("", response_model=list[SecretRead])
async def list_secrets(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[SecretRead]:
    result = await session.exec(
        select(UserSecret).where(UserSecret.user_id == current.id).order_by(UserSecret.key.asc())  # type: ignore[arg-type]
    )
    try:
        return [
            _to_read(
                secret,
                decrypt_user_secret(
                    secret.user_id, secret.id, secret.value, key=settings.USER_SECRET_ENCRYPTION_KEY
                ),
            )
            for secret in result.all()
        ]
    except UserSecretCryptoError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Segredos indisponíveis") from None


@router.post("", response_model=SecretRead)
async def upsert_secret(
    data: SecretUpsert,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> SecretRead:
    key = data.key.strip().upper()
    # Check configuration before the lookup so no write can happen with an invalid key.
    result = await session.exec(
        select(UserSecret).where(UserSecret.user_id == current.id, UserSecret.key == key)
    )
    existing = result.first()
    if existing is not None:
        try:
            existing.value = encrypt_user_secret(
                current.id, existing.id, data.value, key=settings.USER_SECRET_ENCRYPTION_KEY
            )
        except UserSecretCryptoError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Segredos indisponíveis") from None
        session.add(existing)
        await session.commit()
        await session.refresh(existing)
        return _to_read(existing, data.value)
    secret = UserSecret(user_id=current.id, key=key, value="")
    try:
        secret.value = encrypt_user_secret(
            current.id, secret.id, data.value, key=settings.USER_SECRET_ENCRYPTION_KEY
        )
    except UserSecretCryptoError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Segredos indisponíveis") from None
    session.add(secret)
    await session.commit()
    await session.refresh(secret)
    return _to_read(secret, data.value)


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    key: str,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> None:
    normalized = key.strip().upper()
    result = await session.exec(
        select(UserSecret).where(UserSecret.user_id == current.id, UserSecret.key == normalized)
    )
    existing = result.first()
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segredo não encontrado")
    await session.delete(existing)
    await session.commit()
