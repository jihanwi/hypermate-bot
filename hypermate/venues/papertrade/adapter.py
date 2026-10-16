"""Papertrade VenueAdapter (spec 6.7): synthetic BTC / ETH perps on HyperEVM, read from the chain only.

Every open is its own isolated position (positionId), so there is no order book and no per-user fill API.
One cycle reads the whole venue at once (prepare_cycle, called by the venue poller before the account
loop): one eth_getLogs of the trade topics for every user since the last cycle, one Multicall3 for every
tracked wallet's open ids and balance, one Multicall3 for those positions and for the positions closed or
liquidated in the new logs. The cost does not grow with the wallet count (per-wallet calls would fill the
60/min bucket at about 9 wallets). snapshot() and fetch_events() read that cache; a wallet whose cursor is
older than the cycles that covered it (restart, buffer reset) catches up with its own user-topic getLogs.
An open and a close inside one cycle leave the snapshot unchanged, so has_pending_events() tells the
pipeline to fetch events anyway.

Positions are keyed per side with fixed keys (owner decision 2026-10-17, superseding the 2026-10-16 note):
the long side is `BTC`, the short side is `BTC (short)`, always, whether or not the other side is open. A
key that changed while a position was alive made the snapshot diff read a flip and relabelled alerts
mid-life.

Hardening (fix/papertrade-hardening): a getLogs range that answers -32008 is halved down to
MIN_LOG_BLOCKS and skipped below that, so the tip always advances; logs without blockTimestamp get the
block's timestamp from eth_getBlockByNumber (cached for the life of the buffer) or the cycle's clock; the
cycle cache is pruned to the tracked wallets; the daily check waits for the second cycle after a start so
the restart catch-up is not joined by three more calls.
"""

import json
import logging
from decimal import Decimal
from typing import Optional

from hypermate.venues import base
from hypermate.venues.base import AccountSnapshot, VenueAccount, position_entry
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid import scheduler
from hypermate.venues.papertrade.client import (EXCHANGE, TOPIC_CLOSED, TOPIC_OPENED, TRADE_TOPICS, PapertradeClient,
                                                PapertradeTooLarge, word)

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
E18 = Decimal(10) ** 18
SIZE_QUANT = Decimal('1e-8')
EXPLORER = 'https://hyperevmscan.io/address/'
SHORT_SUFFIX = ' (short)'
# instrumentId -> (symbol, priceScale): frontend constant (exchange.papertrade.xyz, 2026-10-15); the
# contract's instruments(id) has the HL assetIndex but no symbol or scale. instrumentCount() is checked daily.
INSTRUMENTS = {0: ('BTC', Decimal(10)), 1: ('ETH', Decimal(100))}
KNOWN_IMPLEMENTATION = '0x0f3febfc94876b0c895f7acecdc8f5c863b549ee'    # EIP-1967 slot, 2026-10-16
# Every Exchange topic0 seen during the probe (named or not); anything else after an upgrade is counted
KNOWN_TOPICS = frozenset((
    '0x88451ea1189f73b554f0bf39c09868560ed149b3d788fcc933ff66ba926827f3',
    '0x87902ed2451849f465fc4d36a01d7f5c67b53af12f63ee85d2814a2d582f89bf',
    '0x0c859bc5db091ba3b9dc10c00f8dfc78b1f0b7e2566d107253ae67c069ae5de8',
    '0xa32e4800a59f03b131f5fea90951bdf00d8d505ad98f48ece29d8620a0bf09b0',
    '0xe7fd56edb8bf641db0dc0a89a9d7d3a10da1fedab015ab0c51f1c418d2bc15b0',
    '0xc36b5179cb9c303b200074996eab2b3473eac370fdd7eba3bec636fe35109696',
    '0x95d1a93901f24eeaa294588802f66fa008f9f3741060c9f79e5dab4709de5e75',
    '0x02d61879cd72906e0951e3a96096407dd2b1f2cd2d063dae983bb20216cf3025',
    '0xa065d6ba54c5124cd73ce640fd3fbc0ce6eb8a4f4c9d5155347dcc461ad0cffe',
    '0x0bb3f42f9fe304326ba30302693425c1db2e2f6a4675df44372fc0af5ce73706',
    '0x18d8b7951a20bbcb2b5f45b29bb62a81963f4b1f75dac5e2493a885a6de74291',
    '0x17d88d5ce12b7103190340ee5304ff19dbf970a0289bb2684460e4a87a7b66f9',
    '0x28f3adb5fa23ae5c83c811e67caf2ec192a816e4ef898c7f90d1484f65a9717e',
    '0x5bcf15b0a82479fbcfcb086707ad8be24c96d445448a1f625310e1c3fe491759',
    '0x62db1b2b923b6f20043c492d3586df1a823e447c63e44d15dbc90c1a3bc9fcc5',
    '0x76a319771459e5329ebc7830ef30b80fc54d20327f8b4946a9539e53c46018c9',
    '0x8da9931e615ce9dfd2d7914e0cee3246839757203d34810cde64e0d0026f75c3',
    '0x9cc23a52214afa42e1ae53cdb2e79dc2d0cd33c33faca383ed3b149300adcb13',
    '0xac4ddb8d63e9d7a862c5d61ce47517623a448262414a7eef6797102104b31214',
    '0xbcd09d92c26e8078d8be2b8380bb7d1ab324d0a9ae6743e158ad8d7f734a84ba',
    '0xfce2fb41f9ff0457a0f012e62cfd6a7cff4e8439ae20008ab6979c740ba5a983',
))

