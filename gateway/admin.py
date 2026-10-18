"""Admin API: gateway keys, session budget status, budget overrides, and alerts.

Protected by ADMIN_API_KEY (sent as `Authorization: Bearer <key>`), which is separate from
client gateway keys: a client must not be able to raise its own budget. The Stage 5
dashboard reads the same endpoints.

A session is identified by (gateway key, session ID), so session URLs name the key:

    GET /admin/keys                                         keys (id, prefix, name)
    POST /admin/keys                                        create one: {"name": "alice"}
    POST /admin/keys/{key_id}/revoke                        revoke one (it stops working)
    GET /admin/keys/{key_id}/sessions/{session_id}          spend, budget, and alerts
    PUT /admin/keys/{key_id}/sessions/{session_id}/budget   set a budget (overrides header)
    POST /admin/keys/{key_id}/sessions/{session_id}/categorize  categorize it now
    GET /admin/keys/{key_id}/sessions/{session_id}/calls    every call, oldest first
    POST /admin/keys/{key_id}/sessions/{session_id}/handoffs  generate a handoff document
    GET /admin/keys/{key_id}/sessions/{session_id}/handoffs   the session's handoffs
    GET /admin/handoffs/{id}                                one handoff, with its document
    GET /admin/handoffs/{id}/markdown                       download it as Markdown
    GET /admin/alerts?key_id=&session_id=&limit=            recent alerts, newest first

Dashboard stats (all take ?from=&to= as ISO datetimes, default the last 7 days, and an
optional ?key_id= to show one user):

    GET /admin/stats/overview?bucket=hour|day   totals, percentiles, time series, by model
    GET /admin/stats/categories                 spend and calls by session category
    GET /admin/stats/users                      spend and calls by gateway key
    GET /admin/sessions?category=&sort=recent|spend&limit=&offset=   sessions list

Money is returned as decimal strings (exact), times as ISO 8601 UTC.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway import budgets, categorizer, stats
from gateway.auth import create_key, extract_key
from gateway.handoff_api import (
    HandoffOut,
    HandoffSummary,
    generate_or_http_error,
    markdown_download,
)
from gateway.models import Alert, ApiKey, Handoff, SessionCategory, utcnow
from gateway.settings import Settings


class KeyOut(BaseModel):
    id: uuid.UUID
    key_prefix: str
    name: str
    created_at: datetime
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class KeyIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class KeyCreatedOut(KeyOut):
    key: str  # the only time the plaintext key is ever returned


class AlertOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    api_key_id: uuid.UUID
    session_id: str
    kind: str
    threshold: Decimal
    observed: Decimal
    request_id: uuid.UUID | None
    detail: dict[str, Any]

    model_config = {"from_attributes": True}


class CategoryOut(BaseModel):
    category: str | None
    status: str
    client_category: str | None
    auto_category: str | None
    auto_reason: str | None
    classifier_model: str | None
    prompt_version: str | None
    classifier_cost_usd: Decimal | None
    error: str | None
    classified_calls: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionOut(BaseModel):
    api_key_id: uuid.UUID
    key_prefix: str
    session_id: str
    calls: int
    spent_usd: Decimal
    api_equivalent_usd: Decimal  # part of spent_usd on the client's own credential
    budget_usd: Decimal
    budget_source: str
    spend_fraction: Decimal
    category: CategoryOut | None
    alerts: list[AlertOut]


def to_json(value: Any) -> Any:
    """Make stats results JSON-safe: exact decimal strings for money, ISO UTC times."""
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json(v) for v in value]
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, datetime):
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


MAX_HOURLY_RANGE = timedelta(days=14)


def window_from(
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    key_id: uuid.UUID | None = None,
) -> stats.Window:
    end = to or datetime.now(UTC)
    start = from_ or end - timedelta(days=7)
    end = end.replace(tzinfo=UTC) if end.tzinfo is None else end
    start = start.replace(tzinfo=UTC) if start.tzinfo is None else start
    if start >= end:
        raise HTTPException(422, "`from` must be before `to`")
    return stats.Window(start, end, key_id)


class BudgetIn(BaseModel):
    budget_usd: Decimal = Field(gt=0, le=budgets.MAX_BUDGET_USD, decimal_places=4)


async def build_session_out(
    session: AsyncSession, settings: Settings, key: ApiKey, session_id: str
) -> SessionOut:
    """A session's spend, budget, category, and alerts. Shared by the admin API (any key)
    and the client API (the caller's own key only)."""
    status = await budgets.budget_status(
        session, key.id, session_id, settings.default_session_budget_usd
    )
    alerts = (
        await session.execute(
            select(Alert)
            .where(Alert.api_key_id == key.id, Alert.session_id == session_id)
            .order_by(Alert.created_at)
        )
    ).scalars()
    category = await session.get(SessionCategory, (key.id, session_id))
    return SessionOut(
        api_key_id=key.id,
        key_prefix=key.key_prefix,
        session_id=session_id,
        calls=status.calls,
        spent_usd=status.spent_usd,
        api_equivalent_usd=status.api_equivalent_usd,
        budget_usd=status.budget_usd,
        budget_source=status.budget_source,
        spend_fraction=status.spend_fraction,
        category=CategoryOut.model_validate(category) if category else None,
        alerts=[AlertOut.model_validate(a) for a in alerts],
    )


def create_router(settings: Settings) -> APIRouter:
    def require_admin(request: Request) -> None:
        expected = settings.admin_api_key.get_secret_value()
        if not expected:
            raise HTTPException(503, "admin API disabled: set ADMIN_API_KEY")
        provided = extract_key(request.headers) or ""
        # Constant-time comparison: response timing reveals nothing about the key.
        if not secrets.compare_digest(provided.encode(), expected.encode()):
            raise HTTPException(401, "invalid admin key")

    router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

    async def get_key(session: AsyncSession, key_id: uuid.UUID) -> ApiKey:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(404, "no such gateway key")
        return key

    async def session_out(request: Request, key_id: uuid.UUID, session_id: str) -> SessionOut:
        async with request.app.state.session_factory() as session:
            key = await get_key(session, key_id)
            return await build_session_out(session, settings, key, session_id)

    @router.get("/keys")
    async def list_keys(request: Request) -> list[KeyOut]:
        async with request.app.state.session_factory() as session:
            keys = (await session.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars()
            return [KeyOut.model_validate(k) for k in keys]

    @router.post("/keys", status_code=201)
    async def create(request: Request, body: KeyIn) -> KeyCreatedOut:
        """Issue a key. This is how keys are made on a deployed gateway, whose database
        isn't reachable from your machine; `gw keys --url URL create NAME` calls it."""
        async with request.app.state.session_factory() as session:
            row, plaintext = await create_key(session, body.name)
            return KeyCreatedOut(**KeyOut.model_validate(row).model_dump(), key=plaintext)

    @router.post("/keys/{key_id}/revoke")
    async def revoke(request: Request, key_id: uuid.UUID) -> KeyOut:
        async with request.app.state.session_factory() as session:
            key = await get_key(session, key_id)
            if key.revoked_at is not None:
                raise HTTPException(409, "this key is already revoked")
            key.revoked_at = utcnow()
            await session.commit()
            return KeyOut.model_validate(key)

    @router.get("/keys/{key_id}/sessions/{session_id}")
    async def get_session(request: Request, key_id: uuid.UUID, session_id: str) -> SessionOut:
        return await session_out(request, key_id, session_id)

    @router.put("/keys/{key_id}/sessions/{session_id}/budget")
    async def put_budget(
        request: Request, key_id: uuid.UUID, session_id: str, body: BudgetIn
    ) -> SessionOut:
        async with request.app.state.session_factory() as session:
            await get_key(session, key_id)
            await budgets.set_budget(session, key_id, session_id, body.budget_usd, "admin")
            await session.flush()
            # Lowering a budget below current spend should alert now, not on the next call.
            await budgets.check_spend(
                session,
                key_id,
                session_id,
                default_budget=settings.default_session_budget_usd,
                thresholds=settings.alert_thresholds,
                request_id=None,
            )
            await session.commit()
        return await session_out(request, key_id, session_id)

    @router.post("/keys/{key_id}/sessions/{session_id}/categorize")
    async def categorize(request: Request, key_id: uuid.UUID, session_id: str) -> SessionOut:
        async with request.app.state.session_factory() as session:
            await get_key(session, key_id)
        await categorizer.categorize_session(
            request.app.state.session_factory,
            request.app.state.classifier,
            categorizer.SessionRef(key_id, session_id),
        )
        return await session_out(request, key_id, session_id)

    @router.get("/stats/overview")
    async def overview(
        request: Request,
        window: Annotated[stats.Window, Depends(window_from)],
        bucket: Literal["hour", "day"] | None = None,
    ) -> JSONResponse:
        span = window.end - window.start
        bucket = bucket or ("hour" if span <= timedelta(days=2) else "day")
        if bucket == "hour" and span > MAX_HOURLY_RANGE:
            raise HTTPException(422, "hourly buckets are limited to 14 days")
        async with request.app.state.session_factory() as session:
            return JSONResponse(to_json(await stats.overview(session, window, bucket)))

    @router.get("/stats/categories")
    async def categories(
        request: Request, window: Annotated[stats.Window, Depends(window_from)]
    ) -> JSONResponse:
        async with request.app.state.session_factory() as session:
            return JSONResponse(to_json(await stats.by_category(session, window)))

    @router.get("/stats/users")
    async def users(
        request: Request, window: Annotated[stats.Window, Depends(window_from)]
    ) -> JSONResponse:
        async with request.app.state.session_factory() as session:
            return JSONResponse(to_json(await stats.by_key(session, window)))

    @router.get("/sessions")
    async def sessions(
        request: Request,
        window: Annotated[stats.Window, Depends(window_from)],
        category: str | None = None,
        sort: Literal["recent", "spend"] = "recent",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        async with request.app.state.session_factory() as session:
            result = await stats.sessions_list(
                session,
                window,
                default_budget=settings.default_session_budget_usd,
                category=category,
                sort=sort,
                limit=limit,
                offset=offset,
            )
            return JSONResponse(to_json(result))

    @router.get("/keys/{key_id}/sessions/{session_id}/calls")
    async def calls(request: Request, key_id: uuid.UUID, session_id: str) -> JSONResponse:
        async with request.app.state.session_factory() as session:
            await get_key(session, key_id)
            return JSONResponse(to_json(await stats.session_calls(session, key_id, session_id)))

    @router.post("/keys/{key_id}/sessions/{session_id}/handoffs", status_code=201)
    async def create_handoff(request: Request, key_id: uuid.UUID, session_id: str) -> HandoffOut:
        async with request.app.state.session_factory() as session:
            await get_key(session, key_id)
        return await generate_or_http_error(request, key_id, session_id, "admin")

    @router.get("/keys/{key_id}/sessions/{session_id}/handoffs")
    async def list_handoffs(
        request: Request, key_id: uuid.UUID, session_id: str
    ) -> list[HandoffSummary]:
        async with request.app.state.session_factory() as session:
            rows = (
                await session.execute(
                    select(Handoff)
                    .where(Handoff.api_key_id == key_id, Handoff.session_id == session_id)
                    .order_by(Handoff.created_at.desc())
                )
            ).scalars()
            return [HandoffSummary.model_validate(h) for h in rows]

    async def get_handoff(request: Request, handoff_id: uuid.UUID) -> Handoff:
        async with request.app.state.session_factory() as session:
            handoff = await session.get(Handoff, handoff_id)
        if handoff is None:
            raise HTTPException(404, "no such handoff")
        return handoff

    @router.get("/handoffs/{handoff_id}")
    async def handoff_json(request: Request, handoff_id: uuid.UUID) -> HandoffOut:
        return HandoffOut.model_validate(await get_handoff(request, handoff_id))

    @router.get("/handoffs/{handoff_id}/markdown")
    async def handoff_markdown(request: Request, handoff_id: uuid.UUID) -> PlainTextResponse:
        return markdown_download(await get_handoff(request, handoff_id))

    @router.get("/alerts")
    async def list_alerts(
        request: Request,
        key_id: uuid.UUID | None = None,
        session_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[AlertOut]:
        query = select(Alert).order_by(Alert.created_at.desc()).limit(limit)
        if key_id is not None:
            query = query.where(Alert.api_key_id == key_id)
        if session_id is not None:
            query = query.where(Alert.session_id == session_id)
        async with request.app.state.session_factory() as session:
            return [AlertOut.model_validate(a) for a in (await session.execute(query)).scalars()]

    return router
