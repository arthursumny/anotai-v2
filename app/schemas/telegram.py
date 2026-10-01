"""AnotAI — schemas do Telegram da plataforma (Pydantic v2)."""

from pydantic import BaseModel, ConfigDict


class TelegramBotInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configured: bool
    username: str | None = None


class TelegramTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    chat_id: str