FIRST_CYCLE_BLOCKS = 20            # about one 20 s cycle
LOG_CHUNK_BLOCKS = 250             # unfiltered getLogs: 500 blocks came back at 806 KB, 1,000 at -32008
MIN_LOG_BLOCKS = 8                 # -32008 halving stops here; a smaller range is skipped with an error
BUFFER_BLOCKS = 900                # logs kept per tracked wallet, about 15 minutes
WALLET_CHUNK_BLOCKS = 1000         # user-topic catch-up: 68 KB per 1,000 blocks for a busy wallet
MAX_CATCHUP_BLOCKS = 3600          # about an hour; older gaps are skipped (logged)
CHECK_INTERVAL_MS = 24 * 3600 * 1000


# --- decoding --------------------------------------------------------------------------------

def _signed(value: int) -> int:
    return value - (1 << 256) if value >> 255 else value


def decode_trade_log(log: dict) -> Optional[dict]:
    """A trade log -> {kind, position_id, user, block, index, time_ms, ...}; None for other topics.
    Shapes: docs/API_NOTES.md, Papertrade (3 topics; 6 data words for an open, 5 for close / liquidation)."""
    topics = log.get('topics') or []
    if len(topics) < 3 or topics[0] not in TRADE_TOPICS:
        return None
    data = log.get('data') or '0x'
    stamp = log.get('blockTimestamp')
    out = {'position_id': int(topics[1], 16), 'user': '0x' + topics[2][-40:].lower(),
           'block': int(log['blockNumber'], 16), 'index': int(log['logIndex'], 16),
           'time_ms': int(stamp, 16) * 1000 if stamp else None}      # None: filled by the adapter
    if topics[0] == TOPIC_OPENED:
        out.update(kind='open', instrument_id=word(data, 0), is_long=bool(word(data, 1)),
                   entry_raw=word(data, 3), margin_raw=word(data, 4), leverage=word(data, 5))
    elif topics[0] == TOPIC_CLOSED:
        out.update(kind='close', exit_raw=word(data, 0), raw_pnl=_signed(word(data, 1)),
                   adjusted_pnl=_signed(word(data, 2)), fee_raw=word(data, 4))
    else:
        out.update(kind='liquidation', price_raw=word(data, 0), bust_raw=word(data, 1))
    return out


