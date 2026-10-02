"""Hyperliquid: positions snapshots, fill classification and order aggregation (spec 5.2).

Stateless: the pipeline loads cursors / snapshots from the DB and stores what
these functions return.
"""

import logging
import time
from decimal import Decimal
from typing import Optional

from hypermate.core.events import HYPERLIQUID, Event, EventType, ledger_event_type
from hypermate.core.numbers import to_decimal
from hypermate.venues.hyperliquid.client import HyperliquidClient

logger = logging.getLogger(__name__)

ZERO = Decimal(0)

_OPEN_DIRS = ('Open Long', 'Open Short')
_CLOSE_DIRS = ('Close Long', 'Close Short')
_FLIP_DIRS = ('Long > Short', 'Short > Long')
_SKIPPED_DIRS = ('Spot Dust Conversion',)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def is_spot_coin(coin: str) -> bool:
    """Spot fills use "PURR/USDC" or "@107"; perp fills use "BTC" or, on HIP-3 dexs, "xyz:MU" (B9)."""
    return '/' in coin or coin.startswith('@')


def coin_dex(coin: str) -> str:
    """HIP-3 dex of a perp coin ("xyz:MU" -> "xyz"); "" for the main dex."""
    return coin.split(':', 1)[0] if ':' in coin and not is_spot_coin(coin) else ''


def items_after(items: list, cursor: int) -> tuple[list, int]:
    """Items with time > cursor, sorted by time, and the advanced cursor."""
    new_items = sorted((i for i in items if int(i.get('time', 0)) > cursor), key=lambda i: int(i['time']))
    new_cursor = max([cursor] + [int(i['time']) for i in new_items])
    return new_items, new_cursor


# Snapshots -------------------------------------------------------------------

def parse_positions(clearinghouse_state: dict) -> dict[str, dict]:
    """Open perp positions of one dex keyed by coin. Values stay API strings (JSON-serializable)."""
    positions = {}
    for pos in clearinghouse_state.get('assetPositions', []):
        position = pos.get('position')
        if not position:
            continue
        szi = to_decimal(position.get('szi', '0')) or ZERO
        if szi == 0:
            continue
        coin = position.get('coin', '')
        positions[coin] = {
            'szi': str(position.get('szi')),
            'direction': 'LONG' if szi > 0 else 'SHORT',
            'entry_px': str(position.get('entryPx', 'N/A')),
            'position_value': str(position.get('positionValue', 'N/A')),
            'coin': coin,
            'unrealized_pnl': str(position.get('unrealizedPnl', 'N/A')),
        }
    return positions


def parse_account_value(clearinghouse_state: dict) -> Optional[Decimal]:
    return to_decimal(clearinghouse_state.get('marginSummary', {}).get('accountValue'))


async def fetch_snapshot(client: HyperliquidClient, address: str,
                         dexs: list[str]) -> tuple[dict[str, dict], Optional[str], dict[str, dict]]:
    """Main dex plus the account's HIP-3 dexs (spec 5.2 HIP-3).

    Returns ({dex: {coin: position}}, total account value as a Decimal string, {dex: raw state}).
    """
    states = {'': await client.clearinghouse_state(address)}
    for dex in dexs:
        states[dex] = await client.clearinghouse_state(address, dex)
    snapshot = {dex: parse_positions(state) for dex, state in states.items()}
    values = [v for v in (parse_account_value(s) for s in states.values()) if v is not None]
    return snapshot, (str(sum(values, ZERO)) if values else None), states


def parse_spot_balances(spot_state: dict) -> dict[str, str]:
    """{coin: total} for non-zero spot balances (change detection only, spec 3.5)."""
    balances = {}
    for balance in spot_state.get('balances', []):
        total = to_decimal(balance.get('total')) or ZERO
        if total != 0:
            balances[str(balance.get('coin', ''))] = str(total)
    return balances


def spot_changed(previous: Optional[dict], current: dict) -> bool:
    if previous is None:
        return False
    return {c: to_decimal(v) for c, v in previous.items()} != {c: to_decimal(v) for c, v in current.items()}


def snapshot_changed(previous: Optional[dict], current: dict) -> bool:
    """True if any position size differs (account value / price moves alone do not count)."""
    def sizes(snapshot):
        return {(dex, coin): to_decimal(p.get('szi')) for dex, positions in (snapshot or {}).items()
                for coin, p in positions.items()}
    return sizes(previous) != sizes(current)


async def scan_dexs(client: HyperliquidClient, address: str) -> list[str]:
    """HIP-3 dexs where the address has an open position (spec 5.2: /add and /rescan)."""
    found = []
    for dex in await client.perp_dexs():
        state = await client.clearinghouse_state(address, dex)
        if parse_positions(state):
            found.append(dex)
    return found


# Fills -> events (spec 5.2 포지션, 체결 집계) -----------------------------------

def fill_sign(fill: dict) -> int:
    """Sign of the position change caused by the fill: buy +1, sell -1."""
    return 1 if str(fill.get('side', '')).upper() == 'B' else -1


def group_orders(fills: list[dict]) -> list[list[dict]]:
    """Group a poll window's fills by (coin, dir, oid), keeping first-seen order (spec 5.2 체결 집계)."""
    groups: dict[tuple, list[dict]] = {}
    # Stable sort on time only: fills in the same millisecond keep the API order. tid is not monotonic
    # (fixture: same-ms fills sorted by tid break the startPosition chain), so it must not break ties.
    for fill in sorted(fills, key=lambda f: int(f.get('time', 0))):
        if fill.get('dir') in _SKIPPED_DIRS:
            continue
        key = (fill.get('coin'), fill.get('dir'), fill.get('oid'))
        groups.setdefault(key, []).append(fill)
    return list(groups.values())


