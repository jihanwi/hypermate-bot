"""RISEx WebSocket (spec 6.3): wss://api.rise.trade/ws/, no auth, 10 req/s.

One connection for every tracked address:
  positions: {"method": "subscribe", "params": {"channel": "positions", "makers": [addr, ...],
              "market_ids": [all ids]}} -> a "snapshot" message with the current positions (human
              units), then "subscribed", then "update" messages.
  trades:    {"method": "subscribe", "params": {"channel": "trades", "market_ids": [...]}} -> "update"
              messages carrying maker and taker addresses; only those of tracked addresses are kept.
PM live check 2026-10-04: connect, subscribe and snapshot receipt worked (fixtures risex_ws_*.json).
"""

from decimal import Decimal
from typing import Optional

from hypermate.venues.stream import ReconnectingStream


class RisexStream(ReconnectingStream):
    name = 'RISEx'

    def __init__(self, url: str, connect=None, clock=None) -> None:
        super().__init__(url, connect, **({'clock': clock} if clock else {}))
        self.addresses: set[str] = set()
        self.market_ids: list[int] = []
        self._positions: dict[str, dict[str, dict]] = {}      # address -> market_id -> row (human units)
        self._snapshot_seen: set[str] = set()
        self._balances: dict[str, Optional[Decimal]] = {}
        self._trades: dict[str, list[dict]] = {}

    # Caches ------------------------------------------------------------------------

    def positions_for(self, address: str) -> Optional[list[dict]]:
        """Position rows of an address once the positions snapshot arrived, else None (REST)."""
        address = address.lower()
        if not self.connected or address not in self._snapshot_seen:
            return None
        return list(self._positions.get(address, {}).values())

    def balance_for(self, address: str) -> Optional[Decimal]:
        return self._balances.get(address.lower())

    def set_balance(self, address: str, balance: Optional[Decimal]) -> None:
        self._balances[address.lower()] = balance

    def has_trades(self, address: str) -> bool:
        return bool(self._trades.get(address.lower()))

    def drain_trades(self, address: str) -> list[dict]:
        address = address.lower()
        trades, self._trades[address] = self._trades.get(address, []), []
        return trades

    def subscriptions(self) -> int:
        return len(self.addresses)

    # Subscriptions -----------------------------------------------------------------

    def set_markets(self, market_ids) -> None:
        self.market_ids = sorted(int(m) for m in market_ids)

    async def track(self, address: str) -> None:
        """Add an address; the positions subscription is resent with the full makers list."""
        address = address.lower()
        if address in self.addresses:
            return
        self.addresses.add(address)
        if self.connected:
            self._snapshot_seen.discard(address)
            await self.send(self._positions_subscription())

    async def untrack(self, address: str) -> None:
        address = address.lower()
        self.addresses.discard(address)
        for cache in (self._positions, self._balances, self._trades):
            cache.pop(address, None)
        self._snapshot_seen.discard(address)

    def _positions_subscription(self) -> dict:
        return {'method': 'subscribe', 'params': {'channel': 'positions', 'makers': sorted(self.addresses),
                                                  'market_ids': list(self.market_ids)}}

    def _trades_subscription(self) -> dict:
        return {'method': 'subscribe', 'params': {'channel': 'trades', 'market_ids': list(self.market_ids)}}

    async def on_connected(self, ws) -> None:
        self._snapshot_seen.clear()
        if self.addresses:
            await ws.send_json(self._positions_subscription())
        await ws.send_json(self._trades_subscription())

    def on_disconnected(self) -> None:
        self._positions.clear()
        self._snapshot_seen.clear()
        self._balances.clear()

    # Messages ----------------------------------------------------------------------

    def handle(self, data: dict) -> None:
        channel = str(data.get('channel') or '')
        kind = str(data.get('type') or data.get('method') or '')
        if channel == 'positions':
            rows = data.get('data')
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list):
                if kind == 'snapshot':
                    for address in self.addresses:
                        self._positions[address] = {}
                        self._snapshot_seen.add(address)
                for row in rows:
                    address = str(row.get('account', '')).lower()
                    if address not in self.addresses:
                        continue
                    self._positions.setdefault(address, {})[str(row.get('market_id'))] = row
                    self._snapshot_seen.add(address)
                return
            if kind == 'subscribed':
                return
        elif channel == 'trades':
            row = data.get('data')
            if isinstance(row, dict):
                for address in (str(row.get('maker', '')).lower(), str(row.get('taker', '')).lower()):
                    if address in self.addresses:
                        self._trades.setdefault(address, []).append(data)
                return
            if kind == 'subscribed':
                return
        self.log_unknown(channel or kind, data)