def position_size(row: dict) -> Optional[tuple[str, Decimal, Decimal, Decimal]]:
    """(coin, size in coin, notional = margin x leverage, entry price) of an open or positions(id) row."""
    instrument = INSTRUMENTS.get(int(row['instrument_id']))
    if instrument is None:
        return None
    coin, scale = instrument
    entry = Decimal(row['entry_raw']) / scale
    notional = Decimal(row['margin_raw']) / E18 * int(row['leverage'])
    size = (notional / entry).quantize(SIZE_QUANT) if entry else ZERO
    return coin, size, notional, entry


def side_key(coin: str, is_long: bool) -> str:
    """Snapshot / fill key, fixed per side: the long side is `<coin>`, the short side `<coin> (short)`."""
    return coin if is_long else f"{coin}{SHORT_SUFFIX}"


def split_key(key: str, szi: Decimal) -> tuple[str, bool]:
    """Stored snapshot key + signed size -> (coin, is_long)."""
    if key.endswith(SHORT_SUFFIX):
        return key[:-len(SHORT_SUFFIX)], False
    return key, szi > 0


def plain(value: Decimal) -> str:
    text = format(value, 'f')
    return (text.rstrip('0').rstrip('.') or '0') if '.' in text else text


def build_positions(rows: list[dict], mids: Optional[dict] = None) -> dict[str, dict]:
    """Open positions(id) rows -> snapshot positions keyed per side. size and notional are summed, entry is
    the size-weighted average, leverage the notional-weighted average, uPnL from the HL mid when known
    (before the winning-close haircut)."""
    sides: dict[tuple[str, bool], dict] = {}
    for row in rows:
        sized = position_size(row)
        if sized is None or not row.get('is_open', True):
            continue
        coin, size, notional, entry = sized
        agg = sides.setdefault((coin, bool(row['is_long'])), {'size': ZERO, 'notional': ZERO, 'lev_ntl': ZERO,
                                                              'margin': ZERO})
        agg['size'] += size
        agg['notional'] += notional
        agg['lev_ntl'] += notional * int(row['leverage'])
        agg['margin'] += Decimal(row['margin_raw']) / E18
    positions = {}
    for (coin, is_long), agg in sides.items():
        if agg['size'] <= 0:
            continue
        szi = agg['size'] if is_long else -agg['size']
        entry = agg['notional'] / agg['size']
        mid = Decimal(str((mids or {}).get(coin))) if (mids or {}).get(coin) is not None else None
        upnl = (szi * (mid - entry)).quantize(Decimal('0.01')) if mid is not None else None
        value = (agg['size'] * mid if mid is not None else agg['notional']).quantize(Decimal('0.01'))
        key = side_key(coin, is_long)
        scale = next(s for c, s in INSTRUMENTS.values() if c == coin)
        decimals = max(0, len(str(int(scale))) - 1)
        positions[key] = position_entry(coin, szi, entry.quantize(Decimal(1) / scale), value, upnl)
        positions[key]['coin'] = key
        positions[key]['leverage'] = plain((agg['lev_ntl'] / agg['notional']).quantize(Decimal('0.1')))
        positions[key]['px_decimals'] = decimals
        positions[key]['margin'] = plain(agg['margin'])
    return positions


