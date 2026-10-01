"""AnotAI — fixtures: sqlite em memória + cliente ASGI no mesmo loop."""

import os

os.environ.setdefault("JWT_SECRET_KEY", "segredo-teste-com-32-chars-minimo")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("STT_PROVIDER", "fake")
os.environ.setdefault("USER_SECRET_ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import app.models  # noqa: F401 — registra o metadata
import app.worker as worker
from app.core.database import get_session
from app.main import app


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient):
    """Registra usuário único, loga e retorna (headers, user_id)."""
    email = f"t+{uuid.uuid4().hex[:8]}@anotai.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"name": "Tester", "email": email, "password": "senha-forte-123"},
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", data={"username": email, "password": "senha-forte-123"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return {"Authorization": f"Bearer {token}"}, me.json()["id"]


class _SessionFactory:
    """Adapta a sessão de teste p/ a interface de async_session_factory do worker."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *args: object) -> bool:
        return False


@pytest.fixture
def worker_db(monkeypatch, db_session: AsyncSession):
    """Sessão de teste plugada no worker (jobs sem Postgres/Redis reais)."""
    monkeypatch.setattr(worker, "async_session_factory", _SessionFactory(db_session))
    return db_session
