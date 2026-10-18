"""Admin API: gateway keys, session budget status, budget overrides, and alerts.

Protected by ADMIN_API_KEY (sent as `Authorization: Bearer <key>`), which is separate from
client gateway keys: a client must not be able to raise its own budget. The Stage 5
dashboard reads the same endpoints.

A session is identified by (gateway key, session ID), so session URLs name the key:

    GET /admin/keys                                         keys (id, prefix, name)
    GET /admin/keys/{key_id}/sessions/{session_id}          spend, budget, and alerts
    PUT /admin/keys/{key_id}/sessions/{session_id}/budget   set a budget (overrides header)
    POST /admin/keys/{key_id}/sessions/{session_id}/categorize  categorize it now
    GET /admin/alerts?key_id=&session_id=&limit=            recent alerts, newest first
"""

import secrets
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway import budgets, categorizer
from gateway.auth import extract_key
from gateway.models import Alert, ApiKey, SessionCategory
from gateway.settings import Settings


class KeyOut(BaseModel):
    id: uuid.UUID
    key_prefix: str
    name: str
    created_at: datetime
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


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
    budget_usd: Decimal
    budget_source: str
    spend_fraction: Decimal
    category: CategoryOut | None
    alerts: list[AlertOut]


class BudgetIn(BaseModel):
    budget_usd: Decimal = Field(gt=0, le=budgets.MAX_BUDGET_USD, decimal_places=4)


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
            status = await budgets.budget_status(
                session, key_id, session_id, settings.default_session_budget_usd
            )
            alerts = (
                await session.execute(
                    select(Alert)
                    .where(Alert.api_key_id == key_id, Alert.session_id == session_id)
                    .order_by(Alert.created_at)
                )
            ).scalars()
            category = await session.get(SessionCategory, (key_id, session_id))
            return SessionOut(
                api_key_id=key_id,
                key_prefix=key.key_prefix,
                session_id=session_id,
                calls=status.calls,
                spent_usd=status.spent_usd,
                budget_usd=status.budget_usd,
                budget_source=status.budget_source,
                spend_fraction=status.spend_fraction,
                category=CategoryOut.model_validate(category) if category else None,
                alerts=[AlertOut.model_validate(a) for a in alerts],
            )

    @router.get("/keys")
    async def list_keys(request: Request) -> list[KeyOut]:
        async with request.app.state.session_factory() as session:
            keys = (await session.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars()
            return [KeyOut.model_validate(k) for k in keys]

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