def logs_to_fills(trades: list[dict], rows: dict[int, dict], positions_before: dict[str, dict]) -> list[dict]:
    """Decoded trade logs (oldest first) -> HL fills. The position walk runs per (coin, side) from the
    stored snapshot; dir comes from the event. A close or liquidation takes its coin, side and size from
    positions(id) (the row stays with isOpen = false). closedPnl = adjustedPnl (after the haircut, before
    the fee, HL's gross convention) and fee = fee; a liquidation loses the margin and pays no fee
    (checked against /query trades 2026-10-16: adjustedPnlRaw = -margin, userPaidFeeRaw = 0)."""
    running: dict[tuple[str, bool], Decimal] = {}
    for key, p in positions_before.items():
        szi = Decimal(str(p.get('szi') or 0))
        if szi:
            running[split_key(key, szi)] = szi
    fills = []
    for t in trades:
        row = t if t['kind'] == 'open' else rows.get(t['position_id'])
        sized = position_size(row) if row is not None else None
        if sized is None:
            logger.warning(f"Papertrade: no position row or unknown instrument for {t['kind']} "
                           f"#{t['position_id']} of {t['user']}, skipped")
            continue
        coin, size, notional, entry = sized
        _, scale = INSTRUMENTS[int(row['instrument_id'])]
        is_long = bool(row['is_long'])
        side_id = (coin, is_long)
        start = running.get(side_id, ZERO)
        signed = size if is_long else -size
        opening = t['kind'] == 'open'
        if opening:
            end = start + signed
        elif abs(start) <= size:
            # the last position of that side (or one the stored snapshot never saw): a full close
            start, end = (start if start and (start > 0) == is_long else signed), ZERO
        else:
            end = start - signed
        fill = {'coin': side_key(coin, is_long),
                'sz': plain(size), 'side': ('B' if is_long else 'A') if opening else ('A' if is_long else 'B'),
                'time': t['time_ms'], 'startPosition': plain(start),
                'dir': ('Open ' if opening else 'Close ') + ('Long' if is_long else 'Short'),
                'oid': str(t['position_id']), 'tid': f"{t['block']}:{t['index']}", 'feeToken': 'USD',
                'crossed': True, 'leverage': int(row['leverage'])}
        if opening:
            fill.update(px=plain(entry), closedPnl='0', fee='0')
        elif t['kind'] == 'close':
            fill.update(px=plain(Decimal(t['exit_raw']) / scale), closedPnl=plain(Decimal(t['adjusted_pnl']) / E18),
                        fee=plain(Decimal(t['fee_raw']) / E18))
        else:
            px = Decimal(t['price_raw']) / scale
            fill.update(px=plain(px), closedPnl=plain(-(Decimal(row['margin_raw']) / E18)), fee='0',
                        liquidation={'method': 'bust', 'markPx': plain(px)})
        running[side_id] = end
        fills.append(fill)
    return fills


def encode_cursor(block: int, index: int) -> str:
    return json.dumps({'block': int(block), 'index': int(index)})


