import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from starlette.datastructures import Headers

from gateway.auth import extract_key, hash_key
from gateway.models import ApiKey, utcnow
from tests.conftest import fetch_rows, message_response

BODY = {
    "model": "claude-sonnet-5-5",
    "max_tokens": 10,
    "messages": [{"role": "user", "content": "Hi"}],
}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-api-key": "gw_a"}, "gw_a"),
        ({"authorization": "Bearer gw_b"}, "gw_b"),
        ({"authorization": "bearer  gw_c "}, "gw_c"),
        ({"x-api-key": "gw_a", "authorization": "Bearer gw_b"}, "gw_a"),
        ({"authorization": "Basic abc"}, None),
        ({"authorization": "Bearer "}, None),
        ({}, None),
    ],
)
def test_extract_key(headers: dict[str, str], expected: str | None) -> None:
    assert extract_key(Headers(headers)) == expected


async def test_missing_key_is_rejected_without_calling_upstream(
    app: FastAPI, http: httpx.AsyncClient, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages")
    resp = await http.post("/v1/messages", json=BODY)
    assert resp.status_code == 401
    assert resp.json() == {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid or missing gateway key"},
    }
    assert not route.called
    assert await fetch_rows(app) == []


async def test_unknown_key_is_rejected(http: httpx.AsyncClient) -> None:
    resp = await http.post("/v1/messages", headers={"x-api-key": "gw_not_a_real_key"}, json=BODY)
    assert resp.status_code == 401


async def test_real_anthropic_key_is_not_accepted_as_gateway_key(http: httpx.AsyncClient) -> None:
    from tests.conftest import REAL_KEY

    resp = await http.post("/v1/messages", headers={"x-api-key": REAL_KEY}, json=BODY)
    assert resp.status_code == 401


async def test_bearer_token_is_accepted(
    http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    resp = await http.post(
        "/v1/messages", headers={"authorization": f"Bearer {gateway_key}"}, json=BODY
    )
    assert resp.status_code == 200


async def test_revoked_key_is_rejected(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str
) -> None:
    async with app.state.session_factory() as session:
        row = (
            await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(gateway_key)))
        ).scalar_one()
        row.revoked_at = utcnow()
        await session.commit()

    resp = await http.post("/v1/messages", headers={"x-api-key": gateway_key}, json=BODY)
    assert resp.status_code == 401


async def test_only_hash_is_stored(app: FastAPI, gateway_key: str) -> None:
    async with app.state.session_factory() as session:
        [row] = (await session.execute(select(ApiKey))).scalars().all()
    assert row.key_hash == hash_key(gateway_key)
    assert row.key_hash != gateway_key
    assert gateway_key.startswith(row.key_prefix)
