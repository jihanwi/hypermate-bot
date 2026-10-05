"""RISEx VenueAdapter (spec 6.3).

Number formats (PM live check 2026-10-04): /v1/positions carries 18-decimal fixed-point strings
(size "-20892000000000000" = -0.020892, leverage "10000000000000000000" = 10, avg_entry_price
"84828000000000000000000" = 84828); /v1/trade-history, the cross-margin balance and every WS
message carry human units ("0.020892", "84828"). Both forms are parsed here.

Fills: trade-history has no position-before, so the start position of each fill is derived
from the last stored snapshot and accumulated through the batch (owner question in the 2B plan,
answered by this implementation and noted in the PR).
"""

import json
import logging
from decimal import Decimal
from typing import Optional

from hypermate.core.numbers import to_decimal
from hypermate.venues import base
from hypermate.venues.base import AccountSnapshot, VenueAccount, fill_direction, position_entry
from hypermate.venues.hyperliquid import scheduler
from hypermate.venues.risex.client import RisexClient

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
WEI = Decimal(10) ** 18
EXPLORER_FALLBACK = 'https://app.rise.trade/'       # RISE chain explorer address format not confirmed [?]
RECONCILE_SEC = 300          # with the WS connected, compare the cache with a REST snapshot this often


def from_wei(value) -> Optional[Decimal]:
    """18-decimal fixed-point string -> Decimal."""
    d = to_decimal(value)
    return d / WEI if d is not None else None


def human(value) -> Optional[Decimal]:
    return to_decimal(value)


def coin_of(market_name: str) -> str:
    """'BTC/USDC' -> 'BTC' (the quote is always USDC on RISEx)."""
    return str(market_name).split('/', 1)[0]


def parse_rest_positions(data: dict, markets: dict[str, dict]) -> dict[str, dict]:
    """Positions from /v1/positions (18-dec) keyed by coin."""
    return _positions([(p, from_wei) for p in (data or {}).get('positions') or []], markets)


def parse_ws_positions(rows: list[dict], markets: dict[str, dict]) -> dict[str, dict]:
    """Positions from the WS positions snapshot / updates (human units) keyed by coin."""
    return _positions([(p, human) for p in rows or []], markets)


def _positions(rows, markets):
    """Notional = |size| x mark and unrealized PnL = size x (mark - entry) from the market's mark price
    (D-3, 2026-10-05); without a mark the quote amount stands in and PnL is unknown. Entry is rounded to
    the market's step_price decimals."""
    positions = {}
    for row, convert in rows:
        size = convert(row.get('size')) or ZERO
        market = markets.get(str(row.get('market_id')), {})
        coin = coin_of(market.get('name') or f"market:{row.get('market_id')}")
        if size == 0:
            positions.pop(coin, None)
            continue
        entry = convert(row.get('avg_entry_price'))
        mark = to_decimal(market.get('mark_price'))
        step = to_decimal(market.get('step_price'))
        quote = convert(row.get('quote_amount'))
        if mark is not None:
            value = abs(size) * mark
            upnl = size * (mark - entry) if entry is not None else None      # PnL from the exact entry
        else:
            value = abs(quote) if quote is not None else (abs(size) * entry if entry is not None else None)
            upnl = None
        shown_entry = entry.quantize(step) if entry is not None and step is not None and step > 0 else entry
        positions[coin] = position_entry(coin, size, shown_entry, value, upnl)
        positions[coin]['leverage'] = str(convert(row.get('leverage')) or '')
        positions[coin]['isolated_balance'] = str(convert(row.get('isolated_usdc_balance')) or ZERO)
        positions[coin]['unsettled_funding'] = str(convert(row.get('unsettled_funding')) or ZERO)
    return positions


def account_value(cross_balance: Optional[Decimal], positions: dict[str, dict]) -> Optional[Decimal]:
    """Cross-margin balance plus isolated balances; None when nothing is known."""
    isolated = sum((to_decimal(p.get('isolated_balance')) or ZERO for p in positions.values()), ZERO)
    if cross_balance is None and not positions:
        return None
    return (cross_balance or ZERO) + isolated


def ns_to_ms(value) -> int:
    return int(Decimal(str(value)) / 1_000_000) if value is not None else 0


