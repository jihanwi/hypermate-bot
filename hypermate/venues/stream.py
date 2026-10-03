"""Reconnecting WebSocket loop shared by the venue streams (Lighter, RISEx).

A stream is best effort: while connected its caches answer the adapter at zero REST cost;
on connect failure or disconnect it degrades (the adapter polls REST) and keeps retrying
with backoff, calling on_connected() again to resubscribe. Subclasses implement
on_connected(ws) and handle(data).
"""

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

RECONNECT_MIN_SEC = 5
RECONNECT_MAX_SEC = 60


class ReconnectingStream:
    name = 'stream'

    def __init__(self, url: str, connect: Optional[Callable[[str], Awaitable[Any]]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """connect(url) returns an object with async send_json(dict), async iteration over
        messages (str, bytes, dict or an aiohttp WSMessage), and async close(). Default: aiohttp."""
        self.url = url
        self._connect = connect or aiohttp_connect
        self.clock = clock
        self.connected = False
        self.status = 'stopped'                # stopped | connecting | connected | degraded
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._backoff = RECONNECT_MIN_SEC
        self.connected_since: Optional[float] = None
        self.last_error: Optional[str] = None
        self.reconnects = 0
        self._unknown_logged: set[str] = set()

    # Subclass hooks ----------------------------------------------------------------

    async def on_connected(self, ws) -> None:
        """Send the subscriptions for everything tracked."""

    def handle(self, data: dict) -> None:
        """One decoded message."""

    def on_disconnected(self) -> None:
        """Caches are stale after a disconnect; drop what REST must refresh."""

    def subscriptions(self) -> int:
        return 0

    # Public state ----------------------------------------------------------------

    def status_line(self) -> str:
        if self.connected and self.connected_since is not None:
            return f"WS connected {int(self.clock() - self.connected_since)}s, {self.subscriptions()} subscriptions"
        detail = f" ({self.last_error})" if self.last_error else ""
        return f"WS {self.status}, REST polling{detail}"

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

    async def send(self, data: dict) -> bool:
        """Send when connected; on failure the stream degrades. Returns whether it was sent."""
        if not self.connected or self._ws is None:
            return False
        try:
            await self._ws.send_json(data)
            return True
        except Exception as e:
            await self._degrade(f"send failed: {e}")
            return False

    # Internals ---------------------------------------------------------------------

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
            logger.warning(f"{self.name} WS degraded to REST polling: {reason}")
        self.last_error = reason
        self.status = 'degraded'
        self.on_disconnected()
        await self._close_ws()

    async def _run(self) -> None:
        while self.status != 'stopped':
            self.status = 'connecting'
            try:
                self._ws = await self._connect(self.url)
                await self.on_connected(self._ws)
                self.connected = True
                self.status = 'connected'
                self.connected_since = self.clock()
                self._backoff = RECONNECT_MIN_SEC
                self.reconnects += 1
                logger.info(f"{self.name} WS connected, {self.subscriptions()} subscriptions")
                async for message in self._ws:
                    data = decode(message)
                    if data is not None:
                        self.handle(data)
                await self._degrade('connection closed')
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._degrade(str(e))
            if self.status == 'stopped':
                return
            await asyncio.sleep(self._backoff)
            self._backoff = min(RECONNECT_MAX_SEC, self._backoff * 2)

    def log_unknown(self, channel: str, data: dict) -> None:
        if channel not in self._unknown_logged:
            self._unknown_logged.add(channel)
            logger.info(f"{self.name} WS: unhandled message on {channel!r} (keys {sorted(data)[:8]})")


def decode(message: Any) -> Optional[dict]:
    data = message.data if hasattr(message, 'data') else message
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


async def aiohttp_connect(url: str):
    import aiohttp
    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(url, heartbeat=30)
    except Exception:
        await session.close()
        raise
    return SessionWS(session, ws)


class SessionWS:
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
