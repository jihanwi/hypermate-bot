"""Phase 2B RISEx (spec 6.3): 18-dec and human-unit parsing, WS snapshot and trades channel, start
position accumulation, REST fallback, resolve and /positions."""

import asyncio
import json
from decimal import Decimal

import pytest

from hypermate.bot import commands
from hypermate.core import pipeline, poller
from hypermate.venues import base
from hypermate.venues.base import VenueAccount
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid.scheduler import WeightBudget
from hypermate.venues.hyperliquid.venue import HyperliquidVenue
from hypermate.venues.risex import adapter as risex
from hypermate.venues.risex.client import RisexAPIError, RisexClient, RisexRateLimited
from hypermate.venues.risex.stream import RisexStream
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, load_fixture, make_context, make_update

ADDR = '0x068731bac41a937440e14859f0fa8c3b453c2266'
T0 = 1_791_041_100_000


def markets():
    data = load_fixture('risex_markets.json')['data']['markets']
    return {str(m['market_id']): {'name': m['config']['name']} for m in data}


# Parsing ---------------------------------------------------------------------------

def test_rest_and_ws_positions_parse_to_the_same_snapshot():
    rest = risex.parse_rest_positions(load_fixture('risex_positions.json')['data'], markets())
    ws_rows = load_fixture('risex_ws_positions.json')[0]['data']
    ws = risex.parse_ws_positions(ws_rows, markets())
    for positions in (rest, ws):
        assert list(positions) == ['BTC']
        assert positions['BTC']['szi'] == '-0.020892' and positions['BTC']['direction'] == 'SHORT'
        assert Decimal(positions['BTC']['entry_px']) == Decimal('84828')
        assert Decimal(positions['BTC']['position_value']) == Decimal('1771.6949080272')
        assert positions['BTC']['leverage'] == '10'
    assert risex.from_wei('10000000000000000000') == Decimal(10)
    assert risex.from_wei('-20892000000000000') == Decimal('-0.020892')
    assert risex.account_value(Decimal('241.231363689661806191'), rest) == Decimal('241.231363689661806191')
    assert risex.account_value(None, {}) is None
    # a zero-size row (closed position) removes the coin
    assert risex.parse_ws_positions(ws_rows + [{**ws_rows[0], 'size': '0'}], markets()) == {}


def test_trade_history_to_fills_with_accumulated_start_positions():
    trades = load_fixture('risex_trade_history.json')['data']['trades']
    first = risex.rest_trade_to_fill(trades[0], Decimal(0), markets())
    assert (first['coin'], first['side'], first['sz'], first['px']) == ('BTC', 'A', '0.020892', '84828')
    assert first['time'] == 1791041079000 and first['oid'] == trades[0]['order_id'] and first['crossed'] is True
    assert first['closedPnl'] == '-0.5316679728' and first['fee'] == '0.5316679728'
    # oldest first, starting flat: the first sell opens a short, later trades accumulate
    fills = [risex.rest_trade_to_fill(t, Decimal(0), markets()) for t in reversed(trades)]
    fills = risex.accumulate(fills, {})
    assert fills[0]['dir'].startswith('Open') and fills[0]['startPosition'] == '0'
    running = Decimal(0)
    for f in fills:
        assert Decimal(f['startPosition']) == running
        running += Decimal(f['sz']) if f['side'] == 'B' else -Decimal(f['sz'])
    events = hl_adapter.fill_events(1, fills)
    assert sum(e.meta['fills'] for e in events) == len(trades)
    assert events[0].type.value == 'position_open'
    # starting from a stored snapshot instead of flat: a buy reduces the short
    reduced = risex.accumulate([{'coin': 'BTC', 'side': 'B', 'sz': '0.01', 'px': '84000', 'time': 1, 'tid': 'x',
                                 'oid': 'x', 'startPosition': '0', 'dir': ''}], {'BTC': {'szi': '-0.020892'}})
    assert reduced[0]['dir'] == 'Close Short' and reduced[0]['startPosition'] == '-0.020892'
    liquidation = risex.rest_trade_to_fill({**trades[0], 'is_liquidation': True}, Decimal('-0.02'), markets())
    assert liquidation['liquidation']['method'] == 'market'


