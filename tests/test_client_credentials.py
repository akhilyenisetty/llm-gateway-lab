"""Client-credential mode (e.g. Claude Code on a claude.ai plan) and Claude Code's request
labels. The client identifies itself with X-Gateway-Key; its own credential is forwarded
unchanged, and the gateway's own Anthropic key is never used."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select, text

from gateway.models import RequestLog
from gateway.transcript import pick_main
from tests.conftest import (
    ADMIN_KEY,
    REAL_KEY,
    fetch_alerts,
    fetch_rows,
    key_id_of,
    message_response,
)
from tests.streams import text_stream

OAUTH = "fake-subscription-login-token-for-tests"
OAUTH_BETA = "oauth-2025-04-20,interleaved-thinking-2025-05-14"
BODY: dict[str, Any] = {
    "model": "claude-sonnet-5-5",
    "max_tokens": 10,
    "messages": [{"role": "user", "content": "Hi"}],
}


def subscription(gateway_key: str, **extra: str) -> dict[str, str]:
    """Headers Claude Code sends on a claude.ai login, plus X-Gateway-Key via
    ANTHROPIC_CUSTOM_HEADERS."""
    return {
        "authorization": f"Bearer {OAUTH}",
        "anthropic-beta": OAUTH_BETA,
        "anthropic-version": "2023-06-01",
        "x-gateway-key": gateway_key,
        **extra,
    }


async def test_subscription_credential_is_forwarded_and_gateway_key_never_sent(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages").respond(200, json=message_response())
    resp = await http.post("/v1/messages?beta=true", headers=subscription(gateway_key), json=BODY)
    assert resp.status_code == 200

    sent = route.calls.last.request.headers
    assert sent["authorization"] == f"Bearer {OAUTH}"  # unchanged
    assert sent["anthropic-beta"] == OAUTH_BETA  # the OAuth capability survives
    assert "x-api-key" not in sent  # the gateway's own key was not added
    assert REAL_KEY not in str(sent.raw)
    assert "x-gateway-key" not in sent and gateway_key not in str(sent.raw)

    [row] = await fetch_rows(app)
    assert row.billing == "client_credential"
    assert row.cost_usd is not None  # still computed, as API-equivalent
    assert row.input_tokens == 12


async def test_own_api_key_is_forwarded_too(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages").respond(200, json=message_response())
    headers = {"x-api-key": "fake-own-api-key-for-tests", "x-gateway-key": gateway_key}
    assert (await http.post("/v1/messages", headers=headers, json=BODY)).status_code == 200
    assert route.calls.last.request.headers["x-api-key"] == "fake-own-api-key-for-tests"
    assert (await fetch_rows(app))[0].billing == "client_credential"


async def test_streaming_works_in_client_credential_mode(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages").respond(
        200, headers={"content-type": "text/event-stream"}, content=text_stream()
    )
    resp = await http.post(
        "/v1/messages", headers=subscription(gateway_key), json={**BODY, "stream": True}
    )
    assert resp.content == text_stream()
    assert "x-api-key" not in route.calls.last.request.headers
    [row] = await fetch_rows(app)
    assert (row.billing, row.is_stream, row.output_tokens) == ("client_credential", True, 15)


async def test_passthrough_paths_forward_the_client_credential(
    http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages/count_tokens").respond(200, json={"input_tokens": 3})
    resp = await http.post(
        "/v1/messages/count_tokens", headers=subscription(gateway_key), json=BODY
    )
    assert resp.status_code == 200
    sent = route.calls.last.request.headers
    assert sent["authorization"] == f"Bearer {OAUTH}" and "x-api-key" not in sent


@pytest.mark.parametrize(
    ("headers", "status", "error"),
    [
        ({"x-gateway-key": "GW"}, 401, "also send your own"),  # nothing of theirs to forward
        ({"x-gateway-key": "GW", "x-api-key": "GW"}, 400, "not a gateway key"),
        ({"x-gateway-key": "gw_unknown", "authorization": f"Bearer {OAUTH}"}, 401, "invalid"),
    ],
)
async def test_bad_client_credential_requests_never_reach_upstream(
    http: httpx.AsyncClient,
    gateway_key: str,
    upstream: respx.MockRouter,
    headers: dict[str, str],
    status: int,
    error: str,
) -> None:
    route = upstream.post("/v1/messages")
    headers = {k: v.replace("GW", gateway_key) for k, v in headers.items()}
    resp = await http.post("/v1/messages", headers=headers, json=BODY)
    assert resp.status_code == status and error in resp.json()["error"]["message"]
    assert not route.called


@pytest.mark.parametrize("allow_client_credentials", [False])
async def test_client_credentials_can_be_disabled(
    http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages")
    resp = await http.post("/v1/messages", headers=subscription(gateway_key), json=BODY)
    assert resp.status_code == 403
    assert not route.called


@pytest.mark.parametrize("store_content", [True])
async def test_login_token_is_never_stored(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await http.post("/v1/messages", headers=subscription(gateway_key), json=BODY)
    async with app.state.session_factory() as session:
        for table in ("requests", "request_contents", "api_keys", "alerts", "session_budgets"):
            rows = (await session.execute(text(f"select * from {table}"))).all()
            assert OAUTH not in json.dumps([list(map(str, r)) for r in rows]), table


async def test_api_equivalent_spend_is_reported_separately(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    # $0.01 per call: one on the gateway's key, one on the client's own plan.
    cent = {"input_tokens": 5_000, "output_tokens": 0}
    upstream.post("/v1/messages").respond(200, json=message_response(usage=cent))
    budget = {"x-session-id": "s1", "x-session-budget-usd": "0.0125"}
    await http.post("/v1/messages", headers={"x-api-key": gateway_key, **budget}, json=BODY)
    await http.post("/v1/messages", headers=subscription(gateway_key, **budget), json=BODY)

    admin = {"authorization": f"Bearer {ADMIN_KEY}"}
    status = (
        await http.get(
            f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1", headers=admin
        )
    ).json()
    assert (Decimal(status["spent_usd"]), Decimal(status["api_equivalent_usd"])) == (
        Decimal("0.02"),
        Decimal("0.01"),
    )
    # Budgets count both (as a usage signal), and the alert says how much was API-equivalent.
    spend_alerts = [a for a in await fetch_alerts(app) if a.kind == "spend"]
    assert spend_alerts[-1].detail["api_equivalent_usd"] == "0.01"
    overview = (await http.get("/admin/stats/overview", headers=admin)).json()
    assert (overview["spend_usd"], overview["api_equivalent_usd"]) == ("0.02", "0.01")
    [session] = (await http.get("/admin/sessions", headers=admin)).json()["items"]
    assert session["api_equivalent_usd"] == "0.01"


async def test_client_api_accepts_the_gateway_key_header(
    http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    await http.post(
        "/v1/messages", headers=subscription(gateway_key, **{"x-session-id": "mine"}), json=BODY
    )
    listed = (await http.get("/gateway/sessions", headers={"x-gateway-key": gateway_key})).json()
    assert [s["session_id"] for s in listed] == ["mine"]


# --- Claude Code's request labels -----------------------------------------------------


async def test_request_class_and_agent_are_recorded(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response())
    hints = {
        "x-claude-code-session-id": "cc-1",
        "x-claude-code-request-class": "subagent",
        "x-claude-code-agent-id": "agent-42",
    }
    await http.post("/v1/messages", headers=subscription(gateway_key, **hints), json=BODY)
    async with app.state.session_factory() as session:
        row = (await session.execute(select(RequestLog))).scalar_one()
    assert (row.request_class, row.agent_id, row.session_id) == ("subagent", "agent-42", "cc-1")


def test_pick_main_prefers_main_over_a_longer_auxiliary_request() -> None:
    main_turn = {"messages": [1, 2, 3, 4, 5]}
    suggestion = {"messages": [1, 2, 3, 4, 5, 6]}  # history + an instruction of its own
    title = {"messages": [1]}
    requests = [title, main_turn, suggestion]
    assert pick_main(requests) == 2  # without labels, the longest wins
    assert pick_main(requests, ["auxiliary", "main", "auxiliary"]) == 1
    assert pick_main(requests, [None, None, None]) == 2  # no labels: unchanged behavior


@pytest.mark.parametrize("store_content", [True])
async def test_digest_skips_auxiliary_requests(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, upstream: respx.MockRouter
) -> None:
    from gateway import categorizer
    from gateway.categorizer import SessionRef

    upstream.post("/v1/messages").respond(200, json=message_response())
    base = {"x-claude-code-session-id": "cc-1"}
    for cls, prompt in (("main", "Fix the login bug"), ("auxiliary", "[SUGGESTION MODE: guess")):
        body = {**BODY, "messages": [{"role": "user", "content": prompt}]}
        headers = subscription(gateway_key, **base, **{"x-claude-code-request-class": cls})
        await http.post("/v1/messages", headers=headers, json=body)
    async with app.state.session_factory() as session:
        digest = await categorizer._digest(
            session, SessionRef(await key_id_of(app, gateway_key), "cc-1")
        )
    assert digest is not None
    assert "Fix the login bug" in digest and "SUGGESTION MODE" not in digest
