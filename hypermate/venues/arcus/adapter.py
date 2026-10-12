"""Arcus VenueAdapter (spec 6.5): /v1/account per accountIndex for resolve and snapshot, /v1/fills for
events (microsecond cursor, pages followed by lowering `to`). Shapes from the PM's live recording of
2026-10-10 (docs/API_NOTES.md, Arcus): positions are a dict keyed by marketId with side / size /
averageEntryPrice / markPx / unrealizedPnl / leverage; fills carry positionEffect, which decides the
direction label instead of the position walk.
"""

import json
import logging
from decimal import Decimal
from typing import Optional

from hypermate.core.numbers import to_decimal
from hypermate.venues import base
from hypermate.venues.arcus.client import (MAX_ACCOUNT_INDEX, PAGE_SIZE, ArcusClient, ArcusNoActivity,
                                           ArcusNotWhitelisted)
from hypermate.venues.base import AccountSnapshot, VenueAccount, position_entry
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid import scheduler
from hypermate.venues.risex.adapter import accumulate

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
EXPLORER_FALLBACK = 'https://arcus.xyz/'      # Robinhood Chain explorer address format not confirmed [?]
MAX_CATCHUP_PAGES = 5
MAX_PX_DECIMALS = 8
# positionEffect (recorded) -> Hyperliquid dir label
POSITION_EFFECTS = {'ADD_LONG': 'Open Long', 'ADD_SHORT': 'Open Short', 'CLOSE_LONG': 'Close Long',
                    'CLOSE_SHORT': 'Close Short', 'FLIP_LONG_TO_SHORT': 'Long > Short',
                    'FLIP_SHORT_TO_LONG': 'Short > Long'}


def coin_of(market: str) -> str:
    """'BTC-USD' -> 'BTC', 'QQQ-USD' -> 'QQQ'."""
    name = str(market or '').upper()
    return name[:-4] if name.endswith('-USD') and len(name) > 4 else name


def tick_for_price(market: Optional[dict], price: Optional[Decimal]) -> Optional[Decimal]:
    """The tick of the tickTiers entry the price falls in: entries are {upToPrice, tick}, the last one has no
    upToPrice (no upper bound). Without tiers or a price, the market's tickSize."""
    if not market:
        return None
    tiers = []
    for tier in market.get('tick_tiers') or []:
        if isinstance(tier, dict) and to_decimal(tier.get('tick')) is not None:
            tiers.append((to_decimal(tier.get('upToPrice')), to_decimal(tier.get('tick'))))
    if price is not None and tiers:
        for up_to, tick in tiers:
            if up_to is None or price <= up_to:
                return tick
        return tiers[-1][1]
    return to_decimal(market.get('tick_size'))


def px_decimals(tick: Optional[Decimal]) -> Optional[int]:
    if tick is None or tick <= 0:
        return None
    return min(MAX_PX_DECIMALS, max(0, -tick.normalize().as_tuple().exponent))


def _position_rows(account: dict) -> list[tuple[str, dict]]:
    """[(marketId, row)]: the recorded shape is a dict keyed by marketId; a list is accepted too."""
    rows = account.get('positions') or {}
    if isinstance(rows, dict):
        return [(str(k), v) for k, v in rows.items() if isinstance(v, dict)]
    return [(str(r.get('marketId', '')), r) for r in rows if isinstance(r, dict)]


