"""Forwarding requests to the Anthropic API.

The gateway is a "reverse proxy": clients talk to it as if it were Anthropic, and it
makes the same call upstream. Request bodies are forwarded as the original bytes, so
API fields the gateway doesn't know about still reach Anthropic unchanged.
"""

from typing import Any

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

# Hop-by-hop headers describe a single network connection, not the request itself,
# so a proxy must not forward them (RFC 9110 section 7.6.1).
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Request headers we never forward upstream: client credentials (replaced with the real
# key), gateway-only metadata, and headers the HTTP client recomputes itself.
# X-App is read but still forwarded: Claude Code sends `x-app: cli` to Anthropic itself.
DROP_REQUEST_HEADERS = HOP_BY_HOP | {
    "host",
    "content-length",
    "accept-encoding",
    "x-api-key",
    "authorization",
    "cookie",
    "x-session-id",
    "x-category",
    "x-session-budget-usd",
}

# Response headers recomputed by our server. The upstream body is already decompressed
# by httpx, so its content-encoding/length no longer describe what we send.
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length", "content-encoding", "server", "date"}


def upstream_headers(incoming: Headers, real_api_key: str) -> dict[str, str]:
    headers = {k: v for k, v in incoming.items() if k.lower() not in DROP_REQUEST_HEADERS}
    headers["x-api-key"] = real_api_key
    return headers


def client_headers(upstream: Any) -> dict[str, str]:
    """Upstream response headers safe to return to the client (request-id, rate limits...)."""
    return {k: v for k, v in upstream.items() if k.lower() not in DROP_RESPONSE_HEADERS}


def anthropic_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    """An error in Anthropic's format, so SDKs raise their normal typed exceptions."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )
