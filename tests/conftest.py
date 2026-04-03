"""
Shared fixtures for DeviceBridgeService tests.

Uses an in-memory SQLite database with StaticPool so all sessions share the same
connection — required for SQLite :memory: to be visible across multiple sessions.

External HTTP calls (UserManager, ToolGateway, AgentManager, VoiceService) are
mocked with respx so tests run without any live services.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import AsyncGenerator

import pytest
import pytest_asyncio
import httpx
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# ── Point settings at in-memory DB before importing app ───────────────────────
os.environ.setdefault("DBS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("DBS_TOOLGATEWAY_SERVICE_KEY", "test-service-key")
os.environ.setdefault("DBS_USERMANAGER_URL", "http://usermanager.test")
os.environ.setdefault("DBS_TOOLGATEWAY_URL", "http://toolgateway.test")
os.environ.setdefault("DBS_AGENTMANAGER_URL", "http://agentmanager.test")
os.environ.setdefault("DBS_VOICESERVICE_URL", "http://voiceservice.test")

from app.main import app
from app.database import Base, get_db
from app.config import settings


# ── In-memory DB engine ───────────────────────────────────────────────────────
# StaticPool: all sessions reuse the same single connection — required for
# SQLite :memory: so data written by one session is visible in another.

_TEST_ENGINE = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = async_sessionmaker(_TEST_ENGINE, class_=AsyncSession, expire_on_commit=False)


async def _create_tables() -> None:
    from app import models  # noqa: F401 — ensure all models are imported
    async with _TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _drop_tables() -> None:
    async with _TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        yield session


app.dependency_overrides[get_db] = _override_get_db


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_db():
    """Create all tables once per session; drop after."""
    await _create_tables()
    yield
    await _drop_tables()


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    """Truncate all tables between tests for isolation."""
    yield
    async with _TEST_ENGINE.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Direct DB session for test setup/assertions."""
    async with _TestSession() as session:
        yield session


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTPX client wired to the FastAPI app (no live server)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── Auth mock helpers ─────────────────────────────────────────────────────────

ADMIN_TOKEN = "admin-test-token"
USER_TOKEN  = "user-test-token"

ADMIN_PRINCIPAL = {
    "valid": True,
    "is_admin": True,
    "principal_id": "admin:1",
    "name": "Test Admin",
}

USER_PRINCIPAL = {
    "valid": True,
    "is_admin": False,
    "principal_id": "agent:42",
    "name": "Test Agent",
}

ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
USER_HEADERS  = {"Authorization": f"Bearer {USER_TOKEN}"}


def mock_usermanager(respx_mock, *, admin: bool = True):
    """Mock UserManager /auth/validate to return admin or user principal."""
    principal = ADMIN_PRINCIPAL if admin else USER_PRINCIPAL
    respx_mock.get(f"{settings.usermanager_url}/auth/validate").mock(
        return_value=httpx.Response(200, json=principal)
    )


def mock_toolgateway(respx_mock):
    """Mock ToolGateway tool listing and registration endpoints."""
    respx_mock.get(f"{settings.toolgateway_url}/api/tools").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx_mock.post(f"{settings.toolgateway_url}/api/tools").mock(
        return_value=httpx.Response(201, json={"tool_id": "tg-tool-1", "name": "test"})
    )
    # Pattern-match PATCH/DELETE on any tool ID
    respx_mock.patch(
        re.compile(rf"{re.escape(settings.toolgateway_url)}/api/tools/.*")
    ).mock(return_value=httpx.Response(200, json={"tool_id": "tg-tool-1"}))
    respx_mock.delete(
        re.compile(rf"{re.escape(settings.toolgateway_url)}/api/tools/.*")
    ).mock(return_value=httpx.Response(204))


def mock_agentmanager(respx_mock, agent_id: str = "agent-1"):
    """Mock AgentManager session creation, profile, and message endpoints."""
    # Session creation
    respx_mock.post(
        f"{settings.agentmanager_url}/agents/{agent_id}/session"
    ).mock(
        return_value=httpx.Response(201, json={"session_id": "am-session-1"})
    )
    # Agent profile fetch
    respx_mock.get(
        f"{settings.agentmanager_url}/agents/{agent_id}"
    ).mock(
        return_value=httpx.Response(200, json={
            "agent_id": agent_id,
            "profile": {
                "appearance": {"eye_count": 2, "primary_color": "#00FF00"},
                "emotions": {"happy": {"brightness": 1.0}},
            },
        })
    )
    # System message injection — match any /sessions/{id}/message path
    respx_mock.post(
        re.compile(rf"{re.escape(settings.agentmanager_url)}/sessions/.+/message")
    ).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
