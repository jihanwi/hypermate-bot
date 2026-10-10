"""Arcus public REST (spec 6.5): GET https://api.arcus.xyz/v1/{account,markets,fills}, no auth.

Weights (per IP, 1,500 per minute): account 2, fills / funding pages 20, markets 20 (assumed, cached
30 s). The venue bucket starts at ARCUS_REQ_BUDGET (1,200); a 429 pauses the queue for the Retry-After
or 30 s. Status meanings on /v1/account (PM 2026-10-10): 403 = address not on the access whitelist (not
an Arcus user), 404 = no activity on that accountIndex, 400 = missing address.
"""

import functools
import json
import logging
import time
from decimal import Decimal
from typing import Any, Optional

import aiohttp

from hypermate.venues.hyperliquid.scheduler import P_SNAPSHOT, WeightBudget

logger = logging.getLogger(__name__)

_json_loads = functools.partial(json.loads, parse_float=Decimal)
WEIGHT_ACCOUNT = 2
WEIGHT_PAGE = 20
PAGE_SIZE = 1000
MAX_ACCOUNT_INDEX = 9


class ArcusAPIError(Exception):
    """Non-200 response or transport failure."""


class ArcusRateLimited(ArcusAPIError):
    """HTTP 429."""


class ArcusNotWhitelisted(ArcusAPIError):
    """HTTP 403: the address is not an Arcus user (resolve answers ✗)."""


class ArcusNoActivity(ArcusAPIError):
    """HTTP 404: nothing on that accountIndex."""


class ArcusClient:
    def __init__(self, url: str, budget: Optional[WeightBudget] = None, markets_ttl_sec: int = 30) -> None:
        self.url = url.rstrip('/')
        self.budget = budget
        self.markets_ttl_sec = markets_ttl_sec
        self._session: Optional[aiohttp.ClientSession] = None
        self._markets: Optional[dict[str, dict]] = None
        self._markets_at = 0.0

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _fetch(self, path: str, params: dict) -> tuple[int, Any, dict]:
        """(status, parsed body or None, headers). Separate so tests can swap the transport."""
        await self.start()
        async with self._session.get(f"{self.url}{path}", params=params) as response:
            try:
                body = await response.json(loads=_json_loads, content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                body = None
            return response.status, body, dict(response.headers)

    async def _get(self, path: str, params: dict, weight: int, priority: int) -> Any:
        if self.budget is not None:
            await self.budget.acquire(weight, priority)
        try:
            status, body, headers = await self._fetch(path, params)
        except aiohttp.ClientError as e:
            raise ArcusAPIError(f"GET {path} failed: {e}") from e
        if status == 429:
            retry = headers.get('Retry-After')
            if self.budget is not None:
                self.budget.rate_limited(int(retry) if retry and str(retry).isdigit() else None)
            raise ArcusRateLimited(f"GET {path} returned HTTP 429")
        if status == 403:
            raise ArcusNotWhitelisted(f"GET {path}: {(body or {}).get('error') if isinstance(body, dict) else 'forbidden'}")
        if status == 404:
            raise ArcusNoActivity(f"GET {path}: no activity")
        if status != 200 or body is None:
            raise ArcusAPIError(f"GET {path} returned HTTP {status}")
        return body

    async def account(self, address: str, index: int, priority: int = P_SNAPSHOT) -> dict:
        """/v1/account for one accountIndex. Raises ArcusNotWhitelisted (403) / ArcusNoActivity (404)."""
        body = await self._get('/v1/account', {'address': address.lower(), 'accountIndex': int(index)},
                               WEIGHT_ACCOUNT, priority)
        return body if isinstance(body, dict) else {}

    async def fills(self, address: str, index: int, from_us: Optional[int] = None, to_us: Optional[int] = None,
                    limit: int = PAGE_SIZE, priority: int = 2) -> list[dict]:
        """One page of /v1/fills, newest first (at most `limit`)."""
        params: dict = {'address': address.lower(), 'accountIndex': int(index), 'limit': int(limit)}
        if from_us is not None:
            params['from'] = int(from_us)
        if to_us is not None:
            params['to'] = int(to_us)
        body = await self._get('/v1/fills', params, WEIGHT_PAGE, priority)
        rows = body.get('fills') if isinstance(body, dict) else body
        return list(rows or [])

    async def markets(self, priority: int = 1) -> dict[str, dict]:
        """{marketDisplayName: {market_id, name, base, tick_size, step_size, tick_tiers, status}}, cached."""
        if self._markets is not None and time.monotonic() - self._markets_at < self.markets_ttl_sec:
            return self._markets
        body = await self._get('/v1/markets', {}, WEIGHT_PAGE, priority)
        rows = body.get('markets') if isinstance(body, dict) else body
        out = {}
        for market in rows or []:
            name = str(market.get('marketDisplayName') or market.get('name') or '')
            if not name:
                continue
            out[name] = {'market_id': market.get('marketId'), 'name': name, 'base': market.get('baseAsset'),
                         'tick_size': market.get('tickSize'), 'step_size': market.get('stepSize'),
                         'tick_tiers': market.get('tickTiers') or [], 'status': market.get('status')}
        self._markets, self._markets_at = out, time.monotonic()
        return out