def test_ws_trade_update_filters_by_tracked_side():
    update = load_fixture('risex_ws_trades.json')[1]
    taker = risex.ws_trade_to_fill(update, ADDR, Decimal(0), markets())        # ADDR is the taker, maker bought
    assert taker['side'] == 'A' and taker['crossed'] is True and taker['oid'] == update['data']['taker_order_id']
    assert taker['fee'] == '0.5316679728' and taker['time'] == 1791041077813 and taker['coin'] == 'BTC'
    maker = risex.ws_trade_to_fill(update, update['data']['maker'], Decimal(0), markets())
    assert maker['side'] == 'B' and maker['crossed'] is False and maker['fee'] == '-0.0886113288'
    assert risex.ws_trade_to_fill(update, '0x' + '1' * 40, Decimal(0), markets()) is None
    flipped = risex.ws_trade_to_fill({**update, 'data': {**update['data'], 'maker_side': 1}}, ADDR, Decimal(0), markets())
    assert flipped['side'] == 'B'


# Client -----------------------------------------------------------------------------

async def test_client_budget_and_error_handling(monkeypatch):
    clock = {'now': 100.0}

    async def sleep(s):
        clock['now'] += s

    budget = WeightBudget(2400, clock=lambda: clock['now'], sleep=sleep)
    client = RisexClient('http://unused', budget)
    responses = [(200, load_fixture('risex_positions.json')), (500, None), (429, None),
                 (200, load_fixture('risex_markets.json'))]

    async def fake_fetch(path, params):
        return responses.pop(0)

    monkeypatch.setattr(client, '_fetch', fake_fetch)
    assert (await client.positions(ADDR))['total_count'] == 1
    assert await client.cross_margin_balance(ADDR) is None              # 500: unknown account, not an error
    with pytest.raises(RisexRateLimited):
        await client.trade_history(ADDR)
    assert budget.paused_until > clock['now']
    budget.paused_until = 0
    m = await client.markets()
    assert len(m) == 38 and m['1']['name'] == 'BTC/USDC'
    assert await client.markets() == m                                  # cached, no request
    with pytest.raises(RisexAPIError):
        responses.append((503, None))
        await client.positions(ADDR)


# Fake client / stream for adapter and pipeline tests --------------------------------

class FakeRisexClient:
    def __init__(self):
        self.positions_by = {}
        self.balances = {}
        self.trades_by = {}
        self.calls = []

    async def positions(self, account, page=1, page_size=100, priority=0):
        self.calls.append(('positions', account))
        return {'positions': self.positions_by.get(account.lower(), []), 'total_count': 0}

    async def cross_margin_balance(self, account, priority=0):
        self.calls.append(('balance', account))
        return self.balances.get(account.lower())

    async def trade_history(self, account, limit=100, market_id=None, priority=2):
        self.calls.append(('trade-history', account, limit, market_id))
        return sorted(self.trades_by.get(account.lower(), []), key=lambda t: -int(t['time']))[:limit]

    async def markets(self, priority=1):
        self.calls.append(('markets',))
        return markets()


def rest_trade(trade_id, time_ms, side, size, price='84828'):
    return {'id': trade_id, 'market_id': '1', 'order_id': f"o-{trade_id}", 'side': side, 'price': price,
            'size': size, 'fee': '0', 'liquidity_indicator': 'TAKER', 'time': str(time_ms * 1_000_000),
            'is_liquidation': False, 'realized_pnl': '0', 'position_side': side}


def wei(value: str) -> str:
    return str(int(Decimal(value) * Decimal(10) ** 18))


