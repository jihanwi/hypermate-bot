"""Database access. All SQL lives here."""

import logging
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

ADDED = 'added'
ALIAS_EXISTS = 'alias_exists'
ADDRESS_EXISTS = 'address_exists'


class Repo:
    def __init__(self, path: str) -> None:
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS tracked_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                wallet_address TEXT,
                alias TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await self.db.execute(
            'CREATE INDEX IF NOT EXISTS idx_tracked_wallets_user_id ON tracked_wallets(user_id)')
        await self.db.execute(
            'CREATE INDEX IF NOT EXISTS idx_tracked_wallets_address ON tracked_wallets(wallet_address)')
        await self.db.commit()
        logger.info(f"Database initialized at {self.path}")

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def add_subscription(self, user_id: int, address: str, alias: str, now_ms: int) -> str:
        cur = await self.db.execute(
            "SELECT 1 FROM tracked_wallets WHERE user_id = ? AND alias = ?", (str(user_id), alias))
        if await cur.fetchone():
            return ALIAS_EXISTS
        cur = await self.db.execute(
            "SELECT 1 FROM tracked_wallets WHERE user_id = ? AND wallet_address = ?", (str(user_id), address))
        if await cur.fetchone():
            return ADDRESS_EXISTS
        await self.db.execute(
            "INSERT INTO tracked_wallets (user_id, wallet_address, alias) VALUES (?, ?, ?)",
            (str(user_id), address, alias))
        await self.db.commit()
        return ADDED

    async def remove_subscription(self, user_id: int, alias: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM tracked_wallets WHERE user_id = ? AND alias = ?", (str(user_id), alias))
        await self.db.commit()
        return cur.rowcount > 0

    async def list_subscriptions(self, user_id: int) -> list[tuple[str, str]]:
        """[(alias, address)] ordered by alias."""
        cur = await self.db.execute(
            "SELECT alias, wallet_address FROM tracked_wallets WHERE user_id = ? ORDER BY alias",
            (str(user_id),))
        return [(alias, address) for alias, address in await cur.fetchall()]

    async def find_subscription(self, user_id: int, alias: str) -> Optional[tuple[str, str]]:
        """(alias, address) for the user's alias, or None."""
        cur = await self.db.execute(
            "SELECT alias, wallet_address FROM tracked_wallets WHERE user_id = ? AND alias = ?",
            (str(user_id), alias))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def tracked_accounts(self) -> list[tuple[str, str]]:
        """[(account key, address)] for every wallet with at least one subscriber, each once."""
        cur = await self.db.execute("SELECT DISTINCT wallet_address FROM tracked_wallets")
        return [(address, address) for (address,) in await cur.fetchall()]

    async def subscribers(self, account_key: str) -> list[tuple[int, str]]:
        """[(user_id, alias)] subscribed to the account."""
        cur = await self.db.execute(
            "SELECT user_id, alias FROM tracked_wallets WHERE wallet_address = ?", (account_key,))
        return [(int(user_id), alias) for user_id, alias in await cur.fetchall()]
