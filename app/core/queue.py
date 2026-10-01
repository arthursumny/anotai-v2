"""AnotAI — fila Redis/ARQ: settings compartilhadas entre API e worker."""

import hashlib
from urllib.parse import urlparse
from datetime import datetime, timezone

import arq
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings


def redis_settings() -> RedisSettings:
    parsed = urlparse(settings.REDIS_URL)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        password=parsed.password,
        database=int((parsed.path or "/0").lstrip("/") or 0),
    )


async def enqueue_audio_job(note_id: str, audio_path: str, user_id: str, capture_mode: str = "intentional") -> str:
    """Enfileira `process_audio_job` e retorna o job_id. Levanta RuntimeError se o Redis falhar."""
    try:
        pool: ArqRedis = await arq.create_pool(redis_settings())
        try:
            job = await pool.enqueue_job("process_audio_job", note_id, audio_path, user_id, capture_mode)
            return str(job.job_id)
        finally:
            await pool.close()
    except Exception as exc:
        raise RuntimeError(f"Redis: {exc}") from exc


async def enqueue_gmail_history_job(connection_id: str, history_id: str) -> str:
    """Enfileira Gmail history com ID determinístico para dedupe best-effort no ARQ."""
    job_id = hashlib.sha256(f"{connection_id}{history_id}".encode("utf-8")).hexdigest()
    pool: ArqRedis | None = None
    try:
        pool = await arq.create_pool(redis_settings())
        await pool.enqueue_job(
            "gmail_history_job", connection_id, history_id, _job_id=job_id,
        )
        return job_id
    except Exception as exc:
        raise RuntimeError("Redis enqueue failed") from exc
    finally:
        if pool is not None:
            try:
                await pool.close()
            except Exception:
                pass


async def enqueue_delayed_wakeup(
    job_name: str,
    item_id: str,
    target: datetime,
    *,
    redis: ArqRedis | None = None,
) -> str | None:
    """Best-effort ARQ delayed wake-up; persisted DB state remains authoritative."""
    if target.tzinfo is None or target.utcoffset() is None:
        raise ValueError("target deve incluir timezone")
    target_utc = target.astimezone(timezone.utc)
    expected = target_utc.isoformat()
    job_id = f"{job_name}:{item_id}:{expected}"
    owns_pool = redis is None
    pool = redis
    try:
        if pool is None:
            pool = await arq.create_pool(redis_settings())
        await pool.enqueue_job(
            job_name,
            item_id,
            expected,
            _defer_until=target_utc,
            _job_id=job_id,
        )
        return job_id
    except Exception:
        # Scheduler state is stored in SQL; reconciliation repairs missing wakes.
        return None
    finally:
        if owns_pool and pool is not None:
            try:
                await pool.close()
            except Exception:
                pass
