"""Hyperliquid info API client.

All numbers are parsed as Decimal (JSON floats included), never float.
"""

import functools
import json
import logging
import time
from decimal import Decimal
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

_json_loads = functools.partial(json.loads, parse_float=Decimal)


class HyperliquidAPIError(Exception):
    """Non-200 response or transport failure from the Hyperliquid info API."""


class HyperliquidClient:
    """Thin wrapper over POST /info. One aiohttp session per process."""

    def __init__(self, base_url: str, spot_meta_ttl_sec: int = 3600) -> None:
        self.base_url = base_url.rstrip('/')
        self.spot_meta_ttl_sec = spot_meta_ttl_sec
        self._session: Optional[aiohttp.ClientSession] = None
        self._spot_names: dict[str, str] = {}
        self._spot_names_fetched_at = 0.0

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _info(self, payload: dict) -> Any:
        await self.start()
        url = f"{self.base_url}/info"
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status != 200:
                    raise HyperliquidAPIError(f"{payload['type']} returned HTTP {response.status}")
                return await response.json(loads=_json_loads)
        except aiohttp.ClientError as e:
            raise HyperliquidAPIError(f"{payload['type']} failed: {e}") from e

    async def clearinghouse_state(self, user: str) -> dict:
        return await self._info({"type": "clearinghouseState", "user": user})

    async def spot_clearinghouse_state(self, user: str) -> dict:
        return await self._info({"type": "spotClearinghouseState", "user": user})

    async def user_fills_by_time(self, user: str, start_time: int) -> list:
        """Fills with time >= start_time (ms). Replaces userFills, which ignores startTime (B10)."""
        data = await self._info({"type": "userFillsByTime", "user": user, "startTime": start_time})
        return data if isinstance(data, list) else []

    async def ledger_updates(self, user: str, start_time: int) -> list:
        """Non-funding ledger updates (deposits, withdrawals, transfers) with time >= start_time (ms)."""
        data = await self._info({"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_time})
        return data if isinstance(data, list) else []

    async def portfolio(self, user: str) -> list:
        data = await self._info({"type": "portfolio", "user": user})
        return data if isinstance(data, list) else []

    async def spot_meta(self) -> dict:
        return await self._info({"type": "spotMeta"})

    async def spot_display_name(self, coin: str) -> str:
        """Display name for a spot fill coin ("PURR/USDC" or "@107"), cached for spot_meta_ttl_sec.

        Falls back to the raw coin string if spotMeta is unavailable or has no entry.
        """
        if time.monotonic() - self._spot_names_fetched_at > self.spot_meta_ttl_sec:
            try:
                self._spot_names = build_spot_names(await self.spot_meta())
                self._spot_names_fetched_at = time.monotonic()
            except HyperliquidAPIError as e:
                logger.warning(f"spotMeta unavailable, showing raw spot coin names: {e}")
        return self._spot_names.get(coin, coin)


def build_spot_names(spot_meta: dict) -> dict[str, str]:
    """Map spot fill coin -> display name from a spotMeta response.

    A pair is addressed in fills either by its name ("PURR/USDC") or by "@{index}".
    Display is the base token name when the quote is USDC, otherwise "BASE/QUOTE".
    """
    token_names = {t['index']: t['name'] for t in spot_meta.get('tokens', [])}
    names = {}
    for pair in spot_meta.get('universe', []):
        base_idx, quote_idx = pair['tokens'][0], pair['tokens'][1]
        base = token_names.get(base_idx, pair['name'])
        quote = token_names.get(quote_idx, '')
        display = base if quote == 'USDC' else f"{base}/{quote}"
        names[f"@{pair['index']}"] = display
        names[pair['name']] = display
    return names
