"""Read-only aggregate queries behind the dashboard.

Definitions used throughout:
- An **error** is a call that failed: HTTP status >= 400, or a failure recorded during a
  stream (error_type set). A client disconnecting is not an error: nothing failed on the
  gateway's or Anthropic's side.
- **Latency** is time to the last byte; **TTFT** (time to first token) exists for
  streamed calls only.
- Percentiles use linear interpolation between ranks, as Postgres's `percentile_cont`
  does. Postgres computes them in SQL; SQLite (used by the default test run) has no such
  function, so they're computed in Python with the same formula there.
- Time buckets are UTC.
- Spend includes calls made on the client's own credential (billing "client_credential",
  e.g. Claude Code on a claude.ai plan); their cost is API-equivalent, not money the
  gateway paid, and is also reported separately as `api_equivalent_usd`.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Select, and_, case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import Alert, ApiKey, RequestLog, SessionBudget, SessionCategory

Bucket = Literal["hour", "day"]
UNCATEGORIZED = "uncategorized"

IS_ERROR = or_(
    RequestLog.status_code >= 400,
    and_(RequestLog.error_type.is_not(None), RequestLog.error_type != "client_disconnected"),
)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    key_id: uuid.UUID | None = None

    def where(self) -> list[Any]:
        conditions = [RequestLog.created_at >= self.start, RequestLog.created_at < self.end]
        if self.key_id is not None:
            conditions.append(RequestLog.api_key_id == self.key_id)
        return conditions


def percentile(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks, matching Postgres percentile_cont."""
    if not values:
        return None
    ordered = sorted(values)
    position = p * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "postgresql"


async def _percentiles(
    session: AsyncSession, column: Any, conditions: list[Any], group_by: Any = None
) -> dict[Any, tuple[float | None, float | None]]:
    """{group: (p50, p95)} over non-null values of `column` (group None if ungrouped)."""
    conditions = [*conditions, column.is_not(None)]
    if _is_postgres(session):
        query = select(
            group_by if group_by is not None else literal_column("NULL"),
            func.percentile_cont(0.5).within_group(column),
            func.percentile_cont(0.95).within_group(column),
        ).where(*conditions)
        if group_by is not None:
            query = query.group_by(group_by)
        return {
            g: (float(p50) if p50 is not None else None, float(p95) if p95 is not None else None)
            for g, p50, p95 in (await session.execute(query)).all()
        }
    values: dict[Any, list[float]] = defaultdict(list)
    query = select(group_by if group_by is not None else literal_column("NULL"), column)
    for g, v in (await session.execute(query.where(*conditions))).all():
        values[g].append(float(v))
    return {g: (percentile(v, 0.5), percentile(v, 0.95)) for g, v in values.items()}


def _bucket_start(at: datetime, bucket: Bucket) -> datetime:
    at = at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)
    if bucket == "day":
        return at.replace(hour=0, minute=0, second=0, microsecond=0)
    return at.replace(minute=0, second=0, microsecond=0)


def _bucket_column(session: AsyncSession, bucket: Bucket) -> Any:
    if _is_postgres(session):
        return func.date_trunc(bucket, func.timezone("UTC", RequestLog.created_at))
    fmt = "%Y-%m-%d 00:00:00" if bucket == "day" else "%Y-%m-%d %H:00:00"
    return func.strftime(fmt, RequestLog.created_at)


def _as_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


async def _count_sessions(session: AsyncSession, where: list[Any]) -> int:
    """Distinct (key, session) pairs. A subquery keeps this portable: concatenating a UUID
    with text behaves differently across databases."""
    pairs = select(RequestLog.api_key_id, RequestLog.session_id).where(*where).distinct()
    return (await session.execute(select(func.count()).select_from(pairs.subquery()))).scalar_one()


def _api_equivalent_column() -> Any:
    return func.coalesce(
        func.sum(case((RequestLog.billing == "client_credential", RequestLog.cost_usd))), 0
    )


def _totals_columns() -> list[Any]:
    return [
        func.count().label("calls"),
        func.coalesce(func.sum(RequestLog.cost_usd), 0).label("spend_usd"),
        func.coalesce(func.sum(case((IS_ERROR, 1), else_=0)), 0).label("errors"),
    ]


