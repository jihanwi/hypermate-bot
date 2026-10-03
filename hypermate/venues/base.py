"""VenueAdapter interface (spec 3.2) shared by Hyperliquid, Lighter, RISEx and Aster.

Adapters know nothing about Telegram and keep no state: cursors and snapshots live in
the DB and are passed in. They raise on 429/5xx; the scheduler's budget does backoff.

Events: every adapter returns fills in the Hyperliquid userFills shape (coin, px, sz,
side B|A, time, startPosition, dir, oid, tid, closedPnl, fee, liquidation?), so the
aggregation, debounce and algo pipeline (spec 5.2) is shared. A venue without fills
returns a snapshot only and the pipeline diffs it (spec 6.1).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Protocol, runtime_checkable

HYPERLIQUID = 'hyperliquid'
LIGHTER = 'lighter'
RISEX = 'risex'
ASTER = 'aster'

BADGES = {HYPERLIQUID: '[HL]', LIGHTER: '[LTR]', RISEX: '[RISE]', ASTER: '[ASTER]'}
NAMES = {HYPERLIQUID: 'HL', LIGHTER: 'Lighter', RISEX: 'RISEx', ASTER: 'Aster'}


@dataclass
class VenueAccount:
    venue: str
    account_ref: str                      # HL: address, Lighter: sub-account index, RISEx/Aster: address
    address: str                          # the EVM wallet it belongs to (lowercase)
    venue_account_id: Optional[int] = None
    meta: dict = field(default_factory=dict)

    def label(self, alias: str) -> str:
        """Alias as shown in alerts; Lighter sub-accounts carry '#<index>' (spec 10)."""
        return f"{alias}#{self.account_ref}" if self.venue == LIGHTER else alias


@dataclass
class AccountSnapshot:
    """Positions keyed by coin in the HL snapshot shape ({szi, direction, entry_px, position_value,
    unrealized_pnl, coin}) so repo.snapshots and the diff engine are shared."""
    positions: dict[str, dict]
    account_value: Optional[Decimal] = None
    raw: Any = None
    extra: dict = field(default_factory=dict)


@runtime_checkable
class VenueAdapter(Protocol):
    venue: str

    async def resolve(self, evm_address: str) -> list[VenueAccount]:
        """Accounts of this venue for the address; [] when there is no activity."""

    async def snapshot(self, account: VenueAccount) -> AccountSnapshot:
        """Current positions and account value (cheapest endpoint)."""

    async def fetch_events(self, account: VenueAccount, cursor: Optional[str],
                           positions_before: Optional[dict] = None) -> tuple[list[dict], Optional[str]]:
        """Fills after the cursor in the HL fill shape, oldest first, and the new cursor. Idempotent.
        positions_before is the last stored snapshot ({coin: position}) for venues whose fills carry
        no position-before (RISEx)."""

    def explorer_url(self, account: VenueAccount) -> str:
        ...

    def cost(self, op: str) -> int:
        """Weight of one operation in this venue's bucket (op: resolve | snapshot | events)."""

    async def health(self) -> dict:
        """Venue line for /health: at least {'mode': 'rest' | 'ws' | ..., 'detail': str}."""


def position_entry(coin: str, szi: Decimal, entry_px: Optional[Decimal], position_value: Optional[Decimal],
                   unrealized_pnl: Optional[Decimal]) -> dict:
    """One snapshot position in the shared shape (values as strings, JSON-safe)."""
    return {
        'szi': str(szi),
        'direction': 'LONG' if szi > 0 else 'SHORT',
        'entry_px': str(entry_px) if entry_px is not None else 'N/A',
        'position_value': str(position_value) if position_value is not None else 'N/A',
        'coin': coin,
        'unrealized_pnl': str(unrealized_pnl) if unrealized_pnl is not None else 'N/A',
    }


def as_clearinghouse_state(snapshot: AccountSnapshot) -> dict:
    """The snapshot as an HL-style clearinghouseState, so /positions renders every venue the same way."""
    return {
        'assetPositions': [{'type': 'oneWay', 'position': {
            'coin': p['coin'], 'szi': p['szi'], 'entryPx': p.get('entry_px'),
            'positionValue': p.get('position_value'), 'unrealizedPnl': p.get('unrealized_pnl'),
        }} for p in snapshot.positions.values()],
        'marginSummary': {'accountValue': str(snapshot.account_value) if snapshot.account_value is not None else None},
    }


def fill_direction(side: str, start: Decimal, size: Decimal) -> tuple[str, Decimal]:
    """HL 'dir' label and the position after the fill, from the side (B buys, A sells), the signed
    position before and the fill size."""
    delta = size if side == 'B' else -size
    end = start + delta
    if start == 0:
        return ('Open Long' if delta > 0 else 'Open Short'), end
    if (start > 0) == (end > 0) or end == 0:
        if abs(end) > abs(start):
            return ('Open Long' if start > 0 else 'Open Short'), end
        return ('Close Long' if start > 0 else 'Close Short'), end
    return ('Long > Short' if start > 0 else 'Short > Long'), end