def parse_positions(account: dict, markets: Optional[dict[str, dict]] = None) -> dict[str, dict]:
    """Positions keyed by coin. size is unsigned with `side` LONG/SHORT; zero rows skipped; mark price
    from markPx; notional from positionValueNotional; no liquidation price on Arcus."""
    positions = {}
    for market_id, row in _position_rows(account):
        size = abs(to_decimal(row.get('size')) or ZERO)
        if size == 0:
            continue
        if str(row.get('side', '')).upper() == 'SHORT':
            size = -size
        market = (markets or {}).get(market_id) or {}
        market_name = str(row.get('marketDisplayName') or market.get('name') or f"market:{market_id}")
        coin = coin_of(market_name)
        entry = to_decimal(row.get('averageEntryPrice'))
        mark = to_decimal(row.get('markPx', row.get('markPrice')))
        upnl = to_decimal(row.get('unrealizedPnl'))
        value = to_decimal(row.get('positionValueNotional'))
        if value is None and mark is not None:
            value = abs(size) * mark
        if value is None and entry is not None:
            value = abs(size) * entry
        value = abs(value) if value is not None else None
        if upnl is None and mark is not None and entry is not None:
            upnl = size * (mark - entry)
        tick = tick_for_price(market, mark or entry)
        shown_entry = entry.quantize(tick) if entry is not None and tick is not None and tick > 0 else entry
        positions[coin] = position_entry(coin, size, shown_entry, value, upnl)
        decimals = px_decimals(tick)
        if decimals is not None:
            positions[coin]['px_decimals'] = decimals
        if row.get('leverage') is not None:
            positions[coin]['leverage'] = str(to_decimal(row.get('leverage')) or '')
        if row.get('marginMode') is not None:
            positions[coin]['margin_mode'] = str(row.get('marginMode'))
        funding = row.get('cumulativeFunding') if isinstance(row.get('cumulativeFunding'), dict) else None
        if funding is not None and funding.get('sinceOpen') is not None:
            positions[coin]['cumulative_funding_since_open'] = str(to_decimal(funding.get('sinceOpen')) or ZERO)
    return positions


def parse_snapshot(account: dict, markets: Optional[dict[str, dict]] = None) -> AccountSnapshot:
    return AccountSnapshot(parse_positions(account, markets), to_decimal(account.get('equity')), raw=account,
                           extra={'free_collateral': account.get('freeCollateral'),
                                  'net_deposits': account.get('netDeposits')})


def fill_to_hl(fill: dict) -> dict:
    """/v1/fills row -> HL fill. closedPnl is after the fee (an ADD with no realized PnL shows
    closedPnl = -fee), so the fee is added back for HL's gross convention; maker rebates are negative
    fees. dir comes from positionEffect; startPosition is set by accumulate() from the previous snapshot."""
    side = 'B' if str(fill.get('side', '')).upper() in ('BUY', 'LONG', 'B') else 'A'
    fee = to_decimal(fill.get('fee')) or ZERO
    closed = to_decimal(fill.get('closedPnl'))
    effect = str(fill.get('positionEffect') or '').upper()
    liquidation = fill.get('liquidation') if isinstance(fill.get('liquidation'), dict) else None
    if closed is not None:
        closed = closed + fee
    out = {'coin': coin_of(str(fill.get('marketDisplayName') or '')),
           'px': str(to_decimal(fill.get('price')) or ZERO), 'sz': str(to_decimal(fill.get('size')) or ZERO),
           'side': side, 'time': int(fill.get('createdAt') or 0) // 1000, 'startPosition': '0',
           'dir': POSITION_EFFECTS.get(effect, ''),
           'oid': str(fill.get('orderId') or fill.get('tradeId') or ''), 'tid': str(fill.get('tradeId') or ''),
           'closedPnl': plain(closed) if closed is not None else None, 'fee': plain(fee), 'feeToken': 'USDG',
           'crossed': str(fill.get('role', '')).upper() == 'TAKER'}
    if liquidation is not None:
        out['liquidation'] = {'method': liquidation.get('method'), 'markPx': out['px']}
    return out


def plain(value: Decimal) -> str:
    text = format(value, 'f')
    return (text.rstrip('0').rstrip('.') or '0') if '.' in text else text


def account_ref(address: str, index: int) -> str:
    """'<address>:<index>': indexes repeat across wallets, so the ref must carry the address."""
    return f"{address.lower()}:{int(index)}"


def index_of(ref: str) -> int:
    return int(base.account_index(ref))


