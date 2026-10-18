"""Dashboard stats against a hand-built dataset, so every expected number can be checked
by hand. Run on SQLite by default and on Postgres with TEST_DATABASE_URL."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway.auth import create_key
from gateway.models import Alert, RequestLog, SessionBudget, SessionCategory
from gateway.stats import percentile
from tests.conftest import ADMIN_KEY

ADMIN = {"authorization": f"Bearer {ADMIN_KEY}"}
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)  # a fixed hour, so buckets are predictable
RANGE = {"from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"}


def call(key: uuid.UUID, session: str, minutes: int, **fields: Any) -> RequestLog:
    defaults: dict[str, Any] = {
        "app": "test",
        "session_id_source": "header",
        "path": "/v1/messages",
        "model": "claude-sonnet-5-5",
        "status_code": 200,
        "latency_ms": 1000,
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cost_usd": Decimal("0.01"),
    }
    return RequestLog(
        api_key_id=key,
        session_id=session,
        created_at=T0 + timedelta(minutes=minutes),
        **(defaults | fields),
    )


@pytest.fixture
async def seeded(app: FastAPI) -> dict[str, uuid.UUID]:
    """alice: session a1 (3 calls, coding), a2 (1 call, uncategorized, $0.50 budget);
    bob: session b1 (2 calls, writing). Plus one call outside the range."""
    async with app.state.session_factory() as session:
        alice, _ = await create_key(session, "alice")
        bob, _ = await create_key(session, "bob")
        a, b = alice.id, bob.id
        session.add_all(
            [
                # 10:00 hour
                call(a, "a1", 0, latency_ms=100, ttft_ms=50, is_stream=True,
                     context_tokens=150_000, context_window=1_000_000),
                call(a, "a1", 10, latency_ms=200, status_code=529, error_type="overloaded_error",
                     input_tokens=None, output_tokens=None, cost_usd=None),
                call(a, "a1", 20, latency_ms=300, error_type="client_disconnected",
                     context_tokens=900_000, context_window=1_000_000),
                # 12:00 hour
                call(a, "a2", 125, latency_ms=400, model="claude-haiku-5-5",
                     cost_usd=Decimal("0.40")),
                call(b, "b1", 130, latency_ms=500, cost_usd=Decimal("0.02")),
                call(b, "b1", 140, latency_ms=600, cost_usd=Decimal("0.03"), ttft_ms=150,
                     is_stream=True),
                # outside the range
                call(b, "b1", 60 * 24, cost_usd=Decimal("9.99")),
            ]
        )  # fmt: skip
        session.add_all(
            [
                SessionCategory(api_key_id=a, session_id="a1", category="coding",
                                status="classified", classified_calls=3),
                SessionCategory(api_key_id=b, session_id="b1", category="writing",
                                status="client_tag", classified_calls=2),
                SessionBudget(api_key_id=a, session_id="a2", budget_usd=Decimal("0.50"),
                              source="admin"),
                Alert(api_key_id=a, session_id="a2", kind="spend", threshold=Decimal("0.8"),
                      observed=Decimal("0.8"), detail={}),
            ]
        )  # fmt: skip
        await session.commit()
    return {"alice": a, "bob": b}


def test_percentile_matches_postgres_percentile_cont() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.5) == 25.0  # halfway between 20 and 30
    assert percentile(values, 0.95) == pytest.approx(38.5)  # position 2.85
    assert percentile([7.0], 0.95) == 7.0
    assert percentile([], 0.5) is None


async def test_overview_totals(http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]) -> None:
    body = (await http.get("/admin/stats/overview", headers=ADMIN, params=RANGE)).json()
    assert body["calls"] == 6  # the next-day call is outside the range
    assert body["sessions"] == 3
    # 0.01 + 0.01 + 0.40 + 0.02 + 0.03 (the failed call has no cost)
    assert body["spend_usd"] == "0.47"
    # The 529 counts; the client disconnect doesn't.
    assert body["errors"] == 1
    assert body["error_rate"] == pytest.approx(1 / 6)
    assert body["output_tokens"] == 50 and body["prompt_tokens"] == 500
    # latencies 100..600: p50 = 350, p95 = 500 + 0.75 * 100
    assert body["latency_p50_ms"] == pytest.approx(350)
    assert body["latency_p95_ms"] == pytest.approx(575)
    assert body["ttft_p50_ms"] == pytest.approx(100)  # 50 and 150


async def test_overview_series_has_every_bucket(
    http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    body = (await http.get("/admin/stats/overview", headers=ADMIN, params=RANGE)).json()
    assert body["bucket"] == "hour"  # 1-day range
    series = body["series"]
    assert len(series) == 24
    assert series[0]["start"] == "2026-10-01T00:00:00Z"
    by_hour = {s["start"][11:13]: s for s in series}
    assert (by_hour["10"]["calls"], by_hour["10"]["spend_usd"], by_hour["10"]["errors"]) == (
        3,
        "0.02",
        1,
    )
    assert (by_hour["12"]["calls"], by_hour["12"]["spend_usd"]) == (3, "0.45")
    assert by_hour["11"] == {
        "start": "2026-10-01T11:00:00Z",
        "calls": 0,
        "spend_usd": "0",
        "errors": 0,
    }

    daily = (
        await http.get("/admin/stats/overview", headers=ADMIN, params=RANGE | {"bucket": "day"})
    ).json()["series"]
    assert [(d["start"], d["calls"]) for d in daily] == [("2026-10-01T00:00:00Z", 6)]


async def test_overview_by_model_and_user_filter(
    http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    body = (await http.get("/admin/stats/overview", headers=ADMIN, params=RANGE)).json()
    models = {m["model"]: m for m in body["by_model"]}
    assert models["claude-haiku-5-5"]["spend_usd"] == "0.4"
    assert models["claude-sonnet-5-5"]["calls"] == 5
    assert models["claude-haiku-5-5"]["latency_p50_ms"] == pytest.approx(400)
    assert body["by_model"][0]["model"] == "claude-haiku-5-5"  # sorted by spend

    bob = (
        await http.get(
            "/admin/stats/overview", headers=ADMIN, params=RANGE | {"key_id": str(seeded["bob"])}
        )
    ).json()
    assert (bob["calls"], bob["spend_usd"], bob["sessions"]) == (2, "0.05", 1)


async def test_spend_by_category(http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]) -> None:
    rows = (await http.get("/admin/stats/categories", headers=ADMIN, params=RANGE)).json()
    assert [(r["category"], r["spend_usd"], r["calls"], r["sessions"]) for r in rows] == [
        ("uncategorized", "0.4", 1, 1),
        ("writing", "0.05", 2, 1),
        ("coding", "0.02", 3, 1),
    ]


async def test_spend_by_user(http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]) -> None:
    rows = (await http.get("/admin/stats/users", headers=ADMIN, params=RANGE)).json()
    assert [(r["name"], r["spend_usd"], r["calls"], r["sessions"], r["errors"]) for r in rows] == [
        ("alice", "0.42", 4, 2, 1),
        ("bob", "0.05", 2, 1, 0),
    ]
    assert rows[0]["last_call_at"] == "2026-10-01T12:05:00Z"


async def test_sessions_list(http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]) -> None:
    body = (await http.get("/admin/sessions", headers=ADMIN, params=RANGE)).json()
    assert body["total"] == 3
    assert [i["session_id"] for i in body["items"]] == ["b1", "a2", "a1"]  # most recent first
    items = {i["session_id"]: i for i in body["items"]}

    a2 = items["a2"]
    assert (a2["spend_usd"], a2["budget_usd"], a2["budget_source"]) == ("0.4", "0.5", "admin")
    assert a2["spend_fraction"] == "0.8"
    assert (a2["alerts"], a2["category"], a2["key_name"]) == (1, "uncategorized", "alice")

    # Budget spend is the session's whole history, including the call outside the range.
    b1 = items["b1"]
    assert (b1["spend_usd"], b1["calls"], b1["calls_in_window"]) == ("10.04", 3, 2)
    assert b1["budget_source"] == "default"

    assert items["a1"]["peak_context_fraction"] == pytest.approx(0.9)
    assert items["b1"]["peak_context_fraction"] is None


async def test_sessions_list_filters_sorts_and_pages(
    http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    def get(**params: Any) -> Any:
        return http.get("/admin/sessions", headers=ADMIN, params=RANGE | params)

    by_spend = (await get(sort="spend")).json()["items"]
    assert [i["session_id"] for i in by_spend] == ["b1", "a2", "a1"]
    coding = (await get(category="coding")).json()
    assert (coding["total"], [i["session_id"] for i in coding["items"]]) == (1, ["a1"])
    uncategorized = (await get(category="uncategorized")).json()["items"]
    assert [i["session_id"] for i in uncategorized] == ["a2"]
    page = (await get(limit=1, offset=1)).json()
    assert (page["total"], [i["session_id"] for i in page["items"]]) == (3, ["a2"])
    alice_only = (await get(key_id=str(seeded["alice"]))).json()
    assert {i["session_id"] for i in alice_only["items"]} == {"a1", "a2"}


async def test_session_calls_timeline(
    http: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    url = f"/admin/keys/{seeded['alice']}/sessions/a1/calls"
    calls = (await http.get(url, headers=ADMIN)).json()
    assert [c["status_code"] for c in calls] == [200, 529, 200]
    assert calls[1]["error_type"] == "overloaded_error" and calls[1]["cost_usd"] is None
    assert calls[0]["created_at"] == "2026-10-01T10:00:00Z"
    assert (calls[0]["context_tokens"], calls[0]["ttft_ms"]) == (150_000, 50)


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-10-02T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
        {"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z", "bucket": "hour"},
    ],
)
async def test_invalid_ranges(http: httpx.AsyncClient, params: dict[str, str]) -> None:
    assert (
        await http.get("/admin/stats/overview", headers=ADMIN, params=params)
    ).status_code == 422


async def test_stats_need_the_admin_key(http: httpx.AsyncClient) -> None:
    for path in ("/admin/stats/overview", "/admin/stats/users", "/admin/sessions"):
        assert (await http.get(path)).status_code == 401


async def test_empty_range(http: httpx.AsyncClient) -> None:
    body = (await http.get("/admin/stats/overview", headers=ADMIN, params=RANGE)).json()
    assert (body["calls"], body["spend_usd"], body["error_rate"]) == (0, "0", None)
    assert body["latency_p50_ms"] is None and len(body["series"]) == 24