def rest_trade_to_fill(trade: dict, start: Decimal, markets: dict[str, dict]) -> dict:
    """One trade-history entry (human units) -> HL fill, given the position before it."""
    side = 'B' if str(trade.get('side', '')).upper() == 'BUY' else 'A'
    size = human(trade.get('size')) or ZERO
    direction, _ = fill_direction(side, start, size)
    market = markets.get(str(trade.get('market_id')), {})
    coin = coin_of(market.get('name') or f"market:{trade.get('market_id')}")
    fill = {
        'coin': coin, 'px': str(human(trade.get('price')) or ZERO), 'sz': str(size), 'side': side,
        'time': ns_to_ms(trade.get('time')), 'startPosition': str(start), 'dir': direction,
        'oid': trade.get('order_id') or trade.get('id'), 'tid': trade.get('id'),
        'closedPnl': str(human(trade.get('realized_pnl')) or ZERO), 'fee': str(human(trade.get('fee')) or ZERO),
        'feeToken': 'USDC', 'hash': (trade.get('blockchain_data') or {}).get('tx_hash'),
        'crossed': str(trade.get('liquidity_indicator', '')).upper() == 'TAKER',
    }
    if trade.get('is_liquidation'):
        fill['liquidation'] = {'method': 'market', 'markPx': fill['px']}
    return fill


def ws_trade_to_fill(update: dict, address: str, start: Decimal, markets: dict[str, dict]) -> Optional[dict]:
    """A trades-channel update -> HL fill for the tracked side, or None when the address is neither side.
    maker_side 0 = the maker bought (so the taker sold), 1 = the maker sold."""
    data = update.get('data') or {}
    address = address.lower()
    maker, taker = str(data.get('maker', '')).lower(), str(data.get('taker', '')).lower()
    maker_bought = int(data.get('maker_side', 0)) == 0
    if address == maker:
        side, oid, fee, crossed = ('B' if maker_bought else 'A'), data.get('maker_order_id'), data.get('fee_maker'), False
    elif address == taker:
        side, oid, fee, crossed = ('A' if maker_bought else 'B'), data.get('taker_order_id'), data.get('fee_taker'), True
    else:
        return None
    size = human(data.get('size')) or ZERO
    direction, _ = fill_direction(side, start, size)
    market = markets.get(str(update.get('market_id')), {})
    coin = coin_of(market.get('name') or f"market:{update.get('market_id')}")
    fill = {
        'coin': coin, 'px': str(human(data.get('price')) or ZERO), 'sz': str(size), 'side': side,
        'time': ns_to_ms(update.get('worker_timestamp')), 'startPosition': str(start), 'dir': direction,
        'oid': oid or data.get('id'), 'tid': data.get('id'), 'closedPnl': '0',
        'fee': str(human(fee) or ZERO), 'feeToken': 'USDC', 'hash': update.get('tx_hash'), 'crossed': crossed,
    }
    if (human(data.get('fee_liquidation')) or ZERO) > 0:
        fill['liquidation'] = {'method': 'market', 'markPx': fill['px']}
    return fill


def accumulate(fills_in_order: list[dict], positions_before: dict[str, dict]) -> list[dict]:
    """Set each fill's startPosition from the snapshot before the batch, walking the fills oldest first."""
    running = {coin: to_decimal(p.get('szi')) or ZERO for coin, p in positions_before.items()}
    out = []
    for fill in fills_in_order:
        start = running.get(fill['coin'], ZERO)
        size = to_decimal(fill['sz']) or ZERO
        direction, end = fill_direction(fill['side'], start, size)
        out.append({**fill, 'startPosition': str(start), 'dir': direction})
        running[fill['coin']] = end
    return out


def encode_cursor(last_id: str, last_ms: int) -> str:
    return json.dumps({'id': last_id, 'ms': int(last_ms)})


def decode_cursor(cursor: Optional[str]) -> tuple[Optional[str], int]:
    if not cursor:
        return None, 0
    try:
        data = json.loads(cursor)
        return data.get('id'), int(data.get('ms', 0))
    except (ValueError, AttributeError, TypeError):
        return None, 0          # a new account's ms cursor: baseline, no replay