def encode_cursor(last_ms: int) -> str:
    return json.dumps({'ms': int(last_ms)})


def decode_cursor(cursor: Optional[str]) -> Optional[int]:
    if not cursor:
        return None
    try:
        return int(json.loads(cursor).get('ms', 0))
    except (ValueError, AttributeError, TypeError):
        return None                  # a new account's ms cursor: baseline


class ArcusAdapter:
    venue = base.ARCUS

    def __init__(self, client: ArcusClient) -> None:
        self.client = client

    async def resolve(self, evm_address: str) -> list[VenueAccount]:
        """Walk accountIndex 0..9: 200 = that index is active, 403 = not an Arcus user (stop, ✗),
        404 = nothing on that index; all 404 = ✗."""
        address = evm_address.lower()
        found = []
        for index in range(MAX_ACCOUNT_INDEX + 1):
            try:
                account = await self.client.account(address, index, priority=scheduler.P_LEDGER)
            except ArcusNotWhitelisted as e:
                logger.info(f"Arcus: {address} not whitelisted: {e}")
                return []
            except ArcusNoActivity:
                continue
            found.append(VenueAccount(self.venue, account_ref(address, index), address,
                                      meta={'equity': str(to_decimal(account.get('equity')) or ZERO)}))
        return found

    async def snapshot(self, account: VenueAccount) -> AccountSnapshot:
        try:
            raw = await self.client.account(account.address, index_of(account.account_ref), priority=scheduler.P_SNAPSHOT)
        except ArcusNoActivity:
            return AccountSnapshot({}, None)
        try:
            markets = await self.client.markets(priority=scheduler.P_SNAPSHOT)
        except Exception as e:                         # the snapshot does not depend on it
            logger.warning(f"Arcus markets unavailable: {e}")
            markets = {}
        return parse_snapshot(raw, markets)

    async def fetch_events(self, account: VenueAccount, cursor: Optional[str],
                           positions_before: Optional[dict] = None) -> tuple[list[dict], Optional[str]]:
        """Fills after the cursor (ms), oldest first. The first poll only sets the cursor to now. A full
        page is followed backwards (to = oldest createdAt - 1) up to MAX_CATCHUP_PAGES."""
        last_ms = decode_cursor(cursor)
        now = hl_adapter.now_ms()
        if last_ms is None:
            return [], encode_cursor(now)
        index = index_of(account.account_ref)
        from_us = (last_ms + 1) * 1000
        rows: list[dict] = []
        to_us = None
        for _ in range(MAX_CATCHUP_PAGES):
            page = await self.client.fills(account.address, index, from_us, to_us, priority=scheduler.P_FILLS)
            rows += page
            if len(page) < PAGE_SIZE:
                break
            oldest = min(int(f.get('createdAt') or 0) for f in page)
            to_us = oldest - 1 if to_us is None or oldest < to_us else to_us - 1
            if to_us < from_us:
                break
        seen = set()
        fresh = []
        for f in rows:
            if int(f.get('createdAt') or 0) // 1000 <= last_ms or str(f.get('tradeId')) in seen:
                continue
            seen.add(str(f.get('tradeId')))
            fresh.append(f)
        fresh.sort(key=lambda f: (int(f.get('createdAt') or 0), int(f.get('sequenceNumber') or 0), str(f.get('tradeId') or '')))
        fills = accumulate([fill_to_hl(f) for f in fresh], positions_before or {}, keep_dir=True)
        newest = max([last_ms] + [f['time'] for f in fills])
        return fills, encode_cursor(newest)

    def explorer_url(self, account: VenueAccount) -> str:
        return EXPLORER_FALLBACK

    def cost(self, op: str) -> int:
        return {'resolve': 2, 'snapshot': 2, 'events': 20}.get(op, 2)

    async def health(self) -> dict:
        budget = self.client.budget
        detail = f"REST polling, bucket {budget.per_minute}/min" if budget is not None else "REST polling"
        return {'mode': 'rest', 'detail': detail}
