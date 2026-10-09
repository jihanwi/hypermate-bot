"""Per-subscription notification settings (spec 9.4, PR feat/ux-settings).

Settings and mutes live on the subscription (user x wallet). Polling and event recording stay per
wallet; the filters here run at send time, per subscriber. Resolution order: subscriptions.settings_json
-> users.settings_json (/settings default) -> DEFAULTS, deep-merged. debounce_sec stays global.
"""

import copy
from decimal import Decimal
from typing import Any, Optional

from hypermate.config import Config
from hypermate.core.events import EventType
from hypermate.core.numbers import to_decimal
from hypermate.venues import base as venues

DEFAULTS: dict = {
    'venues': {venues.HYPERLIQUID: True, venues.LIGHTER: True, venues.RISEX: True, venues.ASTER: True},
    'events': {'position': True, 'liquidation': True, 'twap': True, 'spot': True, 'transfer': True,
               'deposit_withdraw': True, 'vault': False, 'account_class_transfer': False},
    'min_notional': {'mode': 'auto'},       # auto = max($100, account value x 0.5%) | fixed + usd | off
    'twap_progress': False,                 # stored only; no per-user progress edits yet
}

EVENT_KEYS = ('position', 'liquidation', 'twap', 'spot', 'transfer', 'deposit_withdraw', 'vault',
              'account_class_transfer')
MIN_NOTIONAL_CHOICES = ('auto', '100', '1000', '10000', '100000', 'off')

# event type -> settings key; types not listed (privacy_on) are always delivered
CATEGORY = {
    EventType.POSITION_OPEN: 'position', EventType.POSITION_INCREASE: 'position',
    EventType.POSITION_DECREASE: 'position', EventType.POSITION_CLOSE: 'position',
    EventType.POSITION_FLIP: 'position',
    EventType.LIQUIDATION: 'liquidation',
    EventType.TWAP_START: 'twap', EventType.TWAP_END: 'twap', EventType.ALGO_START: 'twap',
    EventType.ALGO_END: 'twap', EventType.MULTI_ALGO_ENTER: 'twap', EventType.MULTI_ALGO_EXIT: 'twap',
    EventType.SPOT_BUY: 'spot', EventType.SPOT_SELL: 'spot',
    EventType.TRANSFER_IN: 'transfer', EventType.TRANSFER_OUT: 'transfer',
    EventType.DEPOSIT: 'deposit_withdraw', EventType.WITHDRAW: 'deposit_withdraw',
    EventType.VAULT_DEPOSIT: 'vault', EventType.VAULT_WITHDRAW: 'vault',
    EventType.ACCOUNT_CLASS_TRANSFER: 'account_class_transfer',
    EventType.DEX_COLLATERAL_TRANSFER: 'account_class_transfer',
}
# the notional threshold applies to these; full closes and liquidations are always exempt
THRESHOLD_TYPES = {EventType.POSITION_OPEN, EventType.POSITION_INCREASE, EventType.POSITION_DECREASE,
                   EventType.POSITION_FLIP, EventType.SPOT_BUY, EventType.SPOT_SELL}


def deep_merge(base: dict, override: Optional[dict]) -> dict:
    """base updated by override, nested dicts merged; neither input is modified."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def resolve(subscription: Optional[dict], user: Optional[dict]) -> dict:
    """Effective settings: DEFAULTS <- users.settings_json <- subscriptions.settings_json."""
    return deep_merge(deep_merge(DEFAULTS, user), subscription)


def category_of(event_type: str) -> Optional[str]:
    try:
        return CATEGORY.get(EventType(event_type))
    except ValueError:
        return None


def threshold_usd(settings: dict, account_value: Optional[Decimal]) -> Optional[Decimal]:
    """The subscriber's notional floor: None when off; auto follows the global rule (spec 9.4 + #16)."""
    choice = settings.get('min_notional') or {}
    mode = choice.get('mode', 'auto')
    if mode == 'off':
        return None
    if mode == 'fixed':
        return to_decimal(choice.get('usd')) or Decimal(0)
    floor = Decimal(Config.MIN_NOTIONAL_FLOOR_USD)
    if account_value is None or account_value <= 0:
        return floor
    return max(floor, account_value * Config.MIN_NOTIONAL_PCT)


def allows(settings: dict, event_type: Optional[str], venue: str, notional_usd: Optional[Decimal],
           account_value: Optional[Decimal]) -> tuple[bool, str]:
    """(deliver?, reason). Reasons: 'venue', 'event', 'threshold' or '' when allowed."""
    if not (settings.get('venues') or {}).get(venue, True):
        return False, 'venue'
    if event_type is None:
        return True, ''
    category = category_of(event_type)
    if category is not None and not (settings.get('events') or {}).get(category, True):
        return False, 'event'
    try:
        kind = EventType(event_type)
    except ValueError:
        return True, ''
    if kind in THRESHOLD_TYPES:
        floor = threshold_usd(settings, account_value)
        if floor is not None and (notional_usd or Decimal(0)) < floor:
            return False, 'threshold'
    return True, ''


def set_path(settings: dict, path: tuple[str, ...], value: Any) -> dict:
    """Copy of settings with settings[path[0]][path[1]]... = value (intermediate dicts created)."""
    out = copy.deepcopy(settings)
    node = out
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return out


def min_notional_choice(settings: dict) -> str:
    """Button value of the stored min_notional: 'auto', 'off' or the fixed USD as an integer string."""
    choice = settings.get('min_notional') or {}
    mode = choice.get('mode', 'auto')
    if mode == 'fixed':
        usd = to_decimal(choice.get('usd')) or Decimal(0)
        return str(int(usd))
    return mode


def min_notional_from_choice(choice: str) -> dict:
    if choice in ('auto', 'off'):
        return {'mode': choice}
    return {'mode': 'fixed', 'usd': int(choice)}