async def test_fetch_events_rest_baseline_then_incremental():
    client = FakeRisexClient()
    client.trades_by[ADDR] = [rest_trade('t1', T0 - 60_000, 'SELL', '0.020892')]
    client.positions_by[ADDR] = load_fixture('risex_positions.json')['data']['positions']   # market 1 held
    ad = risex.RisexAdapter(client)
    account = VenueAccount(base.RISEX, ADDR, ADDR, 1)
    fills, cursor = await ad.fetch_events(account, None, {})
    assert fills == [] and json.loads(cursor) == {'id': 't1', 'ms': T0 - 60_000}
    client.trades_by[ADDR] += [rest_trade('t2', T0, 'BUY', '0.01'), rest_trade('t3', T0 + 1000, 'BUY', '0.02')]
    fills, cursor = await ad.fetch_events(account, cursor, {'BTC': {'szi': '-0.020892'}})
    assert [(f['tid'], f['dir'], f['startPosition']) for f in fills] == [
        ('t2', 'Close Short', '-0.020892'), ('t3', 'Short > Long', '-0.010892')]
    assert json.loads(cursor) == {'id': 't3', 'ms': T0 + 1000}
    # an account with no trades yet still gets a baseline cursor, so later trades count as new
    client.trades_by['0x' + 'a' * 40] = []
    fills, cursor = await ad.fetch_events(VenueAccount(base.RISEX, '0x' + 'a' * 40, '0x' + 'a' * 40, 2), None, {})
    assert fills == [] and json.loads(cursor)['ms'] == 1


async def test_resolve_rules():
    client = FakeRisexClient()
    ad = risex.RisexAdapter(client)
    assert await ad.resolve(ADDR) == []                                      # nothing
    client.trades_by[ADDR] = [rest_trade('t1', T0, 'SELL', '0.01')]
    assert [a.account_ref for a in await ad.resolve(ADDR)] == [ADDR]         # trade history only
    client.positions_by[ADDR] = load_fixture('risex_positions.json')['data']['positions']
    client.calls.clear()
    assert [a.account_ref for a in await ad.resolve(ADDR.upper())] == [ADDR]  # positions, lowercased
    assert not any(c[0] == 'trade-history' for c in client.calls)