def decode_cursor(cursor: Optional[str]) -> Optional[tuple[int, int]]:
    if not cursor:
        return None
    try:
        data = json.loads(cursor)
        return int(data['block']), int(data.get('index', -1))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class PapertradeAdapter:
    venue = base.PAPERTRADE

    def __init__(self, client: PapertradeClient, hl=None) -> None:
        self.client = client
        self.hl = hl                                   # HL info client for allMids (uPnL estimate)
        self.tip: Optional[int] = None                 # last block read by the cycle
        self.buffer_from: Optional[int] = None         # first block the buffer covers
        self.buffer: dict[str, list[dict]] = {}        # address -> decoded trade logs, oldest first
        self.views: dict[str, tuple[list[int], int]] = {}
        self.rows: dict[int, dict] = {}
        self.mids: dict = {}
        self.covered_from: dict[str, int] = {}       # address -> first block every cycle since has read
        self.consumed: dict[str, tuple[int, int]] = {}  # address -> last log position handed to the pipeline
        self.last_check_ms = 0
        self.cycles = 0                                # cycles since this adapter started
        self.block_times: dict[int, int] = {}          # block -> timestamp (s), for logs without blockTimestamp
        self.skipped_ranges = 0                        # -32008 ranges given up on (health)
        self.implementation: Optional[str] = None
        self.instrument_count: Optional[int] = None
        self.unknown_topics = 0

    # cycle cache -------------------------------------------------------------------------------

    async def prepare_cycle(self, addresses: list[str]) -> None:
        """Read the venue once for every tracked wallet (module docstring). Raises on RPC failure; the
        poller skips the venue for that cycle."""
        tracked = sorted({a.lower() for a in addresses})
        tip = await self.client.block_number()
        if self.tip is None or tip - self.tip > BUFFER_BLOCKS:
            start = tip - FIRST_CYCLE_BLOCKS + 1 if self.tip is None else tip - BUFFER_BLOCKS + 1
            self.buffer, self.buffer_from, self.covered_from = {}, start, {}
        else:
            start = self.tip + 1
        for address in tracked:
            self.covered_from.setdefault(address, start)
        new: list[dict] = []
        wanted = set(tracked)
        catch_up = tip - start + 1 > FIRST_CYCLE_BLOCKS * 2
        warned = [False]
        for frm in range(start, tip + 1, LOG_CHUNK_BLOCKS):
            for log in await self._logs_halving(frm, min(tip, frm + LOG_CHUNK_BLOCKS - 1)):
                trade = decode_trade_log(log)
                if trade is not None and trade['user'] in wanted:
                    new.append(trade)
        await self._fill_times(new, warned)
        new.sort(key=lambda t: (t['block'], t['index']))
        for trade in new:
            self.buffer.setdefault(trade['user'], []).append(trade)
        self.tip = tip
        floor = tip - BUFFER_BLOCKS + 1
        if self.buffer_from is None or self.buffer_from < floor:
            self.buffer_from = floor
            for address in list(self.buffer):
                self.buffer[address] = [t for t in self.buffer[address] if t['block'] >= floor]
        # views at the block the logs stop at, so a position opened after it is not in the snapshot yet
        self.views = await self.client.open_ids_and_balances(tracked, block=tip) if tracked else {}
        ids = [i for open_ids, _ in self.views.values() for i in open_ids]
        ids += [t['position_id'] for t in new if t['kind'] != 'open']
        self.rows = await self.client.positions(ids, block=tip)
        self.mids = await self._mids() if any(open_ids for open_ids, _ in self.views.values()) else {}
        self.cycles += 1
        self._prune(tracked, floor)
        # the daily check waits for the second cycle after a start: the first one already carries the
        # catch-up getLogs of every wallet (restart burst, fix/papertrade-hardening); a cycle that itself
        # caught up more than two cycles' worth of blocks also waits
        if self.cycles >= 2 and not catch_up and hl_adapter.now_ms() - self.last_check_ms >= CHECK_INTERVAL_MS:
            await self._daily_check(tip)

    async def _logs_halving(self, frm: int, to: int, user: Optional[str] = None, priority: int = scheduler.P_SNAPSHOT,
                            depth: int = 0) -> list[dict]:
        """getLogs over [frm, to]; on -32008 the range is halved (this call only) down to MIN_LOG_BLOCKS,
        below that the range is skipped with an error so the tip still advances (spec 6.7)."""
        try:
            return await self.client.logs(frm, to, user=user, priority=priority)
        except PapertradeTooLarge:
            span = to - frm + 1
            if span <= MIN_LOG_BLOCKS:
                self.skipped_ranges += 1
                logger.error(f"Papertrade: getLogs [{frm}, {to}] too large even at {span} blocks, range skipped"
                             + (f" (user {user})" if user else ""))
                return []
            mid = frm + span // 2 - 1
            logger.info(f"Papertrade: getLogs [{frm}, {to}] too large, halving to {span // 2} blocks")
            return (await self._logs_halving(frm, mid, user, priority, depth + 1)
                    + await self._logs_halving(mid + 1, to, user, priority, depth + 1))

    async def _fill_times(self, trades: list[dict], warned: list) -> None:
        """blockTimestamp is a non-standard getLogs field: a log without it gets the block's timestamp from
        one eth_getBlockByNumber per distinct block (cached for the life of the buffer), and the cycle's
        clock when that fails too (warned once per cycle)."""
        missing = sorted({t['block'] for t in trades if t['time_ms'] is None and t['block'] not in self.block_times})
        for block in missing:
            try:
                stamp = await self.client.block_timestamp(block)
            except Exception as e:
                stamp = None
                if not warned[0]:
                    logger.warning(f"Papertrade: eth_getBlockByNumber failed for block {block}: {e}; "
                                   f"using the cycle's clock")
                    warned[0] = True
            if stamp is not None:
                self.block_times[block] = stamp
        now_s = hl_adapter.now_ms() // 1000
        for t in trades:
            if t['time_ms'] is None:
                stamp = self.block_times.get(t['block'])
                if stamp is None and not warned[0]:
                    logger.warning(f"Papertrade: no timestamp for block {t['block']}, using the cycle's clock")
                    warned[0] = True
                t['time_ms'] = (stamp if stamp is not None else now_s) * 1000

    def _prune(self, tracked: list[str], floor: int) -> None:
        """Keep only what the next cycle can use: positions(id) rows that are open for a tracked wallet or
        referenced by a buffered log, and per-address state of tracked wallets."""
        keep = {i for open_ids, _ in self.views.values() for i in open_ids}
        keep |= {t['position_id'] for logs in self.buffer.values() for t in logs}
        self.rows = {i: row for i, row in self.rows.items() if i in keep}
        wanted = set(tracked)
        for store in (self.buffer, self.covered_from, self.consumed, self.views):
            for address in [a for a in store if a not in wanted]:
                del store[address]
        self.block_times = {b: s for b, s in self.block_times.items() if b >= floor}

    async def _mids(self) -> dict:
        if self.hl is None:
            return {}
        try:
            return await self.hl.all_mids()
        except Exception as e:                         # the snapshot does not depend on it
            logger.warning(f"Papertrade: allMids unavailable: {e}")
            return {}

    async def _daily_check(self, tip: int) -> None:
        """Spec 6.7 upgrade detection: the proxy's implementation, the instrument count, and topic0s the
        probe never saw (one unfiltered 20-block getLogs)."""
        self.last_check_ms = hl_adapter.now_ms()
        try:
            self.implementation = await self.client.implementation()
            self.instrument_count = await self.client.instrument_count()
            result = await self.client.rpc('eth_getLogs', [{'address': EXCHANGE,
                                                            'fromBlock': hex(tip - 19), 'toBlock': hex(tip)}], 3)
        except Exception as e:
            logger.warning(f"Papertrade daily check failed: {e}")
            return
        unknown = {log['topics'][0] for log in result or [] if log.get('topics') and log['topics'][0] not in KNOWN_TOPICS}
        self.unknown_topics = len(unknown)
        if self.implementation != KNOWN_IMPLEMENTATION:
            logger.warning(f"Papertrade Exchange implementation changed: {self.implementation} "
                           f"(known {KNOWN_IMPLEMENTATION}); events may not decode")
        if self.instrument_count != len(INSTRUMENTS):
            logger.warning(f"Papertrade instrumentCount is {self.instrument_count}, the bot knows "
                           f"{len(INSTRUMENTS)}; unknown instruments are skipped")
        if unknown:
            logger.warning(f"Papertrade: {len(unknown)} unknown topic0s on the Exchange: {sorted(unknown)}")

    async def _views_of(self, address: str) -> tuple[list[int], int]:
        """Cached by the cycle, or read now (a wallet added between cycles, /positions)."""
        if address in self.views:
            return self.views[address]
        views = (await self.client.open_ids_and_balances([address]))[address]
        missing = [i for i in views[0] if i not in self.rows]
        self.rows.update(await self.client.positions(missing))
        return views

    # VenueAdapter -------------------------------------------------------------------------------

    async def resolve(self, evm_address: str) -> list[VenueAccount]:
        """Active when the address has open positions or a balance (one multicall). A wallet that closed
        everything and withdrew is not found; the daily rescan sees it once it trades again."""
        address = evm_address.lower()
        open_ids, balance = (await self.client.open_ids_and_balances([address], priority=scheduler.P_LEDGER))[address]
        if not open_ids and balance <= 0:
            return []
        return [VenueAccount(self.venue, address, address,
                             meta={'open_positions': len(open_ids), 'balance': plain(Decimal(balance) / E18)})]

    async def snapshot(self, account: VenueAccount) -> AccountSnapshot:
        open_ids, balance = await self._views_of(account.address)
        rows = [self.rows[i] for i in open_ids if i in self.rows]
        positions = build_positions(rows, self.mids)
        margin = sum((Decimal(r['margin_raw']) / E18 for r in rows if r.get('is_open', True)), ZERO)
        return AccountSnapshot(positions, Decimal(balance) / E18 + margin,
                               extra={'balance': plain(Decimal(balance) / E18), 'open_positions': len(open_ids)})

    def has_pending_events(self, account: VenueAccount) -> bool:
        """True when the buffer holds logs the pipeline has not taken yet, or the wallet has not been
        fetched since the start (catch-up)."""
        consumed = self.consumed.get(account.address)
        if consumed is None:
            return True
        return any((t['block'], t['index']) > consumed for t in self.buffer.get(account.address, []))

    async def fetch_events(self, account: VenueAccount, cursor: Optional[str],
                           positions_before: Optional[dict] = None) -> tuple[list[dict], Optional[str]]:
        """Trade logs after the cursor ({block, index}) up to the cycle's tip, oldest first. The first poll
        only sets the cursor. Blocks before the wallet's covered_from come from a user-topic getLogs, at
        most MAX_CATCHUP_BLOCKS back."""
        address = account.address
        tip = self.tip if self.tip is not None else await self.client.block_number()
        done = encode_cursor(tip, 1 << 30)
        position = decode_cursor(cursor)
        if position is None:
            self.consumed[address] = (tip, 1 << 30)
            return [], done
        trades = [t for t in self.buffer.get(address, []) if (t['block'], t['index']) > position]
        covered = min(self.covered_from.get(address, tip + 1), tip + 1)
        if position[0] + 1 < covered:
            frm = position[0] + 1
            if covered - frm > MAX_CATCHUP_BLOCKS:
                logger.warning(f"Papertrade: {address} is {covered - frm} blocks behind, catching up the "
                               f"last {MAX_CATCHUP_BLOCKS} only")
                frm = covered - MAX_CATCHUP_BLOCKS
            older = []
            for chunk in range(frm, covered, WALLET_CHUNK_BLOCKS):
                logs = await self._logs_halving(chunk, min(covered - 1, chunk + WALLET_CHUNK_BLOCKS - 1),
                                                user=address, priority=scheduler.P_FILLS)
                older += [t for t in map(decode_trade_log, logs) if t is not None and (t['block'], t['index']) > position]
            await self._fill_times(older, [False])
            older.sort(key=lambda t: (t['block'], t['index']))
            seen = {(t['block'], t['index']) for t in trades}
            trades = [t for t in older if (t['block'], t['index']) not in seen] + trades
        missing = [t['position_id'] for t in trades if t['kind'] != 'open' and t['position_id'] not in self.rows]
        if missing:
            self.rows.update(await self.client.positions(missing, priority=scheduler.P_FILLS))
        self.consumed[address] = (tip, 1 << 30)
        return logs_to_fills(trades, self.rows, positions_before or {}), done

    def explorer_url(self, account: VenueAccount) -> str:
        return f"{EXPLORER}{account.address}"

    def cost(self, op: str) -> int:
        return {'resolve': 1, 'snapshot': 0, 'events': 0}.get(op, 1)

    async def health(self) -> dict:
        budget = self.client.budget
        parts = [f"RPC, bucket {budget.per_minute}/min" if budget is not None else "RPC",
                 f"{self.client.calls} calls, {self.client.rate_limited} rate-limited"]
        if self.implementation is not None and self.implementation != KNOWN_IMPLEMENTATION:
            parts.append(f"IMPLEMENTATION CHANGED {self.implementation}")
        if self.instrument_count is not None and self.instrument_count != len(INSTRUMENTS):
            parts.append(f"instruments {self.instrument_count} (known {len(INSTRUMENTS)})")
        if self.unknown_topics:
            parts.append(f"{self.unknown_topics} unknown topic0s")
        if self.skipped_ranges:
            parts.append(f"{self.skipped_ranges} log ranges skipped (-32008)")
        return {'mode': 'rpc', 'detail': ', '.join(parts)}
