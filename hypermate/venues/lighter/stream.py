"""Lighter WebSocket stream (spec 6.2): wss://mainnet.zklighter.elliot.ai/stream.

Documented public channels: account_all_positions/<index> and account_all_trades/<index>.
The PM could not connect from their environment (CloudFront 400, possibly a proxy issue), so
the message shapes are from the docs and marked [?]. The stream is best effort: while
connected, its caches answer snapshot and trade requests at zero REST cost; on connect
failure or disconnect the adapter falls back to REST polling, and the loop keeps trying
to reconnect with backoff, resubscribing every account when it succeeds.

Unknown message shapes are logged once and ignored: a cache miss simply means REST.
"""

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

RECONNECT_MIN_SEC = 5
RECONNECT_MAX_SEC = 60


class LighterStream:
    def __init__(self, url: str, connect: Optional[Callable[[str], Awaitable[Any]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """connect(url) returns an object with async send_json(dict), async iteration over
        messages (str or dict), and async close(). The default uses aiohttp."""
        self.url = url
        self._connect = connect or _aiohttp_connect
        self.clock = clock
        self.connected = False
        self.status = 'stopped'                # stopped | connecting | connected | degraded
        self.indexes: set[int] = set()
        self._positions: dict[int, dict] = {}
        self._trades: dict[int, list[dict]] = {}
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._backoff = RECONNECT_MIN_SEC
        self.connected_since: Optional[float] = None
        self.last_error: Optional[str] = None
        self.reconnects = 0
        self._unknown_logged: set[str] = set()

    # Public state -------------------------------------------------------------------

    def positions_for(self, index: int) -> Optional[dict]:
        """Raw account dict (same shape as REST /account accounts[0]) if the stream has one."""
        return self._positions.get(index) if self.connected else None

    def has_trades(self, index: int) -> bool:
        return bool(self._trades.get(index))

    def drain_trades(self, index: int) -> list[dict]:
        trades, self._trades[index] = self._trades.get(index, []), []
        return trades

    def status_line(self) -> str:
        if self.connected and self.connected_since is not None:
            return f"WS connected {int(self.clock() - self.connected_since)}s, {len(self.indexes)} subscriptions"
        detail = f" ({self.last_error})" if self.last_error else ""
        return f"WS {self.status}, REST polling{detail}"

    # Lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self.status = 'connecting'
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.status = 'stopped'
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._close_ws()

    async def subscribe(self, index: int) -> None:
        """Track one sub-account; sent now when connected, otherwise on the next (re)connect."""
        self.indexes.add(int(index))
        if self.connected and self._ws is not None:
            try:
                await self._send_subscriptions([int(index)])
            except Exception as e:
                await self._degrade(f"subscribe failed: {e}")

    async def unsubscribe(self, index: int) -> None:
        self.indexes.discard(int(index))
        self._positions.pop(int(index), None)
        self._trades.pop(int(index), None)

    # Internals ----------------------------------------------------------------------

    async def _send_subscriptions(self, indexes) -> None:
        for index in indexes:
            for channel in (f"account_all_positions/{index}", f"account_all_trades/{index}"):
                await self._ws.send_json({'type': 'subscribe', 'channel': channel})

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        self.connected = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _degrade(self, reason: str) -> None:
        if self.connected or self.status != 'degraded':
            logger.warning(f"Lighter WS degraded to REST polling: {reason}")
        self.last_error = reason
        self.status = 'degraded'
        self._positions.clear()                 # stale after a disconnect; REST refreshes
        await self._close_ws()

    async def _run(self) -> None:
        while self.status != 'stopped':
            self.status = 'connecting'
            try:
                self._ws = await self._connect(self.url)
                await self._send_subscriptions(sorted(self.indexes))
                self.connected = True
                self.status = 'connected'
                self.connected_since = self.clock()
                self._backoff = RECONNECT_MIN_SEC
                self.reconnects += 1
                logger.info(f"Lighter WS connected, {len(self.indexes)} accounts subscribed")
                async for message in self._ws:
                    self._handle(message)
                await self._degrade('connection closed')
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._degrade(str(e))
            if self.status == 'stopped':
                return
            await asyncio.sleep(self._backoff)
            self._backoff = min(RECONNECT_MAX_SEC, self._backoff * 2)

    def _handle(self, message: Any) -> None:
        data = message
        if hasattr(message, 'data'):            # aiohttp WSMessage
            data = message.data
        if isinstance(data, (str, bytes)):
            try:
                data = json.loads(data)
            except ValueError:
                return
        if not isinstance(data, dict):
            return
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
        if channel not in self._unknown_logged:
            self._unknown_logged.add(channel)
            logger.info(f"Lighter WS: unhandled message on {channel!r} (keys {sorted(data)[:8]})")


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


async def _aiohttp_connect(url: str):
    import aiohttp
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(url, heartbeat=30)
    except Exception:
        await session.close()
        raise
    return _SessionWS(session, ws)


class _SessionWS:
    """aiohttp websocket plus the session that owns it, closed together."""

    def __init__(self, session, ws) -> None:
        self.session, self.ws = session, ws

    async def send_json(self, data: dict) -> None:
        await self.ws.send_json(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        import aiohttp
        message = await self.ws.receive()
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
            raise StopAsyncIteration
        return message

    async def close(self) -> None:
        await self.ws.close()
        await self.session.close()
