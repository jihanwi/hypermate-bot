"""Lighter public REST API (spec 6.2): https://mainnet.zklighter.elliot.ai/api/v1, no auth.

Public limit: 60 requests per minute per IP and L1 address, no rate limit headers in the
responses (PM live check 2026-10-04). Every request costs 1 in the Lighter bucket.
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


class LighterAPIError(Exception):
    """Non-200 response, API-level error code, or transport failure."""


class LighterRateLimited(LighterAPIError):
    """HTTP 429."""


class LighterClient:
    def __init__(self, base_url: str, budget: Optional[WeightBudget] = None, order_books_ttl_sec: int = 3600) -> None:
        self.base_url = base_url.rstrip('/')
        self.budget = budget
        self.order_books_ttl_sec = order_books_ttl_sec
        self._session: Optional[aiohttp.ClientSession] = None
        self._symbols: dict[int, str] = {}
        self._symbols_fetched_at: Optional[float] = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _fetch(self, path: str, params: dict) -> tuple[int, Any]:
        """(status, parsed body or None). A body that is not JSON (the 429 page is HTML, PM live check)
        is returned as None. Separate so tests can swap the transport."""
        await self.start()
        async with self._session.get(f"{self.base_url}{path}", params=params) as response:
            try:
                body = await response.json(loads=_json_loads, content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                body = None
            return response.status, body

    async def _get(self, path: str, params: dict, priority: int, meter: Optional[dict] = None) -> Any:
        if self.budget is not None:
            await self.budget.acquire(REQUEST_WEIGHT, priority)
        if meter is not None:
            meter[path] = meter.get(path, 0) + REQUEST_WEIGHT
        try:
            status, body = await self._fetch(path, params)
        except aiohttp.ClientError as e:
            raise LighterAPIError(f"GET {path} failed: {e}") from e
        # Lighter's 429 carries a non-JSON body and no headers; any non-200 or unparsable answer is
        # treated as rate limiting (owner decision 2026-10-04) and pauses the venue bucket.
        if status != 200 or body is None:
            if self.budget is not None:
                self.budget.rate_limited(None)
            raise LighterRateLimited(f"GET {path} returned HTTP {status}" + (" (non-JSON body)" if body is None else ""))
        if isinstance(body, dict) and body.get('code') not in (None, 200, 0):
            raise LighterAPIError(f"GET {path} returned code {body.get('code')}: {body.get('message', '')}")
        return body

    async def accounts_by_l1_address(self, l1_address: str, priority: int = 3) -> list[dict]:
        """sub_accounts of an L1 address ([] when the address has no Lighter account)."""
        try:
            body = await self._get('/accountsByL1Address', {'l1_address': l1_address}, priority)
        except LighterAPIError as e:
            # the API answers an error code for unknown addresses; that is "no account", not a failure
            if 'code' in str(e):
                logger.info(f"Lighter: no account for {l1_address}: {e}")
                return []
            raise
        return list((body or {}).get('sub_accounts') or [])

    async def account(self, index: int, priority: int = 0) -> Optional[dict]:
        """accounts[0] of GET /account?by=index&value=<index>, None when missing."""
        body = await self._get('/account', {'by': 'index', 'value': str(index)}, priority)
        accounts = (body or {}).get('accounts') or []
        return accounts[0] if accounts else None

    async def trades(self, index: int, cursor: Optional[str] = None, limit: int = 100,
                     priority: int = 2) -> tuple[list[dict], Optional[str]]:
        """Newest-first trades of an account and the API's next_cursor (older page)."""
        params = {'sort_by': 'timestamp', 'limit': str(limit), 'account_index': str(index)}
        if cursor:
            params['cursor'] = cursor
        body = await self._get('/trades', params, priority)
        return list((body or {}).get('trades') or []), (body or {}).get('next_cursor')

    async def symbols(self, priority: int = 1) -> dict[int, str]:
        """market_id -> symbol from GET /orderBooks, cached for order_books_ttl_sec."""
        if (self._symbols_fetched_at is None
                or time.monotonic() - self._symbols_fetched_at > self.order_books_ttl_sec):
            body = await self._get('/orderBooks', {}, priority)
            self._symbols = {int(b['market_id']): str(b['symbol']) for b in (body or {}).get('order_books') or []}
            self._symbols_fetched_at = time.monotonic()
        return dict(self._symbols)