async def overview(session: AsyncSession, window: Window, bucket: Bucket) -> dict[str, Any]:
    where = window.where()
    totals = (
        await session.execute(
            select(
                *_totals_columns(),
                func.coalesce(
                    func.sum(
                        RequestLog.input_tokens
                        + RequestLog.cache_read_tokens
                        + RequestLog.cache_write_5m_tokens
                        + RequestLog.cache_write_1h_tokens
                    ),
                    0,
                ),
                func.coalesce(func.sum(RequestLog.output_tokens), 0),
                func.coalesce(func.sum(RequestLog.cache_read_tokens), 0),
                _api_equivalent_column(),
            ).where(*where)
        )
    ).one()
    calls, spend, errors, prompt_tokens, output_tokens, cache_read, api_equivalent = totals
    sessions = await _count_sessions(session, where)
    latency = (await _percentiles(session, RequestLog.latency_ms, where)).get(None, (None, None))
    ttft = (await _percentiles(session, RequestLog.ttft_ms, where)).get(None, (None, None))

    bucket_col = _bucket_column(session, bucket).label("bucket")
    rows = (
        await session.execute(
            select(bucket_col, *_totals_columns()).where(*where).group_by(bucket_col)
        )
    ).all()
    by_bucket = {_as_utc(b): (c, s, e) for b, c, s, e in rows}
    series = []
    step = timedelta(days=1) if bucket == "day" else timedelta(hours=1)
    at = _bucket_start(window.start, bucket)
    while at < window.end:  # every bucket, including empty ones, so charts are continuous
        c, s, e = by_bucket.get(at, (0, 0, 0))
        series.append({"start": at, "calls": c, "spend_usd": Decimal(s), "errors": e})
        at += step

    model_rows = (
        await session.execute(
            select(RequestLog.model, *_totals_columns())
            .where(*where)
            .group_by(RequestLog.model)
            .order_by(func.coalesce(func.sum(RequestLog.cost_usd), 0).desc())
        )
    ).all()
    model_latency = await _percentiles(session, RequestLog.latency_ms, where, RequestLog.model)
    by_model = [
        {
            "model": m,
            "calls": c,
            "spend_usd": Decimal(s),
            "errors": e,
            "latency_p50_ms": model_latency.get(m, (None, None))[0],
            "latency_p95_ms": model_latency.get(m, (None, None))[1],
        }
        for m, c, s, e in model_rows
    ]
    return {
        "calls": calls,
        "sessions": sessions,
        "spend_usd": Decimal(spend),
        "api_equivalent_usd": Decimal(api_equivalent),
        "errors": errors,
        "error_rate": errors / calls if calls else None,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "latency_p50_ms": latency[0],
        "latency_p95_ms": latency[1],
        "ttft_p50_ms": ttft[0],
        "ttft_p95_ms": ttft[1],
        "bucket": bucket,
        "series": series,
        "by_model": by_model,
    }


def _category_join(query: Select) -> Select:
    return query.outerjoin(
        SessionCategory,
        and_(
            SessionCategory.api_key_id == RequestLog.api_key_id,
            SessionCategory.session_id == RequestLog.session_id,
        ),
    )


async def by_category(session: AsyncSession, window: Window) -> list[dict[str, Any]]:
    category = func.coalesce(SessionCategory.category, UNCATEGORIZED).label("category")
    rows = (
        await session.execute(
            _category_join(select(category, *_totals_columns()))
            .where(*window.where())
            .group_by(category)
            .order_by(func.coalesce(func.sum(RequestLog.cost_usd), 0).desc())
        )
    ).all()
    pairs = (
        _category_join(select(category, RequestLog.api_key_id, RequestLog.session_id))
        .where(*window.where())
        .distinct()
        .subquery()
    )
    sessions = dict(
        (
            await session.execute(select(pairs.c.category, func.count()).group_by(pairs.c.category))
        ).all()
    )
    return [
        {
            "category": c,
            "calls": n,
            "spend_usd": Decimal(s),
            "errors": e,
            "sessions": sessions.get(c, 0),
        }
        for c, n, s, e in rows
    ]


async def by_key(session: AsyncSession, window: Window) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                ApiKey.id,
                ApiKey.name,
                ApiKey.key_prefix,
                ApiKey.revoked_at,
                *_totals_columns(),
                func.count(func.distinct(RequestLog.session_id)),
                func.max(RequestLog.created_at),
            )
            .join(RequestLog, RequestLog.api_key_id == ApiKey.id)
            .where(*window.where())
            .group_by(ApiKey.id, ApiKey.name, ApiKey.key_prefix, ApiKey.revoked_at)
            .order_by(func.coalesce(func.sum(RequestLog.cost_usd), 0).desc())
        )
    ).all()
    return [
        {
            "key_id": kid,
            "name": name,
            "key_prefix": prefix,
            "revoked": revoked is not None,
            "calls": c,
            "spend_usd": Decimal(s),
            "errors": e,
            "sessions": ss,
            "last_call_at": last,
        }
        for kid, name, prefix, revoked, c, s, e, ss, last in rows
    ]


