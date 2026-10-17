"""Turns one proxied call into a `requests` row (plus `request_contents` if enabled), then
checks the session's budgets."""

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from gateway import budgets
from gateway.models import RequestContent, RequestLog
from gateway.pricing import PriceTable, Usage
from gateway.request_meta import RequestMeta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetConfig:
    default_budget_usd: Decimal = Decimal("5.00")
    alert_thresholds: list[Decimal] = field(default_factory=lambda: [Decimal("0.8"), Decimal("1")])
    allow_client_budgets: bool = True


@dataclass
class CallRecord:
    api_key_id: uuid.UUID
    meta: RequestMeta
    path: str
    request_body: dict[str, Any]
    status_code: int
    latency_ms: int
    response_body: dict[str, Any] | None = None
    upstream_request_id: str | None = None
    # Set for failures the response body doesn't describe: timeouts, unreachable
    # upstream, and stream outcomes (mid-stream error, disconnects).
    error_type: str | None = None
    ttft_ms: int | None = None
    # Why the recorded usage is incomplete, e.g. the client left before the stream ended.
    usage_note: str | None = None


def build_row(record: CallRecord, prices: PriceTable) -> RequestLog:
    body = record.request_body
    resp = record.response_body or {}
    usage_json = resp.get("usage") if resp.get("type") == "message" else None

    error_type = record.error_type
    if error_type is None and resp.get("type") == "error":
        error_type = (resp.get("error") or {}).get("type")

    # Prefer the model the response reports (it resolves aliases to snapshots).
    model = resp.get("model") or body.get("model")
    # Billing modifiers come from the request; usage.speed reports the speed actually used.
    inference_geo = body.get("inference_geo")
    speed = (usage_json or {}).get("speed") or body.get("speed")

    row = RequestLog(
        id=uuid.uuid4(),
        api_key_id=record.api_key_id,
        app=record.meta.app,
        session_id=record.meta.session_id,
        session_id_source=record.meta.session_id_source,
        category=record.meta.category,
        path=record.path,
        model=model,
        is_stream=bool(body.get("stream")),
        status_code=record.status_code,
        error_type=error_type,
        stop_reason=resp.get("stop_reason"),
        upstream_request_id=record.upstream_request_id,
        latency_ms=record.latency_ms,
        ttft_ms=record.ttft_ms,
        inference_geo=inference_geo,
        speed=speed,
    )
    if usage_json:
        usage = Usage.from_api(usage_json)
        cost = prices.cost(model, usage, inference_geo=inference_geo, speed=speed)
        row.input_tokens = usage.input_tokens
        row.output_tokens = usage.output_tokens
        row.cache_read_tokens = usage.cache_read_tokens
        row.cache_write_5m_tokens = usage.cache_write_5m_tokens
        row.cache_write_1h_tokens = usage.cache_write_1h_tokens
        row.service_tier = usage_json.get("service_tier")
        row.context_tokens = usage.prompt_tokens + usage.output_tokens
        row.context_window = prices.context_window(model)
        row.cost_usd = cost.cost_usd
        row.cost_note = "; ".join(n for n in (record.usage_note, cost.note) if n) or None
        if cost.cost_usd is None:
            logger.warning("cost not computed for request %s: %s", row.id, cost.note)
    return row


async def save(
    session_factory: async_sessionmaker,
    record: CallRecord,
    prices: PriceTable,
    *,
    store_content: bool,
    budget_config: BudgetConfig | None = None,
) -> uuid.UUID | None:
    """Write the row, then check budgets. Never raises: a logging failure must not fail
    the client's call."""
    budget_config = budget_config or BudgetConfig()
    try:
        row = build_row(record, prices)
        async with session_factory() as session:
            session.add(row)
            if store_content:
                # Insert the parent row first so the content row's foreign key resolves.
                # (Without a relationship() the ORM doesn't order these two inserts.)
                await session.flush()
                session.add(
                    RequestContent(
                        request_id=row.id,
                        request_json=record.request_body,
                        response_json=record.response_body,
                    )
                )
            await session.commit()
    except Exception:
        logger.exception("failed to record call to %s", record.path)
        return None

    # Budgets run in a second transaction, after the request row is committed, so nothing
    # here (a failed budget write or alert check) can lose the record of the call.
    try:
        async with session_factory() as session:
            if record.meta.budget_usd is not None and budget_config.allow_client_budgets:
                await budgets.set_budget(
                    session,
                    record.api_key_id,
                    record.meta.session_id,
                    record.meta.budget_usd,
                    "header",
                )
            await budgets.check_after_call(
                session,
                row,
                default_budget=budget_config.default_budget_usd,
                thresholds=budget_config.alert_thresholds,
            )
            await session.commit()
    except Exception:
        logger.exception("failed to check budgets for session %s", row.session_id)
    return row.id