class FakeWS:
    def __init__(self, messages, hang=True):
        self.sent, self.messages, self.hang, self.closed = [], list(messages), hang, False

    async def send_json(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            await asyncio.sleep(0)
            return self.messages.pop(0)
        if self.hang:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


async def test_stream_snapshot_and_trades_then_rest_fallback(monkeypatch):
    monkeypatch.setattr('hypermate.venues.stream.RECONNECT_MIN_SEC', 0)
    ws_positions = load_fixture('risex_ws_positions.json')
    ws_trades = load_fixture('risex_ws_trades.json')
    sockets = [FakeWS(ws_positions + ws_trades, hang=False), FakeWS(ws_positions)]
    connections = []

    async def connect(url):
        ws = sockets.pop(0)
        connections.append(ws)
        return ws

    stream = RisexStream('wss://unused', connect=connect)
    stream.set_markets([1, 2, 3])
    await stream.track(ADDR)
    client = FakeRisexClient()
    client.balances[ADDR] = Decimal('241.23')
    client.positions_by[ADDR] = load_fixture('risex_positions.json')['data']['positions']
    client.trades_by[ADDR] = []
    ad = risex.RisexAdapter(client, stream)
    account = VenueAccount(base.RISEX, ADDR, ADDR, 1)

    stream.start()
    for _ in range(60):
        await asyncio.sleep(0)
        if stream.connected and stream.positions_for(ADDR) is not None and stream.has_trades(ADDR):
            break
    assert stream.connected
    assert [m['params']['channel'] for m in connections[0].sent] == ['positions', 'trades']
    assert connections[0].sent[0]['params'] == {'channel': 'positions', 'makers': [ADDR], 'market_ids': [1, 2, 3]}
    # snapshot from the WS cache (human units) and the balance fetched once over REST
    snap = await ad.snapshot(account)
    assert snap.positions['BTC']['szi'] == '-0.020892' and snap.account_value == Decimal('241.23')
    # the first snapshot reconciles the cache against REST once; the balance is fetched once
    assert client.calls.count(('positions', ADDR)) == 1 and client.calls.count(('balance', ADDR)) == 1
    await ad.snapshot(account)
    assert client.calls.count(('positions', ADDR)) == 1                    # cache only within 5 minutes
    assert client.calls.count(('balance', ADDR)) == 1                      # cached while connected
    # trades from the WS buffer, no REST call
    await ad.fetch_events(account, None, {})                                 # baseline
    stream._trades[ADDR] = [ws_trades[1]]
    fills, cursor = await ad.fetch_events(account, json.dumps({'id': '', 'ms': 1}), snap.positions)
    assert len(fills) == 1 and fills[0]['side'] == 'A' and fills[0]['dir'] == 'Open Short'
    assert not any(c[0] == 'trade-history' for c in client.calls)          # every fill came over the WS

    # the first socket ends -> degraded (REST), then the second connects and resubscribes
    for _ in range(60):
        await asyncio.sleep(0)
        if stream.reconnects == 2 and stream.connected:
            break
    assert stream.reconnects == 2 and connections[1].sent[0]['params']['makers'] == [ADDR]
    health = await ad.health()
    assert health['mode'] == 'ws' and 'WS connected' in health['detail']
    await stream.stop()
    assert stream.positions_for(ADDR) is None
    snap = await ad.snapshot(account)                                        # REST fallback
    assert ('positions', ADDR) in client.calls and snap.positions['BTC']['szi'] == '-0.020892'
    assert (await ad.health())['mode'] == 'rest'


# /add, /positions, polling --------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    state = {'ms': T0}
    monkeypatch.setattr(hl_adapter, 'now_ms', lambda: state['ms'])
    monkeypatch.setattr(commands, 'now_ms', lambda: state['ms'])

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    return state


async def call(handler, repo, bot_data, user_id, *args):
    update = make_update(user_id)
    await handler(update, make_context(bot_data, args=args))
    for r in update.message.replies:
        check_telegram_html(r['text'])
    return [r['text'] for r in update.message.replies]


async def test_add_positions_and_alert_through_the_pipeline(repo, clock):
    client = FakeRisexClient()
    client.positions_by[ADDR] = load_fixture('risex_positions.json')['data']['positions']
    client.balances[ADDR] = Decimal('241.231363689661806191')
    client.trades_by[ADDR] = [rest_trade('t1', T0 - 60_000, 'SELL', '0.020892')]
    ad = risex.RisexAdapter(client)
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.RISEX: ad}}
    out = await call(commands.add_wallet, repo, bot_data, 5, ADDR, 'rise')
    assert out == ['✅ Wallet added as <b>rise</b> · HL ✗ · RISEx ✅']
    view = (await call(commands.positions_command, repo, bot_data, 5, 'rise'))[0]
    assert '<b>RISEx</b> · account $241.23' in view and '<b>SHORT</b> $BTC' in view and 'Size: $1,772' in view

    bot = FakeBot()
    rows = await repo.venue_accounts_of(ADDR)
    account = VenueAccount(base.RISEX, ADDR, ADDR, next(r['key'] for r in rows if r['venue'] == base.RISEX))
    await pipeline.poll_venue_account(bot, repo, ad, account)               # baseline
    assert bot.sent == []
    # the short is covered: position gone, one BUY trade
    client.positions_by[ADDR] = []
    client.trades_by[ADDR].append(rest_trade('t2', T0 + 5000, 'BUY', '0.020892', price='84000'))
    clock['ms'] = T0 + 30_000
    activity, weight = await pipeline.poll_venue_account(bot, repo, ad, account)
    assert activity and weight == 2 and len(bot.sent) == 1
    text = bot.sent[0]['text']
    check_telegram_html(text)
    assert text.startswith('[RISE] 🔒 <b><a href="https://app.rise.trade/">rise</a></b> closed SHORT $BTC')
    assert '(0.02089 BTC) @ 84,000' in text and 'realized' in text


async def test_venue_poll_job_tracks_addresses_on_the_stream(repo, clock):
    client = FakeRisexClient()
    client.positions_by[ADDR] = load_fixture('risex_positions.json')['data']['positions']
    stream = RisexStream('wss://unused', connect=None)
    ad = risex.RisexAdapter(client, stream)
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.RISEX: ad}}
    await call(commands.add_wallet, repo, bot_data, 1, ADDR, 'rise')
    context = make_context(bot_data)
    await poller.venue_poll_job(context)
    assert stream.addresses == {ADDR} and len(stream.market_ids) == 38
    state = poller.get_state(context)
    assert state.venue_accounts[base.RISEX] == 1 and state.venue_interval_sec[base.RISEX] == 20


