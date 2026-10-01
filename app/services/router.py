"""AnotAI — roteador semântico: transcrição x templates via LLM (com fallback)."""

import json
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from openai import AsyncOpenAI

from app.core.config import settings
from app.models.template import Template
from app.services.llm import chat_create


class RouterError(Exception):
    """Falha irrecuperável no roteador (uso interno; pipeline cai em no_match)."""


@dataclass(frozen=True, slots=True)
class RouterResult:
    template_id: uuid.UUID | None
    confidence: float
    reason: str

    @property
    def matched(self) -> bool:
        return self.template_id is not None


@dataclass(frozen=True, slots=True)
class RouterRule:
    """Deterministic route rule; unset scope fields match any input scope."""

    template_id: uuid.UUID | str
    priority: int = 0
    workspace: Any | None = None
    context: Any | None = None
    capture_mode: Any | None = None


def _rule_match(
    rule: RouterRule,
    catalog_ids: set[uuid.UUID],
    workspace: Any | None,
    context: Any | None,
    capture_mode: Any | None,
) -> tuple[int, int, uuid.UUID] | None:
    """Return sort key and catalog UUID when the rule is valid and matches."""
    try:
        if isinstance(rule.template_id, bool):
            return None
        template_id = uuid.UUID(str(rule.template_id))
        if template_id not in catalog_ids:
            return None
        if not isinstance(rule.priority, int) or isinstance(rule.priority, bool):
            return None
        scoped = ((rule.workspace, workspace), (rule.context, context), (rule.capture_mode, capture_mode))
        if any(expected is not None and expected != actual for expected, actual in scoped):
            return None
        specificity = sum(expected is not None for expected, _ in scoped)
        return specificity, rule.priority, template_id
    except (AttributeError, TypeError, ValueError):
        return None


def _heuristic_fallback(transcription: str, templates: list[Template]) -> RouterResult:
    """Fallback sem LLM: overlap simples de palavras do semantic_trigger."""
    words = {w.strip(".,;:!?()[]").lower() for w in transcription.split() if len(w) > 3}
    if not words:
        return RouterResult(template_id=None, confidence=0.0, reason="Transcrição vazia")
    best: Template | None = None
    best_score = 0.0
    for tpl in templates:
        trigger_words = {w.strip(".,;:!?()[]").lower() for w in tpl.semantic_trigger.split() if len(w) > 3}
        if not trigger_words:
            continue
        score = len(words & trigger_words) / max(len(trigger_words), 1)
        if score > best_score:
            best_score = score
            best = tpl
    if best is not None and best_score >= 0.15:
        return RouterResult(template_id=best.id, confidence=round(best_score, 3), reason="heuristic-fallback")
    return RouterResult(template_id=None, confidence=round(best_score, 3), reason="heuristic-fallback: sem match")


async def route_transcription(
    transcription: str,
    templates: list[Template],
    rules: Sequence[RouterRule] | None = None,
    *,
    workspace: Any | None = None,
    context: Any | None = None,
    capture_mode: Any | None = None,
) -> RouterResult:
    """Determina qual template se aplica ou retorna `template_id=None` (fallback/caixa de entrada)."""
    text = (transcription or "").strip()
    if not text:
        return RouterResult(template_id=None, confidence=0.0, reason="Transcrição vazia")
    if not templates:
        return RouterResult(template_id=None, confidence=0.0, reason="Usuário sem templates")

    catalog_ids = {template.id for template in templates if isinstance(template.id, uuid.UUID)}
    matches = [
        matched
        for rule in (rules or ())
        if isinstance(rule, RouterRule)
        if (matched := _rule_match(rule, catalog_ids, workspace, context, capture_mode)) is not None
    ]
    if matches:
        # More constrained rules win; explicit priority breaks ties.
        specificity, priority, template_id = max(matches, key=lambda item: (item[0], item[1]))
        return RouterResult(
            template_id=template_id,
            confidence=1.0,
            reason=f"deterministic-rule:specificity={specificity}:priority={priority}",
        )

    if not settings.OPENAI_API_KEY:
        return _heuristic_fallback(text, templates)

    def _entry(t: Template) -> str:
        parts = [f"- id={t.id}", f"título: {t.title}"]
        if (t.purpose or "").strip():
            parts.append(f"para que serve: {t.purpose.strip()}")
        if (t.semantic_trigger or "").strip():
            parts.append(f"quando usar: {t.semantic_trigger.strip()}")
        category = (t.category or "").strip().replace("\n", " ")
        if category:
            parts.append(f"categoria: {category[:80]}")
        use_cases = [str(item).strip().replace("\n", " ")[:120] for item in (t.use_cases or [])[:8]]
        use_cases = [item for item in use_cases if item]
        if use_cases:
            parts.append(f"casos de uso: {', '.join(use_cases)}")
        routing_description = (t.routing_description or "").strip().replace("\n", " ")
        if routing_description:
            parts.append(f"descrição de roteamento: {routing_description[:500]}")
        example = (t.example_output or "").strip().replace("\n", " ")
        if example:
            parts.append(f"exemplo de saída: {example[:500]}")
        return " | ".join(parts)

    catalog = "\n".join(_entry(t) for t in templates)
    system = (
        "Você é o roteador semântico do AnotAI. Dada a transcrição e o catálogo de templates, "
        "responda SOMENTE JSON com {template_id, confidence, reason}. "
        "Cada template mostra como a nota deve sair (exemplo): escolha aquele cujo "
        "formato melhor aproveita o conteúdo da transcrição. "
        "template_id deve ser um id do catálogo ou null se nenhum se aplicar. "
        "confidence é float 0..1."
    )
    user_msg = f"Transcrição:\n{text[:6000]}\n\nCatálogo:\n{catalog}"
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await chat_create(
            client,
            model=settings.OPENAI_ROUTER_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception:
        return _heuristic_fallback(text, templates)

    try:
        raw_id = data.get("template_id")
        confidence = float(data.get("confidence", 0.0))
        reason = str(data.get("reason", "llm"))
    except (TypeError, ValueError):
        return RouterResult(template_id=None, confidence=0.0, reason="Resposta LLM inválida")

    if raw_id is None:
        return RouterResult(template_id=None, confidence=min(max(confidence, 0.0), 1.0), reason=reason)
    try:
        tid = uuid.UUID(str(raw_id))
    except ValueError:
        return RouterResult(template_id=None, confidence=0.0, reason="template_id inválido da LLM")
    if not any(t.id == tid for t in templates):
        return RouterResult(template_id=None, confidence=0.0, reason="template_id fora do catálogo")
    if confidence < settings.ROUTER_CONFIDENCE_THRESHOLD:
        return RouterResult(template_id=None, confidence=confidence, reason=f"abaixo do threshold: {reason}")
    return RouterResult(template_id=tid, confidence=confidence, reason=reason)
