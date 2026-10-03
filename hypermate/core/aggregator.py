"""Aggregation rules (spec 5.2): debounce of consecutive orders and synthetic TWAP (algo) detection.

Pure functions over event payloads (dicts as stored in the events table), so the
same logic works on events from this poll and on events read back from the DB.
"""

from decimal import Decimal
from statistics import median
from typing import Iterable, Optional

from hypermate.core.events import EventType
from hypermate.core.numbers import to_decimal

ZERO = Decimal(0)

# Position events that can belong to a synthetic TWAP. Flips and liquidations never do.
ALGO_TYPES = (EventType.POSITION_OPEN.value, EventType.POSITION_INCREASE.value,
              EventType.POSITION_DECREASE.value, EventType.POSITION_CLOSE.value)


# Debounce -------------------------------------------------------------------

def debounce_key(payload: dict) -> tuple:
    """Orders with the same (coin, dir) can be merged into one message (spec 5.2)."""
    return payload.get('coin'), (payload.get('meta') or {}).get('dir')


def can_merge(chain: dict, payload: dict, debounce_sec: int) -> bool:
    """chain: cumulative state of the message being edited (see start_chain)."""
    gap_ms = int(payload['meta']['first_ms']) - int(chain['last_ms'])
    return debounce_key(payload) == tuple(chain['key']) and gap_ms <= debounce_sec * 1000


def start_chain(payload: dict) -> dict:
    """Cumulative state of a message that later orders may be merged into."""
    return {
        'key': list(debounce_key(payload)),
        'type': payload['type'],
        'coin': payload.get('coin'),
        'side': payload.get('side'),
        'size': payload.get('size'),
        'notional_usd': payload.get('notional_usd'),
        'realized_pnl': payload.get('realized_pnl'),
        'position_after': payload.get('position_after'),
        'fills': (payload.get('meta') or {}).get('fills', 1),
        'orders': 1,
        'first_ms': (payload.get('meta') or {}).get('first_ms', payload['ts_ms']),
        'last_ms': payload['ts_ms'],
        'meta': chain_meta(payload.get('meta') or {}),
    }


CHAIN_META_KEYS = ('dir', 'sign', 'dex', 'display_coin')


def chain_meta(meta: dict) -> dict:
    """The part of an order's meta a merged message still needs (the rest is on the order's own row)."""
    return {k: meta[k] for k in CHAIN_META_KEYS if k in meta}


def merge_into_chain(chain: dict, payload: dict) -> dict:
    """Add one order to the chain. The header type stays the first order's (e.g. OPEN then adds)."""
    size = (to_decimal(chain['size']) or ZERO) + (to_decimal(payload.get('size')) or ZERO)
    notional = (to_decimal(chain['notional_usd']) or ZERO) + (to_decimal(payload.get('notional_usd')) or ZERO)
    pnl = chain.get('realized_pnl')
    if payload.get('realized_pnl') is not None:
        pnl = str((to_decimal(pnl) or ZERO) + (to_decimal(payload['realized_pnl']) or ZERO))
    return {**chain,
            'size': str(size), 'notional_usd': str(notional), 'realized_pnl': pnl,
            'position_after': payload.get('position_after'),
            'fills': int(chain['fills']) + int((payload.get('meta') or {}).get('fills', 1)),
            'orders': int(chain['orders']) + 1,
            'last_ms': payload['ts_ms']}


def chain_price(chain: dict) -> Optional[Decimal]:
    size = to_decimal(chain.get('size')) or ZERO
    return (to_decimal(chain.get('notional_usd')) or ZERO) / size if size else None


# Synthetic TWAP (algo) ---------------------------------------------------------

def algo_key(payload: dict) -> Optional[tuple[str, int]]:
    """(coin, sign) for position events that can belong to an algo, else None."""
    if payload.get('type') not in ALGO_TYPES:
        return None
    return payload.get('coin'), int((payload.get('meta') or {}).get('sign', 0))


def should_start_algo(orders: list[dict], settings: dict) -> bool:
    """Entry rule (spec 5.2 합성 TWAP) over the orders of one (coin, sign) key in the window, oldest first.

    - orders from at least 3 different poll cycles
    - at least algo_min_fills orders (oid units, owner decision)
    - median order notional below algo_max_slice_pct of the current position notional;
      skipped when the position started from 0 (owner decision)
    """
    if len({o['meta'].get('poll_ms') for o in orders}) < 3:
        return False
    if len(orders) < int(settings['algo_min_fills']):
        return False
    if (to_decimal(orders[0]['meta'].get('start_position')) or ZERO) == 0:
        return True
    last = orders[-1]
    position_notional = abs(to_decimal(last.get('position_after')) or ZERO) * (to_decimal(last.get('price')) or ZERO)
    if position_notional == 0:
        return True
    slice_median = median(to_decimal(o.get('notional_usd')) or ZERO for o in orders)
    return slice_median < position_notional * Decimal(str(settings['algo_max_slice_pct'])) / 100


def new_algo_state(coin: str, sign: int, orders: Iterable[dict]) -> dict:
    orders = list(orders)
    return {
        'coin': coin, 'sign': sign,
        'started_ms': int(orders[0]['meta'].get('first_ms', orders[0]['ts_ms'])),
        'last_fill_ms': int(orders[-1]['ts_ms']),
        'fills_count': len(orders),
        'total_sz': sum((to_decimal(o.get('size')) or ZERO for o in orders), ZERO),
        'total_ntl': sum((to_decimal(o.get('notional_usd')) or ZERO for o in orders), ZERO),
    }


def add_to_algo(state: dict, payload: dict) -> dict:
    return {**state,
            'last_fill_ms': max(int(state['last_fill_ms']), int(payload['ts_ms'])),
            'fills_count': int(state['fills_count']) + 1,
            'total_sz': (to_decimal(state['total_sz']) or ZERO) + (to_decimal(payload.get('size')) or ZERO),
            'total_ntl': (to_decimal(state['total_ntl']) or ZERO) + (to_decimal(payload.get('notional_usd')) or ZERO)}


def algo_vwap(state: dict) -> Optional[Decimal]:
    size = to_decimal(state.get('total_sz')) or ZERO
    return (to_decimal(state.get('total_ntl')) or ZERO) / size if size else None


def algo_is_idle(state: dict, now_ms: int, settings: dict) -> bool:
    return now_ms - int(state['last_fill_ms']) >= int(settings['algo_idle_sec']) * 1000
