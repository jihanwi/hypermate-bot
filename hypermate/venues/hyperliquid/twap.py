"""Hyperliquid TWAP tracking (spec 5.2 TWAP).

Active TWAPs come from webData2.twapStates; the final status of a TWAP that
disappeared comes from twapHistory. Functions here are pure; the pipeline
does the I/O and keeps twap_active in the DB.
"""

from decimal import Decimal
from typing import Iterable, Optional

from hypermate.core.numbers import to_decimal

# TWAP side -> sign of the position change it causes
SIDE_SIGN = {'B': 1, 'A': -1}

# Marker stored in twap_active.state_json when a TWAP left twapStates but
# twapHistory had no final entry yet; END is emitted on the next cycle.
END_PENDING = '_end_pending'


def parse_twap_states(web_data2: dict) -> dict[str, dict]:
    """{twap_id (str): state} from webData2.twapStates = [[twapId, state], ...]."""
    return {str(twap_id): state for twap_id, state in web_data2.get('twapStates') or []}


def mark_prices(web_data2: dict) -> dict[str, Decimal]:
    """Main-dex mark prices from webData2 (meta.universe and assetCtxs are parallel arrays)."""
    universe = (web_data2.get('meta') or {}).get('universe') or []
    contexts = web_data2.get('assetCtxs') or []
    prices = {}
    for asset, ctx in zip(universe, contexts):
        px = to_decimal((ctx or {}).get('markPx'))
        if px is not None:
            prices[asset['name']] = px
    return prices


def final_entry(history: list, twap_id: str) -> Optional[dict]:
    """Latest non-'activated' twapHistory entry for twap_id, or None if not there yet."""
    entries = [h for h in history
               if str(h.get('twapId')) == twap_id
               and (h.get('status') or {}).get('status') != 'activated']
    return max(entries, key=lambda h: h.get('time', 0)) if entries else None


def _alert_sign(alert: dict) -> Optional[int]:
    """Sign of the szi change behind a v1 diff alert; None for alerts never suppressed."""
    szi = to_decimal(alert.get('szi')) or Decimal(0)
    direction = 1 if szi > 0 else -1
    alert_type = alert.get('alert_type')
    if alert_type in ('NEW_POSITION', 'POSITION_INCREASE'):
        return direction
    if alert_type in ('POSITION_DECREASE', 'POSITION_CLOSED'):
        return -direction
    return None  # LIQUIDATION and anything unknown always go out


def is_suppressed(alert: dict, twap_states: Iterable[dict]) -> bool:
    """True if an active TWAP on the alert's coin trades in the same direction as the change."""
    sign = _alert_sign(alert)
    if sign is None:
        return False
    return any(state.get('coin') == alert.get('coin') and SIDE_SIGN.get(state.get('side')) == sign
               for state in twap_states)