def test_ws_update_rows_parse_like_the_snapshot():
    """Recorded updates (PM 2026-10-04): data is an array of snapshot-shaped rows, size signed."""
    recorded = load_fixture('risex_ws_positions_updates.json')
    stream = RisexStream('wss://unused', connect=None)
    tracked = '0x36dc0d99dcaee443cebaa5fb8c11877d157c26df'
    stream.addresses.add(tracked)
    stream.connected = True
    stream.handle(recorded['snapshot'])
    before = len(stream.positions_for(tracked))
    assert before == 37
    for update in recorded['updates']:
        assert update['type'] == 'update' and isinstance(update['data'], list)
        stream.handle(update)
    rows = stream.positions_for(tracked)
    assert len(rows) == before                                    # updates replace rows per market
    updated = {u['market_id']: u['data'][0] for u in recorded['updates'] if u['data'][0]['account'].lower() == tracked}
    assert updated and all(next(r for r in rows if r['market_id'] == m)['size'] == row['size']
                           for m, row in updated.items())
    positions = risex.parse_ws_positions(rows, markets())
    for m, row in updated.items():
        coin = risex.coin_of(markets()[m]['name'])
        assert positions[coin]['szi'] == row['size']
        assert positions[coin]['direction'] == ('SHORT' if row['size'].startswith('-') else 'LONG')
    assert all(not r['size'].startswith('-') for r in rows if r['side'] == 'BUY')
    assert all(r['size'].startswith('-') for r in rows if r['side'] == 'SELL')


async def test_cached_position_missing_from_rest_is_treated_as_closed():
    clock = {'now': 1000.0}
    stream = RisexStream('wss://unused', connect=None, clock=lambda: clock['now'])
    stream.set_markets([1, 2])
    stream.addresses.add(ADDR)
    stream.connected = True
    ws_rows = load_fixture('risex_ws_positions.json')[0]['data']
    stream.handle({'channel': 'positions', 'type': 'snapshot', 'data': ws_rows + [
        {**ws_rows[0], 'market_id': '2', 'size': '-0.163', 'side': 'SELL', 'avg_entry_price': '2683', 'quote_amount': '437'}]})
    client = FakeRisexClient()
    client.balances[ADDR] = Decimal('241')
    client.positions_by[ADDR] = load_fixture('risex_positions.json')['data']['positions']   # BTC only
    ad = risex.RisexAdapter(client, stream)
    account = VenueAccount(base.RISEX, ADDR, ADDR, 1)
    snap = await ad.snapshot(account)                      # first snapshot reconciles: ETH gone from REST
    assert set(snap.positions) == {'BTC'} and ('positions', ADDR) in client.calls
    stream.handle({'channel': 'positions', 'type': 'update', 'market_id': '2', 'data': [
        {**ws_rows[0], 'market_id': '2', 'size': '-0.2', 'side': 'SELL'}]})
    client.calls.clear()
    snap = await ad.snapshot(account)                      # within 5 minutes: cache only, no REST
    assert set(snap.positions) == {'BTC', 'ETH'} and ('positions', ADDR) not in client.calls
    clock['now'] += 301
    snap = await ad.snapshot(account)                      # reconciled again: ETH closed
    assert set(snap.positions) == {'BTC'}


def test_ws_size_zero_rows_leave_the_positions_cache():
    """fix/vacuum-freelist (2): a size-0 row (a close) in the snapshot or an update must not stay cached,
    otherwise the REST reconcile later reports it as '17 cached positions gone'."""
    stream = RisexStream('wss://unused', connect=None)
    stream.addresses = {ADDR}
    stream.connected = True
    snapshot = load_fixture('risex_ws_positions.json')[0]
    row = snapshot['data'][0]
    closed = {**row, 'market_id': '5', 'size': '0', 'quote_amount': '0'}
    stream.handle({**snapshot, 'data': [row, closed]})
    assert [r['market_id'] for r in stream.positions_for(ADDR)] == ['1']
    stream.handle({'channel': 'positions', 'type': 'update', 'data': [{**row, 'size': '0'}]})
    assert stream.positions_for(ADDR) == []
    assert stream.reconcile(ADDR, set()) == []
