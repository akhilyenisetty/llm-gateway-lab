"""Turns one proxied call into a `requests` row (plus `request_contents` if enabled)."""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from gateway.models import RequestContent, RequestLog
from gateway.pricing import PriceTable, Usage
from gateway.request_meta import RequestMeta

logger = logging.getLogger(__name__)


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
    error_type: str | None = None  # set for gateway-side failures (timeout, unreachable)


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
        row.cost_usd = cost.cost_usd
        row.cost_note = cost.note
        if cost.cost_usd is None:
            logger.warning("cost not computed for request %s: %s", row.id, cost.note)
    return row


async def save(
    session_factory: async_sessionmaker,
    record: CallRecord,
    prices: PriceTable,
    *,
    store_content: bool,
) -> uuid.UUID | None:
    """Write the row. Never raises: a logging failure must not fail the client's call."""
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
        return row.id
    except Exception:
        logger.exception("failed to record call to %s", record.path)
        return None