class RisexAdapter:
    venue = base.RISEX

    def __init__(self, client: RisexClient, stream=None) -> None:
        self.client = client
        self.stream = stream

    async def resolve(self, evm_address: str) -> list[VenueAccount]:
        """Active when /v1/positions has positions or trade-history has at least one trade.

        /v1/trade-history without market_id is answered as market_id=0 and returns nothing (PM live
        check 2026-10-05), so the no-position case sweeps every market with limit=1 (38 requests,
        once per /add or daily rescan, inside the 2400/min bucket).
        """
        address = evm_address.lower()
        data = await self.client.positions(address, priority=scheduler.P_LEDGER)
        if data.get('positions'):
            return [VenueAccount(self.venue, address, address)]
        for market_id in await self.client.markets(priority=scheduler.P_LEDGER):
            if await self.client.trade_history(address, limit=1, market_id=market_id, priority=scheduler.P_LEDGER):
                return [VenueAccount(self.venue, address, address)]
        return []

    async def snapshot(self, account: VenueAccount) -> AccountSnapshot:
        address = account.account_ref
        markets = await self.client.markets(priority=scheduler.P_SNAPSHOT)
        cached = self.stream.positions_for(address) if self.stream is not None else None
        if cached is not None:
            # Safeguard: a position that the WS cache still holds but a REST snapshot no longer lists is
            # treated as closed (size-0 update rows are not confirmed [?]); checked every RECONCILE_SEC
            last = self.stream.last_reconcile.get(address.lower())
            if last is None or self.stream.clock() - last >= RECONCILE_SEC:
                rest = await self.client.positions(address, priority=scheduler.P_SNAPSHOT)
                rest_ids = {str(p.get('market_id')) for p in (rest or {}).get('positions') or []
                            if (from_wei(p.get('size')) or ZERO) != 0}
                removed = self.stream.reconcile(address, rest_ids)
                if removed:
                    logger.info(f"RISEx {address}: {len(removed)} cached positions gone from REST, treated as closed")
                cached = self.stream.positions_for(address) or []
            positions = parse_ws_positions(cached, markets)
            balance = self.stream.balance_for(address)
            if balance is None:
                balance = await self.client.cross_margin_balance(address, priority=scheduler.P_SNAPSHOT)
                self.stream.set_balance(address, balance)
        else:
            positions = parse_rest_positions(await self.client.positions(address, priority=scheduler.P_SNAPSHOT),
                                             markets)
            balance = await self.client.cross_margin_balance(address, priority=scheduler.P_SNAPSHOT)
        return AccountSnapshot(positions, account_value(balance, positions),
                               extra={'cross_balance': str(balance) if balance is not None else None})

    async def fetch_events(self, account: VenueAccount, cursor: Optional[str],
                           positions_before: Optional[dict] = None) -> tuple[list[dict], Optional[str]]:
        """Fills newer than the cursor, oldest first. WS trades buffered for the address are used when
        the stream is connected; otherwise /v1/trade-history (newest first, no position-before)."""
        address = account.account_ref
        last_id, last_ms = decode_cursor(cursor)
        markets = await self.client.markets(priority=scheduler.P_FILLS)
        raw: list[dict]
        if self.stream is not None and self.stream.connected and self.stream.has_trades(address):
            raw = [f for f in (ws_trade_to_fill(u, address, ZERO, markets) for u in self.stream.drain_trades(address))
                   if f is not None]
        else:
            # REST fallback: trade-history is per market (no market_id means market 0), so the markets
            # the account holds now or held in the last snapshot are queried; a fill on any other market
            # also creates a position, which the next snapshot diff shows
            market_ids = self._markets_of(positions_before or {}, markets)
            current = await self.client.positions(address, priority=scheduler.P_FILLS)
            market_ids |= {str(p.get('market_id')) for p in (current or {}).get('positions') or []}
            trades = []
            for market_id in sorted(market_ids):
                trades += await self.client.trade_history(address, market_id=market_id, priority=scheduler.P_FILLS)
            raw = [rest_trade_to_fill(t, ZERO, markets) for t in trades]
        raw.sort(key=lambda f: (f['time'], str(f['tid'])))
        baseline = last_id is None and last_ms == 0
        fresh = [] if baseline else [f for f in raw if (f['time'], str(f['tid'])) > (last_ms, str(last_id or ''))]
        fills = accumulate(fresh, positions_before or {})
        newest = max(raw, key=lambda f: (f['time'], str(f['tid'])), default=None)
        if newest is None:
            # nothing seen yet: mark the baseline as taken so later trades count as new
            return fills, cursor if not baseline else encode_cursor('', 1)
        return fills, encode_cursor(str(newest['tid']), newest['time'])

    @staticmethod
    def _markets_of(positions: dict, markets: dict[str, dict]) -> set[str]:
        coins = set(positions)
        return {market_id for market_id, m in markets.items() if coin_of(m.get('name', '')) in coins}

    def explorer_url(self, account: VenueAccount) -> str:
        return EXPLORER_FALLBACK

    def cost(self, op: str) -> int:
        return {'resolve': 2, 'snapshot': 2, 'events': 1}.get(op, 1)

    async def health(self) -> dict:
        if self.stream is None:
            return {'mode': 'rest', 'detail': 'REST polling'}
        return {'mode': 'ws' if self.stream.connected else 'rest', 'detail': self.stream.status_line()}
