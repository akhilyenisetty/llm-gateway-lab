"""Endpoints for clients (gateway keys) about their own sessions.

Authenticated with the client's gateway key (x-api-key or Authorization: Bearer), and
scoped to that key: a key only ever sees and acts on its own sessions. Lives under
/gateway, so it can't collide with the Anthropic API paths the gateway forwards.

    GET  /gateway/sessions?limit=                          your recent sessions
    POST /gateway/sessions/{session_id}/handoffs           generate a handoff now
    GET  /gateway/sessions/{session_id}/handoffs/latest.md download the latest handoff

Example, for a Claude Code user about to start fresh:

    curl -s -H "x-api-key: $GW_KEY" localhost:8000/gateway/sessions
    curl -s -X POST -H "x-api-key: $GW_KEY" localhost:8000/gateway/sessions/<id>/handoffs
    curl -s -H "x-api-key: $GW_KEY" -o handoff.md \\
        localhost:8000/gateway/sessions/<id>/handoffs/latest.md
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from gateway.auth import authenticate_request
from gateway.handoff_api import HandoffOut, generate_or_http_error, markdown_download
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
