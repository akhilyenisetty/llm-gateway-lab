"""Command-line tool for managing gateway keys.

python -m gateway.keys create --name rag-eval-lab
python -m gateway.keys list
python -m gateway.keys revoke gw_AbC123
"""

import argparse
import asyncio

from sqlalchemy import select

from gateway.auth import create_key
from gateway.db import make_engine, make_session_factory
from gateway.models import ApiKey, utcnow
from gateway.settings import get_settings


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.keys")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="issue a new key")
    create.add_argument("--name", required=True, help="who or what uses this key")
    sub.add_parser("list", help="list keys (prefixes only)")
    revoke = sub.add_parser("revoke", help="revoke a key by its displayed prefix")
    revoke.add_argument("prefix")
    args = parser.parse_args(argv)

    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as session:
            if args.command == "create":
                row, plaintext = await create_key(session, args.name)
                print(f"Created key '{row.name}' ({row.key_prefix}...)")
                print(f"\n  {plaintext}\n")
                print("Store it now; the gateway keeps only a hash and cannot show it again.")
            elif args.command == "list":
                rows = (await session.execute(select(ApiKey).order_by(ApiKey.created_at))).scalars()
                for row in rows:
                    status = f"revoked {row.revoked_at:%Y-%m-%d}" if row.revoked_at else "active"
                    print(
                        f"{row.key_prefix}...  {row.name:<30} {row.created_at:%Y-%m-%d}  {status}"
                    )
            elif args.command == "revoke":
                matches = (
                    (
                        await session.execute(
                            select(ApiKey).where(
                                ApiKey.key_prefix == args.prefix, ApiKey.revoked_at.is_(None)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(matches) != 1:
                    print(
                        f"Expected exactly one active key with prefix {args.prefix!r}, "
                        f"found {len(matches)}."
                    )
                    return 1
                matches[0].revoked_at = utcnow()
                await session.commit()
                print(f"Revoked {args.prefix}... ({matches[0].name})")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
