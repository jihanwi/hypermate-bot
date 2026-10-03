"""Lighter WebSocket stream (spec 6.2): wss://mainnet.zklighter.elliot.ai/stream.

Documented public channels: account_all_positions/<index> and account_all_trades/<index>.
The PM could not connect from their environment (CloudFront 400, possibly a proxy issue), so
the message shapes are from the docs and marked [?]. Unknown message shapes are logged once
and ignored: a cache miss simply means REST. Reconnect handling is in venues/stream.py.
"""

from typing import Optional

from hypermate.venues.stream import ReconnectingStream


class LighterStream(ReconnectingStream):
    name = 'Lighter'

    def __init__(self, url: str, connect=None, clock=None) -> None:
        super().__init__(url, connect, **({'clock': clock} if clock else {}))
        self.indexes: set[int] = set()
        self._positions: dict[int, dict] = {}
        self._trades: dict[int, list[dict]] = {}

    # Caches ------------------------------------------------------------------------

    def positions_for(self, index: int) -> Optional[dict]:
        """Raw account dict (same shape as REST /account accounts[0]) if the stream has one."""
        return self._positions.get(index) if self.connected else None

    def has_trades(self, index: int) -> bool:
        return bool(self._trades.get(index))

    def drain_trades(self, index: int) -> list[dict]:
        trades, self._trades[index] = self._trades.get(index, []), []
        return trades

    def subscriptions(self) -> int:
        return len(self.indexes)

    # Subscriptions -----------------------------------------------------------------

    async def subscribe(self, index: int) -> None:
        """Track one sub-account; sent now when connected, otherwise on the next (re)connect."""
        self.indexes.add(int(index))
        if self.connected:
            for data in self._subscription_messages([int(index)]):
                if not await self.send(data):
                    break

    async def unsubscribe(self, index: int) -> None:
        self.indexes.discard(int(index))
        self._positions.pop(int(index), None)
        self._trades.pop(int(index), None)

    @staticmethod
    def _subscription_messages(indexes) -> list[dict]:
        return [{'type': 'subscribe', 'channel': f"{channel}/{index}"}
                for index in indexes for channel in ('account_all_positions', 'account_all_trades')]

    async def on_connected(self, ws) -> None:
        for data in self._subscription_messages(sorted(self.indexes)):
            await ws.send_json(data)

    def on_disconnected(self) -> None:
        self._positions.clear()

    # Messages ----------------------------------------------------------------------

    def handle(self, data: dict) -> None:
        channel = str(data.get('channel') or data.get('type') or '')
        index = _index_of(channel, data)
        if index is None or index not in self.indexes:
            return
        if 'account_all_positions' in channel:
            positions = data.get('positions')
            if isinstance(positions, dict):
                positions = list(positions.values())
            if isinstance(positions, list):
                self._positions[index] = {**data.get('account', {}), 'positions': positions}
                return
        elif 'account_all_trades' in channel:
            trades = data.get('trades')
            if isinstance(trades, dict):
                trades = [t for ts in trades.values() for t in (ts if isinstance(ts, list) else [ts])]
            if isinstance(trades, list):
                self._trades.setdefault(index, []).extend(t for t in trades if isinstance(t, dict))
                return
        self.log_unknown(channel, data)

    def _handle(self, message) -> None:
        """Raw message (str or dict) -> handle(); used by the adapter tests."""
        from hypermate.venues.stream import decode
        data = decode(message)
        if data is not None:
            self.handle(data)


def _index_of(channel: str, data: dict) -> Optional[int]:
    if '/' in channel and channel.rsplit('/', 1)[1].isdigit():
        return int(channel.rsplit('/', 1)[1])
    for key in ('account', 'account_index', 'index'):
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, dict) and isinstance(value.get('index'), int):
            return value['index']
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None
