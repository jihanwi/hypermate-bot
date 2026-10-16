"""Phase 2E Papertrade (spec 6.7) on the probe's recordings of 2026-10-15 (papertrade_exchange_abi.json,
papertrade_logs_sample.json): selectors and topic0s against the ABI fixture, log decoding, per-side
position keys, open / close / liquidation fills, the venue-wide cycle cache, catch-up, the pending-events
gate for an open and a close inside one cycle, /add, polling with the [PAPER] alert, /health."""

import importlib.util
import logging
import pathlib
from decimal import Decimal
from typing import Optional

import pytest

from hypermate.bot import commands
from hypermate.core import pipeline, poller
from hypermate.core.venues import resolve_summary, resolve_wallet, split_venue_prefix
from hypermate.venues import base
from hypermate.venues.base import VenueAccount
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid.venue import HyperliquidVenue
from hypermate.venues.papertrade import adapter as paper
from hypermate.venues.papertrade import client as pclient
from hypermate.venues.papertrade.client import PapertradeClient, PapertradeRateLimited, PapertradeTooLarge
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, load_fixture, make_context, make_update

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDR = '0x97f290319734d0ce22215079417f3fe6a6439932'
OTHER = '0x' + 'e' * 40
T0 = 1_792_180_000_000
E18 = 10 ** 18