async def sessions_list(
    session: AsyncSession,
    window: Window,
    *,
    default_budget: Decimal,
    category: str | None = None,
    sort: Literal["recent", "spend"] = "recent",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Sessions with activity in the window. Spend and budget use the session's whole
    history (that's what the budget applies to); `calls_in_window` is the window only."""
    active = (
        select(
            RequestLog.api_key_id.label("key_id"),
            RequestLog.session_id.label("session_id"),
            func.count().label("calls_in_window"),
            func.max(RequestLog.created_at).label("last_call_at"),
        )
        .where(*window.where())
        .group_by(RequestLog.api_key_id, RequestLog.session_id)
        .subquery()
    )
    whole = (
        select(
            RequestLog.api_key_id.label("key_id"),
            RequestLog.session_id.label("session_id"),
            func.count().label("calls"),
            func.coalesce(func.sum(RequestLog.cost_usd), 0).label("spend_usd"),
            _api_equivalent_column().label("api_equivalent_usd"),
            func.min(RequestLog.created_at).label("first_call_at"),
            func.max(RequestLog.app).label("app"),
            func.max(
                case(
                    (
                        RequestLog.context_window > 0,
                        RequestLog.context_tokens * 1.0 / RequestLog.context_window,
                    ),
                    else_=None,
                )
            ).label("peak_context"),
        )
        .group_by(RequestLog.api_key_id, RequestLog.session_id)
        .subquery()
    )
    effective_category = func.coalesce(SessionCategory.category, UNCATEGORIZED)
    query = (
        select(
            active.c.key_id,
            active.c.session_id,
            active.c.calls_in_window,
            active.c.last_call_at,
            whole.c.calls,
            whole.c.spend_usd,
            whole.c.api_equivalent_usd,
            whole.c.first_call_at,
            whole.c.app,
            whole.c.peak_context,
            effective_category.label("category"),
            SessionCategory.status,
            ApiKey.name,
            ApiKey.key_prefix,
            SessionBudget.budget_usd,
            SessionBudget.source,
        )
        .join(
            whole,
            and_(whole.c.key_id == active.c.key_id, whole.c.session_id == active.c.session_id),
        )
        .join(ApiKey, ApiKey.id == active.c.key_id)
        .outerjoin(
            SessionCategory,
            and_(
                SessionCategory.api_key_id == active.c.key_id,
                SessionCategory.session_id == active.c.session_id,
            ),
        )
        .outerjoin(
            SessionBudget,
            and_(
                SessionBudget.api_key_id == active.c.key_id,
                SessionBudget.session_id == active.c.session_id,
            ),
        )
    )
    if category is not None:
        query = query.where(effective_category == category)
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    order = whole.c.spend_usd.desc() if sort == "spend" else active.c.last_call_at.desc()
    rows = (await session.execute(query.order_by(order).limit(limit).offset(offset))).all()

    alert_counts = {
        (k, sid): n
        for k, sid, n in (
            await session.execute(
                select(Alert.api_key_id, Alert.session_id, func.count()).group_by(
                    Alert.api_key_id, Alert.session_id
                )
            )
        ).all()
    }
    items = []
    for r in rows:
        budget = Decimal(r.budget_usd) if r.budget_usd is not None else default_budget
        spend = Decimal(r.spend_usd)
        items.append(
            {
                "key_id": r.key_id,
                "key_name": r.name,
                "key_prefix": r.key_prefix,
                "session_id": r.session_id,
                "app": r.app,
                "category": r.category,
                "category_status": r.status,
                "calls": r.calls,
                "calls_in_window": r.calls_in_window,
                "first_call_at": r.first_call_at,
                "last_call_at": r.last_call_at,
                "spend_usd": spend,
                "api_equivalent_usd": Decimal(r.api_equivalent_usd),
                "budget_usd": budget,
                "budget_source": r.source or "default",
                "spend_fraction": (spend / budget).quantize(Decimal("0.0001")),
                "peak_context_fraction": float(r.peak_context) if r.peak_context else None,
                "alerts": alert_counts.get((r.key_id, r.session_id), 0),
            }
        )
    return {"total": total, "items": items}


async def session_calls(
    session: AsyncSession, key_id: uuid.UUID, session_id: str
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(RequestLog)
            .where(RequestLog.api_key_id == key_id, RequestLog.session_id == session_id)
            .order_by(RequestLog.created_at)
        )
    ).scalars()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "model": r.model,
            "is_stream": r.is_stream,
            "status_code": r.status_code,
            "error_type": r.error_type,
            "stop_reason": r.stop_reason,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cache_write_tokens": (r.cache_write_5m_tokens or 0) + (r.cache_write_1h_tokens or 0)
            if r.input_tokens is not None
            else None,
            "context_tokens": r.context_tokens,
            "context_window": r.context_window,
            "cost_usd": r.cost_usd,
            "cost_note": r.cost_note,
            "latency_ms": r.latency_ms,
            "ttft_ms": r.ttft_ms,
            "category_tag": r.category,
            "app": r.app,
            "billing": r.billing,
            "request_class": r.request_class,
        }
        for r in rows
    ]
