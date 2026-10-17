"""FastAPI app: the gateway's HTTP surface.

    POST /v1/messages   forwarded to Anthropic and recorded (streaming and non-streaming)
    *    /v1/{path}     other Anthropic endpoints, forwarded with the key swap, not recorded
    GET  /health        liveness check, no auth
    /admin/...          budgets and alerts, with ADMIN_API_KEY (see admin.py)

Run with: uvicorn gateway.main:app
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from gateway import admin, recorder
from gateway.auth import extract_key, find_active_key
from gateway.db import make_engine, make_session_factory
from gateway.models import ApiKey
from gateway.pricing import PriceTable
from gateway.proxy import anthropic_error, client_headers, upstream_headers
from gateway.request_meta import read_meta
from gateway.settings import Settings, get_settings
from gateway.streaming import ClosingStreamingResponse, stream_body

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.anthropic_api_key.get_secret_value():
            logger.warning("ANTHROPIC_API_KEY is not set; upstream calls will fail")
        engine = make_engine(settings.database_url)
        app.state.session_factory = make_session_factory(engine)
        app.state.prices = PriceTable.load(settings.price_table_path)
        # One shared client = connection pooling (reuses TCP/TLS connections upstream).
        app.state.upstream = httpx.AsyncClient(
            base_url=settings.anthropic_base_url,
            timeout=httpx.Timeout(settings.upstream_timeout_s, connect=10.0),
        )
        yield
        await app.state.upstream.aclose()
        await engine.dispose()

    app = FastAPI(title="llm-gateway-lab", lifespan=lifespan)
    app.include_router(admin.create_router(settings))

    async def authenticate(request: Request) -> ApiKey | None:
        key = extract_key(request.headers)
        if not key:
            return None
        async with request.app.state.session_factory() as session:
            return await find_active_key(session, key)

    def unauthorized() -> Response:
        return anthropic_error(401, "authentication_error", "invalid or missing gateway key")

    async def forward(
        request: Request, path: str, body: bytes, *, stream: bool = False
    ) -> httpx.Response:
        """Send the request upstream. With stream=True, returns once the status line and
        headers arrive; the body is read later (and the response must be closed)."""
        client: httpx.AsyncClient = request.app.state.upstream
        upstream_request = client.build_request(
            request.method,
            path,
            params=request.query_params,  # e.g. ?beta=true, sent by the SDK's beta client
            content=body,
            headers=upstream_headers(
                request.headers, settings.anthropic_api_key.get_secret_value()
            ),
        )
        return await client.send(upstream_request, stream=stream)

    async def save(request: Request, record: recorder.CallRecord) -> None:
        await recorder.save(
            request.app.state.session_factory,
            record,
            request.app.state.prices,
            store_content=settings.store_content,
            budget_config=recorder.BudgetConfig(
                settings.default_session_budget_usd,
                settings.alert_thresholds,
                settings.allow_client_budgets,
            ),
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        api_key = await authenticate(request)
        if api_key is None:
            return unauthorized()

        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return anthropic_error(400, "invalid_request_error", "request body must be JSON")
        is_stream = bool(body.get("stream"))

        meta = read_meta(request.headers, api_key.id, body)
        record = recorder.CallRecord(
            api_key_id=api_key.id,
            meta=meta,
            path=request.url.path,
            request_body=body,
            status_code=0,
            latency_ms=0,
        )

        start = time.perf_counter()
        try:
            upstream = await forward(request, "/v1/messages", raw, stream=is_stream)
        except httpx.TimeoutException:
            record.status_code, record.error_type = 504, "upstream_timeout"
            response: Response = anthropic_error(504, "api_error", "upstream request timed out")
        except httpx.RequestError as exc:
            record.status_code, record.error_type = 502, "upstream_unreachable"
            response = anthropic_error(502, "api_error", f"could not reach upstream: {exc!r}")
        else:
            record.status_code = upstream.status_code
            record.upstream_request_id = upstream.headers.get("request-id")
            if is_stream and upstream.status_code == 200:
                # Hand the open upstream stream to the response; it records the call
                # itself when the stream ends (see streaming.py).
                return ClosingStreamingResponse(
                    stream_body(
                        upstream,
                        record,
                        start,
                        lambda r: save(request, r),
                        rebuild_message=settings.store_content,
                    ),
                    status_code=200,
                    headers=client_headers(upstream.headers),
                )
            # Non-streaming, or a streaming request that failed before streaming began
            # (Anthropic then sends a normal JSON error): read the whole body.
            await upstream.aread()
            await upstream.aclose()
            try:
                record.response_body = upstream.json()
            except ValueError:
                record.error_type = "invalid_upstream_body"
            response = Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=client_headers(upstream.headers),
            )
        record.latency_ms = round((time.perf_counter() - start) * 1000)

        # Written before responding: simple and testable. It adds one DB insert of
        # latency; moving it off the request path is an option if that ever matters.
        await save(request, record)
        return response

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def passthrough(request: Request, path: str) -> Response:
        if await authenticate(request) is None:
            return unauthorized()
        try:
            upstream = await forward(request, f"/v1/{path}", await request.body())
        except httpx.TimeoutException:
            return anthropic_error(504, "api_error", "upstream request timed out")
        except httpx.RequestError as exc:
            return anthropic_error(502, "api_error", f"could not reach upstream: {exc!r}")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=client_headers(upstream.headers),
        )

    return app


app = create_app()
