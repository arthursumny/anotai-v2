"""AnotAI — configuração central via pydantic-settings (Pydantic v2)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "AnotAI"
    API_V1_PREFIX: str = "/api/v1"
    ENV: str = "dev"

    DATABASE_URL: str = "postgresql+asyncpg://anotai:anotai@localhost:5432/anotai"
    SYNC_DATABASE_URL: str = "postgresql+psycopg2://anotai:anotai@localhost:5432/anotai"
    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET_KEY: str = Field(default="troque-este-segredo-dev", min_length=8)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    OPENAI_API_KEY: str = ""
    OPENAI_ROUTER_MODEL: str = "gpt-4o-mini"
    OPENAI_EXTRACTOR_MODEL: str = "gpt-4o"
    OPENAI_TRANSCRIPT_CHUNK_CHARS: int = Field(default=24000, ge=1000, le=64000)
    STT_PROVIDER: Literal["soniox", "fake"] = "soniox"
    SONIOX_API_KEY: str = ""
    SONIOX_STT_MODEL: str = "stt-async-v5"
    SONIOX_API_BASE_URL: str = "https://api.soniox.com"
    SONIOX_STT_TIMEOUT_SECONDS: float = Field(default=900, gt=0)
    SONIOX_STT_POLL_INTERVAL_SECONDS: float = Field(default=1, gt=0)
    SMTP_HOST: str = ""
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_TLS: Literal["starttls", "ssl", "none"] = "starttls"
    SMTP_TIMEOUT_SECONDS: float = Field(default=15, gt=0)

    ROUTER_CONFIDENCE_THRESHOLD: float = 0.6
    FOLLOW_UP_DELAY_HOURS: int = Field(default=24, ge=1, le=168)
    DEFAULT_TIMEZONE: str = "America/Sao_Paulo"
    DEFAULT_EVENT_RETENTION_DAYS: int = Field(default=30, ge=1, le=365)
    AUDIT_RETENTION_DAYS: int = Field(default=90, ge=1, le=365)
    # Separate key for future UserSecret encryption; empty keeps secret writes fail-closed.
    USER_SECRET_ENCRYPTION_KEY: str = ""

    MAX_AUDIO_MB: int = 25
    ALLOWED_AUDIO_TYPES: str = (
        "audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/ogg,"
        "audio/opus,audio/webm,audio/m4a,audio/mp4,video/mp4"
    )

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_DEFAULT_CHAT_ID: str = ""
    MCP_SERVER_URL: str = ""
    WEBHOOK_TIMEOUT_SECONDS: int = 15

    # OAuth para conexões remotas MCP. A chave de cifra é exclusiva para
    # tokens OAuth e nunca é enviada ao frontend.
    MCP_TOKEN_ENCRYPTION_KEY: str = ""
    MCP_PUBLIC_BASE_URL: str = "http://localhost:8000"
    MCP_WEB_APP_URL: str = "http://localhost:5174"
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""

    # Nango stores provider credentials; these values only identify its environment/configs.
    NANGO_ENV_KEY: str = ""
    NANGO_SIGNING_KEY: str = ""
    NANGO_BASE_URL: str = ""
    NANGO_ENVIRONMENT: str = ""
    NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY: str = ""
    NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY: str = ""
    GMAIL_PUBSUB_TOPIC: str = ""
    GMAIL_PUSH_AUDIENCE: str = ""
    GMAIL_PUSH_SA_EMAIL: str = ""
    GMAIL_WATCH_LABEL_ID: str = "INBOX"
    GMAIL_WATCH_RENEWAL_INTERVAL_HOURS: int = 24

    # Evolution credentials are isolated from MCP credentials. Empty values keep
    # WhatsApp integration disabled until explicitly configured.
    WHATSAPP_EVOLUTION_BASE_URL: str = ""
    WHATSAPP_EVOLUTION_GLOBAL_API_KEY: str = ""
    WHATSAPP_TOKEN_ENCRYPTION_KEY: str = ""
    WHATSAPP_WEBHOOK_BASE_URL: str = ""

    AUDIO_UPLOAD_DIR: str = "/tmp/anotai-audio"

    @field_validator("ROUTER_CONFIDENCE_THRESHOLD")
    @classmethod
    def _check_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("ROUTER_CONFIDENCE_THRESHOLD deve estar entre 0 e 1")
        return v

    @property
    def allowed_audio_types_set(self) -> set[str]:
        return {t.strip() for t in self.ALLOWED_AUDIO_TYPES.split(",") if t.strip()}

    @property
    def max_audio_bytes(self) -> int:
        return self.MAX_AUDIO_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
