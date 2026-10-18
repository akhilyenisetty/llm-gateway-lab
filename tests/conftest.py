"""Shared fixtures: a real gateway app on a fresh database, with Anthropic mocked by respx.

The database is a temp SQLite file by default. Set TEST_DATABASE_URL to run the same tests
against Postgres (which is what production uses, and which SQLite doesn't fully imitate:
foreign keys, concurrency, percentile and date functions):

    TEST_DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway_test pytest

That database is wiped and recreated for every test, so never point it at real data.

respx intercepts the gateway's outgoing httpx calls, so tests need no API key or network.
Clients reach the gateway in-process through an ASGI transport (no real sockets).
"""

import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.auth import create_key, hash_key
from gateway.main import create_app
from gateway.models import Alert, ApiKey, Base, RequestContent, RequestLog
from gateway.settings import Settings

UPSTREAM = "https://api.anthropic.test"
REAL_KEY = "fake-upstream-key-for-tests"
ADMIN_KEY = "test-admin-key"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if TEST_DATABASE_URL and not TEST_DATABASE_URL.rstrip("/").endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL must name a database ending in _test (it is wiped)")
GATEWAY_URL = "http://gateway.test"


def message_response(
    model: str = "claude-sonnet-5-5",
    usage: dict[str, Any] | None = None,
    text: str = "Hello!",
) -> dict[str, Any]:
    """A Messages API success body, shaped like the real API's."""
    return {
        "id": "msg_test123",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage
        or {
            "input_tokens": 12,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            "service_tier": "standard",
        },
    }


@pytest.fixture
def store_content() -> bool:
    """Override per test with @pytest.mark.parametrize("store_content", [True])."""
    return False


@pytest.fixture
def default_budget() -> Decimal:
    """Override per test with @pytest.mark.parametrize("default_budget", [...])."""
    return Decimal("5.00")


@pytest.fixture
def allow_client_budgets() -> bool:
    return True


@pytest.fixture
def allow_client_credentials() -> bool:
    return True


@pytest.fixture
def admin_key() -> str:
    """Override with "" to test the admin API disabled."""
    return ADMIN_KEY


@pytest.fixture
def settings(
    tmp_path: Path,
    store_content: bool,
    default_budget: Decimal,
    admin_key: str,
    allow_client_budgets: bool,
    allow_client_credentials: bool,
) -> Settings:
    return Settings(
        anthropic_api_key=REAL_KEY,
        anthropic_base_url=UPSTREAM,
        database_url=TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        store_content=store_content,
        default_session_budget_usd=default_budget,
        admin_api_key=admin_key,
        allow_client_budgets=allow_client_budgets,
        allow_client_credentials=allow_client_credentials,
        categorize_enabled=False,  # tests drive the categorizer directly
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)  # a fresh schema for every test
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def upstream() -> respx.MockRouter:
    with respx.mock(base_url=UPSTREAM, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def gateway_key(app: FastAPI) -> str:
    async with app.state.session_factory() as session:
        _, plaintext = await create_key(session, "test-client")
    return plaintext


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A plain HTTP client talking to the gateway in-process."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=GATEWAY_URL
    ) as client:
        yield client


@pytest.fixture
async def sdk(app: FastAPI, gateway_key: str) -> AsyncIterator[anthropic.AsyncAnthropic]:
    """The real Anthropic SDK, pointed at the gateway with a gateway key.

    SDK 1.x uses httpx2, so its in-process transport comes from httpx2.
    """
    client = anthropic.AsyncAnthropic(
        api_key=gateway_key,
        base_url=GATEWAY_URL,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)),
    )
    yield client
    await client.close()


async def fetch_rows(app: FastAPI) -> list[RequestLog]:
    async with app.state.session_factory() as session:
        result = await session.execute(select(RequestLog).order_by(RequestLog.created_at))
        return list(result.scalars())


async def fetch_contents(app: FastAPI) -> list[RequestContent]:
    async with app.state.session_factory() as session:
        return list((await session.execute(select(RequestContent))).scalars())


async def fetch_alerts(app: FastAPI) -> list[Alert]:
    async with app.state.session_factory() as session:
        result = await session.execute(select(Alert).order_by(Alert.created_at))
        return list(result.scalars())


async def key_id_of(app: FastAPI, plaintext: str) -> uuid.UUID:
    async with app.state.session_factory() as session:
        result = await session.execute(
            select(ApiKey.id).where(ApiKey.key_hash == hash_key(plaintext))
        )
        return result.scalar_one()
