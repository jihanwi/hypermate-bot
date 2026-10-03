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

from hypermate.venues.hyperliquid.scheduler import WeightBudget, item_weight, request_cost

logger = logging.getLogger(__name__)

_json_loads = functools.partial(json.loads, parse_float=Decimal)


class HyperliquidAPIError(Exception):
    """Non-200 response or transport failure from the Hyperliquid info API."""


class HyperliquidRateLimited(HyperliquidAPIError):
    """HTTP 429. The weight budget is already paused when this is raised."""


def _retry_after(headers) -> Optional[int]:
    """Retry-After in whole seconds (the delay-seconds form; a date form is ignored)."""
    try:
        return int(headers.get('Retry-After'))
    except (TypeError, ValueError):
        return None


class HyperliquidClient:
    """Thin wrapper over POST /info. One aiohttp session per process."""

    def __init__(self, base_url: str, spot_meta_ttl_sec: int = 3600, perp_dexs_ttl_sec: int = 3600,
                 budget: Optional[WeightBudget] = None) -> None:
        self.base_url = base_url.rstrip('/')
        self.budget = budget
        self.spot_meta_ttl_sec = spot_meta_ttl_sec
        self.perp_dexs_ttl_sec = perp_dexs_ttl_sec
        self._perp_dexs: list[str] = []
        self._perp_dexs_fetched_at: Optional[float] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._spot_names: dict[str, str] = {}
        self._spot_names_fetched_at: Optional[float] = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, payload: dict) -> tuple[int, Any, Any]:
        """(status, headers, parsed body or None). Separate so tests can swap the transport."""
        await self.start()
        async with self._session.post(f"{self.base_url}/info", json=payload) as response:
            body = await response.json(loads=_json_loads) if response.status == 200 else None
            return response.status, response.headers, body

    async def _info(self, payload: dict, priority: Optional[int] = None, meter: Optional[dict] = None) -> Any:
        """One info request, paced by the weight budget (spec 3.5).

        priority overrides the request type's default; meter ({type: weight}) accumulates the
        weight this request cost, per-item part included (used by /related for its 300 cap).
        """
        request_type = payload['type']
        weight, default_priority = request_cost(request_type)
        if priority is None:
            priority = default_priority
        if self.budget is not None:
            await self.budget.acquire(weight, priority)
        if meter is not None:
            meter[request_type] = meter.get(request_type, 0) + weight
        try:
            status, headers, body = await self._post(payload)
        except aiohttp.ClientError as e:
            raise HyperliquidAPIError(f"{request_type} failed: {e}") from e
        if status == 429:
            if self.budget is not None:
                self.budget.rate_limited(_retry_after(headers))
            raise HyperliquidRateLimited(f"{request_type} returned HTTP 429")
        if status != 200:
            raise HyperliquidAPIError(f"{request_type} returned HTTP {status}")
        if isinstance(body, list):
            extra = item_weight(request_type, len(body))
            if self.budget is not None:
                self.budget.charge(extra, priority)
            if meter is not None and extra:
                meter[request_type] += extra
        return body

    async def clearinghouse_state(self, user: str, dex: str = '', **opts) -> dict:
        """Positions and margin of one perp dex; "" is the main dex (no dex param, spec 5.1 HIP-3)."""
        payload = {"type": "clearinghouseState", "user": user}
        if dex:
            payload["dex"] = dex
        return await self._info(payload, **opts)

    async def perp_dexs(self) -> list[str]:
        """HIP-3 builder dex names (the main dex, null in the response, is left out). Cached."""
        if (self._perp_dexs_fetched_at is None
                or time.monotonic() - self._perp_dexs_fetched_at > self.perp_dexs_ttl_sec):
            data = await self._info({"type": "perpDexs"})
            self._perp_dexs = [d['name'] if isinstance(d, dict) else d for d in data or [] if d]
            self._perp_dexs_fetched_at = time.monotonic()
        return list(self._perp_dexs)

    async def spot_clearinghouse_state(self, user: str) -> dict:
        return await self._info({"type": "spotClearinghouseState", "user": user})

    async def user_fills_by_time(self, user: str, start_time: int) -> list:
        """Fills with time >= start_time (ms). Replaces userFills, which ignores startTime (B10)."""
        data = await self._info({"type": "userFillsByTime", "user": user, "startTime": start_time})
        return data if isinstance(data, list) else []

    async def ledger_updates(self, user: str, start_time: int, **opts) -> list:
        """Non-funding ledger updates (deposits, withdrawals, transfers) with time >= start_time (ms)."""
        data = await self._info({"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_time},
                                **opts)
        return data if isinstance(data, list) else []

    # /related discovery (spec 7.1). All take priority= and meter= (see _info). ---------------

    async def user_role(self, user: str, **opts) -> dict:
        """{role: user|agent|vault|subAccount|missing, data?: {master | user}} (weight 60, cache it)."""
        data = await self._info({"type": "userRole", "user": user}, **opts)
        return data if isinstance(data, dict) else {}

    async def sub_accounts(self, user: str, **opts) -> list:
        """[{name, subAccountUser, master, clearinghouseState, ...}] when user is a master, else []."""
        data = await self._info({"type": "subAccounts", "user": user}, **opts)
        return data if isinstance(data, list) else []

    async def extra_agents(self, user: str, **opts) -> list:
        data = await self._info({"type": "extraAgents", "user": user}, **opts)
        return data if isinstance(data, list) else []

    async def user_fees(self, user: str, **opts) -> dict:
        data = await self._info({"type": "userFees", "user": user}, **opts)
        return data if isinstance(data, dict) else {}

    async def referral(self, user: str, **opts) -> dict:
        data = await self._info({"type": "referral", "user": user}, **opts)
        return data if isinstance(data, dict) else {}

    async def user_vault_equities(self, user: str, **opts) -> list:
        data = await self._info({"type": "userVaultEquities", "user": user}, **opts)
        return data if isinstance(data, list) else []

    async def vault_details(self, vault_address: str, **opts) -> dict:
        data = await self._info({"type": "vaultDetails", "vaultAddress": vault_address}, **opts)
        return data if isinstance(data, dict) else {}

    async def portfolio(self, user: str) -> list:
        data = await self._info({"type": "portfolio", "user": user})
        return data if isinstance(data, list) else []

    async def web_data2(self, user: str, **opts) -> dict:
        """Frontend endpoint; twapStates lists the user's active TWAPs (main dex only, spec 5.1)."""
        return await self._info({"type": "webData2", "user": user}, **opts)

    async def twap_history(self, user: str) -> list:
        """[{time (s), state, status: {status, description?}, twapId}], newest first."""
        data = await self._info({"type": "twapHistory", "user": user})
        return data if isinstance(data, list) else []

    async def spot_meta(self) -> dict:
        return await self._info({"type": "spotMeta"})

    async def spot_display_name(self, coin: str) -> str:
        """Display name for a spot fill coin ("PURR/USDC" or "@107"), cached for spot_meta_ttl_sec.

        Falls back to the raw coin string if spotMeta is unavailable or has no entry.
        """
        if (self._spot_names_fetched_at is None
                or time.monotonic() - self._spot_names_fetched_at > self.spot_meta_ttl_sec):
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