def _side(position: Decimal) -> Optional[str]:
    if position > 0:
        return 'LONG'
    if position < 0:
        return 'SHORT'
    return None


def _position_type(direction: str, start: Decimal, end: Decimal, liquidated: bool) -> EventType:
    if liquidated:
        return EventType.LIQUIDATION
    if direction in _OPEN_DIRS:
        return EventType.POSITION_OPEN if start == 0 else EventType.POSITION_INCREASE
    if direction in _CLOSE_DIRS:
        return EventType.POSITION_CLOSE if end == 0 else EventType.POSITION_DECREASE
    if direction in _FLIP_DIRS:
        return EventType.POSITION_FLIP
    # dir is a display string without an enum guarantee: fall back to the positions (spec 5.1)
    if start == 0:
        return EventType.POSITION_OPEN
    if end == 0:
        return EventType.POSITION_CLOSE
    if (start > 0) != (end > 0):
        return EventType.POSITION_FLIP
    return EventType.POSITION_INCREASE if abs(end) > abs(start) else EventType.POSITION_DECREASE


def order_event(venue_account_id: int, fills: list[dict]) -> Event:
    """One event for the fills of one order (same coin, dir, oid)."""
    first, last = fills[0], fills[-1]
    coin = first.get('coin', '')
    sign = fill_sign(first)
    size = sum((to_decimal(f.get('sz')) or ZERO for f in fills), ZERO)
    notional = sum(((to_decimal(f.get('px')) or ZERO) * (to_decimal(f.get('sz')) or ZERO) for f in fills), ZERO)
    vwap = notional / size if size else None
    pnl = sum((to_decimal(f.get('closedPnl')) or ZERO for f in fills), ZERO)
    fee = sum((to_decimal(f.get('fee')) or ZERO for f in fills), ZERO)
    meta = {'dir': first.get('dir'), 'oid': first.get('oid'), 'fills': len(fills), 'sign': sign,
            'first_ms': int(first['time']), 'fee': str(fee), 'fee_token': first.get('feeToken'),
            'dex': coin_dex(coin)}

    if is_spot_coin(coin):
        event_type = EventType.SPOT_BUY if sign > 0 else EventType.SPOT_SELL
        return Event(HYPERLIQUID, venue_account_id, event_type, int(last['time']), str(first.get('tid')),
                     coin=coin, size=size, notional_usd=notional, price=vwap, meta=meta)

    start = to_decimal(first.get('startPosition')) or ZERO
    end = (to_decimal(last.get('startPosition')) or ZERO) + sign * (to_decimal(last.get('sz')) or ZERO)
    liquidation = next((f.get('liquidation') for f in fills if f.get('liquidation')), None)
    event_type = _position_type(first.get('dir', ''), start, end, liquidation is not None)
    if event_type in (EventType.POSITION_OPEN, EventType.POSITION_INCREASE, EventType.POSITION_FLIP):
        side = _side(end)
    else:
        side = _side(start)
    meta['start_position'] = str(start)
    if liquidation is not None:
        meta['liquidation'] = liquidation
    has_pnl = event_type in (EventType.POSITION_DECREASE, EventType.POSITION_CLOSE, EventType.POSITION_FLIP,
                             EventType.LIQUIDATION)
    return Event(HYPERLIQUID, venue_account_id, event_type, int(last['time']), str(first.get('tid')),
                 coin=coin, side=side, size=size, notional_usd=notional, price=vwap, position_after=end,
                 realized_pnl=pnl if has_pnl else None, meta=meta)


def fill_events(venue_account_id: int, fills: list[dict]) -> list[Event]:
    return [order_event(venue_account_id, group) for group in group_orders(fills)]


def ledger_events(venue_account_id: int, address: str, updates: list[dict]) -> list[Event]:
    """Ledger updates -> events (spec 5.2 ledger). Unknown or ignored types produce nothing."""
    events = []
    for update in updates:
        event_type = ledger_event_type(update, address)
        if event_type is None:
            continue
        delta = update.get('delta', {})
        meta = {'delta': delta, 'hash': update.get('hash')}
        if event_type in (EventType.TRANSFER_IN, EventType.TRANSFER_OUT):
            out = event_type == EventType.TRANSFER_OUT
            meta['counterparty'] = delta.get('destination') if out else delta.get('user')
        coin = None
        if event_type == EventType.LIQUIDATION:
            coin = ','.join(p.get('coin', '') for p in delta.get('liquidatedPositions') or [])
        events.append(Event(HYPERLIQUID, venue_account_id, event_type, int(update['time']),
                            f"{update.get('hash')}:{update['time']}", coin=coin, meta=meta))
    return events


async def fetch_fills(client: HyperliquidClient, address: str, cursor: int) -> tuple[list[dict], int]:
    """Fills (all dexs, perp and spot) since cursor, via userFillsByTime (B10)."""
    fills = await client.user_fills_by_time(address, cursor + 1)
    return items_after(fills, cursor)


async def fetch_ledger(client: HyperliquidClient, address: str, cursor: int) -> tuple[list[dict], int]:
    """Ledger updates since cursor (B5: info API only)."""
    updates = await client.ledger_updates(address, cursor + 1)
    return items_after(updates, cursor)
