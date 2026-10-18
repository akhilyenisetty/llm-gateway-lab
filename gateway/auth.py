"""Gateway-issued client keys: generation, hashing, and lookup.

Clients send their gateway key the same way they'd send an Anthropic key (`x-api-key`
or `Authorization: Bearer`), so the Anthropic SDK and Claude Code work unchanged; the
gateway then calls Anthropic with its own key.

Alternatively a client can keep its own Anthropic credential (for example Claude Code
signed in to a claude.ai plan, whose login travels in `Authorization`) and identify
itself to the gateway with an `X-Gateway-Key` header instead. The gateway then forwards
the client's credential unchanged and never uses its own key ("client credential" mode).
Only a SHA-256 hash of each key is stored. Keys are long random strings, so a fast
hash is enough (slow hashes like bcrypt are for low-entropy passwords).
"""

import hashlib
import secrets
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.requests import Request

from gateway.models import ApiKey

Billing = Literal["gateway_key", "client_credential"]

KEY_PREFIX = "gw_"
GATEWAY_KEY_HEADER = "x-gateway-key"
DISPLAY_PREFIX_LEN = 10


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def extract_key(headers: Headers) -> str | None:
    if key := headers.get("x-api-key"):
        return key.strip()
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


async def find_active_key(session: AsyncSession, key: str) -> ApiKey | None:
    result = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_key(key), ApiKey.revoked_at.is_(None))
    )
    return result.scalar_one_or_none()


async def create_key(session: AsyncSession, name: str) -> tuple[ApiKey, str]:
    """Create a key and return (row, plaintext). The plaintext is never stored."""
    plaintext = generate_key()
    row = ApiKey(name=name, key_hash=hash_key(plaintext), key_prefix=plaintext[:DISPLAY_PREFIX_LEN])
    session.add(row)
    await session.commit()
    return row, plaintext


def gateway_key_of(headers: Headers) -> tuple[str | None, Billing]:
    """The gateway key a request identifies itself with, and who pays for the call: the
    X-Gateway-Key header means the client's own credential is used ("client_credential");
    otherwise the credential header itself is the gateway key ("gateway_key")."""
    if key := headers.get(GATEWAY_KEY_HEADER, "").strip():
        return key, "client_credential"
    return extract_key(headers), "gateway_key"


async def authenticate_request(request: Request) -> ApiKey | None:
    """The active gateway key a request carries (in either mode), or None."""
    key, _ = gateway_key_of(request.headers)
    if not key:
        return None
    async with request.app.state.session_factory() as session:
        return await find_active_key(session, key)
