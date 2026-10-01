"""AnotAI — Telegram da plataforma: info do bot oficial + envio de teste.

O envio usa o bot oficial (`TELEGRAM_BOT_TOKEN` global, com override legado
por usuário). O usuário só precisa conectar o próprio chat (`TELEGRAM_CHAT_ID`).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession
from telegram import Bot
from telegram.error import TelegramError

from app.core.config import settings
from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.telegram import TelegramBotInfo, TelegramTestResult
from app.services.dispatcher import load_user_secrets

router = APIRouter()

TEST_MESSAGE = "✅ AnotAI conectado! A partir de agora suas notas podem chegar aqui no Telegram."


def _platform_token(secrets: dict[str, str]) -> str:
    """Token do bot oficial: override do usuário (legado) ou global."""
    return secrets.get("TELEGRAM_BOT_TOKEN") or settings.TELEGRAM_BOT_TOKEN


@router.get("/bot", response_model=TelegramBotInfo)
async def bot_info(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> TelegramBotInfo:
    """Retorna o @username do bot oficial (para link `t.me/...`).

    Sem token configurado, retorna `configured=False` em vez de erro —
    a UI mostra instrução alternativa.
    """
    secrets = await load_user_secrets(session, current.id)
    token = _platform_token(secrets)
    if not token:
        return TelegramBotInfo(configured=False, username=None)
    try:
        me = await Bot(token=token).get_me()
    except TelegramError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Não consegui falar com o Telegram: {exc}",
        ) from exc
    return TelegramBotInfo(configured=True, username=me.username)


@router.post("/test", response_model=TelegramTestResult)
async def send_test(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> TelegramTestResult:
    """Envia mensagem de teste para o chat conectado do usuário."""
    secrets = await load_user_secrets(session, current.id)
    chat_id = (secrets.get("TELEGRAM_CHAT_ID") or "").strip()
    if not chat_id or "${" in chat_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conecte seu Telegram primeiro (cole seu ID de chat e salve).",
        )
    token = _platform_token(secrets)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot oficial ainda não configurado na plataforma. Fale com o suporte.",
        )
    try:
        await Bot(token=token).send_message(chat_id=chat_id, text=TEST_MESSAGE)
    except TelegramError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Não chegou? Confira se você mandou /start no bot oficial e se "
                f"o ID está certo. Detalhe: {exc}"
            ),
        ) from exc
    return TelegramTestResult(ok=True, chat_id=chat_id)
