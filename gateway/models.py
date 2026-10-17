"""Database tables.

Column types are kept portable (generic UUID/JSON/Numeric) so the test suite can run on
SQLite while production runs on Postgres, where JSON becomes JSONB.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    """A gateway-issued client key. Only the SHA-256 hash is stored."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # First characters of the key, safe to display (e.g. "gw_3f9a1c").
    key_prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestLog(Base):
    """One row per proxied /v1/messages call: who, what model, tokens, cost, latency, outcome."""

    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_created_at", "created_at"),
        Index("ix_requests_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"))

    # Request metadata from headers (or derived; see request_meta.py).
    app: Mapped[str | None] = mapped_column(String(100))
    session_id: Mapped[str] = mapped_column(String(200))
    session_id_source: Mapped[str] = mapped_column(String(20))  # "header" | "derived"
    category: Mapped[str | None] = mapped_column(String(100))

    path: Mapped[str] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(100))
    is_stream: Mapped[bool] = mapped_column(default=False)

    # Outcome.
    status_code: Mapped[int] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(100))
    stop_reason: Mapped[str | None] = mapped_column(String(50))
    upstream_request_id: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[int] = mapped_column(Integer)  # until the last byte
    # Streaming only: time until the first content delta, i.e. when output starts appearing.
    ttft_ms: Mapped[int | None] = mapped_column(Integer)

    # Usage, copied from the response's `usage` object. Null when the call failed.
    input_tokens: Mapped[int | None] = mapped_column(Integer)  # uncached input only
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_5m_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_1h_tokens: Mapped[int | None] = mapped_column(Integer)
    inference_geo: Mapped[str | None] = mapped_column(String(20))
    speed: Mapped[str | None] = mapped_column(String(20))
    service_tier: Mapped[str | None] = mapped_column(String(20))

    # Cost in USD, computed when the row is written. Null if the model has no price entry
    # or the call used a pricing mode we don't model (see pricing.py).
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    cost_note: Mapped[str | None] = mapped_column(String(200))


class RequestContent(Base):
    """Full request/response bodies. Written only when STORE_CONTENT is on."""

    __tablename__ = "request_contents"

    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), primary_key=True
    )
    request_json: Mapped[Any] = mapped_column(JsonType)
    response_json: Mapped[Any | None] = mapped_column(JsonType)
