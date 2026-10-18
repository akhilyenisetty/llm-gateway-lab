"""The client ("My usage") API: each gateway key sees and acts on its own data only."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from gateway.auth import create_key
from tests.conftest import message_response
from tests.test_handoff import CONTENT, api_message, fake_client

BODY: dict[str, Any] = {
    "model": "claude-sonnet-5-5",
    "max_tokens": 10,
    "messages": [{"role": "user", "content": "Hi"}],
}
CENT = {"input_tokens": 5_000, "output_tokens": 0}  # $0.01 on Sonnet 5.5


@pytest.fixture
async def two_users(
    app: FastAPI, http: httpx.AsyncClient, upstream: respx.MockRouter
) -> dict[str, dict[str, str]]:
    """alice has sessions a1 (2 calls) and a2; bob has b1. Returns each user's headers."""
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    users = {}
    for name in ("alice", "bob"):
        async with app.state.session_factory() as session:
            _, key = await create_key(session, name)
        users[name] = {"x-api-key": key}
    for name, sid in (("alice", "a1"), ("alice", "a1"), ("alice", "a2"), ("bob", "b1")):
        await http.post("/v1/messages", headers={**users[name], "x-session-id": sid}, json=BODY)
    return users


async def test_me_identifies_the_key(
    http: httpx.AsyncClient, two_users: dict[str, dict[str, str]]
) -> None:
    me = (await http.get("/gateway/me", headers=two_users["alice"])).json()
    assert me["name"] == "alice" and me["key_prefix"].startswith("gw_")


async def test_usage_and_session_list_are_only_your_own(
    http: httpx.AsyncClient, two_users: dict[str, dict[str, str]]
) -> None:
    alice = (await http.get("/gateway/usage", headers=two_users["alice"])).json()
    assert (alice["calls"], alice["sessions"], alice["spend_usd"]) == (3, 2, "0.03")
    bob = (await http.get("/gateway/usage", headers=two_users["bob"])).json()
    assert (bob["calls"], bob["spend_usd"]) == (1, "0.01")

    listed = (await http.get("/gateway/usage/sessions", headers=two_users["alice"])).json()
    assert listed["total"] == 2
    assert {i["session_id"] for i in listed["items"]} == {"a1", "a2"}
    assert listed["items"][0]["budget_usd"] == "5"  # default budget, with a meter's numbers


async def test_a_key_parameter_cannot_widen_the_view(
    app: FastAPI, http: httpx.AsyncClient, two_users: dict[str, dict[str, str]]
) -> None:
    """The window always uses the caller's key; a key_id in the query is ignored."""
    from sqlalchemy import select

    from gateway.models import ApiKey

    async with app.state.session_factory() as session:
        alice_id = (
            await session.execute(select(ApiKey.id).where(ApiKey.name == "alice"))
        ).scalar_one()
    resp = await http.get(f"/gateway/usage?key_id={alice_id}", headers=two_users["bob"])
    assert resp.json()["calls"] == 1  # still bob's


async def test_session_status_and_calls(
    http: httpx.AsyncClient, two_users: dict[str, dict[str, str]]
) -> None:
    status = (await http.get("/gateway/sessions/a1", headers=two_users["alice"])).json()
    assert (status["calls"], Decimal(status["spent_usd"])) == (2, Decimal("0.02"))
    calls = (await http.get("/gateway/sessions/a1/calls", headers=two_users["alice"])).json()
    assert [c["status_code"] for c in calls] == [200, 200]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/gateway/sessions/a1"),
        ("GET", "/gateway/sessions/a1/calls"),
        ("PUT", "/gateway/sessions/a1/budget"),
        ("POST", "/gateway/sessions/a1/handoffs"),
    ],
)
async def test_another_users_session_is_not_found(
    http: httpx.AsyncClient, two_users: dict[str, dict[str, str]], method: str, path: str
) -> None:
    resp = await http.request(method, path, headers=two_users["bob"], json={"budget_usd": "1"})
    assert resp.status_code == 404  # alice's session doesn't exist as far as bob can tell


async def test_user_can_set_own_budget_but_not_override_admin(
    app: FastAPI, http: httpx.AsyncClient, two_users: dict[str, dict[str, str]]
) -> None:
    alice = two_users["alice"]
    resp = await http.put("/gateway/sessions/a1/budget", headers=alice, json={"budget_usd": "0.02"})
    body = resp.json()
    assert resp.status_code == 200 and body["budget_source"] == "header"
    assert [a["threshold"] for a in body["alerts"]] == ["0.800", "1.000"]  # spend is $0.02

    admin = {"authorization": "Bearer test-admin-key"}
    async with app.state.session_factory() as session:
        from sqlalchemy import select

        from gateway.models import ApiKey

        alice_id = (
            await session.execute(select(ApiKey.id).where(ApiKey.name == "alice"))
        ).scalar_one()
    await http.put(
        f"/admin/keys/{alice_id}/sessions/a2/budget", headers=admin, json={"budget_usd": "9"}
    )
    resp = await http.put("/gateway/sessions/a2/budget", headers=alice, json={"budget_usd": "100"})
    assert resp.status_code == 409


@pytest.mark.parametrize("allow_client_budgets", [False])
async def test_user_budgets_can_be_disabled(
    http: httpx.AsyncClient, two_users: dict[str, dict[str, str]]
) -> None:
    resp = await http.put(
        "/gateway/sessions/a1/budget", headers=two_users["alice"], json={"budget_usd": "1"}
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("store_content", [True])
async def test_handoffs_are_listed_and_downloaded_only_by_their_owner(
    app: FastAPI, http: httpx.AsyncClient, two_users: dict[str, dict[str, str]]
) -> None:
    app.state.anthropic = fake_client(lambda _: api_message(json.dumps(CONTENT)), [])
    alice, bob = two_users["alice"], two_users["bob"]
    created = (await http.post("/gateway/sessions/a1/handoffs", headers=alice)).json()

    [listed] = (await http.get("/gateway/sessions/a1/handoffs", headers=alice)).json()
    assert listed["id"] == created["id"] and "markdown" not in listed
    md = await http.get(f"/gateway/sessions/a1/handoffs/{created['id']}/markdown", headers=alice)
    assert md.status_code == 200 and md.text.startswith("# Handoff:")

    # bob can't list it, and can't fetch it even with its id (under any session path).
    assert (await http.get("/gateway/sessions/a1/handoffs", headers=bob)).json() == []
    for sid in ("a1", "b1"):
        url = f"/gateway/sessions/{sid}/handoffs/{created['id']}/markdown"
        assert (await http.get(url, headers=bob)).status_code == 404


async def test_admin_key_is_not_a_user_key(http: httpx.AsyncClient) -> None:
    admin = {"authorization": "Bearer test-admin-key"}
    assert (await http.get("/gateway/me", headers=admin)).status_code == 401
