"""Gateway-issued client keys: generation, hashing, and lookup.

Clients send their gateway key the same way they'd send an Anthropic key (`x-api-key`
or `Authorization: Bearer`), so the Anthropic SDK and Claude Code work unchanged.
Only a SHA-256 hash of each key is stored. Keys are long random strings, so a fast
hash is enough (slow hashes like bcrypt are for low-entropy passwords).
"""

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

from gateway.models import ApiKey

KEY_PREFIX = "gw_"
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
