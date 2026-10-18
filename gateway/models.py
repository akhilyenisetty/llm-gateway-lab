"""Database tables.

Column types are kept portable (generic UUID/JSON/Numeric) so the test suite can run on
SQLite while production runs on Postgres, where JSON becomes JSONB.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
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

    # Context use of this call: prompt (input + cache read + cache write) plus output, which
    # all count toward the model's context window. Window is null for unknown models.
    context_tokens: Mapped[int | None] = mapped_column(Integer)
    context_window: Mapped[int | None] = mapped_column(Integer)

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


class SessionBudget(Base):
    """A per-session spend budget, overriding the default. Only sessions with an override
    have a row; spend itself is always computed from `requests`.

    A session is identified by (gateway key, session ID) everywhere, so the same session ID
    sent with two keys is two sessions, and one key can't affect another key's sessions.
    """

    __tablename__ = "session_budgets"

    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    source: Mapped[str] = mapped_column(String(20))  # "header" | "admin"
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    """A session crossed a threshold of its spend budget or of a model's context window.

    The unique constraint makes each (session, kind, threshold) alert fire once, even when
    concurrent calls cross the threshold at the same moment.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint(
            "api_key_id",
            "session_id",
            "kind",
            "threshold",
            name="uq_alerts_key_session_kind_threshold",
        ),
        Index("ix_alerts_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"))
    session_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # "spend" | "context"
    threshold: Mapped[Decimal] = mapped_column(Numeric(5, 3))
    observed: Mapped[Decimal] = mapped_column(Numeric(10, 4))  # fraction reached, e.g. 0.8312
    # The call that triggered it (null when triggered by a budget change).
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("requests.id", ondelete="SET NULL")
    )
    detail: Mapped[Any] = mapped_column(JsonType)  # e.g. spent and budget, or tokens and window


class SessionCategory(Base):
    """A session's category: the client's tag if it sent one, otherwise the classifier's.

    `category` is the effective value (what spend-by-category uses). The classifier fields
    record exactly what produced `auto_category`, so results stay comparable across prompt
    and model changes. `classified_calls` is the session's call count when this was
    computed; a session that has grown since is categorized again.
    """

    __tablename__ = "session_categories"

    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    category: Mapped[str | None] = mapped_column(String(100))
    # "client_tag" | "classified" | "no_content" (STORE_CONTENT off) | "error"
    status: Mapped[str] = mapped_column(String(20))
    client_category: Mapped[str | None] = mapped_column(String(100))
    auto_category: Mapped[str | None] = mapped_column(String(100))
    auto_reason: Mapped[str | None] = mapped_column(String(300))
    classifier_model: Mapped[str | None] = mapped_column(String(100))
    classifier_effort: Mapped[str | None] = mapped_column(String(20))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    categories_version: Mapped[int | None] = mapped_column(Integer)
    classifier_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    error: Mapped[str | None] = mapped_column(String(500))
    classified_calls: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Handoff(Base):
    """A generated handoff document for a session. Every generation is kept (a session
    can have several, as it grows); failed attempts are kept too, with the error."""

    __tablename__ = "handoffs"
    __table_args__ = (Index("ix_handoffs_session", "api_key_id", "session_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"))
    session_id: Mapped[str] = mapped_column(String(200))
    # Who asked: "admin" (dashboard/admin API) or "client" (the session's own gateway key).
    requested_by: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))  # "ready" | "error"
    error: Mapped[str | None] = mapped_column(String(500))
    # What it was made from: the session's main request and how much of it fit.
    source_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("requests.id", ondelete="SET NULL")
    )
    session_calls: Mapped[int] = mapped_column(Integer)
    transcript_messages: Mapped[int] = mapped_column(Integer)
    omitted_blocks: Mapped[int] = mapped_column(Integer)
    # What produced it, and what it cost.
    model: Mapped[str] = mapped_column(String(100))
    effort: Mapped[str] = mapped_column(String(20))
    prompt_version: Mapped[str] = mapped_column(String(20))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    content: Mapped[Any | None] = mapped_column(JsonType)  # the structured sections
    markdown: Mapped[str | None] = mapped_column(Text)
