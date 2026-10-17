"""Spend and context budgets: alerts recorded through real calls, and the admin API."""

from decimal import Decimal
from typing import Any

import anthropic
import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from gateway import budgets
from gateway.auth import create_key
from gateway.models import Alert
from tests.conftest import (
    ADMIN_KEY,
    GATEWAY_URL,
    fetch_alerts,
    fetch_rows,
    key_id_of,
    message_response,
)
from tests.streams import text_stream

USER_MSG = [{"role": "user", "content": "Hi"}]
ADMIN = {"authorization": f"Bearer {ADMIN_KEY}"}


def usage(input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": 0,
    }


# Sonnet 5.5 input is $2/MTok, so 5,000 input tokens cost exactly $0.01.
CENT = usage(input_tokens=5_000)


async def call(
    sdk: anthropic.AsyncAnthropic,
    session_id: str = "s1",
    model: str = "claude-sonnet-5-5",
    **headers: str,
) -> None:
    await sdk.messages.create(
        model=model,
        max_tokens=10,
        messages=USER_MSG,
        extra_headers={"X-Session-Id": session_id, **headers},
    )


# --- Spend ---------------------------------------------------------------------------


async def test_spend_alerts_fire_once_per_threshold(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    budget = {"X-Session-Budget-Usd": "0.025"}

    await call(sdk, **budget)  # $0.01 = 40%
    assert await fetch_alerts(app) == []

    await call(sdk, **budget)  # $0.02 = 80%
    await call(sdk, **budget)  # $0.03 = 120%
    await call(sdk, **budget)  # $0.04: both thresholds already fired

    rows = await fetch_rows(app)
    alerts = await fetch_alerts(app)
    assert [(a.kind, a.threshold, a.observed) for a in alerts] == [
        ("spend", Decimal("0.8"), Decimal("0.8")),
        ("spend", Decimal("1"), Decimal("1.2")),
    ]
    assert [a.request_id for a in alerts] == [rows[1].id, rows[2].id]
    assert alerts[0].detail == {
        "spent_usd": "0.02",
        "budget_usd": "0.025",
        "budget_source": "header",
    }


@pytest.mark.parametrize("default_budget", [Decimal("0.01")])
async def test_default_budget_applies_without_header(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    await call(sdk)
    alerts = await fetch_alerts(app)
    assert [a.threshold for a in alerts] == [Decimal("0.8"), Decimal("1")]
    assert alerts[0].detail["budget_source"] == "default"


async def test_sessions_have_separate_budgets(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    await call(sdk, "a", **{"X-Session-Budget-Usd": "0.01"})
    await call(sdk, "b", **{"X-Session-Budget-Usd": "1.00"})
    assert {a.session_id for a in await fetch_alerts(app)} == {"a"}


async def test_budget_header_is_not_forwarded(
    sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, **{"X-Session-Budget-Usd": "3"})
    assert "x-session-budget-usd" not in route.calls.last.request.headers


async def test_streamed_calls_count_toward_spend(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    # text_stream: 25 input + 100 cache read + 15 output on Sonnet 5.5 = $0.00021
    upstream.post("/v1/messages").respond(
        200, headers={"content-type": "text/event-stream"}, content=text_stream()
    )
    async with sdk.messages.stream(
        model="claude-sonnet-5-5",
        max_tokens=10,
        messages=USER_MSG,
        extra_headers={"X-Session-Id": "s1", "X-Session-Budget-Usd": "0.0002"},
    ) as stream:
        await stream.get_final_message()
    assert [a.threshold for a in await fetch_alerts(app)] == [Decimal("0.8"), Decimal("1")]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.5", Decimal("2.5")),
        (" 10 ", Decimal("10")),
        ("0.00001", None),  # would round to a $0 budget
        ("0", None),
        ("-1", None),
        ("abc", None),
        ("NaN", None),
        ("Infinity", None),
        ("1e9", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_budget(raw: str | None, expected: Decimal | None) -> None:
    assert budgets.parse_budget(raw) == expected


# --- Context -------------------------------------------------------------------------


async def test_context_alert_from_a_large_call(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    # Haiku 4.5 has a 200K window: 150K input + 10K cache read + 5K output = 165K = 82.5%
    big = usage(input_tokens=150_000, cache_read=10_000, output_tokens=5_000)
    upstream.post("/v1/messages").respond(
        200, json=message_response(model="claude-haiku-4-5-20251001", usage=big)
    )
    await call(sdk, model="claude-haiku-4-5")

    [row] = await fetch_rows(app)
    assert (row.context_tokens, row.context_window) == (165_000, 200_000)
    [alert] = await fetch_alerts(app)
    assert (alert.kind, alert.threshold, alert.observed) == (
        "context",
        Decimal("0.8"),
        Decimal("0.825"),
    )
    assert alert.request_id == row.id
    assert alert.detail == {
        "context_tokens": 165_000,
        "context_window": 200_000,
        "model": "claude-haiku-4-5-20251001",
    }


async def test_small_call_after_large_one_does_not_reset_or_refire(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    """Like Claude Code's title calls between main turns: small calls don't affect alerts."""
    big = usage(input_tokens=170_000)
    small = usage(input_tokens=1_000)
    route = upstream.post("/v1/messages")
    route.side_effect = [
        httpx.Response(200, json=message_response(model="claude-haiku-4-5-20251001", usage=u))
        for u in (big, small, big)
    ]
    for _ in range(3):
        await call(sdk, model="claude-haiku-4-5")
    assert len([a for a in await fetch_alerts(app) if a.kind == "context"]) == 1


async def test_unknown_model_records_tokens_but_no_context_alert(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(
        200, json=message_response(model="claude-new-9", usage=usage(input_tokens=999_999))
    )
    await call(sdk, model="claude-new-9")
    [row] = await fetch_rows(app)
    assert (row.context_tokens, row.context_window) == (999_999, None)
    assert [a.kind for a in await fetch_alerts(app)] == []


# --- Robustness ----------------------------------------------------------------------


async def test_duplicate_alert_is_rejected_by_the_database(app: FastAPI, gateway_key: str) -> None:
    """The unique constraint is what makes "fire once" hold under concurrency."""
    fields = {
        "api_key_id": await key_id_of(app, gateway_key),
        "session_id": "s1",
        "kind": "spend",
        "threshold": Decimal("0.8"),
        "observed": Decimal("0.9"),
        "detail": {},
    }
    async with app.state.session_factory() as session:
        session.add(Alert(**fields))
        await session.commit()
        session.add(Alert(**fields))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_set_budget_upserts_and_respects_admin(app: FastAPI, gateway_key: str) -> None:
    key = await key_id_of(app, gateway_key)
    async with app.state.session_factory() as session:
        assert await budgets.set_budget(session, key, "s1", Decimal("1"), "header")
        assert await budgets.set_budget(session, key, "s1", Decimal("2"), "header")  # update
        assert await budgets.set_budget(session, key, "s1", Decimal("3"), "admin")
        assert not await budgets.set_budget(session, key, "s1", Decimal("4"), "header")
        assert await budgets.set_budget(session, key, "s1", Decimal("5"), "admin")
        await session.commit()
        status = await budgets.budget_status(session, key, "s1", Decimal("9"))
    assert (status.budget_usd, status.budget_source) == (Decimal("5"), "admin")


async def test_budget_write_failure_keeps_the_request_row(
    app: FastAPI,
    sdk: anthropic.AsyncAnthropic,
    upstream: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a failed budget write (e.g. a unique violation under concurrency) once
    rolled back the request row it shared a transaction with."""

    async def broken(*args: Any, **kwargs: Any) -> bool:
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(budgets, "set_budget", broken)
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk, **{"X-Session-Budget-Usd": "1"})
    assert len(await fetch_rows(app)) == 1


async def test_budget_check_failure_keeps_the_request_row(
    app: FastAPI,
    sdk: anthropic.AsyncAnthropic,
    upstream: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("budget check failed")

    monkeypatch.setattr(budgets, "check_after_call", broken)
    upstream.post("/v1/messages").respond(200, json=message_response())
    await call(sdk)
    assert len(await fetch_rows(app)) == 1


# --- Sessions are scoped by gateway key ----------------------------------------------


async def second_client(app: FastAPI) -> anthropic.AsyncAnthropic:
    import httpx2

    async with app.state.session_factory() as session:
        _, plaintext = await create_key(session, "second-client")
    return anthropic.AsyncAnthropic(
        api_key=plaintext,
        base_url=GATEWAY_URL,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)),
    )


async def test_same_session_id_from_two_keys_is_two_sessions(
    app: FastAPI, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    other = await second_client(app)
    budget = {"X-Session-Budget-Usd": "0.025"}
    # Each key spends $0.02 in "s1": 80% each if separate, 160% if they were mixed.
    for client in (sdk, other, sdk, other):
        await call(client, "s1", **budget)
    alerts = await fetch_alerts(app)
    assert [(a.threshold, a.observed) for a in alerts] == [
        (Decimal("0.8"), Decimal("0.8")),
        (Decimal("0.8"), Decimal("0.8")),
    ]
    assert len({a.api_key_id for a in alerts}) == 2


async def test_one_key_cannot_change_another_keys_session_budget(
    app: FastAPI,
    http: httpx.AsyncClient,
    sdk: anthropic.AsyncAnthropic,
    gateway_key: str,
    upstream: respx.MockRouter,
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    await call(sdk, "s1", **{"X-Session-Budget-Usd": "1.00"})
    other = await second_client(app)
    await call(other, "s1", **{"X-Session-Budget-Usd": "0.01"})  # targets "s1" too

    url = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1"
    status = (await http.get(url, headers=ADMIN)).json()
    assert Decimal(status["budget_usd"]) == Decimal("1.00")
    assert status["calls"] == 1  # the other key's call isn't counted here


@pytest.mark.parametrize("allow_client_budgets", [False])
async def test_client_budget_header_ignored_when_disabled(
    app: FastAPI,
    http: httpx.AsyncClient,
    sdk: anthropic.AsyncAnthropic,
    gateway_key: str,
    upstream: respx.MockRouter,
) -> None:
    route = upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    await call(sdk, **{"X-Session-Budget-Usd": "0.01"})
    assert await fetch_alerts(app) == []  # $0.01 of the $5 default, not of $0.01
    assert "x-session-budget-usd" not in route.calls.last.request.headers  # still stripped
    url = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1"
    assert (await http.get(url, headers=ADMIN)).json()["budget_source"] == "default"


# --- Admin API -----------------------------------------------------------------------


async def test_admin_session_status(
    app: FastAPI,
    http: httpx.AsyncClient,
    sdk: anthropic.AsyncAnthropic,
    gateway_key: str,
    upstream: respx.MockRouter,
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    await call(sdk, **{"X-Session-Budget-Usd": "0.0125"})
    key = await key_id_of(app, gateway_key)

    resp = await http.get(f"/admin/keys/{key}/sessions/s1", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_prefix"] == gateway_key[:10]
    assert body["calls"] == 1
    assert Decimal(body["spent_usd"]) == Decimal("0.01")
    assert Decimal(body["budget_usd"]) == Decimal("0.0125")
    assert body["budget_source"] == "header"
    assert Decimal(body["spend_fraction"]) == Decimal("0.8")
    assert [a["kind"] for a in body["alerts"]] == ["spend"]


async def test_admin_budget_overrides_header_and_alerts_immediately(
    app: FastAPI,
    http: httpx.AsyncClient,
    sdk: anthropic.AsyncAnthropic,
    gateway_key: str,
    upstream: respx.MockRouter,
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    await call(sdk)  # $0.01 of the $5 default: no alert
    url = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1"

    resp = await http.put(f"{url}/budget", headers=ADMIN, json={"budget_usd": "0.01"})
    assert resp.status_code == 200
    assert resp.json()["budget_source"] == "admin"
    alerts = await fetch_alerts(app)
    assert [a.threshold for a in alerts] == [Decimal("0.8"), Decimal("1")]
    assert all(a.request_id is None for a in alerts)  # triggered by the budget change

    # A later header doesn't override the admin's budget.
    await call(sdk, **{"X-Session-Budget-Usd": "100"})
    status = (await http.get(url, headers=ADMIN)).json()
    assert (status["budget_source"], Decimal(status["budget_usd"])) == ("admin", Decimal("0.01"))


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "100001", "0.00001"])
async def test_admin_budget_validation(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str, bad: str
) -> None:
    url = f"/admin/keys/{await key_id_of(app, gateway_key)}/sessions/s1/budget"
    resp = await http.put(url, headers=ADMIN, json={"budget_usd": bad})
    assert resp.status_code == 422


async def test_admin_unknown_key_is_404(http: httpx.AsyncClient) -> None:
    url = "/admin/keys/00000000-0000-0000-0000-000000000000/sessions/s1"
    assert (await http.get(url, headers=ADMIN)).status_code == 404
    resp = await http.put(f"{url}/budget", headers=ADMIN, json={"budget_usd": "1"})
    assert resp.status_code == 404


async def test_admin_lists_keys_without_secrets(
    app: FastAPI, http: httpx.AsyncClient, gateway_key: str
) -> None:
    [key] = (await http.get("/admin/keys", headers=ADMIN)).json()
    assert key["id"] == str(await key_id_of(app, gateway_key))
    assert key["key_prefix"] == gateway_key[:10]
    assert gateway_key not in str(key) and "key_hash" not in key


async def test_admin_alerts_list_newest_first_and_filters(
    http: httpx.AsyncClient, sdk: anthropic.AsyncAnthropic, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(200, json=message_response(usage=CENT))
    await call(sdk, "a", **{"X-Session-Budget-Usd": "0.01"})
    await call(sdk, "b", **{"X-Session-Budget-Usd": "0.01"})

    all_alerts = (await http.get("/admin/alerts", headers=ADMIN)).json()
    assert [a["session_id"] for a in all_alerts] == ["b", "b", "a", "a"]
    only_a = (await http.get("/admin/alerts?session_id=a", headers=ADMIN)).json()
    assert {a["session_id"] for a in only_a} == {"a"}
    one = (await http.get("/admin/alerts?limit=1", headers=ADMIN)).json()
    assert len(one) == 1


async def test_admin_requires_admin_key(http: httpx.AsyncClient, gateway_key: str) -> None:
    assert (await http.get("/admin/alerts")).status_code == 401
    wrong = {"authorization": "Bearer nope"}
    assert (await http.get("/admin/alerts", headers=wrong)).status_code == 401
    # A client gateway key is not an admin key.
    client = {"x-api-key": gateway_key}
    assert (await http.get("/admin/alerts", headers=client)).status_code == 401


@pytest.mark.parametrize("admin_key", [""])
async def test_admin_disabled_without_key(http: httpx.AsyncClient) -> None:
    resp = await http.get("/admin/alerts", headers={"authorization": "Bearer "})
    assert resp.status_code == 503
