"""Aster VenueAdapter (spec 6.4): aster_getBalance for resolve and snapshot, aster_userFills for fills.

Shapes from the RPC docs (an active address was not recorded by the PM; fixture aster_getBalance.json
is an inactive address with no perpAssets / positions keys at all [?]):
  result.accountPrivacy "disabled" | "enabled"; result.perpAssets[] {asset, walletBalance};
  result.positions[] {tradingProduct, positions[] {symbol, positionAmount, entryPrice, unrealizedProfit,
  notionalValue, markPrice, leverage, isolated, positionSide, marginValue}}.
  userFills: fills[] {symbol, side, price, qty, time (ms)}, no ids and no PnL.
"""

import json
import logging
from decimal import Decimal
from typing import Optional

from hypermate.core.numbers import to_decimal
from hypermate.venues import base
from hypermate.venues.aster.client import FILLS_WINDOW_MS, AsterClient, AsterNotFound
from hypermate.venues.base import AccountSnapshot, VenueAccount, position_entry
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid import scheduler
from hypermate.venues.risex.adapter import accumulate

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
EXPLORER_FALLBACK = 'https://www.asterdex.com/'   # Aster Chain explorer address format not confirmed [?]
QUOTES = ('USDT', 'USDC', 'USD1', 'USD')
PRIVACY_ON, PRIVACY_OFF = 'enabled', 'disabled'


def coin_of(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC'."""
    for quote in QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)]
    return symbol


def parse_positions(result: dict) -> dict[str, dict]:
    """Perp positions keyed by coin; positionAmount is signed (negative = short), zero rows skipped."""
    positions = {}
    for group in result.get('positions') or []:
        for pos in group.get('positions') or []:
            amount = to_decimal(pos.get('positionAmount')) or ZERO
            if amount == 0:
                continue
            if str(pos.get('positionSide', '')).upper() == 'SHORT' and amount > 0:
                amount = -amount
            coin = coin_of(str(pos.get('symbol', '')))
            positions[coin] = position_entry(coin, amount, to_decimal(pos.get('entryPrice')),
                                             to_decimal(pos.get('notionalValue')), to_decimal(pos.get('unrealizedProfit')))
            positions[coin]['leverage'] = str(pos.get('leverage', ''))
    return positions


def wallet_balance(result: dict) -> Optional[Decimal]:
    assets = result.get('perpAssets')
    if not assets:
        return None
    return sum((to_decimal(a.get('walletBalance')) or ZERO for a in assets), ZERO)


def parse_snapshot(result: dict) -> AccountSnapshot:
    positions = parse_positions(result)
    balance = wallet_balance(result)
    unrealized = sum((to_decimal(p.get('unrealized_pnl')) or ZERO for p in positions.values()), ZERO)
    value = (balance + unrealized) if balance is not None else None
    return AccountSnapshot(positions, value, raw=result,
                           extra={'privacy': str(result.get('accountPrivacy') or PRIVACY_OFF),
                                  'wallet_balance': str(balance) if balance is not None else None})


def is_active(result: dict) -> bool:
    return bool(parse_positions(result)) or (wallet_balance(result) or ZERO) > 0


def fill_to_hl(fill: dict, index: int) -> dict:
    """userFills entry -> HL fill. No ids on Aster: oid groups the fills of one (ms, symbol, side)
    and tid adds the index; startPosition is set later by accumulate()."""
    side = 'B' if str(fill.get('side', '')).upper() == 'BUY' else 'A'
    coin = coin_of(str(fill.get('symbol', '')))
    time_ms = int(fill.get('time') or 0)
    key = f"{time_ms}:{coin}:{side}"
    return {'coin': coin, 'px': str(to_decimal(fill.get('price')) or ZERO), 'sz': str(to_decimal(fill.get('qty')) or ZERO),
            'side': side, 'time': time_ms, 'startPosition': '0', 'dir': '', 'oid': key, 'tid': f"{key}:{index}",
            'closedPnl': None, 'fee': '0', 'feeToken': 'USDT', 'crossed': True}


def encode_cursor(last_ms: int) -> str:
    return json.dumps({'ms': int(last_ms)})


def decode_cursor(cursor: Optional[str]) -> Optional[int]:
    if not cursor:
        return None
    try:
        return int(json.loads(cursor).get('ms', 0))
    except (ValueError, AttributeError, TypeError):
        return None                  # a new account's ms cursor: baseline


class AsterAdapter:
    venue = base.ASTER

    def __init__(self, client: AsterClient) -> None:
        self.client = client

    async def resolve(self, evm_address: str) -> list[VenueAccount]:
        address = evm_address.lower()
        try:
            result = await self.client.get_balance(address, priority=scheduler.P_LEDGER)
        except AsterNotFound as e:
            logger.info(f"Aster: no account for {address}: {e}")
            return []                      # "✗" in the /add summary; any other failure stays an error ("?")
        if not is_active(result):
            return []
        return [VenueAccount(self.venue, address, address, meta={'privacy': result.get('accountPrivacy')})]

    async def snapshot(self, account: VenueAccount) -> AccountSnapshot:
        return parse_snapshot(await self.client.get_balance(account.account_ref, priority=scheduler.P_SNAPSHOT))

    async def fetch_events(self, account: VenueAccount, cursor: Optional[str],
                           positions_before: Optional[dict] = None) -> tuple[list[dict], Optional[str]]:
        """Fills after the cursor (ms), oldest first; the first poll only sets the cursor to now."""
        last_ms = decode_cursor(cursor)
        now = hl_adapter.now_ms()
        if last_ms is None:
            return [], encode_cursor(now)
        to_ms = min(last_ms + FILLS_WINDOW_MS, now)
        result = await self.client.user_fills(account.account_ref, last_ms + 1, to_ms, priority=scheduler.P_FILLS)
        raw = sorted((f for f in result.get('fills') or [] if int(f.get('time') or 0) > last_ms),
                     key=lambda f: int(f.get('time') or 0))
        fills = accumulate([fill_to_hl(f, i) for i, f in enumerate(raw)], positions_before or {})
        newest = max([last_ms] + [f['time'] for f in fills])
        if not fills and to_ms < now:
            newest = to_ms                         # empty 7-day window: move on
        return fills, encode_cursor(newest)

    def explorer_url(self, account: VenueAccount) -> str:
        return EXPLORER_FALLBACK

    def cost(self, op: str) -> int:
        return 1

    async def health(self) -> dict:
        budget = self.client.budget
        detail = f"REST polling, bucket {budget.per_minute}/min" if budget is not None else "REST polling"
        return {'mode': 'rest', 'detail': detail}
