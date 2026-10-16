"""Papertrade over the public HyperEVM JSON-RPC (spec 6.7): eth_getLogs on the Exchange proxy, view
functions through Multicall3 aggregate3, the EIP-1967 implementation slot. No auth, no published limit.

Measured 2026-10-15 (docs/API_NOTES.md, Papertrade): about 7 calls back to back get -32005 (rate limited)
and it clears after 0.7 s; eth_getLogs has a slower budget; a JSON-RPC batch is capped at 20, which is
why the views go through Multicall3; a getLogs range that returns too much gets -32008 (1,000 blocks of
the unfiltered trade topics), so callers keep ranges at 250 blocks.

Selectors and topic0s are the keccak of the ABI fixture's signatures (tests/test_papertrade.py checks them).
"""

import functools
import json
import logging
from decimal import Decimal
from typing import Any, Optional

import aiohttp

from hypermate.venues.hyperliquid.scheduler import P_SNAPSHOT, WeightBudget

logger = logging.getLogger(__name__)

_json_loads = functools.partial(json.loads, parse_float=Decimal)

EXCHANGE = '0x6cd5661646289fb6e65ea5c032310fded797d0a2'
MULTICALL3 = '0xca11bde05977b3631167028862be2a173976ca11'
# keccak('eip1967.proxy.implementation') - 1
IMPLEMENTATION_SLOT = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'

TOPIC_OPENED = '0x88451ea1189f73b554f0bf39c09868560ed149b3d788fcc933ff66ba926827f3'
TOPIC_CLOSED = '0x87902ed2451849f465fc4d36a01d7f5c67b53af12f63ee85d2814a2d582f89bf'
TOPIC_LIQUIDATED = '0x0c859bc5db091ba3b9dc10c00f8dfc78b1f0b7e2566d107253ae67c069ae5de8'
TRADE_TOPICS = (TOPIC_OPENED, TOPIC_CLOSED, TOPIC_LIQUIDATED)

SEL_OPEN_IDS = '0xa25dc87f'        # getUserOpenPositionIds(address) -> uint256[]
SEL_POSITIONS = '0x99fbab88'       # positions(uint256) -> 9 words
SEL_BALANCES = '0x27e235e3'        # balances(address) -> uint256
SEL_INSTRUMENT_COUNT = '0xf28bbdb2'
SEL_AGGREGATE3 = '0x82ad56cb'      # Multicall3.aggregate3((address,bool,bytes)[])

RATE_LIMITED = -32005
TOO_LARGE = -32008
MULTICALL_CHUNK = 150              # calls per aggregate3 (calldata and response stay well under 1 MB)
BACKOFF_START_SEC = 2
BACKOFF_MAX_SEC = 30


