"""Hyperliquid change detection.

Phase 0 keeps the v1 snapshot-diff logic (replaced in Phase 1). The functions
here are stateless: the caller loads the previous snapshot / cursor and stores
the returned one.
"""

import logging
import time
from decimal import Decimal
from typing import Optional

from hypermate.core.numbers import to_decimal
from hypermate.venues.hyperliquid.client import HyperliquidClient

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def is_spot_coin(coin: str) -> bool:
    """Spot fills use "PURR/USDC" or "@107"; perp fills use the bare asset name (B9)."""
    return '/' in coin or coin.startswith('@')


def parse_positions(clearinghouse_state: dict) -> dict[str, dict]:
    """Open perp positions keyed by coin. Values are kept as API strings so they serialize to JSON as-is."""
    positions = {}
    for pos in clearinghouse_state.get('assetPositions', []):
        position = pos.get('position')
        if not position:
            continue
        coin = position.get('coin', '')
        szi = to_decimal(position.get('szi', '0')) or ZERO
        if szi == 0:
            continue
        positions[coin] = {
            'szi': str(position.get('szi')),
            'direction': 'LONG' if szi > 0 else 'SHORT',
            'entry_px': str(position.get('entryPx', 'N/A')),
            'position_value': str(position.get('positionValue', 'N/A')),
            'coin': coin,
            'unrealized_pnl': str(position.get('unrealizedPnl', 'N/A')),
        }
    return positions


def diff_positions(previous: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    """Position alerts from two snapshots. v1 logic (B3/B8 heuristics are fixed in Phase 1)."""
    alerts = []

    for coin, curr_pos in current.items():
        if coin not in previous:
            alerts.append({**curr_pos, 'alert_type': 'NEW_POSITION'})
            continue

        prev_szi = to_decimal(previous[coin]['szi'])
        curr_szi = to_decimal(curr_pos['szi'])

        # Size increased in the same direction
        if (prev_szi > 0 and curr_szi > prev_szi) or (prev_szi < 0 and curr_szi < prev_szi):
            alerts.append({**curr_pos, 'alert_type': 'POSITION_INCREASE',
                           'size_change': str(abs(curr_szi - prev_szi))})

        # Size decreased without closing (partial close)
        elif ((prev_szi > 0 and 0 < curr_szi < prev_szi) or
              (prev_szi < 0 and prev_szi < curr_szi < 0)):
            alerts.append({**curr_pos, 'alert_type': 'POSITION_DECREASE',
                           'size_change': str(abs(prev_szi - curr_szi)),
                           'remaining_size': str(abs(curr_szi))})

    for coin, prev_pos in previous.items():
        if coin in current:
            continue
        position_size = abs(to_decimal(prev_pos['szi']))
        # B8: last polled unrealized PnL stands in for realized PnL until Phase 1
        closing_pnl = to_decimal(prev_pos.get('unrealized_pnl'))

        # B3: "loss > 15% of position value" liquidation heuristic until Phase 1
        is_liquidation = False
        position_value = to_decimal(prev_pos.get('position_value'))
        if closing_pnl is not None and position_value is not None:
            position_value = abs(position_value)
            if position_value > 0 and closing_pnl < Decimal('-0.15') * position_value:
                is_liquidation = True

        alert = {**prev_pos, 'closing_pnl': None if closing_pnl is None else str(closing_pnl)}
        if is_liquidation:
            alert.update(alert_type='LIQUIDATION', liquidated_size=str(position_size))
        else:
            alert.update(alert_type='POSITION_CLOSED', closed_size=str(position_size))
        alerts.append(alert)

    return alerts


def items_after(items: list, cursor: int) -> tuple[list, int]:
    """Items with time > cursor, sorted by time, and the advanced cursor."""
    new_items = sorted((i for i in items if int(i.get('time', 0)) > cursor), key=lambda i: int(i['time']))
    new_cursor = max([cursor] + [int(i['time']) for i in new_items])
    return new_items, new_cursor


def parse_account_value(clearinghouse_state: dict) -> Optional[str]:
    """marginSummary.accountValue as a Decimal string, or None."""
    value = to_decimal(clearinghouse_state.get('marginSummary', {}).get('accountValue'))
    return None if value is None else str(value)


async def check_positions(client: HyperliquidClient, address: str,
                          previous: Optional[dict]) -> tuple[list[dict], dict, Optional[str]]:
    """Return (alerts, current snapshot, account value). previous=None records a baseline without alerts."""
    state = await client.clearinghouse_state(address)
    current = parse_positions(state)
    account_value = parse_account_value(state)
    if previous is None:
        logger.info(f"Baseline snapshot for {address}: {len(current)} positions")
        return [], current, account_value
    alerts = diff_positions(previous, current)
    for alert in alerts:
        logger.info(f"{alert['alert_type']} {alert['coin']} for {address}")
    return alerts, current, account_value


async def check_ledger(client: HyperliquidClient, address: str, cursor: int) -> tuple[list[dict], int]:
    """Ledger updates (deposits, withdrawals, transfers) since cursor (B5: info API only)."""
    updates = await client.ledger_updates(address, cursor + 1)
    return items_after(updates, cursor)


async def check_spot_fills(client: HyperliquidClient, address: str, cursor: int) -> tuple[list[dict], int]:
    """Spot fills since cursor (B10: userFillsByTime). The cursor advances over perp fills too."""
    fills = await client.user_fills_by_time(address, cursor + 1)
    new_fills, new_cursor = items_after(fills, cursor)
    spot_fills = [f for f in new_fills if is_spot_coin(f.get('coin', ''))]
    for fill in spot_fills:
        fill['display_coin'] = await client.spot_display_name(fill['coin'])
    return spot_fills, new_cursor
