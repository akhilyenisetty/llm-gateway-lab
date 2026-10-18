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

# Request headers never forwarded upstream: gateway-only metadata, the gateway key header,
# and headers the HTTP client recomputes itself. X-App is read but still forwarded:
# Claude Code sends `x-app: cli` to Anthropic itself.
DROP_REQUEST_HEADERS = HOP_BY_HOP | {
    "host",
    "content-length",
    "accept-encoding",
    "cookie",
    "x-session-id",
    "x-category",
    "x-session-budget-usd",
    "x-gateway-key",
}
# The client's credential headers. Normally they hold the gateway key, so they're replaced
# with the gateway's own Anthropic key; in client-credential mode they're forwarded as-is.
CREDENTIAL_HEADERS = {"x-api-key", "authorization"}

# Response headers recomputed by our server. The upstream body is already decompressed
# by httpx, so its content-encoding/length no longer describe what we send.
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length", "content-encoding", "server", "date"}


def upstream_headers(
    incoming: Headers, real_api_key: str, *, forward_client_credential: bool = False
) -> dict[str, str]:
    drop = (
        DROP_REQUEST_HEADERS
        if forward_client_credential
        else DROP_REQUEST_HEADERS | CREDENTIAL_HEADERS
    )
    headers = {k: v for k, v in incoming.items() if k.lower() not in drop}
    if not forward_client_credential:
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