def probe_module():
    spec = importlib.util.spec_from_file_location('probe_papertrade', ROOT / 'scripts' / 'probe_papertrade.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- synthetic logs ---------------------------------------------------------------------------

def _w(value: int) -> str:
    return format(value % (1 << 256), '064x')


def _log(topic, pid, user, words, block, index, ts):
    return {'blockNumber': hex(block), 'logIndex': hex(index), 'blockTimestamp': hex(ts),
            'topics': [topic, '0x' + _w(pid), '0x' + _w(int(user, 16))], 'data': '0x' + ''.join(_w(x) for x in words)}


def open_log(pid, user, instrument, is_long, entry_raw, margin_usd, leverage, block, index=0, ts=1_792_180_000):
    return _log(pclient.TOPIC_OPENED, pid, user,
                [instrument, int(is_long), 0, entry_raw, int(margin_usd * E18), leverage], block, index, ts)


def close_log(pid, user, exit_raw, pnl_usd, fee_usd, block, index=0, ts=1_792_180_010):
    pnl = int(Decimal(str(pnl_usd)) * E18)
    return _log(pclient.TOPIC_CLOSED, pid, user, [exit_raw, pnl, pnl, 0, int(Decimal(str(fee_usd)) * E18)], block, index, ts)


def liq_log(pid, user, price_raw, block, index=0, ts=1_792_180_020):
    return _log(pclient.TOPIC_LIQUIDATED, pid, user, [price_raw, price_raw - 100, 0, E18 // 4, E18 // 4], block, index, ts)


def row(instrument, is_long, entry_raw, margin_usd, leverage, owner=ADDR, is_open=True):
    return {'owner': owner, 'instrument_id': instrument, 'entry_raw': entry_raw, 'margin_raw': int(margin_usd * E18),
            'leverage': leverage, 'opened_at': 0, 'is_long': is_long, 'from_queue': False, 'is_open': is_open}


class FakeChain:
    """Stands in for PapertradeClient: a block height, logs by block, open ids / balances, position rows."""

    def __init__(self, tip=1000):
        self.tip = tip
        self.all_logs: list[dict] = []
        self.open_ids: dict[str, list[int]] = {}
        self.balances: dict[str, int] = {}
        self.rows: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.budget = None
        self.rate_limited = 0
        self.too_large = 0
        self.too_large_over: Optional[int] = None     # a getLogs span above this answers -32008
        self.block_stamps: dict[int, int] = {}
        self.block_lookup_fails = False

    async def block_number(self, priority=0):
        self.calls.append(('block_number',))
        return self.tip

    async def block_timestamp(self, block, priority=0):
        self.calls.append(('block_timestamp', block))
        if self.block_lookup_fails:
            raise RuntimeError('rpc down')
        return self.block_stamps.get(block)

    async def logs(self, from_block, to_block, user=None, priority=0):
        self.calls.append(('logs', from_block, to_block, user))
        if self.too_large_over is not None and to_block - from_block + 1 > self.too_large_over:
            self.too_large += 1
            raise PapertradeTooLarge('query returned more than 10000 results', pclient.TOO_LARGE)
        out = [lg for lg in self.all_logs if from_block <= int(lg['blockNumber'], 16) <= to_block]
        if user is not None:
            out = [lg for lg in out if lg['topics'][2][-40:] == user[-40:]]
        return out

    async def open_ids_and_balances(self, addresses, priority=0, block=None):
        self.calls.append(('views', tuple(addresses)))
        return {a: (list(self.open_ids.get(a, [])), self.balances.get(a, 0)) for a in addresses}

    async def positions(self, ids, priority=0, block=None):
        ids = list(dict.fromkeys(ids))
        if ids:
            self.calls.append(('positions', tuple(ids)))
        return {i: self.rows[i] for i in ids if i in self.rows}

    async def implementation(self, priority=3):
        return paper.KNOWN_IMPLEMENTATION

    async def instrument_count(self, priority=3):
        return 2

    async def rpc(self, method, params, priority=0):
        self.calls.append(('rpc', method))
        return []

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


# --- ABI, client encoding ---------------------------------------------------------------------

def test_selectors_and_topics_match_the_abi_fixture():
    probe = probe_module()
    abi = probe.load_abi()
    functions = {e['name']: probe.selector(e) for e in abi['abi'] if e['type'] == 'function'}
    events = {e['name']: probe.topic0(e) for e in abi['abi'] if e['type'] == 'event'}
    assert functions['getUserOpenPositionIds'] == pclient.SEL_OPEN_IDS
    assert functions['positions'] == pclient.SEL_POSITIONS
    assert functions['balances'] == pclient.SEL_BALANCES
    assert functions['instrumentCount'] == pclient.SEL_INSTRUMENT_COUNT
    assert (events['PositionOpened'], events['PositionClosed'], events['Liquidated']) == pclient.TRADE_TOPICS
    assert set(abi['observed_topic0']) | set(abi['unknown_topic0']) == paper.KNOWN_TOPICS
    assert probe.keccak256(b'eip1967.proxy.implementation').hex() == format(int(pclient.IMPLEMENTATION_SLOT, 16) + 1, '064x')


def test_aggregate3_round_trip():
    calls = [pclient.call_data(pclient.SEL_OPEN_IDS, ADDR), pclient.call_data(pclient.SEL_POSITIONS, 7)]
    data = pclient.encode_aggregate3(calls)
    assert data.startswith(pclient.SEL_AGGREGATE3) and pclient.word(data[8:], 1) == 2
    ids = '0x' + _w(0x20) + _w(2) + _w(5) + _w(9)
    pos = '0x' + ''.join(_w(x) for x in (int(ADDR, 16), 1, 235365, 25 * E18, 100, 0, 0, 0, 1))
    def enc(ret):
        body = bytes.fromhex(ret[2:])
        return _w(1) + _w(0x40) + _w(len(body)) + body.hex() + '00' * ((32 - len(body) % 32) % 32)
    elems = [enc(ids), enc(pos), _w(0) + _w(0x40) + _w(0)]
    offsets, at = [], 32 * len(elems)
    for e in elems:
        offsets.append(_w(at))
        at += len(e) // 2
    raw = '0x' + _w(0x20) + _w(len(elems)) + ''.join(offsets) + ''.join(elems)
    out = pclient.decode_aggregate3(raw)
    assert pclient.decode_uint_array(out[0]) == [5, 9] and out[2] is None
    decoded = pclient.decode_position(out[1])
    assert decoded['owner'] == ADDR and decoded['instrument_id'] == 1 and not decoded['is_long'] and decoded['is_open']


async def test_rate_limit_pauses_and_backs_off():
    pauses = []

    class Budget:
        per_minute = 60

        async def acquire(self, weight, priority):
            return None

        def rate_limited(self, retry):
            pauses.append(retry)

    client = PapertradeClient('http://unused', Budget())
    replies = [(200, {'error': {'code': -32005, 'message': 'rate limited'}})] * 2 + [(200, {'result': '0x10'})]

    async def post(body):
        return replies.pop(0)
    client._post = post
    for _ in range(2):
        with pytest.raises(PapertradeRateLimited):
            await client.block_number()
    assert await client.block_number() == 16
    assert pauses == [2, 4] and client._backoff == pclient.BACKOFF_START_SEC and client.rate_limited == 2


# --- decoding and mapping ---------------------------------------------------------------------

def test_fixture_logs_decode_like_the_probe():
    sample = load_fixture('papertrade_logs_sample.json')
    kinds = {'PositionOpened': 'open', 'PositionClosed': 'close', 'Liquidated': 'liquidation'}
    for item in sample['logs']:
        t = paper.decode_trade_log(item['log'])
        args = item['decoded']['args']
        assert t['kind'] == kinds[item['decoded']['event']]
        assert t['position_id'] == int(args['positionId']) and t['user'] == args['user']
        assert t['time_ms'] == int(item['log']['blockTimestamp'], 16) * 1000
        if t['kind'] == 'open':
            assert (t['entry_raw'], t['margin_raw'], t['leverage']) == (
                int(args['entryPrice']), int(args['margin']), int(args['leverage']))
            coin, size, notional, entry = paper.position_size(t)
            assert float(notional) == pytest.approx(item['human']['notional_usd'])
            assert float(entry) == pytest.approx(item['human']['entry_px'])
        elif t['kind'] == 'close':
            assert t['adjusted_pnl'] == int(args['adjustedPnl']) and t['fee_raw'] == int(args['fee'])
        else:
            assert t['price_raw'] == int(args['price']) and t['bust_raw'] == int(args['bustPrice'])


def test_positions_are_keyed_per_side():
    """Fixed keys per side (owner decision 2026-10-17): the long side is `BTC`, the short side `ETH (short)`,
    whether or not the other side is open. Size and notional summed, entry size-weighted, leverage
    notional-weighted, uPnL from the HL mid."""
    rows = [row(0, True, 800000, 100, 10), row(0, True, 820000, 100, 30), row(1, False, 250000, 50, 100)]
    pos = paper.build_positions(rows, {'BTC': '81000', 'ETH': '2400'})
    assert set(pos) == {'BTC', 'ETH (short)'}
    btc = pos['BTC']
    # 1,000 / 80,000 + 3,000 / 82,000 BTC, notional 4,000
    size = Decimal(1000) / 80000 + Decimal(3000) / 82000
    assert Decimal(btc['szi']) == size.quantize(Decimal('1e-8'))
    assert btc['leverage'] == '25' and btc['direction'] == 'LONG' and btc['px_decimals'] == 1
    assert Decimal(pos['ETH (short)']['szi']) == -2 and Decimal(pos['ETH (short)']['unrealized_pnl']) == 200   # 5,000 / 2,500
    assert pos['ETH (short)']['coin'] == 'ETH (short)'
    hedged = paper.build_positions(rows + [row(0, False, 810000, 20, 50)])
    assert set(hedged) == {'BTC', 'BTC (short)', 'ETH (short)'}
    assert Decimal(hedged['BTC (short)']['szi']) < 0 and hedged['BTC (short)']['coin'] == 'BTC (short)'
    assert hedged['BTC']['unrealized_pnl'] == 'N/A'
    assert paper.side_key('BTC', True) == 'BTC' and paper.side_key('BTC', False) == 'BTC (short)'
    assert paper.split_key('BTC (short)', Decimal(-1)) == ('BTC', False) and paper.split_key('BTC', Decimal(1)) == ('BTC', True)


def test_open_close_liquidation_fills():
    rows = {11: row(0, True, 800000, 100, 10, is_open=False), 12: row(1, False, 250000, 25, 1000, is_open=False)}
    trades = [paper.decode_trade_log(lg) for lg in (
        open_log(11, ADDR, 0, True, 800000, 100, 10, block=5),
        open_log(12, ADDR, 1, False, 250000, 25, 1000, block=5, index=1),
        close_log(11, ADDR, 810000, 12.5, 0.25, block=6),
        liq_log(12, ADDR, 252500, block=7))]
    fills = paper.logs_to_fills(trades, rows, {})
    o1, o2, c1, liq = fills
    assert (o1['coin'], o1['side'], o1['dir'], o1['px'], o1['sz'], o1['startPosition']) == (
        'BTC', 'B', 'Open Long', '80000', '0.0125', '0')
    assert (o2['coin'], o2['side'], o2['dir'], o2['sz']) == ('ETH (short)', 'A', 'Open Short', '10')
    assert (c1['side'], c1['dir'], c1['px'], c1['closedPnl'], c1['fee'], c1['startPosition']) == (
        'A', 'Close Long', '81000', '12.5', '0.25', '0.0125')
    assert c1['oid'] == '11' and c1['tid'] == '6:0' and c1['time'] == 1_792_180_010_000
    assert (liq['side'], liq['dir'], liq['closedPnl'], liq['fee'], liq['startPosition']) == (
        'B', 'Close Short', '-25', '0', '-10')
    assert liq['liquidation'] == {'method': 'bust', 'markPx': '2525'} and liq['coin'] == 'ETH (short)'
    events = hl_adapter.fill_events(1, fills)
    assert [e.type.value for e in events][-2:] == ['position_close', 'liquidation']


def test_close_of_an_unseen_position_is_a_full_close():
    """A close whose open the stored snapshot never had (opened and closed between snapshots, or before
    tracking) starts at its own size and ends at 0, not below."""
    rows = {51: row(0, True, 800000, 100, 10, is_open=False)}
    fill = paper.logs_to_fills([paper.decode_trade_log(close_log(51, ADDR, 810000, 12.5, 0.25, block=6))], rows, {})[0]
    assert fill['startPosition'] == '0.0125'
    assert hl_adapter.fill_events(1, [fill])[0].type.value == 'position_close'


def test_short_fill_key_is_stable():
    """A short is `BTC (short)` whether or not a long is open (owner decision 2026-10-17); from a stored
    hedged snapshot the walk starts on the short side."""
    before = paper.build_positions([row(0, True, 800000, 100, 10), row(0, False, 800000, 10, 10)])
    trades = [paper.decode_trade_log(open_log(21, ADDR, 0, False, 800000, 10, 10, block=9))]
    fill = paper.logs_to_fills(trades, {}, before)[0]
    assert fill['coin'] == 'BTC (short)' and fill['startPosition'] == '-0.00125'
    alone = paper.logs_to_fills(trades, {}, {})[0]
    assert alone['coin'] == 'BTC (short)' and alone['startPosition'] == '0'
    long_only = paper.logs_to_fills(trades, {}, paper.build_positions([row(0, True, 800000, 100, 10)]))[0]
    assert long_only['coin'] == 'BTC (short)' and long_only['startPosition'] == '0'


# --- adapter cycle ----------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    state = {'ms': T0}
    monkeypatch.setattr(hl_adapter, 'now_ms', lambda: state['ms'])
    monkeypatch.setattr(commands, 'now_ms', lambda: state['ms'])
    return state


async def test_cycle_reads_the_venue_once_for_every_wallet(clock):
    chain = FakeChain(tip=1000)
    wallets = ['0x' + f'{i:040x}' for i in range(1, 31)]
    for i, w in enumerate(wallets):
        chain.open_ids[w] = [100 + i]
        chain.rows[100 + i] = row(0, True, 800000, 10, 10, owner=w)
    ad = paper.PapertradeAdapter(chain)
    await ad.prepare_cycle(wallets)
    # one block number, one getLogs (first cycle: 20 blocks), one views multicall, one positions multicall;
    # the daily check (implementation, instrument count, one unfiltered getLogs) waits for the second cycle
    assert chain.count('logs') == 1 and chain.count('views') == 1 and chain.count('positions') == 1
    assert chain.count('rpc') == 0 and ad.cycles == 1
    assert ('logs', 981, 1000, None) in chain.calls
    snaps = [await ad.snapshot(VenueAccount(base.PAPERTRADE, w, w)) for w in wallets]
    assert all(set(s.positions) == {'BTC'} for s in snaps) and chain.count('views') == 1
    assert snaps[0].account_value == Decimal(10)
    chain.calls.clear()
    chain.tip = 1600                                    # 600 blocks later: 3 chunks of 250; a catch-up cycle, no check
    await ad.prepare_cycle(wallets)
    assert [c[1:3] for c in chain.calls if c[0] == 'logs'] == [(1001, 1250), (1251, 1500), (1501, 1600)]
    assert chain.count('views') == 1 and chain.count('positions') == 1 and chain.count('rpc') == 0
    chain.calls.clear()
    chain.tip = 1620                                    # an ordinary cycle: the daily check runs now
    await ad.prepare_cycle(wallets)
    assert chain.count('rpc') == 1 and ad.last_check_ms == T0


async def test_events_baseline_buffer_pending_and_catch_up(clock):
    chain = FakeChain(tip=1000)
    ad = paper.PapertradeAdapter(chain)
    account = VenueAccount(base.PAPERTRADE, ADDR, ADDR, 1)
    await ad.prepare_cycle([ADDR])
    assert ad.has_pending_events(account)                 # not fetched since the start
    fills, cursor = await ad.fetch_events(account, None)
    assert fills == [] and paper.decode_cursor(cursor)[0] == 1000 and not ad.has_pending_events(account)
    # an open and a close inside one cycle: the snapshot does not change, the gate still fetches
    chain.rows[31] = row(0, True, 800000, 50, 20, is_open=False)
    chain.all_logs += [open_log(31, ADDR, 0, True, 800000, 50, 20, block=1005),
                       close_log(31, ADDR, 790000, -12.5, 0, block=1010)]
    chain.tip = 1020
    await ad.prepare_cycle([ADDR])
    assert (await ad.snapshot(account)).positions == {} and ad.has_pending_events(account)
    fills, cursor = await ad.fetch_events(account, cursor)
    assert [f['dir'] for f in fills] == ['Open Long', 'Close Long'] and fills[1]['closedPnl'] == '-12.5'
    assert paper.decode_cursor(cursor)[0] == 1020 and not ad.has_pending_events(account)
    # restart: a fresh adapter covers only its first cycle; the stored cursor catches up per wallet
    chain.all_logs.append(open_log(32, ADDR, 1, False, 240000, 10, 5, block=1500))
    chain.open_ids[ADDR] = [32]
    chain.rows[32] = row(1, False, 240000, 10, 5)
    chain.tip = 3000
    restarted = paper.PapertradeAdapter(chain)
    await restarted.prepare_cycle([ADDR])
    chain.calls.clear()
    fills, cursor = await restarted.fetch_events(account, cursor, {})
    assert [c[1:] for c in chain.calls if c[0] == 'logs'] == [(1021, 2020, ADDR), (2021, 2980, ADDR)]
    assert [(f['coin'], f['dir'], f['sz']) for f in fills] == [('ETH (short)', 'Open Short', '0.02083333')]
    assert paper.decode_cursor(cursor)[0] == 3000


async def test_catch_up_is_capped(clock, caplog):
    chain = FakeChain(tip=100_000)
    ad = paper.PapertradeAdapter(chain)
    await ad.prepare_cycle([ADDR])
    chain.calls.clear()
    await ad.fetch_events(VenueAccount(base.PAPERTRADE, ADDR, ADDR, 1), paper.encode_cursor(10, 0), {})
    spans = [c[1:3] for c in chain.calls if c[0] == 'logs']
    assert spans[0][0] == 99_981 - paper.MAX_CATCHUP_BLOCKS and spans[-1][1] == 99_980 and len(spans) == 4
    assert 'blocks behind' in caplog.text


async def test_daily_check_flags_an_upgrade(clock, caplog):
    chain = FakeChain()

    async def changed(priority=3):
        return '0x' + '1' * 40

    async def unfiltered(method, params, priority=0):
        return [{'topics': ['0x' + 'ab' * 32]}, {'topics': [pclient.TOPIC_OPENED]}]
    chain.implementation, chain.rpc = changed, unfiltered
    ad = paper.PapertradeAdapter(chain)
    await ad.prepare_cycle([ADDR])
    assert ad.implementation is None                    # first cycle: no check yet
    chain.tip += 10
    await ad.prepare_cycle([ADDR])
    health = await ad.health()
    assert 'IMPLEMENTATION CHANGED' in health['detail'] and '1 unknown topic0s' in health['detail']
    assert 'implementation changed' in caplog.text


# --- bot integration --------------------------------------------------------------------------

async def call(handler, repo, bot_data, user_id, *args):
    update = make_update(user_id)
    await handler(update, make_context(bot_data, args=args))
    for r in update.message.replies:
        check_telegram_html(r['text'])
    return [r['text'] for r in update.message.replies]


async def test_add_poll_alert_and_health(repo, clock):
    chain = FakeChain(tip=1000)
    chain.open_ids[ADDR] = [41]
    chain.balances[ADDR] = 5 * E18
    chain.rows[41] = row(0, True, 800000, 100, 10)
    ad = paper.PapertradeAdapter(chain)
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.PAPERTRADE: ad}}
    assert await call(commands.add_wallet, repo, bot_data, 5, ADDR, 'pt') == [
        '✅ Wallet added as <b>pt</b> · HL ✗ · Papertrade ✅']
    assert split_venue_prefix(f'papertrade:{OTHER}') == (base.PAPERTRADE, OTHER)
    assert await call(commands.add_wallet, repo, bot_data, 5, f'papertrade:{OTHER}', 'pinned') == [
        '✅ Wallet added as <b>pinned</b> · Papertrade ✅ (added as given, no activity seen)']
    key = next(r['key'] for r in await repo.venue_accounts_of(ADDR) if r['venue'] == base.PAPERTRADE)
    account = VenueAccount(base.PAPERTRADE, ADDR, ADDR, key)
    bot = FakeBot()
    await ad.prepare_cycle([ADDR])
    await pipeline.poll_venue_account(bot, repo, ad, account)       # baseline
    assert bot.sent == []
    # the long is closed with a profit: one [PAPER] alert linking HyperEVMScan
    chain.rows[41] = {**chain.rows[41], 'is_open': False}
    chain.open_ids[ADDR] = []
    chain.all_logs.append(close_log(41, ADDR, 820000, 25, 0.5, block=1010))
    chain.tip = 1020
    clock['ms'] = T0 + 20_000
    await ad.prepare_cycle([ADDR])
    activity, _ = await pipeline.poll_venue_account(bot, repo, ad, account)
    assert activity and len(bot.sent) == 1
    text = bot.sent[0]['text']
    check_telegram_html(text)
    assert text.startswith(f'[PAPER] 🔒 <b><a href="https://hyperevmscan.io/address/{ADDR}">pt</a></b> closed LONG $BTC')
    assert '(0.0125 BTC) @ 82,000' in text and 'realized 🟢 +$25' in text
    assert poller.venue_interval_sec(base.PAPERTRADE, ad, 1) == 20
    health = await poller.venue_health(make_context(bot_data))
    assert health[base.PAPERTRADE]['mode'] == 'rpc'
    results = await resolve_wallet(repo, {base.PAPERTRADE: ad}, ADDR, clock['ms'])
    assert resolve_summary(results) == 'Papertrade ✅'                 # the $5 balance keeps it active
    chain.balances[ADDR] = 0
    assert resolve_summary(await resolve_wallet(repo, {base.PAPERTRADE: ad}, ADDR, clock['ms'])) == 'Papertrade ✗'


# --- hardening (fix/papertrade-hardening) -----------------------------------------------------

async def test_consecutive_rpc_calls_are_spaced_by_min_gap():
    """7 calls back to back never reach the RPC closer than PAPERTRADE_MIN_GAP_MS, bucket or not."""
    clock = {'now': 100.0}

    async def sleep(seconds):
        clock['now'] += seconds

    class Budget:
        per_minute = 36

        async def acquire(self, weight, priority):
            return None

    client = PapertradeClient('http://unused', Budget(), min_gap_ms=150, clock=lambda: clock['now'], sleep=sleep)
    hits = []

    async def post(body):
        hits.append(clock['now'])
        return 200, {'result': '0x10'}
    client._post = post
    for _ in range(7):
        await client.block_number()
    gaps = [b - a for a, b in zip(hits, hits[1:])]
    assert len(hits) == 7 and all(g >= 0.15 - 1e-9 for g in gaps) and max(gaps) < 0.16
    # no gap configured: calls go straight through
    free = PapertradeClient('http://unused', None, min_gap_ms=0, clock=lambda: clock['now'], sleep=sleep)
    free._post = post
    before = clock['now']
    for _ in range(3):
        await free.block_number()
    assert clock['now'] == before


async def test_too_large_halves_the_range_and_the_tip_advances(clock, caplog):
    """-32008 on a 250-block catch-up: 250 -> 125 -> 63 / 62 until the RPC accepts; the cycle's tip moves
    on. When even MIN_LOG_BLOCKS is refused the range is skipped with an error and the tip still moves."""
    chain = FakeChain(tip=1000)
    ad = paper.PapertradeAdapter(chain)
    await ad.prepare_cycle([ADDR])
    chain.all_logs.append(open_log(61, ADDR, 0, True, 800000, 10, 10, block=1100))
    chain.open_ids[ADDR] = [61]
    chain.rows[61] = row(0, True, 800000, 10, 10)
    chain.too_large_over = 64
    chain.tip = 1250
    chain.calls.clear()
    await ad.prepare_cycle([ADDR])
    spans = [(c[1], c[2], c[2] - c[1] + 1) for c in chain.calls if c[0] == 'logs']
    assert [s[2] for s in spans] == [250, 125, 62, 63, 125, 62, 63]              # halved in place, in order
    assert chain.too_large == 3 and ad.tip == 1250 and ad.skipped_ranges == 0
    assert [t['position_id'] for t in ad.buffer[ADDR]] == [61]                    # the log was still read
    # the next cycle starts at 250 again (the halving applies to one cycle only)
    chain.too_large_over = None
    chain.tip = 1500
    chain.calls.clear()
    await ad.prepare_cycle([ADDR])
    assert [c[1:3] for c in chain.calls if c[0] == 'logs'] == [(1251, 1500)]
    # nothing is accepted: halved down to 8 blocks, then skipped, tip advanced, counted for /health
    chain.too_large_over = 0
    chain.tip = 1532
    caplog.set_level(logging.ERROR)
    await ad.prepare_cycle([ADDR])
    assert ad.tip == 1532 and ad.skipped_ranges == 4 and 'range skipped' in caplog.text
    assert '4 log ranges skipped (-32008)' in (await ad.health())['detail']
    # the per-wallet catch-up uses the same halving
    chain.too_large_over = 500
    chain.calls.clear()
    await ad.fetch_events(VenueAccount(base.PAPERTRADE, ADDR, ADDR, 1), paper.encode_cursor(400, 0), {})
    user_spans = [(c[2] - c[1] + 1) for c in chain.calls if c[0] == 'logs' and c[3] == ADDR]
    assert user_spans == [580, 290, 290]                                           # 401..980, halved once


async def test_cycle_cache_is_pruned(clock):
    """Closed rows and removed wallets leave rows / covered_from / consumed / buffer after a cycle."""
    chain = FakeChain(tip=1000)
    gone = '0x' + 'd' * 40
    chain.open_ids = {ADDR: [71], gone: [72]}
    chain.rows = {71: row(0, True, 800000, 10, 10), 72: row(1, True, 250000, 10, 10, owner=gone),
                  73: row(0, False, 800000, 5, 10, is_open=False)}
    ad = paper.PapertradeAdapter(chain)
    await ad.prepare_cycle([ADDR, gone])
    await ad.fetch_events(VenueAccount(base.PAPERTRADE, gone, gone, 2), None)
    assert set(ad.rows) == {71, 72} and gone in ad.consumed and gone in ad.covered_from
    # ADDR closes 71 (its row is referenced by a buffered log, so it stays this cycle); `gone` is untracked
    chain.all_logs.append(close_log(71, ADDR, 810000, 1, 0.1, block=1010))
    chain.rows[71] = {**chain.rows[71], 'is_open': False}
    chain.open_ids[ADDR] = []
    chain.tip = 1020
    await ad.prepare_cycle([ADDR])
    assert set(ad.rows) == {71} and gone not in ad.consumed and gone not in ad.covered_from and gone not in ad.buffer
    assert ad.views.keys() == {ADDR}
    # once the buffer no longer holds the close, the closed row goes too
    chain.tip = 1020 + paper.BUFFER_BLOCKS + 5
    await ad.prepare_cycle([ADDR])
    assert ad.rows == {} and ad.buffer.get(ADDR, []) == []


async def test_missing_block_timestamp_falls_back_to_the_block_then_the_clock(clock, caplog):
    chain = FakeChain(tip=1000)
    ad = paper.PapertradeAdapter(chain)
    await ad.prepare_cycle([ADDR])
    account = VenueAccount(base.PAPERTRADE, ADDR, ADDR, 1)
    _, cursor = await ad.fetch_events(account, None)
    logs = [open_log(81, ADDR, 0, True, 800000, 10, 10, block=1005, index=0),
            open_log(82, ADDR, 0, True, 800000, 10, 10, block=1005, index=1),
            open_log(83, ADDR, 0, True, 800000, 10, 10, block=1007)]
    for lg in logs:
        del lg['blockTimestamp']
    chain.all_logs += logs
    chain.block_stamps = {1005: 1_792_190_000, 1007: 1_792_190_002}
    chain.tip = 1010
    caplog.set_level(logging.WARNING)
    await ad.prepare_cycle([ADDR])
    assert [c[1] for c in chain.calls if c[0] == 'block_timestamp'] == [1005, 1007]      # one per distinct block
    fills, cursor = await ad.fetch_events(account, cursor)
    assert [f['time'] for f in fills] == [1_792_190_000_000, 1_792_190_000_000, 1_792_190_002_000]
    assert caplog.text == ''
    # the block is cached: a later log of the same block costs nothing
    chain.calls.clear()
    chain.all_logs.append({**open_log(84, ADDR, 0, True, 800000, 10, 10, block=1007, index=5)})
    del chain.all_logs[-1]['blockTimestamp']
    chain.tip = 1012
    await ad.prepare_cycle([ADDR])
    assert not any(c[0] == 'block_timestamp' for c in chain.calls)
    # the block read fails: the cycle's clock, one warning
    chain.block_lookup_fails = True
    chain.all_logs.append({**open_log(85, ADDR, 0, True, 800000, 10, 10, block=1015)})
    del chain.all_logs[-1]['blockTimestamp']
    chain.tip = 1016
    clock['ms'] = T0 + 60_000
    await ad.prepare_cycle([ADDR])
    fills, _ = await ad.fetch_events(account, cursor)
    assert fills[-1]['time'] == T0 + 60_000 and caplog.text.count("using the cycle's clock") == 1


async def test_fills_come_from_fetch_events_never_from_the_snapshot_diff(repo, clock):
    """A long opening while a short is open: exactly one Open Long fill on `BTC`, nothing on `BTC (short)`
    (no synthetic diff fill, no flip), even though the snapshot changed."""
    chain = FakeChain(tip=1000)
    chain.open_ids[ADDR] = [91]
    chain.rows[91] = row(0, False, 800000, 10, 10)
    ad = paper.PapertradeAdapter(chain)
    await repo.add_subscription(5, ADDR, 'pt', T0 - 60_000)
    key, _ = await repo.ensure_venue_account(ADDR, base.PAPERTRADE, ADDR, True, T0)
    account = VenueAccount(base.PAPERTRADE, ADDR, ADDR, key)
    bot = FakeBot()
    await ad.prepare_cycle([ADDR])
    await pipeline.poll_venue_account(bot, repo, ad, account)                     # baseline: the short
    assert (await repo.get_snapshot(key))[''].keys() == {'BTC (short)'}
    chain.all_logs.append(open_log(92, ADDR, 0, True, 800000, 20, 10, block=1010))
    chain.open_ids[ADDR] = [91, 92]
    chain.rows[92] = row(0, True, 800000, 20, 10)
    chain.tip = 1020
    clock['ms'] = T0 + 20_000
    await ad.prepare_cycle([ADDR])
    activity, _ = await pipeline.poll_venue_account(bot, repo, ad, account)
    assert activity and len(bot.sent) == 1 and 'opened LONG $BTC' in bot.sent[0]['text']
    events = [e for e in await repo.events_since(key, 0) if e['type'].startswith('position_')]
    assert [(e['type'], e['payload']['coin'], e['payload']['meta']['dir']) for e in events] == [
        ('position_open', 'BTC', 'Open Long')]
    assert (await repo.get_snapshot(key))[''].keys() == {'BTC', 'BTC (short)'}
    # the fall-through is guarded: an adapter with has_pending_events whose fetch_events answers None
    # gets no synthetic fills either
    async def none(account, cursor, positions_before=None):
        return None
    ad.fetch_events = none
    chain.open_ids[ADDR] = [92]
    chain.tip = 1040
    clock['ms'] = T0 + 40_000
    await ad.prepare_cycle([ADDR])
    await pipeline.poll_venue_account(bot, repo, ad, account)
    assert len(bot.sent) == 1 and len([e for e in await repo.events_since(key, 0) if e['type'].startswith('position_')]) == 1


def test_weight_budget_log_label():
    from hypermate.venues.hyperliquid.scheduler import WeightBudget
    assert WeightBudget(60).name == 'HL' and WeightBudget(36, name='Papertrade').name == 'Papertrade'
