"""Event types, delivery states and dedupe keys (spec 3.3).

PR A of Phase 1 records every alert in the events table; the full Event
dataclass and the fills-based engine come with PR B.
"""

from enum import Enum
from typing import Optional


class EventType(str, Enum):
    POSITION_OPEN = "position_open"
    POSITION_INCREASE = "position_increase"
    POSITION_DECREASE = "position_decrease"
    POSITION_CLOSE = "position_close"
    POSITION_FLIP = "position_flip"
    LIQUIDATION = "liquidation"
    TWAP_START = "twap_start"
    TWAP_END = "twap_end"
    SPOT_BUY = "spot_buy"
    SPOT_SELL = "spot_sell"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    ACCOUNT_CLASS_TRANSFER = "account_class_transfer"
    DEX_COLLATERAL_TRANSFER = "dex_collateral_transfer"
    VAULT_DEPOSIT = "vault_deposit"
    VAULT_WITHDRAW = "vault_withdraw"
    PRIVACY_ON = "privacy_on"


# events.delivery values
SENT = 'sent'
SUPPRESSED_TWAP = 'suppressed_twap'

HYPERLIQUID = 'hyperliquid'


def dedupe_key(venue: str, venue_account_id: int, event_type: EventType, source_id: str) -> str:
    """f"{venue}:{account}:{type}:{source_id}"; UNIQUE in the events table."""
    return f"{venue}:{venue_account_id}:{event_type.value}:{source_id}"


# v1 snapshot-diff alert types (kept until PR B replaces the diff engine)
_POSITION_ALERT_TYPES = {
    'NEW_POSITION': EventType.POSITION_OPEN,
    'POSITION_INCREASE': EventType.POSITION_INCREASE,
    'POSITION_DECREASE': EventType.POSITION_DECREASE,
    'POSITION_CLOSED': EventType.POSITION_CLOSE,
    'LIQUIDATION': EventType.LIQUIDATION,
}

_TRANSFER_DELTAS = ('spotTransfer', 'send', 'internalTransfer', 'subAccountTransfer')
_LEDGER_TYPES = {
    'deposit': EventType.DEPOSIT,
    'withdraw': EventType.WITHDRAW,
    'accountClassTransfer': EventType.ACCOUNT_CLASS_TRANSFER,
    'vaultDeposit': EventType.VAULT_DEPOSIT,
    'vaultWithdraw': EventType.VAULT_WITHDRAW,
    'liquidation': EventType.LIQUIDATION,
}


def position_alert_type(alert: dict) -> EventType:
    return _POSITION_ALERT_TYPES[alert['alert_type']]


def ledger_event_type(update: dict, wallet_address: str) -> Optional[EventType]:
    delta = update.get('delta', {})
    delta_type = delta.get('type')
    if delta_type in _TRANSFER_DELTAS:
        out = str(delta.get('user', '')).lower() == wallet_address.lower()
        return EventType.TRANSFER_OUT if out else EventType.TRANSFER_IN
    return _LEDGER_TYPES.get(delta_type)


def spot_fill_event_type(fill: dict) -> EventType:
    return EventType.SPOT_SELL if str(fill.get('side', '')).upper() == 'A' else EventType.SPOT_BUY
