"""Database access. All SQL lives here. aiosqlite, WAL mode, schema in schema.sql."""

import asyncio
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
CURSOR_KINDS = ('fills', 'ledger', 'twap')   # 'twap': when TWAP tracking began (never advanced)


def slim_payload(payload: dict) -> dict:
    """Payload without the columns the events table already has and without the copied meta in chains."""
    from hypermate.core.aggregator import chain_meta
    slim = {k: v for k, v in payload.items() if k not in ('venue', 'venue_account_id')}
    chain = slim.get('chain')
    if isinstance(chain, dict) and isinstance(chain.get('meta'), dict):
        slim['chain'] = {**chain, 'meta': chain_meta(chain['meta'])}
    return slim


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
        await self._migrate()
        await self.db.commit()
        logger.info(f"Database ready at {self.path}")

    async def _migrate(self) -> None:
        """Add columns introduced after the first deploy (idempotent)."""
        cur = await self.db.execute("PRAGMA table_info(venue_accounts)")
        columns = {row[1] for row in await cur.fetchall()}
        if 'dexs_json' not in columns:
            await self.db.execute("ALTER TABLE venue_accounts ADD COLUMN dexs_json TEXT NOT NULL DEFAULT '[]'")
            logger.info("Migrated venue_accounts: added dexs_json")
        cur = await self.db.execute("PRAGMA table_info(snapshots)")
        columns = {row[1] for row in await cur.fetchall()}
        if 'spot_json' not in columns:
            await self.db.execute("ALTER TABLE snapshots ADD COLUMN spot_json TEXT")
            logger.info("Migrated snapshots: added spot_json")

    PAYLOAD_VERSION = 1
    SLIM_BATCH = 1000

    async def payloads_need_slimming(self) -> bool:
        cur = await self.db.execute("PRAGMA user_version")
        (version,) = await cur.fetchone()
        return version < self.PAYLOAD_VERSION

    async def slim_payloads(self, batch: int = SLIM_BATCH) -> tuple[int, int]:
        """One-time rewrite of events.payload_json to the slim form, in event_id batches so memory
        stays flat (the first version loaded the whole table and OOMed a 256 MB machine).

        Not called from connect(): main schedules it as a background task after the bot is up.
        PRAGMA user_version marks it done. Returns (rows seen, rows changed).
        """
        if not await self.payloads_need_slimming():
            return 0, 0
        seen = changed = batches = 0
        last_id = 0
        while True:
            cur = await self.db.execute(
                "SELECT event_id, payload_json FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?",
                (last_id, batch))
            rows = await cur.fetchall()
            if not rows:
                break
            updates = []
            for event_id, payload_json in rows:
                text = json.dumps(slim_payload(json.loads(payload_json)))
                if len(text) < len(payload_json):
                    updates.append((text, event_id))
            if updates:
                await self.db.executemany("UPDATE events SET payload_json = ? WHERE event_id = ?", updates)
                await self.db.commit()
            seen += len(rows)
            changed += len(updates)
            last_id = rows[-1][0]
            batches += 1
            if batches % 10 == 0:
                logger.info(f"Slimming event payloads: {seen} rows seen, {changed} rewritten")
            del rows, updates
            await asyncio.sleep(0)          # let the bot handle updates between batches
        await self.db.execute(f"PRAGMA user_version = {self.PAYLOAD_VERSION}")
        await self.db.commit()
        logger.info(f"Slimmed {changed} of {seen} event payloads")
        return seen, changed

    async def prune_events(self, before_ms: int) -> tuple[int, int]:
        """Delete events older than before_ms and their sent_messages rows. Returns (events, messages)."""
        cur = await self.db.execute(
            "DELETE FROM sent_messages WHERE event_id IN (SELECT event_id FROM events WHERE ts_ms < ?)", (before_ms,))
        messages = cur.rowcount
        cur = await self.db.execute("DELETE FROM events WHERE ts_ms < ?", (before_ms,))
        await self.db.commit()
        return cur.rowcount, messages

    async def checkpoint(self) -> None:
        """Fold the WAL into the main file and truncate it."""
        await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def db_stats(self, now_ms: int) -> dict:
        """Row counts for /health: events total, events in the last 24 h by type, sent_messages."""
        cur = await self.db.execute("SELECT COUNT(*) FROM events")
        (events_total,) = await cur.fetchone()
        cur = await self.db.execute(
            "SELECT type, COUNT(*) FROM events WHERE ts_ms >= ? GROUP BY type ORDER BY COUNT(*) DESC, type",
            (now_ms - 24 * 3600 * 1000,))
        by_type = [(t, n) for t, n in await cur.fetchall()]
        cur = await self.db.execute("SELECT COUNT(*) FROM sent_messages")
        (messages,) = await cur.fetchone()
        return {'events_total': events_total, 'events_24h': by_type, 'sent_messages': messages}

    async def all_active_algos(self) -> list[dict]:
        """Every algo_active row with its address (debug view for /health)."""
        cur = await self.db.execute(
            "SELECT w.evm_address, a.coin, a.sign, a.started_ms, a.last_fill_ms, a.fills_count, a.total_ntl "
            "FROM algo_active a JOIN venue_accounts va USING (venue_account_id) JOIN wallets w USING (wallet_id) "
            "ORDER BY w.evm_address, a.coin, a.sign")
        return [{'address': r[0], 'coin': r[1], 'sign': r[2], 'started_ms': r[3], 'last_fill_ms': r[4],
                 'fills_count': r[5], 'total_ntl': r[6]} for r in await cur.fetchall()]

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
            "INSERT OR IGNORE INTO venue_accounts (wallet_id, venue, account_ref, last_activity_ms) "
            "VALUES (?, ?, ?, ?)", (wallet_id, HYPERLIQUID, address, now_ms))
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
        return [(key, address) for key, address, _ in await self.tracked_accounts_with_activity()]

    async def tracked_accounts_with_activity(self) -> list[tuple[int, str, Optional[int]]]:
        """[(venue_account_id, address, last_activity_ms)] (tier polling, spec 3.5)."""
        cur = await self.db.execute(
            "SELECT va.venue_account_id, w.evm_address, va.last_activity_ms FROM venue_accounts va "
            "JOIN wallets w USING (wallet_id) "
            "WHERE va.venue = ? AND va.active = 1 "
            "AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.wallet_id = va.wallet_id) "
            "ORDER BY va.venue_account_id", (HYPERLIQUID,))
        return [(va_id, address, last) for va_id, address, last in await cur.fetchall()]

    async def touch_activity(self, venue_account_id: int, now_ms: int) -> None:
        await self.db.execute("UPDATE venue_accounts SET last_activity_ms = ? WHERE venue_account_id = ?",
                              (now_ms, venue_account_id))
        await self.db.commit()

    async def counts(self) -> dict:
        """Row counts for /health: active twaps and algos."""
        out = {}
        for name, table in (('twaps', 'twap_active'), ('algos', 'algo_active')):
            cur = await self.db.execute(f"SELECT COUNT(*) FROM {table}")
            (out[name],) = await cur.fetchone()
        return out

    async def subscribers(self, venue_account_id: int) -> list[tuple[int, str]]:
        """[(user_id, alias)] subscribed to the venue account's wallet."""
        cur = await self.db.execute(
            "SELECT s.user_id, s.alias FROM subscriptions s "
            "JOIN venue_accounts va ON va.wallet_id = s.wallet_id "
            "WHERE va.venue_account_id = ?", (venue_account_id,))
        return [(user_id, alias) for user_id, alias in await cur.fetchall()]

    # Polling state (B6) -----------------------------------------------------

    async def get_snapshot(self, venue_account_id: int) -> Optional[dict]:
        """{dex: {coin: position}} (main dex key ""). Phase 0 rows ({coin: position}) are read as main dex."""
        cur = await self.db.execute(
            "SELECT positions_json FROM snapshots WHERE venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone()
        if not row:
            return None
        snapshot = json.loads(row[0])
        if any(isinstance(v, dict) and 'szi' in v for v in snapshot.values()):
            return {'': snapshot}
        return snapshot

    async def save_snapshot(self, venue_account_id: int, positions: dict, now_ms: int,
                            account_value: Optional[str] = None, spot: Optional[dict] = None) -> None:
        """account_value is a Decimal string (TEXT column), never a float. spot is {coin: total} or None."""
        await self.db.execute(
            "INSERT INTO snapshots (venue_account_id, positions_json, account_value, spot_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(venue_account_id) DO UPDATE SET positions_json = excluded.positions_json, "
            "account_value = excluded.account_value, spot_json = excluded.spot_json, "
            "updated_at = excluded.updated_at",
            (venue_account_id, json.dumps(positions, sort_keys=True), account_value,
             json.dumps(spot, sort_keys=True) if spot is not None else None, now_ms))
        await self.db.commit()

    async def get_spot_snapshot(self, venue_account_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT spot_json FROM snapshots WHERE venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row and row[0] else None

    async def backup(self, target_path: str) -> None:
        """Consistent copy of the live DB via the sqlite backup API (safe while WAL writers run)."""
        async with aiosqlite.connect(target_path) as target:
            await self.db.backup(target)

    async def hl_account_id(self, address: str) -> Optional[int]:
        cur = await self.db.execute(
            "SELECT venue_account_id FROM venue_accounts WHERE venue = ? AND account_ref = ?", (HYPERLIQUID, address))
        row = await cur.fetchone()
        return row[0] if row else None

    async def get_dexs(self, venue_account_id: int) -> list[str]:
        cur = await self.db.execute(
            "SELECT dexs_json FROM venue_accounts WHERE venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row and row[0] else []

    async def set_dexs(self, venue_account_id: int, dexs: list[str]) -> None:
        await self.db.execute("UPDATE venue_accounts SET dexs_json = ? WHERE venue_account_id = ?",
                              (json.dumps(sorted(set(dexs))), venue_account_id))
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

    async def get_event(self, event_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT event_id, dedupe_key, type, ts_ms, payload_json, delivery FROM events WHERE event_id = ?",
            (event_id,))
        r = await cur.fetchone()
        return ({'event_id': r[0], 'dedupe_key': r[1], 'type': r[2], 'ts_ms': r[3],
                 'payload': json.loads(r[4]), 'delivery': r[5]} if r else None)

    async def get_event_by_key(self, dedupe_key: str) -> Optional[dict]:
        cur = await self.db.execute("SELECT event_id FROM events WHERE dedupe_key = ?", (dedupe_key,))
        row = await cur.fetchone()
        return await self.get_event(row[0]) if row else None

    async def update_event_payload(self, event_id: int, payload: dict) -> None:
        await self.db.execute("UPDATE events SET payload_json = ? WHERE event_id = ?",
                              (json.dumps(payload, sort_keys=True, default=str), event_id))
        await self.db.commit()

    async def events_since(self, venue_account_id: int, since_ms: int, types: Optional[list[str]] = None) -> list[dict]:
        """Events with ts_ms >= since_ms, oldest first."""
        sql = ("SELECT event_id, dedupe_key, type, ts_ms, payload_json, delivery FROM events "
               "WHERE venue_account_id = ? AND ts_ms >= ?")
        params: list = [venue_account_id, since_ms]
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params += types
        cur = await self.db.execute(sql + " ORDER BY ts_ms, event_id", params)
        return [{'event_id': r[0], 'dedupe_key': r[1], 'type': r[2], 'ts_ms': r[3],
                 'payload': json.loads(r[4]), 'delivery': r[5]} for r in await cur.fetchall()]

    async def last_event_ts(self, venue_account_id: int, event_type: str, coin: str) -> Optional[int]:
        """ts_ms of the newest event of this type for the coin (e.g. last POSITION_OPEN, for 'held')."""
        cur = await self.db.execute(
            "SELECT MAX(ts_ms) FROM events WHERE venue_account_id = ? AND type = ? "
            "AND json_extract(payload_json, '$.coin') = ?", (venue_account_id, event_type, coin))
        row = await cur.fetchone()
        return row[0] if row else None

    # Sent messages (edits for debounce and algo progress) ---------------------

    async def add_sent_message(self, event_id: int, user_id: int, chat_id: int, message_id: int) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO sent_messages (event_id, user_id, chat_id, message_id) VALUES (?, ?, ?, ?)",
            (event_id, user_id, chat_id, message_id))
        await self.db.commit()

    async def sent_messages(self, event_id: int) -> dict[int, tuple[int, int]]:
        """{user_id: (chat_id, message_id)} for the event."""
        cur = await self.db.execute(
            "SELECT user_id, chat_id, message_id FROM sent_messages WHERE event_id = ?", (event_id,))
        return {u: (c, m) for u, c, m in await cur.fetchall()}

    # Synthetic TWAP (algo) state ----------------------------------------------

    async def active_algos(self, venue_account_id: int) -> dict[tuple[str, int], dict]:
        cur = await self.db.execute(
            "SELECT coin, sign, started_ms, last_fill_ms, fills_count, total_sz, total_ntl "
            "FROM algo_active WHERE venue_account_id = ?", (venue_account_id,))
        return {(coin, sign): {'coin': coin, 'sign': sign, 'started_ms': started, 'last_fill_ms': last,
                               'fills_count': count, 'total_sz': sz, 'total_ntl': ntl}
                for coin, sign, started, last, count, sz, ntl in await cur.fetchall()}

    async def upsert_algo(self, venue_account_id: int, state: dict) -> None:
        await self.db.execute(
            "INSERT INTO algo_active (venue_account_id, coin, sign, started_ms, last_fill_ms, fills_count, "
            "total_sz, total_ntl) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(venue_account_id, coin, sign) DO UPDATE SET last_fill_ms = excluded.last_fill_ms, "
            "fills_count = excluded.fills_count, total_sz = excluded.total_sz, total_ntl = excluded.total_ntl",
            (venue_account_id, state['coin'], state['sign'], state['started_ms'], state['last_fill_ms'],
             state['fills_count'], str(state['total_sz']), str(state['total_ntl'])))
        await self.db.commit()

    async def delete_algo(self, venue_account_id: int, coin: str, sign: int) -> None:
        await self.db.execute("DELETE FROM algo_active WHERE venue_account_id = ? AND coin = ? AND sign = ?",
                              (venue_account_id, coin, sign))
        await self.db.commit()
