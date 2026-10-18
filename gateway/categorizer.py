"""Deciding each session's category, and the background worker that keeps them current.

For one session:
1. If its calls carry X-Category tags, the most frequent tag is the category. No model call.
2. Otherwise, if content was stored (STORE_CONTENT=true), the classifier reads a digest.
3. Otherwise the category stays empty with status "no_content".

The worker categorizes sessions that have been idle for a while (so it reads a finished
conversation, not the first turn), and again if a session grows after being categorized.
Failed classifications are retried after a delay.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anthropic
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.classifier import ClassificationError, Classifier
from gateway.digest import build_digest
from gateway.models import RequestContent, RequestLog, SessionCategory, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionRef:
    api_key_id: uuid.UUID
    session_id: str


async def _client_tag(session: AsyncSession, ref: SessionRef) -> str | None:
    row = (
        await session.execute(
            select(RequestLog.category, func.count().label("n"))
            .where(
                RequestLog.api_key_id == ref.api_key_id,
                RequestLog.session_id == ref.session_id,
                RequestLog.category.is_not(None),
            )
            .group_by(RequestLog.category)
            .order_by(func.count().desc(), RequestLog.category)
            .limit(1)
        )
    ).first()
    return row[0] if row else None


async def _digest(session: AsyncSession, ref: SessionRef) -> str | None:
    """The session's digest, or None if no content was stored for it."""
    rows = (
        await session.execute(
            select(RequestContent.request_json, RequestContent.response_json)
            .join(RequestLog, RequestLog.id == RequestContent.request_id)
            .where(RequestLog.api_key_id == ref.api_key_id, RequestLog.session_id == ref.session_id)
            .order_by(RequestLog.created_at)
        )
    ).all()
    if not rows:
        return None
    return build_digest([r[0] for r in rows], [r[1] for r in rows])


async def categorize_session(
    session_factory: async_sessionmaker, classifier: Classifier | None, ref: SessionRef
) -> SessionCategory:
    """Compute and store one session's category. Returns the stored row.

    Three phases, so no database connection is held during the model call:
    read (call count, client tag, digest), classify, write.
    """
    async with session_factory() as session:
        calls = (
            await session.execute(
                select(func.count()).where(
                    RequestLog.api_key_id == ref.api_key_id,
                    RequestLog.session_id == ref.session_id,
                )
            )
        ).scalar_one()
        client_tag = await _client_tag(session, ref)
        digest = None if client_tag is not None else await _digest(session, ref)

    fields: dict[str, Any] = {"client_category": client_tag, "error": None}
    if client_tag is not None:
        fields.update(status="client_tag", category=client_tag)
    elif digest is None or classifier is None:
        fields.update(status="no_content", category=None)
        if digest is not None:
            fields["error"] = "classifier not configured"
    else:
        try:
            result = await classifier.classify(digest)
        except (ClassificationError, anthropic.APIError) as exc:
            # Keep any earlier category; record the failure so it's visible and retried.
            fields.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
            logger.warning("classification failed for %s: %s", ref, fields["error"])
        else:
            fields.update(
                status="classified",
                category=result.category,
                auto_category=result.category,
                auto_reason=result.reason,
                classifier_model=result.model,
                classifier_effort=result.effort,
                prompt_version=result.prompt_version,
                categories_version=classifier.categories.version,
                classifier_cost_usd=result.cost_usd,
            )

    async with session_factory() as session:
        row = await session.get(SessionCategory, (ref.api_key_id, ref.session_id))
        if row is None:
            row = SessionCategory(api_key_id=ref.api_key_id, session_id=ref.session_id)
            session.add(row)
        for name, value in fields.items():
            setattr(row, name, value)
        row.classified_calls, row.updated_at = calls, utcnow()
        await session.commit()
        return row


async def pending_sessions(
    session_factory: async_sessionmaker,
    *,
    idle: timedelta,
    retry_errors_after: timedelta,
    limit: int,
) -> list[SessionRef]:
    """Sessions idle for at least `idle` that are uncategorized, have grown since, or whose
    last attempt failed more than `retry_errors_after` ago. Oldest activity first."""
    now = utcnow()
    activity = (
        select(
            RequestLog.api_key_id,
            RequestLog.session_id,
            func.count().label("calls"),
            func.max(RequestLog.created_at).label("last_call"),
        )
        .group_by(RequestLog.api_key_id, RequestLog.session_id)
        .subquery()
    )
    sc = SessionCategory
    query = (
        select(activity.c.api_key_id, activity.c.session_id)
        .outerjoin(
            sc,
            and_(
                sc.api_key_id == activity.c.api_key_id,
                sc.session_id == activity.c.session_id,
            ),
        )
        .where(
            activity.c.last_call <= now - idle,
            or_(
                sc.session_id.is_(None),
                sc.classified_calls < activity.c.calls,
                and_(sc.status == "error", sc.updated_at <= now - retry_errors_after),
            ),
        )
        .order_by(activity.c.last_call)
        .limit(limit)
    )
    async with session_factory() as session:
        return [SessionRef(r[0], r[1]) for r in (await session.execute(query)).all()]


async def run_once(
    session_factory: async_sessionmaker,
    classifier: Classifier | None,
    *,
    idle: timedelta,
    retry_errors_after: timedelta = timedelta(minutes=30),
    limit: int = 20,
) -> int:
    """Categorize one batch of pending sessions. Returns how many were processed."""
    refs = await pending_sessions(
        session_factory, idle=idle, retry_errors_after=retry_errors_after, limit=limit
    )
    for ref in refs:
        await categorize_session(session_factory, classifier, ref)
    return len(refs)


async def worker(
    session_factory: async_sessionmaker,
    classifier: Classifier | None,
    *,
    idle: timedelta,
    interval_s: float,
) -> None:
    """Run forever (until cancelled), categorizing pending sessions every `interval_s`."""
    while True:
        try:
            processed = await run_once(session_factory, classifier, idle=idle)
            if processed:
                logger.info("categorized %d sessions", processed)
        except Exception:
            logger.exception("categorizer batch failed")
        await asyncio.sleep(interval_s)
