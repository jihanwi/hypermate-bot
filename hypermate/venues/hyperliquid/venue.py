"""Hyperliquid as a VenueAdapter (spec 3.2): a thin wrapper, the Phase 0/1 logic stays in adapter.py.

resolve() counts the wallet as active on HL when it has a perp position, a non-zero account
value, or a spot balance. The HL polling loop (poller.poll_account) keeps its own path
(HIP-3 dexs, spot balances, native TWAPs); this class serves /add, /rescan, /positions and
the daily rescan like the other venues.
"""

from decimal import Decimal
from typing import Optional

from hypermate.core.links import hl_address_url
from hypermate.venues import base
from hypermate.venues.base import AccountSnapshot, VenueAccount
from hypermate.venues.hyperliquid import adapter
from hypermate.venues.hyperliquid.client import HyperliquidClient


class HyperliquidVenue:
    venue = base.HYPERLIQUID

    def __init__(self, client: HyperliquidClient) -> None:
        self.client = client

    async def resolve(self, evm_address: str) -> list[VenueAccount]:
        address = evm_address.lower()
        state = await self.client.clearinghouse_state(address)
        spot = adapter.parse_spot_balances(await self.client.spot_clearinghouse_state(address))
        value = adapter.parse_account_value(state) or Decimal(0)
        if adapter.parse_positions(state) or value > 0 or spot:
            return [VenueAccount(self.venue, address, address)]
        return []

    async def snapshot(self, account: VenueAccount) -> AccountSnapshot:
        state = await self.client.clearinghouse_state(account.account_ref)
        return AccountSnapshot(adapter.parse_positions(state), adapter.parse_account_value(state), raw=state)

    async def fetch_events(self, account: VenueAccount, cursor: Optional[str]) -> tuple[list[dict], Optional[str]]:
        fills, new_cursor = await adapter.fetch_fills(self.client, account.account_ref, int(cursor or 0))
        return fills, str(new_cursor)

    def explorer_url(self, account: VenueAccount) -> str:
        return hl_address_url(account.account_ref)

    def cost(self, op: str) -> int:
        return {'resolve': 4, 'snapshot': 2, 'events': 20}.get(op, 20)

    async def health(self) -> dict:
        return {'mode': 'rest', 'detail': 'REST polling (weight budget)'}
