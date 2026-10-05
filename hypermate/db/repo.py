"""Database access. All SQL lives here. aiosqlite, WAL mode, schema in schema.sql."""

import asyncio
import json
from decimal import Decimal
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name('schema.sql')

ADDED = 'added'
ALIAS_EXISTS = 'alias_exists'
ADDRESS_EXISTS = 'address_exists'

HYPERLIQUID = 'hyperliquid'
# Cursor kinds the Phase 0 pipeline keeps per venue account
CURSOR_KINDS = ('fills', 'ledger', 'twap', 'trades')   # 'twap': when TWAP tracking began (never advanced)


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

    async def _rows(self, cur) -> list:
        """fetchall then close: no read statement may stay open on the shared connection, or a later
        DELETE / wal_checkpoint fails with "database table is locked" (deploy log 2026-10-04)."""
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def _row(self, cur):
        row = await cur.fetchone()
        await cur.close()
        return row

    async def connect(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute('PRAGMA journal_mode=WAL')
        await self.db.execute('PRAGMA foreign_keys=ON')
        await self.db.execute('PRAGMA busy_timeout=30000')       # wait for other connections (backup copies)
        await self.db.executescript(SCHEMA_PATH.read_text())
        await self._migrate()
        await self.db.commit()
        logger.info(f"Database ready at {self.path}")

    async def _migrate(self) -> None:
        """Add columns introduced after the first deploy (idempotent)."""
        cur = await self.db.execute("PRAGMA table_info(venue_accounts)")
        columns = {row[1] for row in await self._rows(cur)}
        if 'dexs_json' not in columns:
            await self.db.execute("ALTER TABLE venue_accounts ADD COLUMN dexs_json TEXT NOT NULL DEFAULT '[]'")
            logger.info("Migrated venue_accounts: added dexs_json")
        cur = await self.db.execute("PRAGMA table_info(snapshots)")
        columns = {row[1] for row in await self._rows(cur)}
        if 'spot_json' not in columns:
            await self.db.execute("ALTER TABLE snapshots ADD COLUMN spot_json TEXT")
            logger.info("Migrated snapshots: added spot_json")

    # api_cache (spec 3.4): expensive calls such as userRole (weight 60) -----------------

    async def cache_get(self, cache_key: str, now_ms: int) -> Optional[Any]:
        cur = await self.db.execute(
            "SELECT value_json, expires_at FROM api_cache WHERE cache_key = ?", (cache_key,))
        row = await cur.fetchone(); await cur.close()
        if not row or (row[1] is not None and row[1] <= now_ms):
            return None
        return json.loads(row[0])

    async def cache_set(self, cache_key: str, value: Any, expires_at: int) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO api_cache (cache_key, value_json, expires_at) VALUES (?, ?, ?)",
            (cache_key, json.dumps(value), expires_at))
        await self.db.commit()

    # wallet_links (spec 7) ------------------------------------------------------------

    async def wallet_id(self, address: str) -> Optional[int]:
        cur = await self.db.execute("SELECT wallet_id FROM wallets WHERE evm_address = ?", (address.lower(),))
        row = await cur.fetchone(); await cur.close()
        return row[0] if row else None

    async def replace_links(self, wallet_id: int, links: list[dict], now_ms: int) -> None:
        """Store a full discovery result (rows: related_address, link_type, confidence, evidence)."""
        await self.db.execute("DELETE FROM wallet_links WHERE wallet_id = ?", (wallet_id,))
        for link in links:
            await self.db.execute(
                "INSERT OR REPLACE INTO wallet_links (wallet_id, related_address, related_venue, link_type, "
                "confidence, evidence_json, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (wallet_id, link['related_address'].lower(), link.get('related_venue', HYPERLIQUID),
                 link['link_type'], link['confidence'], json.dumps(link.get('evidence') or {}), now_ms))
        await self.db.commit()

    async def links(self, wallet_id: int) -> list[dict]:
        """Rows ordered confirmed > likely > weak, then address. 'row_id' is the SQLite rowid (callbacks)."""
        cur = await self.db.execute(
            "SELECT rowid, related_address, related_venue, link_type, confidence, evidence_json, discovered_at "
            "FROM wallet_links WHERE wallet_id = ? "
            "ORDER BY CASE confidence WHEN 'confirmed' THEN 0 WHEN 'likely' THEN 1 ELSE 2 END, "
            "link_type, related_address", (wallet_id,))
        return [{'row_id': r[0], 'related_address': r[1], 'related_venue': r[2], 'link_type': r[3],
                 'confidence': r[4], 'evidence': json.loads(r[5]), 'discovered_at': r[6]}
                for r in await self._rows(cur)]

    async def link_by_row(self, row_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT wallet_id, related_address, link_type FROM wallet_links WHERE rowid = ?", (row_id,))
        row = await cur.fetchone(); await cur.close()
        return {'wallet_id': row[0], 'related_address': row[1], 'link_type': row[2]} if row else None

    async def links_discovered_at(self, wallet_id: int) -> Optional[int]:
        """Time of the last full discovery (rows with evidence.discovery = True), None if never run."""
        cur = await self.db.execute(
            "SELECT MAX(discovered_at) FROM wallet_links WHERE wallet_id = ? "
            "AND json_extract(evidence_json, '$.discovery') = 1", (wallet_id,))
        row = await cur.fetchone(); await cur.close()
        return row[0] if row and row[0] is not None else None

    async def add_weak_counterparty(self, wallet_id: int, address: str, direction: str, usd: str,
                                    ts_ms: int) -> None:
        """Background accumulation (spec 7.3): a ledger transfer counterparty becomes or updates a weak
        transfer_counterparty row. Never downgrades a row a discovery rated higher."""
        address = address.lower()
        cur = await self.db.execute(
            "SELECT confidence, evidence_json FROM wallet_links WHERE wallet_id = ? AND related_address = ? "
            "AND link_type = 'transfer_counterparty'", (wallet_id, address))
        row = await cur.fetchone(); await cur.close()
        evidence = json.loads(row[1]) if row else {'in': 0, 'out': 0, 'usd': '0', 'last_ms': 0}
        evidence[direction] = int(evidence.get(direction, 0)) + 1
        evidence['usd'] = str(Decimal(str(evidence.get('usd', '0'))) + Decimal(str(usd or '0')))
        evidence['last_ms'] = max(int(evidence.get('last_ms', 0)), ts_ms)
        evidence['background'] = True
        confidence = row[0] if row else 'weak'
        await self.db.execute(
            "INSERT OR REPLACE INTO wallet_links (wallet_id, related_address, related_venue, link_type, "
            "confidence, evidence_json, discovered_at) VALUES (?, ?, ?, 'transfer_counterparty', ?, ?, ?)",
            (wallet_id, address, HYPERLIQUID, confidence, json.dumps(evidence), ts_ms))
        await self.db.commit()

    PAYLOAD_VERSION = 1
    SUPPRESSED_CLEANUP_VERSION = 2
    SLIM_BATCH = 1000

    async def user_version(self) -> int:
        cur = await self.db.execute("PRAGMA user_version")
        (version,) = await cur.fetchone(); await cur.close()
        return version

    async def suppressed_rows_need_cleanup(self) -> bool:
        return await self.user_version() < self.SUPPRESSED_CLEANUP_VERSION

    async def delete_suppressed_rows(self, batch: int = 5000) -> int:
        """One-time removal of the 'suppressed_algo' / 'suppressed_twap' rows older versions recorded
        (fix/multi-algo-summary), in event_id batches. Returns the number of rows removed."""
        if not await self.suppressed_rows_need_cleanup():
            return 0
        removed = batches = 0
        while True:
            cur = await self.db.execute(
                "SELECT event_id FROM events WHERE delivery IN ('suppressed_algo', 'suppressed_twap') "
                "ORDER BY event_id LIMIT ?", (batch,))
            ids = [r[0] for r in await self._rows(cur)]
            if not ids:
                break
            await self.db.execute(
                f"DELETE FROM sent_messages WHERE event_id IN ({','.join('?' * len(ids))})", ids)
            await self.db.execute(f"DELETE FROM events WHERE event_id IN ({','.join('?' * len(ids))})", ids)
            await self.db.commit()
            removed += len(ids)
            batches += 1
            if batches % 10 == 0:
                logger.info(f"Removing suppressed fill rows: {removed} so far")
            del ids
            await asyncio.sleep(0)
        await self.db.execute(f"PRAGMA user_version = {self.SUPPRESSED_CLEANUP_VERSION}")
        await self.db.commit()
        logger.info(f"Removed {removed} suppressed fill rows")
        return removed

    async def payloads_need_slimming(self) -> bool:
        cur = await self.db.execute("PRAGMA user_version")
        (version,) = await cur.fetchone(); await cur.close()
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
        # rows written after this point are already slim; bounding the walk keeps a busy poller from
        # extending it forever
        cur = await self.db.execute("SELECT COALESCE(MAX(event_id), 0) FROM events")
        (max_id,) = await self._row(cur)
        while True:
            cur = await self.db.execute(
                "SELECT event_id, payload_json FROM events WHERE event_id > ? AND event_id <= ? "
                "ORDER BY event_id LIMIT ?", (last_id, max_id, batch))
            rows = await cur.fetchall(); await cur.close()
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
        if await self.user_version() < self.PAYLOAD_VERSION:
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

    async def free_space(self) -> tuple[int, int, int]:
        """(free bytes, file bytes, free ratio in percent) from PRAGMA freelist_count / page_count."""
        free = (await self._row(await self.db.execute("PRAGMA freelist_count")))[0]
        pages = (await self._row(await self.db.execute("PRAGMA page_count")))[0]
        page_size = (await self._row(await self.db.execute("PRAGMA page_size")))[0]
        return free * page_size, pages * page_size, (free * 100 // pages) if pages else 0

    async def vacuum(self) -> int:
        """Rebuild the file to reclaim the space of deleted rows. Needs no open transaction and a volume
        with room for a second copy; run before the poller starts. Returns the bytes freed."""
        await self.db.commit()
        _, before, _ = await self.free_space()
        await self.db.execute("VACUUM")
        await self.db.commit()
        # page_count x page_size, not the file size: in WAL mode the rebuilt pages sit in the WAL until
        # the checkpoint that follows, so the main file shrinks only then
        _, after, _ = await self.free_space()
        logger.info(f"VACUUM: {before // 1_048_576} MB -> {after // 1_048_576} MB")
        return max(0, before - after)

    async def checkpoint(self) -> tuple[int, int, int]:
        """Fold the WAL into the main file and truncate it. Returns (busy, log pages, checkpointed pages).

        A TRUNCATE checkpoint needs no reader on the WAL. An open reader shows as busy = 1 or, from the
        same connection, as "database table is locked"; both are retried a few times after a short wait
        with a PASSIVE checkpoint in between so progress is not lost. The 30 s busy_timeout is lowered
        to 1 s for the duration so a retry never blocks the bot for long.
        """
        log_pages = done = 0
        await self.db.execute("PRAGMA busy_timeout=1000")
        try:
            for attempt in range(5):
                busy = 1
                try:
                    cur = await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    row = await cur.fetchone(); await cur.close()
                    busy, log_pages, done = (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)
                except sqlite3.OperationalError as e:
                    if 'locked' not in str(e) and 'busy' not in str(e):
                        raise
                if not busy:
                    return busy, log_pages, done
                try:
                    await self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.OperationalError:
                    pass
                await asyncio.sleep(0.2 * (attempt + 1))
        finally:
            await self.db.execute("PRAGMA busy_timeout=30000")
        logger.warning("WAL checkpoint stayed busy; the WAL will be truncated at the next checkpoint")
        return 1, log_pages, done

    async def db_stats(self, now_ms: int) -> dict:
        """Row counts for /health: events total, events in the last 24 h by type, sent_messages."""
        cur = await self.db.execute("SELECT COUNT(*) FROM events")
        (events_total,) = await cur.fetchone(); await cur.close()
        cur = await self.db.execute(
            "SELECT type, COUNT(*) FROM events WHERE ts_ms >= ? GROUP BY type ORDER BY COUNT(*) DESC, type",
            (now_ms - 24 * 3600 * 1000,))
        by_type = [(t, n) for t, n in await self._rows(cur)]
        cur = await self.db.execute("SELECT COUNT(*) FROM sent_messages")
        (messages,) = await cur.fetchone(); await cur.close()
        return {'events_total': events_total, 'events_24h': by_type, 'sent_messages': messages}

    # multi-algo summary mode (spec 5.2) ----------------------------------------------

    async def multi_algo_mode(self, venue_account_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT entered_ms, event_id, message_ids_json, last_update_ms, below_since_ms "
            "FROM multi_algo_mode WHERE venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone(); await cur.close()
        if not row:
            return None
        return {'entered_ms': row[0], 'event_id': row[1], 'message_ids': json.loads(row[2]),
                'last_update_ms': row[3], 'below_since_ms': row[4]}

    async def multi_algo_modes(self) -> dict[int, dict]:
        cur = await self.db.execute("SELECT venue_account_id, entered_ms, below_since_ms FROM multi_algo_mode")
        rows = await cur.fetchall(); await cur.close()
        return {r[0]: {'entered_ms': r[1], 'below_since_ms': r[2]} for r in rows}

    async def enter_multi_algo_mode(self, venue_account_id: int, entered_ms: int, event_id: Optional[int],
                                    message_ids: dict) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO multi_algo_mode (venue_account_id, entered_ms, event_id, message_ids_json, "
            "last_update_ms, below_since_ms) VALUES (?, ?, ?, ?, ?, NULL)",
            (venue_account_id, entered_ms, event_id, json.dumps(message_ids), entered_ms))
        await self.db.commit()

    async def update_multi_algo_mode(self, venue_account_id: int, last_update_ms: Optional[int] = None,
                                     below_since_ms: Optional[int] = None, clear_below: bool = False) -> None:
        if last_update_ms is not None:
            await self.db.execute("UPDATE multi_algo_mode SET last_update_ms = ? WHERE venue_account_id = ?",
                                  (last_update_ms, venue_account_id))
        if clear_below:
            await self.db.execute("UPDATE multi_algo_mode SET below_since_ms = NULL WHERE venue_account_id = ?",
                                  (venue_account_id,))
        elif below_since_ms is not None:
            await self.db.execute("UPDATE multi_algo_mode SET below_since_ms = ? WHERE venue_account_id = ?",
                                  (below_since_ms, venue_account_id))
        await self.db.commit()

    async def exit_multi_algo_mode(self, venue_account_id: int) -> None:
        await self.db.execute("DELETE FROM multi_algo_mode WHERE venue_account_id = ?", (venue_account_id,))
        await self.db.commit()

    async def all_active_algos(self) -> list[dict]:
        """Every algo_active row with its address (debug view for /health)."""
        cur = await self.db.execute(
            "SELECT w.evm_address, a.coin, a.sign, a.started_ms, a.last_fill_ms, a.fills_count, a.total_ntl "
            "FROM algo_active a JOIN venue_accounts va USING (venue_account_id) JOIN wallets w USING (wallet_id) "
            "ORDER BY w.evm_address, a.coin, a.sign")
        return [{'address': r[0], 'coin': r[1], 'sign': r[2], 'started_ms': r[3], 'last_fill_ms': r[4],
                 'fills_count': r[5], 'total_ntl': r[6]} for r in await self._rows(cur)]

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    # Subscriptions ----------------------------------------------------------

    async def add_subscription(self, user_id: int, address: str, alias: str, now_ms: int,
                               create_hl: bool = True) -> str:
        """Subscribe user to address (lowercase) under alias.

        When the wallet had no subscribers, its HL cursors restart at now_ms and its
        snapshot is cleared, so a re-added wallet does not replay old activity.
        create_hl=False (/add <venue>:<address>): no Hyperliquid account row is created.
        """
        db = self.db
        cur = await db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id = ? AND alias = ? COLLATE NOCASE", (user_id, alias))
        if await self._row(cur):
            return ALIAS_EXISTS

        await db.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user_id, now_ms))
        await db.execute("INSERT OR IGNORE INTO wallets (evm_address) VALUES (?)", (address,))
        cur = await db.execute("SELECT wallet_id FROM wallets WHERE evm_address = ?", (address,))
        (wallet_id,) = await cur.fetchone(); await cur.close()

        cur = await db.execute(
            "SELECT 1 FROM subscriptions WHERE user_id = ? AND wallet_id = ?", (user_id, wallet_id))
        if await self._row(cur):
            await db.rollback()
            return ADDRESS_EXISTS

        if create_hl:
            await db.execute(
                "INSERT OR IGNORE INTO venue_accounts (wallet_id, venue, account_ref, last_activity_ms) "
                "VALUES (?, ?, ?, ?)", (wallet_id, HYPERLIQUID, address, now_ms))
        cur = await db.execute(
            "SELECT venue_account_id FROM venue_accounts WHERE venue = ? AND account_ref = ?",
            (HYPERLIQUID, address))
        row = await cur.fetchone(); await cur.close()
        venue_account_id = row[0] if row else None

        cur = await db.execute("SELECT COUNT(*) FROM subscriptions WHERE wallet_id = ?", (wallet_id,))
        (subscriber_count,) = await cur.fetchone(); await cur.close()
        if subscriber_count == 0 and venue_account_id is not None:
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
        rows = await cur.fetchall(); await cur.close()
        return [(alias, address) for alias, address in rows]

    async def find_subscription(self, user_id: int, alias: str) -> Optional[tuple[str, str]]:
        """(stored alias, address) for a case-insensitive alias match, or None."""
        cur = await self.db.execute(
            "SELECT s.alias, w.evm_address FROM subscriptions s JOIN wallets w USING (wallet_id) "
            "WHERE s.user_id = ? AND s.alias = ? COLLATE NOCASE", (user_id, alias))
        row = await cur.fetchone(); await cur.close()
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
        rows = await cur.fetchall(); await cur.close()
        return [(va_id, address, last) for va_id, address, last in rows]

    # Multi-venue accounts (spec 6.1) ------------------------------------------------

    async def ensure_venue_account(self, address: str, venue: str, account_ref: str, active: bool,
                                   now_ms: int, meta: Optional[dict] = None) -> tuple[int, bool]:
        """Create or update one venue account of a wallet. Returns (venue_account_id, created).
        A newly created active account starts its cursors at now (no history replay)."""
        wallet_id = await self.wallet_id(address)
        if wallet_id is None:
            await self.db.execute("INSERT OR IGNORE INTO wallets (evm_address) VALUES (?)", (address.lower(),))
            wallet_id = await self.wallet_id(address)
        cur = await self.db.execute(
            "SELECT venue_account_id, active FROM venue_accounts WHERE venue = ? AND account_ref = ?",
            (venue, account_ref))
        row = await cur.fetchone(); await cur.close()
        if row is None:
            cur = await self.db.execute(
                "INSERT INTO venue_accounts (wallet_id, venue, account_ref, active, last_activity_ms, dexs_json) "
                "VALUES (?, ?, ?, ?, ?, ?)", (wallet_id, venue, account_ref, int(active), now_ms,
                                              json.dumps((meta or {}).get('dexs', []))))
            key = cur.lastrowid
            for kind in CURSOR_KINDS:
                await self.db.execute(
                    "INSERT OR REPLACE INTO cursors (venue_account_id, kind, cursor, updated_at) VALUES (?, ?, ?, ?)",
                    (key, kind, str(now_ms), now_ms))
            await self.db.commit()
            return key, True
        key, was_active = row
        if int(was_active) != int(active):
            await self.db.execute("UPDATE venue_accounts SET active = ?, last_activity_ms = ? WHERE venue_account_id = ?",
                                  (int(active), now_ms if active else None, key))
            if active:   # re-activated: start again from now
                await self.db.execute("DELETE FROM snapshots WHERE venue_account_id = ?", (key,))
                for kind in CURSOR_KINDS:
                    await self.db.execute(
                        "INSERT OR REPLACE INTO cursors (venue_account_id, kind, cursor, updated_at) "
                        "VALUES (?, ?, ?, ?)", (key, kind, str(now_ms), now_ms))
            await self.db.commit()
        return key, False

    async def venue_accounts_of(self, address: str) -> list[dict]:
        """Every venue account row of a wallet: {key, venue, account_ref, active, last_activity_ms, dexs}."""
        cur = await self.db.execute(
            "SELECT va.venue_account_id, va.venue, va.account_ref, va.active, va.last_activity_ms, va.dexs_json "
            "FROM venue_accounts va JOIN wallets w USING (wallet_id) WHERE w.evm_address = ? "
            "ORDER BY va.venue, va.account_ref", (address.lower(),))
        return [{'key': r[0], 'venue': r[1], 'account_ref': r[2], 'active': bool(r[3]), 'last_activity_ms': r[4],
                 'dexs': json.loads(r[5] or '[]')} for r in await self._rows(cur)]

    async def tracked_venue_accounts(self, venue: str) -> list[dict]:
        """Active accounts of one venue with at least one subscriber: {key, account_ref, address, last_activity_ms}."""
        cur = await self.db.execute(
            "SELECT va.venue_account_id, va.account_ref, w.evm_address, va.last_activity_ms FROM venue_accounts va "
            "JOIN wallets w USING (wallet_id) WHERE va.venue = ? AND va.active = 1 "
            "AND EXISTS (SELECT 1 FROM subscriptions s WHERE s.wallet_id = va.wallet_id) "
            "ORDER BY va.venue_account_id", (venue,))
        return [{'key': r[0], 'account_ref': r[1], 'address': r[2], 'last_activity_ms': r[3]}
                for r in await self._rows(cur)]

    async def venue_of(self, venue_account_id: int) -> Optional[tuple[str, str, str]]:
        """(venue, account_ref, address) of a venue account."""
        cur = await self.db.execute(
            "SELECT va.venue, va.account_ref, w.evm_address FROM venue_accounts va JOIN wallets w USING (wallet_id) "
            "WHERE va.venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone(); await cur.close()
        return (row[0], row[1], row[2]) if row else None

    async def subscribed_wallets(self) -> list[str]:
        cur = await self.db.execute(
            "SELECT DISTINCT w.evm_address FROM wallets w JOIN subscriptions s USING (wallet_id) ORDER BY w.evm_address")
        rows = await cur.fetchall(); await cur.close()
        return [r[0] for r in rows]

    async def account_value_sum(self, address: str) -> Optional[str]:
        """Sum of the stored snapshot account values over the wallet's active venue accounts, Decimal text."""
        cur = await self.db.execute(
            "SELECT s.account_value FROM snapshots s JOIN venue_accounts va USING (venue_account_id) "
            "JOIN wallets w USING (wallet_id) WHERE w.evm_address = ? AND va.active = 1", (address.lower(),))
        values = [Decimal(r[0]) for r in await self._rows(cur) if r[0] is not None]
        return str(sum(values, Decimal(0))) if values else None

    async def touch_activity(self, venue_account_id: int, now_ms: int) -> None:
        await self.db.execute("UPDATE venue_accounts SET last_activity_ms = ? WHERE venue_account_id = ?",
                              (now_ms, venue_account_id))
        await self.db.commit()

    async def counts(self) -> dict:
        """Row counts for /health: active twaps and algos."""
        out = {}
        for name, table in (('twaps', 'twap_active'), ('algos', 'algo_active')):
            cur = await self.db.execute(f"SELECT COUNT(*) FROM {table}")
            (out[name],) = await cur.fetchone(); await cur.close()
        return out

    async def subscribers(self, venue_account_id: int) -> list[tuple[int, str]]:
        """[(user_id, alias)] subscribed to the venue account's wallet."""
        cur = await self.db.execute(
            "SELECT s.user_id, s.alias FROM subscriptions s "
            "JOIN venue_accounts va ON va.wallet_id = s.wallet_id "
            "WHERE va.venue_account_id = ?", (venue_account_id,))
        rows = await cur.fetchall(); await cur.close()
        return [(user_id, alias) for user_id, alias in rows]

    # Polling state (B6) -----------------------------------------------------

    async def get_snapshot(self, venue_account_id: int) -> Optional[dict]:
        """{dex: {coin: position}} (main dex key ""). Phase 0 rows ({coin: position}) are read as main dex."""
        cur = await self.db.execute(
            "SELECT positions_json FROM snapshots WHERE venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone(); await cur.close()
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
        row = await cur.fetchone(); await cur.close()
        return json.loads(row[0]) if row and row[0] else None

    async def backup(self, target_path: str) -> None:
        """Consistent copy of the live DB via the sqlite backup API (safe while WAL writers run)."""
        async with aiosqlite.connect(target_path) as target:
            await self.db.backup(target)

    async def hl_account_id(self, address: str) -> Optional[int]:
        cur = await self.db.execute(
            "SELECT venue_account_id FROM venue_accounts WHERE venue = ? AND account_ref = ?", (HYPERLIQUID, address))
        row = await cur.fetchone(); await cur.close()
        return row[0] if row else None

    async def get_dexs(self, venue_account_id: int) -> list[str]:
        cur = await self.db.execute(
            "SELECT dexs_json FROM venue_accounts WHERE venue_account_id = ?", (venue_account_id,))
        row = await cur.fetchone(); await cur.close()
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
        row = await cur.fetchone(); await cur.close()
        return row[0] if row else None

    async def get_cursor(self, venue_account_id: int, kind: str) -> Optional[str]:
        cur = await self.db.execute(
            "SELECT cursor FROM cursors WHERE venue_account_id = ? AND kind = ?", (venue_account_id, kind))
        row = await cur.fetchone(); await cur.close()
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
                 'payload': json.loads(r[4]), 'delivery': r[5]} for r in await self._rows(cur)]

    # Active TWAPs ------------------------------------------------------------

    async def active_twaps(self, venue_account_id: int) -> dict[str, dict]:
        """{twap_id: last known state} for the account."""
        cur = await self.db.execute(
            "SELECT twap_id, state_json FROM twap_active WHERE venue_account_id = ?", (venue_account_id,))
        rows = await cur.fetchall(); await cur.close()
        return {twap_id: json.loads(state) for twap_id, state in rows}

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
        r = await cur.fetchone(); await cur.close()
        return ({'event_id': r[0], 'dedupe_key': r[1], 'type': r[2], 'ts_ms': r[3],
                 'payload': json.loads(r[4]), 'delivery': r[5]} if r else None)

    async def get_event_by_key(self, dedupe_key: str) -> Optional[dict]:
        cur = await self.db.execute("SELECT event_id FROM events WHERE dedupe_key = ?", (dedupe_key,))
        row = await cur.fetchone(); await cur.close()
        return await self.get_event(row[0]) if row else None

    async def update_event_delivery(self, event_id: int, delivery: str) -> None:
        await self.db.execute("UPDATE events SET delivery = ? WHERE event_id = ?", (delivery, event_id))
        await self.db.commit()

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
                 'payload': json.loads(r[4]), 'delivery': r[5]} for r in await self._rows(cur)]

    async def last_event_ts(self, venue_account_id: int, event_type: str, coin: str) -> Optional[int]:
        """ts_ms of the newest event of this type for the coin (e.g. last POSITION_OPEN, for 'held')."""
        cur = await self.db.execute(
            "SELECT MAX(ts_ms) FROM events WHERE venue_account_id = ? AND type = ? "
            "AND json_extract(payload_json, '$.coin') = ?", (venue_account_id, event_type, coin))
        row = await cur.fetchone(); await cur.close()
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
        rows = await cur.fetchall(); await cur.close()
        return {u: (c, m) for u, c, m in rows}

    # Synthetic TWAP (algo) state ----------------------------------------------

    async def active_algos(self, venue_account_id: int) -> dict[tuple[str, int], dict]:
        cur = await self.db.execute(
            "SELECT coin, sign, started_ms, last_fill_ms, fills_count, total_sz, total_ntl "
            "FROM algo_active WHERE venue_account_id = ?", (venue_account_id,))
        return {(coin, sign): {'coin': coin, 'sign': sign, 'started_ms': started, 'last_fill_ms': last,
                               'fills_count': count, 'total_sz': sz, 'total_ntl': ntl}
                for coin, sign, started, last, count, sz, ntl in await self._rows(cur)}

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
