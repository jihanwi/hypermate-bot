"""Aster Chain public JSON-RPC (spec 6.4): POST https://tapi.asterdex.com/info, no auth.

Methods used: aster_getBalance [address, "latest"], aster_userFills [address, symbol|null, from, to,
"latest"] (7-day window, at most 1000 fills), aster_openOrders [address, symbol|"", "latest"].
Docs: github.com/asterdex/api-docs/blob/master/RPC/aster-chain-rpc.md (weight 1 per call, no
published per-minute limit). The venue bucket starts at ASTER_REQ_BUDGET per minute and halves
on every 429.
"""

import functools
import json
import logging
from decimal import Decimal
from typing import Any, Optional

import aiohttp

from hypermate.venues.hyperliquid.scheduler import WeightBudget

logger = logging.getLogger(__name__)

_json_loads = functools.partial(json.loads, parse_float=Decimal)
REQUEST_WEIGHT = 1
FILLS_WINDOW_MS = 7 * 24 * 3600 * 1000


class AsterAPIError(Exception):
    """Non-200 response, JSON-RPC error, or transport failure."""


class AsterRateLimited(AsterAPIError):
    """HTTP 429."""


class AsterClient:
    def __init__(self, url: str, budget: Optional[WeightBudget] = None) -> None:
        self.url = url
        self.budget = budget
        self._session: Optional[aiohttp.ClientSession] = None
        self._id = 0

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, payload: dict) -> tuple[int, Any]:
        """(status, parsed body or None). Separate so tests can swap the transport."""
        await self.start()
        async with self._session.post(self.url, json=payload) as response:
            try:
                body = await response.json(loads=_json_loads, content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                body = None
            return response.status, body

    async def call(self, method: str, params: list, priority: int) -> Any:
        """One JSON-RPC call; returns `result`."""
        self._id += 1
        payload = {'jsonrpc': '2.0', 'id': self._id, 'method': method, 'params': params}
        if self.budget is not None:
            await self.budget.acquire(REQUEST_WEIGHT, priority)
        try:
            status, body = await self._post(payload)
        except aiohttp.ClientError as e:
            raise AsterAPIError(f"{method} failed: {e}") from e
        if status == 429:
            if self.budget is not None:
                self.budget.rate_limited(None)
                self.budget.halve()
            raise AsterRateLimited(f"{method} returned HTTP 429")
        if status != 200 or not isinstance(body, dict):
            raise AsterAPIError(f"{method} returned HTTP {status}")
        if body.get('error'):
            raise AsterAPIError(f"{method} error: {body['error']}")
        return body.get('result')

    async def get_balance(self, address: str, priority: int = 0) -> dict:
        result = await self.call('aster_getBalance', [address, 'latest'], priority)
        return result if isinstance(result, dict) else {}

    async def user_fills(self, address: str, from_ms: Optional[int], to_ms: Optional[int],
                         symbol: Optional[str] = None, priority: int = 2) -> dict:
        """{accountPrivacy, startTime, endTime, fills: [{symbol, side, price, qty, time}]}."""
        result = await self.call('aster_userFills', [address, symbol, from_ms, to_ms, 'latest'], priority)
        return result if isinstance(result, dict) else {'fills': []}

    async def open_orders(self, address: str, symbol: str = '', priority: int = 2) -> dict:
        result = await self.call('aster_openOrders', [address, symbol, 'latest'], priority)
        return result if isinstance(result, dict) else {}