class PapertradeAPIError(Exception):
    """Transport failure, non-200, or a JSON-RPC error."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class PapertradeRateLimited(PapertradeAPIError):
    """-32005 or HTTP 429."""


def word(data: str, i: int) -> int:
    """The i-th 32-byte word of 0x-prefixed hex data."""
    return int(data[2 + 64 * i:2 + 64 * (i + 1)] or '0', 16)


def _arg(value) -> str:
    return format(int(value, 16) if isinstance(value, str) else int(value), '064x')


def call_data(selector: str, *args) -> str:
    return selector + ''.join(_arg(a) for a in args)


def encode_aggregate3(calls: list[str], target: str = EXCHANGE) -> str:
    """aggregate3 calldata: every call to `target` with allowFailure = true."""
    def enc_bytes(b: bytes) -> str:
        return format(len(b), '064x') + b.hex() + '00' * ((32 - len(b) % 32) % 32)
    elems = []
    for data in calls:
        body = bytes.fromhex(data[2:])
        elems.append(format(int(target, 16), '064x') + format(1, '064x') + format(0x60, '064x') + enc_bytes(body))
    offsets, pos = [], 32 * len(elems)
    for e in elems:
        offsets.append(format(pos, '064x'))
        pos += len(e) // 2
    return SEL_AGGREGATE3 + format(0x20, '064x') + format(len(elems), '064x') + ''.join(offsets) + ''.join(elems)


def decode_aggregate3(raw: str) -> list[Optional[str]]:
    """(success, returnData)[] -> returnData hex, None for a failed call."""
    n = word(raw, 1)
    out: list[Optional[str]] = []
    for i in range(n):
        base = 64 + word(raw, 2 + i)                 # byte offset of the (success, bytes) tuple
        ok = word(raw, base // 32)
        length = word(raw, base // 32 + 2)
        start = 2 + 2 * (base + 96)
        out.append('0x' + raw[start:start + 2 * length] if ok else None)
    return out


def decode_uint_array(data: Optional[str]) -> list[int]:
    if not data or len(data) < 130:
        return []
    return [word(data, 2 + i) for i in range(word(data, 1))]


def decode_position(data: Optional[str]) -> Optional[dict]:
    """positions(id): owner, instrumentId, entryPrice, margin (1e18), leverage, openedAt (s), isLong,
    fromQueue [?], isOpen. The row stays after a close or a liquidation with isOpen = false."""
    if not data or len(data) < 2 + 64 * 9:
        return None
    return {'owner': '0x' + format(word(data, 0), '040x')[-40:], 'instrument_id': word(data, 1),
            'entry_raw': word(data, 2), 'margin_raw': word(data, 3), 'leverage': word(data, 4),
            'opened_at': word(data, 5), 'is_long': bool(word(data, 6)), 'from_queue': bool(word(data, 7)),
            'is_open': bool(word(data, 8))}


class PapertradeClient:
    def __init__(self, url: str, budget: Optional[WeightBudget] = None) -> None:
        self.url = url
        self.budget = budget
        self._session: Optional[aiohttp.ClientSession] = None
        self._backoff = BACKOFF_START_SEC
        self.calls = 0
        self.rate_limited = 0

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, body: dict) -> tuple[int, Any]:
        """(status, parsed body or None). Separate so tests can swap the transport."""
        await self.start()
        async with self._session.post(self.url, json=body) as response:
            try:
                parsed = await response.json(loads=_json_loads, content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                parsed = None
            return response.status, parsed

    def _limited(self) -> None:
        """-32005 / 429: pause the venue queue, 2 s doubling to 30 s while it repeats (spec 6.7)."""
        self.rate_limited += 1
        if self.budget is not None:
            self.budget.rate_limited(self._backoff)
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_SEC)

    async def rpc(self, method: str, params: list, priority: int = P_SNAPSHOT) -> Any:
        if self.budget is not None:
            await self.budget.acquire(1, priority)
        self.calls += 1
        try:
            status, body = await self._post({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        except aiohttp.ClientError as e:
            raise PapertradeAPIError(f"{method} failed: {e}") from e
        if status == 429:
            self._limited()
            raise PapertradeRateLimited(f"{method} returned HTTP 429", RATE_LIMITED)
        if status != 200 or not isinstance(body, dict):
            raise PapertradeAPIError(f"{method} returned HTTP {status}")
        error = body.get('error')
        if error:
            code = error.get('code') if isinstance(error, dict) else None
            message = error.get('message') if isinstance(error, dict) else error
            if code == RATE_LIMITED:
                self._limited()
                raise PapertradeRateLimited(f"{method}: {message}", code)
            raise PapertradeAPIError(f"{method}: {code} {message}", code)
        self._backoff = BACKOFF_START_SEC
        return body.get('result')

    async def block_number(self, priority: int = P_SNAPSHOT) -> int:
        return int(await self.rpc('eth_blockNumber', [], priority), 16)

    async def logs(self, from_block: int, to_block: int, user: Optional[str] = None,
                   priority: int = P_SNAPSHOT) -> list[dict]:
        """Trade logs (open, close, liquidation) of the Exchange in [from_block, to_block], every user or one
        (the user is topic2). Each log carries blockTimestamp (s)."""
        topics: list = [list(TRADE_TOPICS)]
        if user is not None:
            topics += [None, '0x' + format(int(user, 16), '064x')]
        result = await self.rpc('eth_getLogs', [{'address': EXCHANGE, 'fromBlock': hex(from_block),
                                                 'toBlock': hex(to_block), 'topics': topics}], priority)
        return list(result or [])

    async def multicall(self, calls: list[str], priority: int = P_SNAPSHOT,
                        block: Optional[int] = None) -> list[Optional[str]]:
        """Exchange view calls through Multicall3, MULTICALL_CHUNK per eth_call, at `block` (default latest)."""
        out: list[Optional[str]] = []
        for i in range(0, len(calls), MULTICALL_CHUNK):
            chunk = calls[i:i + MULTICALL_CHUNK]
            tag = hex(block) if block is not None else 'latest'
            raw = await self.rpc('eth_call', [{'to': MULTICALL3, 'data': encode_aggregate3(chunk)}, tag], priority)
            out += decode_aggregate3(raw)
        return out

    async def open_ids_and_balances(self, addresses: list[str], priority: int = P_SNAPSHOT,
                                    block: Optional[int] = None) -> dict[str, tuple[list[int], int]]:
        """{address: (open position ids, balance raw 1e18)} for every address in one multicall."""
        calls = []
        for address in addresses:
            calls += [call_data(SEL_OPEN_IDS, address), call_data(SEL_BALANCES, address)]
        results = await self.multicall(calls, priority, block)
        out = {}
        for i, address in enumerate(addresses):
            ids_raw, balance_raw = results[2 * i], results[2 * i + 1]
            out[address] = (decode_uint_array(ids_raw), word(balance_raw, 0) if balance_raw else 0)
        return out

    async def positions(self, ids: list[int], priority: int = P_SNAPSHOT, block: Optional[int] = None) -> dict[int, dict]:
        """{position id: positions(id) row} in one multicall (chunked)."""
        ids = list(dict.fromkeys(int(i) for i in ids))
        if not ids:
            return {}
        results = await self.multicall([call_data(SEL_POSITIONS, i) for i in ids], priority, block)
        return {i: row for i, row in ((i, decode_position(r)) for i, r in zip(ids, results)) if row is not None}

    async def instrument_count(self, priority: int = 3) -> int:
        raw = await self.rpc('eth_call', [{'to': EXCHANGE, 'data': SEL_INSTRUMENT_COUNT}, 'latest'], priority)
        return word(raw, 0)

    async def implementation(self, priority: int = 3) -> str:
        """The proxy's EIP-1967 implementation address."""
        raw = await self.rpc('eth_getStorageAt', [EXCHANGE, IMPLEMENTATION_SLOT, 'latest'], priority)
        return '0x' + format(int(raw or '0x0', 16), '040x')[-40:]
