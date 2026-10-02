"""Mint a scoped API credential for a named principal.

The plaintext key is printed exactly once — only its SHA-256 is stored.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/mint_api_credential.py \
        --name ci-indexer --scopes jobs:write,jobs:read [--kind service] \
        [--org-id 1] [--ttl-days 90]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from brain.auth.principals import mint_credential
from brain.database.session import async_session_factory, init_db


async def _mint(args: argparse.Namespace) -> str:
    await init_db()
    async with async_session_factory() as session:
        async with session.begin():
            raw, cred = await mint_credential(
                session,
                name=args.name,
                scopes=[s.strip() for s in args.scopes.split(",") if s.strip()],
                kind=args.kind,
                org_id=args.org_id,
                ttl_days=args.ttl_days,
            )
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Principal name")
    parser.add_argument("--scopes", required=True, help="Comma-separated scopes, e.g. jobs:write,jobs:read; '*' grants all")
    parser.add_argument("--kind", default="service", choices=["service", "human"])
    parser.add_argument("--org-id", type=int, default=None)
    parser.add_argument("--ttl-days", type=int, default=None, help="Credential expiry; omit for no expiry")
    args = parser.parse_args()

    raw = asyncio.run(_mint(args))
    print(f"credential for '{args.name}' (showing once): {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
