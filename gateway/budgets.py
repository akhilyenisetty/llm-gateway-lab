"""Per-session budgets and threshold alerts.

A session is (gateway key, session ID): the same ID sent with two keys is two sessions.

Two budgets per session:
- Spend: total cost of the session's calls vs its budget (the default, or a per-session
  override set by the X-Session-Budget-Usd header or the admin API).
- Context: each call's context use (prompt + output tokens) vs its model's context window.
  Checked per call rather than "the session's latest call", because one session can mix
  conversations of very different sizes (Claude Code interleaves its main thread with
  small title/summary calls and subagents). A call near the window is exactly the
  conversation that needs a handoff.

An alert is recorded the first time a session reaches each threshold (default 80% and
100%). Alerts only record and report; they never block calls.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import Alert, RequestLog, SessionBudget, utcnow

logger = logging.getLogger(__name__)

MAX_BUDGET_USD = Decimal("100000")


def parse_budget(value: str | None) -> Decimal | None:
    """A positive USD amount from a header value, or None if absent or invalid."""
    if not value:
        return None
    try:
        budget = Decimal(value.strip())
    except ArithmeticError:
        return None
    if not budget.is_finite() or budget > MAX_BUDGET_USD:
        return None
    # Round before the positivity check, so a tiny value can't round down to a zero budget.
    budget = budget.quantize(Decimal("0.0001"))
    return budget if budget > 0 else None


def usd(value: Decimal) -> str:
    """Plain decimal string without padding zeros: 0.02000000 -> "0.02", 10 -> "10"."""
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class BudgetStatus:
    api_key_id: uuid.UUID
    session_id: str
    spent_usd: Decimal
    budget_usd: Decimal
    budget_source: str  # "default" | "header" | "admin"
    calls: int

    @property
    def spend_fraction(self) -> Decimal:
        return (self.spent_usd / self.budget_usd).quantize(Decimal("0.0001"))


def _insert(session: AsyncSession):  # type: ignore[no-untyped-def]
    """The dialect's INSERT, which supports ON CONFLICT on both Postgres and SQLite."""
    return pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert


async def set_budget(
    session: AsyncSession,
    api_key_id: uuid.UUID,
    session_id: str,
    budget_usd: Decimal,
    source: str,
) -> bool:
    """Store a per-session budget. A header never overrides a budget the admin set.

    One atomic upsert, not "SELECT, then INSERT or UPDATE": two concurrent first calls of a
    session would both see no row and both INSERT, and one would fail on the primary key.
    Returns whether the budget was stored.
    """
    insert = _insert(session)(SessionBudget).values(
        api_key_id=api_key_id,
        session_id=session_id,
        budget_usd=budget_usd,
        source=source,
        updated_at=utcnow(),
    )
    statement = insert.on_conflict_do_update(
        index_elements=["api_key_id", "session_id"],
        set_={
            "budget_usd": insert.excluded.budget_usd,
            "source": insert.excluded.source,
            "updated_at": insert.excluded.updated_at,
        },
        # Admin budgets are only replaced by the admin.
        where=(SessionBudget.source != "admin") | (insert.excluded.source == "admin"),
    )
    result = await session.execute(statement)
    return result.rowcount == 1


async def budget_status(
    session: AsyncSession, api_key_id: uuid.UUID, session_id: str, default_budget: Decimal
) -> BudgetStatus:
    spent, calls = (
        await session.execute(
            select(func.coalesce(func.sum(RequestLog.cost_usd), 0), func.count()).where(
                RequestLog.api_key_id == api_key_id, RequestLog.session_id == session_id
            )
        )
    ).one()
    override = await session.get(SessionBudget, (api_key_id, session_id))
    if override is not None:
        budget, source = override.budget_usd, override.source
    else:
        budget, source = default_budget, "default"
    return BudgetStatus(api_key_id, session_id, Decimal(spent), Decimal(budget), source, calls)


async def _record_alert(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    session_id: str,
    kind: str,
    threshold: Decimal,
    observed: Decimal,
    request_id: uuid.UUID | None,
    detail: dict[str, Any],
) -> bool:
    """Insert the alert unless this (session, kind, threshold) already fired.

    INSERT ... ON CONFLICT DO NOTHING lets the database enforce "once", so two
    concurrent calls crossing the same threshold can't both create an alert.
    """
    statement = (
        _insert(session)(Alert)
        .values(
            id=uuid.uuid4(),
            created_at=utcnow(),
            api_key_id=api_key_id,
            session_id=session_id,
            kind=kind,
            threshold=threshold,
            observed=observed,
            request_id=request_id,
            detail=detail,
        )
        .on_conflict_do_nothing(index_elements=["api_key_id", "session_id", "kind", "threshold"])
    )
    result = await session.execute(statement)
    created = result.rowcount == 1
    if created:
        logger.warning(
            "ALERT session=%s %s reached %s%% (threshold %s%%) %s",
            session_id,
            kind,
            (observed * 100).quantize(Decimal("0.1")),
            threshold * 100,
            detail,
        )
    return created


async def check_spend(
    session: AsyncSession,
    api_key_id: uuid.UUID,
    session_id: str,
    *,
    default_budget: Decimal,
    thresholds: list[Decimal],
    request_id: uuid.UUID | None,
) -> list[Decimal]:
    """Record spend alerts for every threshold the session has reached. Returns the new ones."""
    status = await budget_status(session, api_key_id, session_id, default_budget)
    fired = []
    for threshold in thresholds:
        if status.spent_usd >= threshold * status.budget_usd and await _record_alert(
            session,
            api_key_id=api_key_id,
            session_id=session_id,
            kind="spend",
            threshold=threshold,
            observed=status.spend_fraction,
            request_id=request_id,
            detail={
                "spent_usd": usd(status.spent_usd),
                "budget_usd": usd(status.budget_usd),
                "budget_source": status.budget_source,
            },
        ):
            fired.append(threshold)
    return fired


async def check_context(
    session: AsyncSession, row: RequestLog, *, thresholds: list[Decimal]
) -> list[Decimal]:
    """Record context alerts for the thresholds this call's context use reached."""
    if not row.context_tokens or not row.context_window:
        return []
    fraction = (Decimal(row.context_tokens) / Decimal(row.context_window)).quantize(
        Decimal("0.0001")
    )
    fired = []
    for threshold in thresholds:
        if fraction >= threshold and await _record_alert(
            session,
            api_key_id=row.api_key_id,
            session_id=row.session_id,
            kind="context",
            threshold=threshold,
            observed=fraction,
            request_id=row.id,
            detail={
                "context_tokens": row.context_tokens,
                "context_window": row.context_window,
                "model": row.model,
            },
        ):
            fired.append(threshold)
    return fired


async def check_after_call(
    session: AsyncSession,
    row: RequestLog,
    *,
    default_budget: Decimal,
    thresholds: list[Decimal],
) -> None:
    await check_spend(
        session,
        row.api_key_id,
        row.session_id,
        default_budget=default_budget,
        thresholds=thresholds,
        request_id=row.id,
    )
    await check_context(session, row, thresholds=thresholds)
