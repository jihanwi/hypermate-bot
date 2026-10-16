"""Phase 2E Papertrade (spec 6.7) on the probe's recordings of 2026-10-15 (papertrade_exchange_abi.json,
papertrade_logs_sample.json): selectors and topic0s against the ABI fixture, log decoding, per-side
position keys, open / close / liquidation fills, the venue-wide cycle cache, catch-up, the pending-events
gate for an open and a close inside one cycle, /add, polling with the [PAPER] alert, /health."""

import importlib.util
import pathlib
from decimal import Decimal

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
from hypermate.venues.papertrade.client import PapertradeClient, PapertradeRateLimited
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

    async def block_number(self, priority=0):
        self.calls.append(('block_number',))
        return self.tip

    async def logs(self, from_block, to_block, user=None, priority=0):
        self.calls.append(('logs', from_block, to_block, user))
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
    """One side: the coin. Long and short on the same coin: `BTC` and `BTC (short)`. Size and notional
    summed, entry size-weighted, leverage notional-weighted, uPnL from the HL mid."""
    rows = [row(0, True, 800000, 100, 10), row(0, True, 820000, 100, 30), row(1, False, 250000, 50, 100)]
    pos = paper.build_positions(rows, {'BTC': '81000', 'ETH': '2400'})
    assert set(pos) == {'BTC', 'ETH'}
    btc = pos['BTC']
    # 1,000 / 80,000 + 3,000 / 82,000 BTC, notional 4,000
    size = Decimal(1000) / 80000 + Decimal(3000) / 82000
    assert Decimal(btc['szi']) == size.quantize(Decimal('1e-8'))
    assert btc['leverage'] == '25' and btc['direction'] == 'LONG' and btc['px_decimals'] == 1
    assert Decimal(pos['ETH']['szi']) == -2 and Decimal(pos['ETH']['unrealized_pnl']) == 200      # 5,000 / 2,500
    hedged = paper.build_positions(rows + [row(0, False, 810000, 20, 50)])
    assert set(hedged) == {'BTC', 'BTC (short)', 'ETH'}
    assert Decimal(hedged['BTC (short)']['szi']) < 0 and hedged['BTC (short)']['coin'] == 'BTC (short)'
    assert hedged['BTC']['unrealized_pnl'] == 'N/A'


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
    assert (o2['coin'], o2['side'], o2['dir'], o2['sz']) == ('ETH', 'A', 'Open Short', '10')
    assert (c1['side'], c1['dir'], c1['px'], c1['closedPnl'], c1['fee'], c1['startPosition']) == (
        'A', 'Close Long', '81000', '12.5', '0.25', '0.0125')
    assert c1['oid'] == '11' and c1['tid'] == '6:0' and c1['time'] == 1_792_180_010_000
    assert (liq['side'], liq['dir'], liq['closedPnl'], liq['fee'], liq['startPosition']) == (
        'B', 'Close Short', '-25', '0', '-10')
    assert liq['liquidation'] == {'method': 'bust', 'markPx': '2525'}
    events = hl_adapter.fill_events(1, fills)
    assert [e.type.value for e in events][-2:] == ['position_close', 'liquidation']


def test_close_of_an_unseen_position_is_a_full_close():
    """A close whose open the stored snapshot never had (opened and closed between snapshots, or before
    tracking) starts at its own size and ends at 0, not below."""
    rows = {51: row(0, True, 800000, 100, 10, is_open=False)}
    fill = paper.logs_to_fills([paper.decode_trade_log(close_log(51, ADDR, 810000, 12.5, 0.25, block=6))], rows, {})[0]
    assert fill['startPosition'] == '0.0125'
    assert hl_adapter.fill_events(1, [fill])[0].type.value == 'position_close'


def test_short_fill_key_follows_the_long_side():
    """A short opened while a long is open on the coin is `BTC (short)`; from a stored hedged snapshot the
    walk starts on the right side."""
    before = paper.build_positions([row(0, True, 800000, 100, 10), row(0, False, 800000, 10, 10)])
    trades = [paper.decode_trade_log(open_log(21, ADDR, 0, False, 800000, 10, 10, block=9))]
    fill = paper.logs_to_fills(trades, {}, before)[0]
    assert fill['coin'] == 'BTC (short)' and fill['startPosition'] == '-0.00125'
    alone = paper.logs_to_fills(trades, {}, {})[0]
    assert alone['coin'] == 'BTC' and alone['startPosition'] == '0'


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
    # one block number, one getLogs (first cycle: 20 blocks), one views multicall, one positions multicall,
    # plus the daily check (implementation, instrument count, one unfiltered getLogs)
    assert chain.count('logs') == 1 and chain.count('views') == 1 and chain.count('positions') == 1
    assert ('logs', 981, 1000, None) in chain.calls
    snaps = [await ad.snapshot(VenueAccount(base.PAPERTRADE, w, w)) for w in wallets]
    assert all(set(s.positions) == {'BTC'} for s in snaps) and chain.count('views') == 1
    assert snaps[0].account_value == Decimal(10)
    chain.calls.clear()
    chain.tip = 1600                                    # 600 blocks later: 3 chunks of 250, no daily check
    await ad.prepare_cycle(wallets)
    assert [c[1:3] for c in chain.calls if c[0] == 'logs'] == [(1001, 1250), (1251, 1500), (1501, 1600)]
    assert chain.count('views') == 1 and chain.count('positions') == 1 and chain.count('rpc') == 0


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
    assert [(f['coin'], f['dir'], f['sz']) for f in fills] == [('ETH', 'Open Short', '0.02083333')]
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
