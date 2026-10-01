"""AnotAI — helper de Chat Completions com fallback de `temperature`.

Alguns modelos (ex: `gpt-6-luna`) só aceitam o valor padrão de temperature e
rejeitam `temperature=0` com 400 `unsupported_value`. Aqui tentamos primeiro
com o valor configurado e, só nesse erro específico, repetimos sem o parâmetro.
"""

from typing import Any

from openai import AsyncOpenAI, BadRequestError


def _is_temperature_error(exc: BaseException) -> bool:
    if isinstance(exc, BadRequestError) and getattr(exc, "code", "") == "unsupported_value":
        return "temperature" in str(exc).lower()
    return "temperature" in str(exc).lower() and "does not support" in str(exc).lower()


async def chat_create(client: AsyncOpenAI, **kwargs: Any) -> Any:
    """`client.chat.completions.create` com retry sem `temperature` se rejeitado."""
    try:
        return await client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
    except Exception as exc:
        if kwargs.get("temperature") is not None and _is_temperature_error(exc):
            kwargs.pop("temperature")
            return await client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
        raise
