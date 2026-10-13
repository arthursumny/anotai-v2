"""OpenAI completion and lossless input partitioning."""

from typing import Any

from openai import AsyncOpenAI

from app.core.config import settings


def transcription_chunks(text: str) -> list[str]:
    """Partition the entire input without dropping separators or trailing text."""
    size = settings.OPENAI_TRANSCRIPT_CHUNK_CHARS
    return [text[start:start + size] for start in range(0, len(text), size)]


async def chat_create(client: AsyncOpenAI, **kwargs: Any) -> Any:
    """Use the model's default temperature; reject incomplete output."""
    parameters = {key: value for key, value in kwargs.items() if key != "temperature"}
    response = await client.chat.completions.create(**parameters)
    if not response.choices or response.choices[0].finish_reason not in {"stop", "tool_calls"}:
        raise ValueError("A OpenAI não concluiu a resposta; conteúdo não será salvo como completo")
    if response.choices[0].message.refusal:
        raise ValueError("A OpenAI não gerou o conteúdo solicitado")
    return response
