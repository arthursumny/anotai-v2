"""AnotAI — agregador de rotas v1."""

from fastapi import APIRouter

from app.api.v1 import audio, audit, auth, gmail_push, integrations, kanban, marketplace, mcp, notes, pendencies, privacy, secrets, settings, source_policies, telegram, templates, workflows, whatsapp

v1_router = APIRouter()
v1_router.include_router(auth.router, prefix="/auth", tags=["auth"])
v1_router.include_router(audio.router, prefix="/audio", tags=["audio"])
v1_router.include_router(templates.router, prefix="/templates", tags=["templates"])
v1_router.include_router(workflows.router, prefix="/workflows", tags=["workflows"])
v1_router.include_router(marketplace.router, prefix="/marketplace", tags=["marketplace"])
v1_router.include_router(notes.router, prefix="/notes", tags=["notes"])
v1_router.include_router(pendencies.router, prefix="/pendencies", tags=["pendencies"])
v1_router.include_router(secrets.router, prefix="/secrets", tags=["secrets"])
v1_router.include_router(telegram.router, prefix="/telegram", tags=["telegram"])
v1_router.include_router(mcp.router, prefix="/mcp", tags=["mcp"])
v1_router.include_router(kanban.router, prefix="/kanban", tags=["kanban"])
v1_router.include_router(source_policies.router, prefix="/source-policies", tags=["source-policies"])
v1_router.include_router(integrations.router, prefix="/integrations", tags=["integrations"])
v1_router.include_router(gmail_push.router, prefix="/gmail", tags=["Gmail"])
v1_router.include_router(whatsapp.router, prefix="/whatsapp", tags=["WhatsApp"])
v1_router.include_router(audit.router, prefix="/audit", tags=["audit"])
v1_router.include_router(privacy.router, prefix="/privacy", tags=["privacy"])
v1_router.include_router(settings.router, prefix="/settings", tags=["settings"])
