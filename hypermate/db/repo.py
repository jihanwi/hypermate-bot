"""Database access. All SQL lives here. aiosqlite, WAL mode, schema in schema.sql."""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name('schema.sql')

ADDED = 'added'
ALIAS_EXISTS = 'alias_exists'
ADDRESS_EXISTS = 'address_exists'

HYPERLIQUID = 'hyperliquid'
# Cursor kinds the Phase 0 pipeline keeps per venue account
CURSOR_KINDS = ('fills', 'ledger')


class Repo:
    def __init__(self, path: str) -> None:
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute('PRAGMA journal_mode=WAL')
        await self.db.execute('PRAGMA foreign_keys=ON')
        await self.db.executescript(SCHEMA_PATH.read_text())
        await self.db.commit()
        logger.info(f"Database ready at {self.path}")

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    # Subscriptions ----------------------------------------------------------

    async def add_subscription(self, user_id: int, address: str, alias: str, now_ms: int) -> str:
        """Subscribe user to address (lowercase) under alias.

        When the wallet had no subscribers, its HL cursors restart at now_ms and its
        snapshot is cleared, so a re-added wallet does not replay old activity.
        """
        db = self.db
        cur = await db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id = ? AND alias = ? COLLATE NOCASE", (user_id, alias))
        if await cur.fetchone():
            return ALIAS_EXISTS

        await db.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user_id, now_ms))
        await db.execute("INSERT OR IGNORE INTO wallets (evm_address) VALUES (?)", (address,))
        cur = await db.execute("SELECT wallet_id FROM wallets WHERE evm_address = ?", (address,))
        (wallet_id,) = await cur.fetchone()

        cur = await db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id = ? AND wallet_id = ?", (user_id, wallet_id))
        if await cur.fetchone():
            await db.rollback()
            return ADDRESS_EXISTS

        await db.execute(
            "INSERT OR IGNORE INTO venue_accounts (wallet_id, venue, account_ref) VALUES (?, ?, ?)",
            (wallet_id, HYPERLIQUID, address))
        cur = await db.execute(
            "SELECT venue_account_id FROM venue_accounts WHERE venue = ? AND account_ref = ?",
            (HYPERLIQUID, address))
        (venue_account_id,) = await cur.fetchone()

        cur = await db.execute("SELECT COUNT(*) FROM subscriptions WHERE wallet_id = ?", (wallet_id,))
        (subscriber_count,) = await cur.fetchone()
        if subscriber_count == 0:
            await db.execute("DELETE FROM snapshots WHERE venue_account_id = ?", (venue_account_id,))
            for kind in CURSOR_KINDS:
                await db.execute(
                    "INSERT OR REPLACE INTO cursors (venue_account_id, kind, cursor, updated_at) "
                    "VALUES (?, ?, ?, ?)", (venue_account_id, kind, str(now_ms), now_ms))

        await db.execute(
            "INSERT INTO subscriptions (user_id, wallet_id, alias, created_at) VALUES (?, ?, ?, ?)",
            (user_id, wallet_id, alias, now_ms))
        await db.commit()
        return ADDED

    async def remove_subscription(self, user_id: int, alias: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND alias = ? COLLATE NOCASE", (user_id, alias))
        await self.db.commit()
        return cur.rowcount > 0

    async def list_subscriptions(self, user_id: int) -> list[tuple[str, str]]:
        """[(alias, address)] ordered by alias (case-insensitive)."""
        cur = await self.db.execute(
            "SELECT s.alias, w.evm_address FROM subscriptions s JOIN wallets w USING (wallet_id) "
            "WHERE s.user_id = ? ORDER BY s.alias COLLATE NOCASE", (user_id,))
        return [(alias, address) for alias, address in await cur.fetchall()]

    async def find_subscription(self, user_id: int, alias: str) -> Optional[tuple[str, str]]:
        """(stored alias, address) for a case-insensitive alias match, or None."""
        cur = await self.db.execute(
            "SELECT s.alias, w.evm_address FROM subscriptions s JOIN wallets w USING (wallet_id) "
            "WHERE s.user_id = ? AND s.alias = ? COLLATE NOCASE", (user_id, alias))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def tracked_accounts(self) -> list[tuple[int, str]]:
        """[(venue_account_id, address)] for active HL accounts with at least one subscriber."""
        cur = await self.db.execute(
            "SELECT va.venue_account_id, w.evm_address FROM venue_accounts va "
            "JOIN wallets w USING (wallet_id) "
            "WHERE va.venue = ? AND va.active = 1 "
            "AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.wallet_id = va.wallet_id) "
            "ORDER BY va.venue_account_id", (HYPERLIQUID,))
        return [(va_id, address) for va_id, address in await cur.fetchall()]

    async def subscribers(self, venue_account_id: int) -> list[tuple[int, str]]:
        """[(user_id, alias)] subscribed to the venue account's wallet."""
        cur = await self.db.execute(
            "SELECT s.user_id, s.alias FROM subscriptions s "
            "JOIN venue_accounts va ON va.wallet_id = s.wallet_id "
            "WHERE va.venue_account_id = ?", (venue_account_id,))
        return [(user_id, alias) for user_id, alias in await cur.fetchall()]

    # Polling state (B6) -----------------------------------------------------

    async def get_snapshot(self, venue_account_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT positions_json FROM snapshots WHERE venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def save_snapshot(self, venue_account_id: int, positions: dict, now_ms: int,
                            account_value: Optional[str] = None) -> None:
        """account_value is a Decimal string (TEXT column), never a float."""
        await self.db.execute(
            "INSERT INTO snapshots (venue_account_id, positions_json, account_value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(venue_account_id) DO UPDATE SET positions_json = excluded.positions_json, "
            "account_value = excluded.account_value, updated_at = excluded.updated_at",
            (venue_account_id, json.dumps(positions, sort_keys=True), account_value, now_ms))
        await self.db.commit()

    async def hl_account_value(self, address: str) -> Optional[str]:
        """Last polled HL account value for the address, or None if not polled yet."""
        cur = await self.db.execute(
            "SELECT s.account_value FROM snapshots s "
            "JOIN venue_accounts va USING (venue_account_id) "
            "WHERE va.venue = ? AND va.account_ref = ?", (HYPERLIQUID, address))
        row = await cur.fetchone()
        return row[0] if row else None

    async def get_cursor(self, venue_account_id: int, kind: str) -> Optional[str]:
        cur = await self.db.execute(
            "SELECT cursor FROM cursors WHERE venue_account_id = ? AND kind = ?", (venue_account_id, kind))
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_cursor(self, venue_account_id: int, kind: str, value: str, now_ms: int) -> None:
        await self.db.execute(
            "INSERT INTO cursors (venue_account_id, kind, cursor, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(venue_account_id, kind) DO UPDATE SET "
            "cursor = excluded.cursor, updated_at = excluded.updated_at",
            (venue_account_id, kind, value, now_ms))
        await self.db.commit()

    # Events (spec 3.3: dedupe_key is the only guard against double alerts) ---

    async def record_event(self, dedupe_key: str, venue_account_id: int, event_type: str, ts_ms: int,
                           payload: dict, delivery: str, now_ms: int) -> Optional[int]:
        """Insert an event; returns its id, or None if the dedupe_key was already recorded."""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO events "
            "(dedupe_key, venue_account_id, type, ts_ms, payload_json, delivery, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dedupe_key, venue_account_id, event_type, ts_ms,
             json.dumps(payload, sort_keys=True, default=str), delivery, now_ms))
        await self.db.commit()
        return cur.lastrowid if cur.rowcount == 1 else None

    async def recent_events(self, venue_account_id: int, limit: int = 30) -> list[dict]:
        cur = await self.db.execute(
            "SELECT event_id, dedupe_key, type, ts_ms, payload_json, delivery FROM events "
            "WHERE venue_account_id = ? ORDER BY ts_ms DESC, event_id DESC LIMIT ?", (venue_account_id, limit))
        return [{'event_id': r[0], 'dedupe_key': r[1], 'type': r[2], 'ts_ms': r[3],
                 'payload': json.loads(r[4]), 'delivery': r[5]} for r in await cur.fetchall()]

    # Active TWAPs ------------------------------------------------------------

    async def active_twaps(self, venue_account_id: int) -> dict[str, dict]:
        """{twap_id: last known state} for the account."""
        cur = await self.db.execute(
            "SELECT twap_id, state_json FROM twap_active WHERE venue_account_id = ?", (venue_account_id,))
        return {twap_id: json.loads(state) for twap_id, state in await cur.fetchall()}

    async def upsert_twap(self, venue_account_id: int, twap_id: str, state: dict, started_ms: int) -> None:
        await self.db.execute(
            "INSERT INTO twap_active (venue_account_id, twap_id, state_json, started_ms) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(venue_account_id, twap_id) DO UPDATE SET state_json = excluded.state_json",
            (venue_account_id, twap_id, json.dumps(state, sort_keys=True, default=str), started_ms))
        await self.db.commit()

    async def delete_twap(self, venue_account_id: int, twap_id: str) -> None:
        await self.db.execute(
            "DELETE FROM twap_active WHERE venue_account_id = ? AND twap_id = ?", (venue_account_id, twap_id))
        await self.db.commit()
