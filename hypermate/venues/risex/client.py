"""RISEx public REST API (spec 6.3): https://api.rise.trade, no auth, 500 req / 10 s / IP.

Numbers in /v1/positions are 18-decimal fixed-point strings; /v1/trade-history and the
cross-margin balance are human units (PM live check 2026-10-04). Parsing is in adapter.py.
"""

import functools
import json
import logging
import time
from decimal import Decimal
from typing import Any, Optional

import aiohttp

from hypermate.venues.hyperliquid.scheduler import WeightBudget

logger = logging.getLogger(__name__)

_json_loads = functools.partial(json.loads, parse_float=Decimal)
REQUEST_WEIGHT = 1


class RisexAPIError(Exception):
    """Non-200 response or transport failure."""


class RisexRateLimited(RisexAPIError):
    """HTTP 429."""


class RisexClient:
    def __init__(self, base_url: str, budget: Optional[WeightBudget] = None, markets_ttl_sec: int = 30) -> None:
        """markets_ttl_sec is short (30 s) because /v1/markets also carries the mark prices used for
        notional and unrealized PnL; the symbol and step mapping rides along."""
        self.base_url = base_url.rstrip('/')
        self.budget = budget
        self.markets_ttl_sec = markets_ttl_sec
        self._session: Optional[aiohttp.ClientSession] = None
        self._markets: dict[str, dict] = {}
        self._markets_fetched_at: Optional[float] = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _fetch(self, path: str, params: dict) -> tuple[int, Any]:
        """(status, parsed body or None). Separate so tests can swap the transport."""
        await self.start()
        async with self._session.get(f"{self.base_url}{path}", params=params) as response:
            try:
                body = await response.json(loads=_json_loads, content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                body = None
            return response.status, body

    async def _get(self, path: str, params: dict, priority: int) -> Any:
        if self.budget is not None:
            await self.budget.acquire(REQUEST_WEIGHT, priority)
        try:
            status, body = await self._fetch(path, params)
        except aiohttp.ClientError as e:
            raise RisexAPIError(f"GET {path} failed: {e}") from e
        if status == 429:
            if self.budget is not None:
                self.budget.rate_limited(None)
            raise RisexRateLimited(f"GET {path} returned HTTP 429")
        if status != 200:
            raise RisexAPIError(f"GET {path} returned HTTP {status}")
        return (body or {}).get('data') if isinstance(body, dict) else body

    async def positions(self, account: str, page: int = 1, page_size: int = 100, priority: int = 0) -> dict:
        """{positions: [...], total_count, page, has_next_page} (sizes 18-dec)."""
        data = await self._get('/v1/positions', {'account': account, 'page': str(page), 'page_size': str(page_size)},
                               priority)
        return data if isinstance(data, dict) else {'positions': []}

    async def cross_margin_balance(self, account: str, priority: int = 0) -> Optional[Decimal]:
        """Human-unit balance, or None when the API answers an error (unknown accounts give 500)."""
        try:
            data = await self._get('/v1/account/cross-margin-balance', {'account': account}, priority)
        except RisexRateLimited:
            raise
        except RisexAPIError as e:
            logger.info(f"RISEx cross-margin-balance unavailable for {account}: {e}")
            return None
        value = (data or {}).get('balance') if isinstance(data, dict) else None
        return Decimal(str(value)) if value is not None else None

    async def trade_history(self, account: str, limit: int = 100, market_id: Optional[str] = None,
                            priority: int = 2) -> list[dict]:
        """Newest first, human units, time in ns."""
        params = {'account': account, 'limit': str(limit)}
        if market_id:
            params['market_id'] = str(market_id)
        data = await self._get('/v1/trade-history', params, priority)
        return list((data or {}).get('trades') or []) if isinstance(data, dict) else []

    async def markets(self, priority: int = 1) -> dict[str, dict]:
        """market_id -> {name, step_size, ...} from /v1/markets, cached for markets_ttl_sec."""
        if (self._markets_fetched_at is None
                or time.monotonic() - self._markets_fetched_at > self.markets_ttl_sec):
            data = await self._get('/v1/markets', {}, priority)
            self._markets = {}
            for market in (data or {}).get('markets') or []:
                config = market.get('config') or {}
                self._markets[str(market.get('market_id'))] = {
                    'name': config.get('name') or market.get('display_name') or str(market.get('market_id')),
                    'step_size': config.get('step_size'), 'step_price': config.get('step_price'),
                    'max_leverage': config.get('max_leverage'), 'mark_price': market.get('mark_price'),
                }
            self._markets_fetched_at = time.monotonic()
        return dict(self._markets)
