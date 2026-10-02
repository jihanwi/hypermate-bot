#!/usr/bin/env python3
"""One-off migration: v1 tracked_wallets -> v2 users / wallets / venue_accounts / subscriptions.

Run by hand, never on startup. Safe to run more than once: rows that are already
migrated are counted as "already present" and nothing is duplicated. The v1
tracked_wallets table and the legacy JSON files are left untouched.

    python scripts/migrate_v1.py --source /path/to/old/hypermate.db [--target /data/hypermate.db]

--target defaults to $DATABASE_PATH (or /data/hypermate.db). --source defaults to the
target, for the case where the v1 table lives in the same file.
"""

import argparse
import asyncio
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypermate.db.repo import ADDED, ALIAS_EXISTS, Repo  # noqa: E402

ADDRESS_RE = re.compile(r'0x[0-9a-fA-F]{40}')


def read_v1_rows(source: str) -> list[tuple[str, str, str]]:
    """[(user_id, wallet_address, alias)] from the v1 table, oldest first."""
    con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tracked_wallets'").fetchone()
        if not exists:
            return []
        return con.execute(
            "SELECT user_id, wallet_address, alias FROM tracked_wallets ORDER BY id").fetchall()
    finally:
        con.close()


async def migrate(source: str, target: str) -> dict:
    rows = read_v1_rows(source)
    counts = {'rows': len(rows), 'migrated': 0, 'already_present': 0, 'skipped': 0}
    skipped = []

    repo = Repo(target)
    await repo.connect()
    try:
        for user_id_raw, address_raw, alias_raw in rows:
            address = (address_raw or '').strip().lower()
            alias = (alias_raw or '').strip()
            try:
                user_id = int(user_id_raw)
            except (TypeError, ValueError):
                user_id = None
            if user_id is None or not alias or not ADDRESS_RE.fullmatch(address):
                counts['skipped'] += 1
                skipped.append(f"invalid row: user={user_id_raw!r} address={address_raw!r} alias={alias_raw!r}")
                continue

            result = await repo.add_subscription(user_id, address, alias, time.time_ns() // 1_000_000)
            if result == ADDED:
                counts['migrated'] += 1
                continue

            existing = await repo.find_subscription(user_id, alias)
            if existing is not None and existing[1] == address:
                counts['already_present'] += 1
            elif result == ALIAS_EXISTS:
                counts['skipped'] += 1
                skipped.append(f"user {user_id}: alias {alias!r} already used for {existing[1]}, "
                               f"not adding {address}")
            else:
                counts['skipped'] += 1
                skipped.append(f"user {user_id}: {address} already tracked under another alias, "
                               f"not adding alias {alias!r}")
    finally:
        await repo.close()

    for line in skipped:
        print(f"SKIP {line}")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', default=os.getenv('DATABASE_PATH', '/data/hypermate.db'),
                        help='v2 database (default: $DATABASE_PATH or /data/hypermate.db)')
    parser.add_argument('--source', help='database holding the v1 tracked_wallets table (default: --target)')
    args = parser.parse_args()
    source = args.source or args.target

    if not os.path.exists(source):
        print(f"Source database {source} does not exist", file=sys.stderr)
        return 1

    counts = asyncio.run(migrate(source, args.target))
    if counts['rows'] == 0:
        print(f"No v1 tracked_wallets rows in {source}; nothing to migrate")
    print(f"source={source} target={args.target} rows={counts['rows']} migrated={counts['migrated']} "
          f"already_present={counts['already_present']} skipped={counts['skipped']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
