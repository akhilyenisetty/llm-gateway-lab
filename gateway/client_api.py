"""Endpoints for clients (gateway keys) about their own sessions.

Authenticated with the client's gateway key (x-api-key or Authorization: Bearer), and
scoped to that key: a key only ever sees and acts on its own sessions. Lives under
/gateway, so it can't collide with the Anthropic API paths the gateway forwards.

    GET  /gateway/me                                       which key you are
    GET  /gateway/sessions?limit=                          your recent sessions (short list)
    GET  /gateway/usage?from=&to=&bucket=                  your totals, percentiles, series
    GET  /gateway/usage/sessions?from=&to=&sort=&limit=&offset=   your sessions, with budgets
    GET  /gateway/sessions/{session_id}                    spend, budget, category, alerts
    GET  /gateway/sessions/{session_id}/calls              every call, oldest first
    PUT  /gateway/sessions/{session_id}/budget             set your session's budget
    POST /gateway/sessions/{session_id}/handoffs           generate a handoff now
    GET  /gateway/sessions/{session_id}/handoffs           your handoffs for the session
    GET  /gateway/sessions/{session_id}/handoffs/latest.md download the latest handoff
    GET  /gateway/sessions/{session_id}/handoffs/{id}/markdown   download one handoff

The key is always the caller's own (from authentication), never a parameter, so no path
can reach another key's data. The "My usage" dashboard view is built on these.

Example, for a Claude Code user about to start fresh:

    curl -s -H "x-api-key: $GW_KEY" localhost:8000/gateway/sessions
    curl -s -X POST -H "x-api-key: $GW_KEY" localhost:8000/gateway/sessions/<id>/handoffs
    curl -s -H "x-api-key: $GW_KEY" -o handoff.md \\
        localhost:8000/gateway/sessions/<id>/handoffs/latest.md
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from gateway import budgets, stats
from gateway.admin import MAX_HOURLY_RANGE, BudgetIn, SessionOut, build_session_out, to_json
from gateway.auth import authenticate_request
from gateway.handoff_api import (
    HandoffOut,
    HandoffSummary,
    generate_or_http_error,
    markdown_download,
)
from gateway.models import ApiKey, Handoff, RequestLog
from gateway.settings import Settings


class ClientSession(BaseModel):
    session_id: str
    app: str | None
    calls: int
    spend_usd: Decimal
    first_call_at: datetime
    last_call_at: datetime
    has_handoff: bool


async def require_key(request: Request) -> ApiKey:
    key = await authenticate_request(request)
    if key is None:
        raise HTTPException(401, "invalid or missing gateway key")
    return key


def create_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/gateway")
    Key = Annotated[ApiKey, Depends(require_key)]

    @router.get("/sessions")
    async def my_sessions(
        request: Request, key: Key, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> list[ClientSession]:
        async with request.app.state.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        RequestLog.session_id,
                        func.max(RequestLog.app),
                        func.count(),
                        func.coalesce(func.sum(RequestLog.cost_usd), 0),
                        func.min(RequestLog.created_at),
                        func.max(RequestLog.created_at),
                    )
                    .where(RequestLog.api_key_id == key.id)
                    .group_by(RequestLog.session_id)
                    .order_by(func.max(RequestLog.created_at).desc())
                    .limit(limit)
                )
            ).all()
            with_handoff = set(
                (
                    await session.execute(
                        select(Handoff.session_id)
                        .where(Handoff.api_key_id == key.id, Handoff.status == "ready")
                        .distinct()
                    )
                ).scalars()
            )
        return [
            ClientSession(
                session_id=sid,
                app=app,
                calls=calls,
                spend_usd=Decimal(spend),
                first_call_at=first,
                last_call_at=last,
                has_handoff=sid in with_handoff,
            )
            for sid, app, calls, spend, first, last in rows
        ]

    async def own_session_or_404(request: Request, key: ApiKey, session_id: str) -> None:
        async with request.app.state.session_factory() as session:
            exists = (
                await session.execute(
                    select(RequestLog.id)
                    .where(RequestLog.api_key_id == key.id, RequestLog.session_id == session_id)
                    .limit(1)
                )
            ).first()
        if exists is None:
            raise HTTPException(404, "no such session for this key")

    def own_window(key: ApiKey, from_: datetime | None, to: datetime | None) -> stats.Window:
        end = to or datetime.now(UTC)
        start = from_ or end - timedelta(days=7)
        end = end.replace(tzinfo=UTC) if end.tzinfo is None else end
        start = start.replace(tzinfo=UTC) if start.tzinfo is None else start
        if start >= end:
            raise HTTPException(422, "`from` must be before `to`")
        return stats.Window(start, end, key.id)  # always the caller's own key

    @router.get("/me")
    async def me(key: Key) -> dict[str, str]:
        return {
            "name": key.name,
            "key_prefix": key.key_prefix,
            "created_at": key.created_at.isoformat(),
        }

    @router.get("/usage")
    async def usage(
        request: Request,
        key: Key,
        from_: Annotated[datetime | None, Query(alias="from")] = None,
        to: datetime | None = None,
        bucket: Literal["hour", "day"] | None = None,
    ) -> JSONResponse:
        window = own_window(key, from_, to)
        span = window.end - window.start
        bucket = bucket or ("hour" if span <= timedelta(days=2) else "day")
        if bucket == "hour" and span > MAX_HOURLY_RANGE:
            raise HTTPException(422, "hourly buckets are limited to 14 days")
        async with request.app.state.session_factory() as session:
            return JSONResponse(to_json(await stats.overview(session, window, bucket)))

    @router.get("/usage/sessions")
    async def usage_sessions(
        request: Request,
        key: Key,
        from_: Annotated[datetime | None, Query(alias="from")] = None,
        to: datetime | None = None,
        sort: Literal["recent", "spend"] = "recent",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        async with request.app.state.session_factory() as session:
            result = await stats.sessions_list(
                session,
                own_window(key, from_, to),
                default_budget=settings.default_session_budget_usd,
                sort=sort,
                limit=limit,
                offset=offset,
            )
            return JSONResponse(to_json(result))

    @router.get("/sessions/{session_id}")
    async def session_status(request: Request, key: Key, session_id: str) -> SessionOut:
        await own_session_or_404(request, key, session_id)
        async with request.app.state.session_factory() as session:
            return await build_session_out(session, settings, key, session_id)

    @router.get("/sessions/{session_id}/calls")
    async def session_calls(request: Request, key: Key, session_id: str) -> JSONResponse:
        await own_session_or_404(request, key, session_id)
        async with request.app.state.session_factory() as session:
            return JSONResponse(to_json(await stats.session_calls(session, key.id, session_id)))

    @router.put("/sessions/{session_id}/budget")
    async def set_budget(request: Request, key: Key, session_id: str, body: BudgetIn) -> SessionOut:
        """Like the X-Session-Budget-Usd header: allowed when ALLOW_CLIENT_BUDGETS is on,
        and never overrides a budget the admin set."""
        await own_session_or_404(request, key, session_id)
        if not settings.allow_client_budgets:
            raise HTTPException(403, "this gateway doesn't let clients set budgets")
        async with request.app.state.session_factory() as session:
            stored = await budgets.set_budget(
                session, key.id, session_id, body.budget_usd, "header"
            )
            if not stored:
                raise HTTPException(409, "the admin set this session's budget")
            await session.flush()
            await budgets.check_spend(
                session,
                key.id,
                session_id,
                default_budget=settings.default_session_budget_usd,
                thresholds=settings.alert_thresholds,
                request_id=None,
            )
            await session.commit()
            return await build_session_out(session, settings, key, session_id)

    @router.get("/sessions/{session_id}/handoffs")
    async def list_handoffs(request: Request, key: Key, session_id: str) -> list[HandoffSummary]:
        async with request.app.state.session_factory() as session:
            rows = (
                await session.execute(
                    select(Handoff)
                    .where(Handoff.api_key_id == key.id, Handoff.session_id == session_id)
                    .order_by(Handoff.created_at.desc())
                )
            ).scalars()
            return [HandoffSummary.model_validate(h) for h in rows]

    @router.get("/sessions/{session_id}/handoffs/{handoff_id}/markdown")
    async def handoff_markdown(
        request: Request, key: Key, session_id: str, handoff_id: uuid.UUID
    ) -> PlainTextResponse:
        async with request.app.state.session_factory() as session:
            handoff = await session.get(Handoff, handoff_id)
        if handoff is None or handoff.api_key_id != key.id or handoff.session_id != session_id:
            raise HTTPException(404, "no such handoff")  # someone else's looks like none
        return markdown_download(handoff)

    @router.post("/sessions/{session_id}/handoffs", status_code=201)
    async def generate(request: Request, key: Key, session_id: str) -> HandoffOut:
        await own_session_or_404(request, key, session_id)
        return await generate_or_http_error(request, key.id, session_id, "client")

    @router.get("/sessions/{session_id}/handoffs/latest.md")
    async def latest(request: Request, key: Key, session_id: str) -> PlainTextResponse:
        async with request.app.state.session_factory() as session:
            handoff = (
                await session.execute(
                    select(Handoff)
                    .where(
                        Handoff.api_key_id == key.id,
                        Handoff.session_id == session_id,
                        Handoff.status == "ready",
                    )
                    .order_by(Handoff.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if handoff is None:
            raise HTTPException(404, "no handoff for this session yet; POST to generate one")
        return markdown_download(handoff)

    return router
