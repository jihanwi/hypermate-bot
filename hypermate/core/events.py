"""Event model, event types, delivery states and dedupe keys (spec 3.3)."""

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
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
    ALGO_START = "algo_start"
    ALGO_END = "algo_end"
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


POSITION_TYPES = (EventType.POSITION_OPEN, EventType.POSITION_INCREASE, EventType.POSITION_DECREASE,
                  EventType.POSITION_CLOSE, EventType.POSITION_FLIP, EventType.LIQUIDATION)
SPOT_TYPES = (EventType.SPOT_BUY, EventType.SPOT_SELL)

# events.delivery values (spec 3.4 / 5.2)
SENT = 'sent'
SUPPRESSED_TWAP = 'suppressed_twap'
SUPPRESSED_ALGO = 'suppressed_algo'
FILTERED_SETTINGS = 'filtered_settings'
FILTERED_THRESHOLD = 'filtered_threshold'
MUTED = 'muted'

HYPERLIQUID = 'hyperliquid'


def dedupe_key(venue: str, venue_account_id: int, event_type: EventType, source_id: str) -> str:
    """f"{venue}:{account}:{type}:{source_id}"; UNIQUE in the events table."""
    return f"{venue}:{venue_account_id}:{event_type.value}:{source_id}"


@dataclass
class Event:
    """One alertable change (spec 3.3). Fill events aggregate one order's fills (spec 5.2 체결 집계)."""
    venue: str
    venue_account_id: int
    type: EventType
    ts_ms: int
    source_id: str                          # HL: first fill tid / ledger hash:time / twapId
    coin: Optional[str] = None              # API coin, e.g. "BTC", "xyz:MU", "@107"
    side: Optional[str] = None              # LONG | SHORT (position side the event refers to)
    size: Optional[Decimal] = None          # absolute size changed
    notional_usd: Optional[Decimal] = None
    price: Optional[Decimal] = None         # VWAP
    position_after: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    meta: dict = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.venue, self.venue_account_id, self.type, self.source_id)

    # Not stored in payload_json: the events table has columns for them
    PAYLOAD_SKIP = ('venue', 'venue_account_id')

    def payload(self) -> dict:
        data = asdict(self)
        data['type'] = self.type.value
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items() if k not in self.PAYLOAD_SKIP}


def ledger_event_type(update: dict, wallet_address: str) -> Optional[EventType]:
    """Event type of a userNonFundingLedgerUpdates entry, None for types that are not events."""
    delta = update.get('delta', {})
    delta_type = delta.get('type')
    if delta_type in ('spotTransfer', 'send', 'internalTransfer', 'subAccountTransfer'):
        if delta_type == 'send' and _is_dex_collateral_move(delta):
            return EventType.DEX_COLLATERAL_TRANSFER
        out = str(delta.get('user', '')).lower() == wallet_address.lower()
        return EventType.TRANSFER_OUT if out else EventType.TRANSFER_IN
    return {
        'deposit': EventType.DEPOSIT,
        'withdraw': EventType.WITHDRAW,
        'accountClassTransfer': EventType.ACCOUNT_CLASS_TRANSFER,
        'vaultDeposit': EventType.VAULT_DEPOSIT,
        'vaultWithdraw': EventType.VAULT_WITHDRAW,
        'liquidation': EventType.LIQUIDATION,
    }.get(delta_type)


_SYSTEM_ADDRESS_RE = re.compile(r'0x(?:20|00)0{38}')


def is_system_address(address: str) -> bool:
    """HL system addresses: 0x20 or 0x00 followed by zeros (e.g. 0x2000...0000)."""
    return bool(_SYSTEM_ADDRESS_RE.fullmatch(address.lower()))


def _is_dex_collateral_move(delta: dict) -> bool:
    """'send' whose counterparty is an HL system address with a sourceDex/destinationDex set:
    collateral moving between the user's main account and a HIP-3 dex (spec 5.1 HIP-3)."""
    system_side = (is_system_address(str(delta.get('destination', '')))
                   or is_system_address(str(delta.get('user', ''))))
    return system_side and bool(delta.get('sourceDex') or delta.get('destinationDex'))
