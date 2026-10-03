"""Phase 2A Lighter (spec 6.2): fixture parsing, trade direction and startPosition for maker and taker,
the REST budget arithmetic, WS degrade and recovery, resolve on /add, /positions sections, the poll."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hypermate.bot import commands
from hypermate.core import pipeline, poller
from hypermate.core.venues import resolve_summary, resolve_wallet
from hypermate.db.repo import Repo
from hypermate.venues import base
from hypermate.venues.base import VenueAccount, fill_direction
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid.scheduler import WeightBudget
from hypermate.venues.hyperliquid.venue import HyperliquidVenue
from hypermate.venues.lighter import adapter as lighter
from hypermate.venues.lighter.client import LighterClient
from hypermate.venues.lighter.stream import LighterStream
from tests.helpers import (FakeBot, FakeHLClient, check_telegram_html, clearinghouse, load_fixture, make_context,
                           make_update, position)

L1 = '0x4f266b1a1fc148c38dfc5fc4c7926bee34150566'
INDEX = 702389
T0 = 1_791_041_000_000


# Fixture parsing --------------------------------------------------------------------

def test_resolve_and_snapshot_parsing():
    assert lighter.parse_sub_accounts(load_fixture('lighter_accountsByL1Address.json')) == [INDEX]
    account = load_fixture('lighter_account_by_index.json')['accounts'][0]
    snap = lighter.parse_account(account)
    assert len(account['positions']) == 55 and len(snap.positions) == 15        # "0" / "0.0" rows dropped
    eth = snap.positions['ETH']
    assert eth['szi'] == '153.1986' and eth['direction'] == 'LONG' and eth['entry_px'] == '2680.16'
    assert snap.positions['PUMP']['szi'] == '-5577973' and snap.positions['PUMP']['direction'] == 'SHORT'
    assert snap.account_value == Decimal('2711105.0342539996')
    state = base.as_clearinghouse_state(snap)
    assert state['marginSummary']['accountValue'] == '2711105.0342539996'
    assert {p['position']['coin'] for p in state['assetPositions']} >= {'ETH', 'PUMP', 'SNDK'}


def test_trade_normalisation_maker_and_taker():
    symbols = {0: 'ETH', 1: 'BTC'}
    base_trade = {'trade_id': 10, 'type': 'trade', 'market_id': 0, 'size': '0.0004', 'price': '2679.72',
                  'ask_id': 281477948611531, 'bid_id': 562946874133289, 'timestamp': 1791040966543,
                  'tx_hash': '0xabc', 'taker_position_size_before': '0.0000', 'maker_position_size_before': '153.1990'}
    # we are the ask (sell) and the maker: position before from maker_*, pnl from ask_account_pnl
    maker = {**base_trade, 'ask_account_id': INDEX, 'bid_account_id': 1, 'is_maker_ask': True,
             'ask_account_pnl': '-0.000176'}
    fill = lighter.normalise_trade(maker, INDEX, symbols)
    assert (fill['coin'], fill['side'], fill['sz'], fill['px']) == ('ETH', 'A', '0.0004', '2679.72')
    assert fill['startPosition'] == '153.1990' and fill['dir'] == 'Close Long'
    assert fill['closedPnl'] == '-0.000176' and fill['oid'] == 281477948611531 and fill['tid'] == 10
    assert fill['crossed'] is False
    # we are the bid (buy) and the taker: taker_position_size_before, bid_account_pnl
    taker = {**base_trade, 'ask_account_id': 1, 'bid_account_id': INDEX, 'is_maker_ask': True,
             'taker_position_size_before': '-2.5', 'bid_account_pnl': '1.5'}
    fill = lighter.normalise_trade(taker, INDEX, symbols)
    assert fill['side'] == 'B' and fill['startPosition'] == '-2.5' and fill['dir'] == 'Close Short'
    assert fill['closedPnl'] == '1.5' and fill['oid'] == 562946874133289 and fill['crossed'] is True
    # taker buy from flat opens a long; liquidation type carries the liquidation marker
    opened = lighter.normalise_trade({**taker, 'taker_position_size_before': '0', 'type': 'liquidation'}, INDEX, symbols)
    assert opened['dir'] == 'Open Long' and opened['liquidation'] == {'method': 'market', 'markPx': '2679.72'}
    assert lighter.normalise_trade({**base_trade, 'ask_account_id': 1, 'bid_account_id': 2}, INDEX, symbols) is None


def test_fill_direction_rules():
    assert fill_direction('B', Decimal(0), Decimal(1)) == ('Open Long', Decimal(1))
    assert fill_direction('A', Decimal(0), Decimal(1)) == ('Open Short', Decimal(-1))
    assert fill_direction('B', Decimal(2), Decimal(1)) == ('Open Long', Decimal(3))
    assert fill_direction('A', Decimal(2), Decimal(1)) == ('Close Long', Decimal(1))
    assert fill_direction('A', Decimal(2), Decimal(2)) == ('Close Long', Decimal(0))
    assert fill_direction('A', Decimal(2), Decimal(5)) == ('Long > Short', Decimal(-3))
    assert fill_direction('B', Decimal(-2), Decimal(1)) == ('Close Short', Decimal(-1))
    assert fill_direction('B', Decimal(-2), Decimal(3)) == ('Short > Long', Decimal(1))


def test_recorded_trades_replay_through_the_shared_pipeline():
    """100 recorded trades of the MM account (all sells of a long, maker): every one normalises and the
    HL aggregation groups them by order id."""
    trades = load_fixture('lighter_trades.json')['trades']
    symbols = {b['market_id']: b['symbol'] for b in load_fixture('lighter_orderBooks.json')['order_books']}
    fills = [lighter.normalise_trade(t, INDEX, symbols) for t in trades]
    assert all(f is not None for f in fills)
    assert 'ETH' in {f['coin'] for f in fills}
    events = hl_adapter.fill_events(1, fills)
    assert 0 < len(events) <= len({f['oid'] for f in fills})
    assert sum(e.meta['fills'] for e in events) == 100
    eth = [e for e in events if e.coin == 'ETH']
    assert eth and all(e.side == 'LONG' for e in eth)      # the recorded ETH trades reduce a long


# Cursor and REST paging -------------------------------------------------------------

class FakeLighterClient:
    """Stands in for LighterClient: responses per index, trades newest first, paged by cursor."""

    def __init__(self):
        self.subs = {}
        self.accounts = {}
        self.trades_by_index = {}
        self.symbol_map = {0: 'ETH'}
        self.calls = []

    async def accounts_by_l1_address(self, l1, priority=3):
        self.calls.append(('accountsByL1Address', l1))
        return self.subs.get(l1.lower(), [])

    async def account(self, index, priority=0):
        self.calls.append(('account', index))
        return self.accounts.get(index)

    async def trades(self, index, cursor=None, limit=100, priority=2):
        self.calls.append(('trades', index, cursor))
        all_trades = sorted(self.trades_by_index.get(index, []), key=lambda t: -t['trade_id'])
        start = int(json.loads(cursor)['index']) if cursor else 0
        page = all_trades[start:start + limit]
        next_cursor = json.dumps({'index': start + limit}) if start + limit < len(all_trades) else None
        return page, next_cursor

    async def symbols(self, priority=1):
        self.calls.append(('orderBooks',))
        return dict(self.symbol_map)


def trade(trade_id, ts, size='1', price='100', start='0', index=INDEX):
    return {'trade_id': trade_id, 'type': 'trade', 'market_id': 0, 'size': size, 'price': price,
            'ask_account_id': 999, 'bid_account_id': index, 'is_maker_ask': True, 'timestamp': ts,
            'taker_position_size_before': start, 'bid_id': 5000 + trade_id, 'bid_account_pnl': '0'}


async def test_fetch_events_baseline_then_incremental_with_catch_up_pages():
    client = FakeLighterClient()
    client.trades_by_index[INDEX] = [trade(i, T0 + i * 1000) for i in range(1, 11)]
    ad = lighter.LighterAdapter(client)
    account = VenueAccount(base.LIGHTER, str(INDEX), L1, 1)
    fills, cursor = await ad.fetch_events(account, None)
    assert fills == [] and json.loads(cursor) == {'trade_id': 10}           # baseline: no replay
    client.trades_by_index[INDEX] += [trade(i, T0 + i * 1000) for i in range(11, 14)]
    fills, cursor = await ad.fetch_events(account, cursor)
    assert [f['tid'] for f in fills] == [11, 12, 13] and json.loads(cursor) == {'trade_id': 13}
    assert fills[0]['time'] < fills[-1]['time']
    # 250 new trades: the first page holds only new ones, so older pages are followed to the cursor
    client.trades_by_index[INDEX] += [trade(i, T0 + i * 1000) for i in range(14, 264)]
    calls_before = len([c for c in client.calls if c[0] == 'trades'])
    fills, cursor = await ad.fetch_events(account, cursor)
    assert len(fills) == 250 and json.loads(cursor) == {'trade_id': 263}
    assert len([c for c in client.calls if c[0] == 'trades']) - calls_before == 3   # 3 pages of 100


def test_rest_interval_keeps_under_the_budget():
    assert poller.lighter_interval_sec(10, ws_connected=False) == 40
    assert poller.lighter_interval_sec(25, ws_connected=False) == 60        # 50 requests per 60 s
    assert poller.lighter_interval_sec(40, ws_connected=False) == 96
    assert poller.lighter_interval_sec(25, ws_connected=True) == 20
    for n in (1, 10, 25, 40, 100):
        requests_per_minute = 2 * n * 60 / poller.lighter_interval_sec(n, False)
        assert requests_per_minute <= 50


async def test_client_charges_the_bucket_and_handles_errors(monkeypatch):
    clock = {'now': 100.0}
    slept = []

    async def sleep(s):
        slept.append(s)
        clock['now'] += s

    budget = WeightBudget(50, clock=lambda: clock['now'], sleep=sleep)
    client = LighterClient('http://unused', budget)
    responses = [(200, load_fixture('lighter_accountsByL1Address.json')),
                 (200, {'code': 21100, 'message': 'account not found'}),
                 (429, None), (500, None)]

    async def fake_fetch(path, params):
        return responses.pop(0)

    monkeypatch.setattr(client, '_fetch', fake_fetch)
    assert [s['index'] for s in await client.accounts_by_l1_address(L1)] == [INDEX]
    assert await client.accounts_by_l1_address('0x' + '1' * 40) == []       # API error code -> no account
    with pytest.raises(lighter.LighterClient.__module__ and __import__('hypermate.venues.lighter.client',
                                                                        fromlist=['x']).LighterRateLimited):
        await client.account(INDEX)
    assert budget.paused_until > clock['now']
    budget.paused_until = 0
    with pytest.raises(__import__('hypermate.venues.lighter.client', fromlist=['x']).LighterAPIError):
        await client.account(INDEX)
    assert budget.tokens < budget.capacity


# WS stream --------------------------------------------------------------------------

class FakeWS:
    def __init__(self, messages, fail_after=None):
        self.sent = []
        self.messages = list(messages)
        self.fail_after = fail_after
        self.closed = False

    async def send_json(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            await asyncio.sleep(0)
            return self.messages.pop(0)
        if self.fail_after == 'error':
            raise ConnectionError('socket reset')
        if self.fail_after == 'hang':
            await asyncio.Event().wait()          # stays open until the stream is stopped
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


async def test_stream_degrades_to_rest_and_resubscribes_on_recovery(monkeypatch):
    monkeypatch.setattr('hypermate.venues.lighter.stream.RECONNECT_MIN_SEC', 0)
    account_msg = {'channel': f'account_all_positions/{INDEX}',
                   'positions': {'0': {'market_id': 0, 'symbol': 'ETH', 'sign': 1, 'position': '2.5',
                                       'avg_entry_price': '2600', 'position_value': '6500', 'unrealized_pnl': '1'}},
                   'account': {'total_asset_value': '7000'}}
    trade_msg = {'channel': f'account_all_trades/{INDEX}', 'trades': [trade(77, T0)]}
    attempts = []
    sockets = [ConnectionError('CloudFront 400'), FakeWS([account_msg, trade_msg], fail_after='error'),
               FakeWS([account_msg], fail_after='hang')]

    sockets_sent = []

    async def connect(url):
        attempts.append(url)
        item = sockets.pop(0)
        if isinstance(item, Exception):
            raise item
        sockets_sent.append(item.sent)
        return item

    stream = LighterStream('wss://unused', connect=connect)
    await stream.subscribe(INDEX)
    assert stream.positions_for(INDEX) is None            # not connected: REST
    stream.start()
    for _ in range(50):
        await asyncio.sleep(0)
        if stream.reconnects == 2 and stream.connected and stream.positions_for(INDEX):
            break
    assert stream.reconnects == 2 and len(attempts) == 3   # failed, connected then dropped, connected again
    assert stream.connected and stream.positions_for(INDEX)['positions'][0]['symbol'] == 'ETH'
    assert 'WS connected' in stream.status_line()
    # the subscriptions were sent again on the second connection
    assert attempts and [m['channel'] for m in sockets_sent[-1]] == [
        f'account_all_positions/{INDEX}', f'account_all_trades/{INDEX}']
    await stream.stop()
    assert not stream.connected and 'REST polling' in stream.status_line()


async def test_adapter_uses_the_stream_caches_when_connected():
    client = FakeLighterClient()
    client.accounts[INDEX] = load_fixture('lighter_account_by_index.json')['accounts'][0]
    client.trades_by_index[INDEX] = [trade(1, T0)]
    stream = LighterStream('wss://unused', connect=None)
    stream.indexes.add(INDEX)
    ad = lighter.LighterAdapter(client, stream)
    account = VenueAccount(base.LIGHTER, str(INDEX), L1, 1)
    # degraded: REST
    snap = await ad.snapshot(account)
    assert ('account', INDEX) in client.calls and 'ETH' in snap.positions
    fills, cursor = await ad.fetch_events(account, None)
    assert ('trades', INDEX, None) in client.calls
    # connected with caches: no REST for either
    stream.connected = True
    stream._handle({'channel': f'account_all_positions/{INDEX}', 'positions': [
        {'market_id': 0, 'symbol': 'ETH', 'sign': -1, 'position': '3', 'avg_entry_price': '2600',
         'position_value': '7800', 'unrealized_pnl': '0'}], 'account': {'total_asset_value': '9000'}})
    stream._handle(json.dumps({'channel': f'account_all_trades/{INDEX}', 'trades': [trade(2, T0 + 1000, start='-3')]}))
    calls_before = len(client.calls)
    snap = await ad.snapshot(account)
    assert snap.positions['ETH']['szi'] == '-3' and snap.account_value == Decimal(9000)
    fills, cursor = await ad.fetch_events(account, cursor)
    assert [f['tid'] for f in fills] == [2] and json.loads(cursor) == {'trade_id': 2}
    assert len([c for c in client.calls[calls_before:] if c[0] != 'orderBooks']) == 0
    health = await ad.health()
    assert health['mode'] == 'ws'


# /add, /positions, /list, polling -----------------------------------------------------

def lighter_adapter_with_fixture():
    client = FakeLighterClient()
    client.subs[L1] = load_fixture('lighter_accountsByL1Address.json')['sub_accounts']
    client.accounts[INDEX] = load_fixture('lighter_account_by_index.json')['accounts'][0]
    client.trades_by_index[INDEX] = []
    return lighter.LighterAdapter(client), client


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


async def test_add_resolves_every_venue_and_positions_show_sections(repo, clock):
    hl = FakeHLClient()
    hl.clearinghouse[L1] = clearinghouse(position('BTC', '1', position_value='61000'), account_value='2000')
    ad, client = lighter_adapter_with_fixture()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.LIGHTER: ad}}
    out = await call(commands.add_wallet, repo, bot_data, 5, L1, 'cl')
    assert out == ['✅ Wallet added as <b>cl</b> · HL ✅ · Lighter ✅ (1 sub-account)']
    rows = await repo.venue_accounts_of(L1)
    assert [(r['venue'], r['account_ref'], r['active']) for r in rows] == [
        (base.HYPERLIQUID, L1, True), (base.LIGHTER, str(INDEX), True)]
    assert await repo.tracked_venue_accounts(base.LIGHTER) == [
        {'key': rows[1]['key'], 'account_ref': str(INDEX), 'address': L1, 'last_activity_ms': T0}]

    view = (await call(commands.positions_command, repo, bot_data, 5, 'cl'))[0]
    assert '<b>LONG</b> $BTC' in view
    assert '<b>Lighter #702389</b> · account $2,711,105.03' in view and '<b>LONG</b> $ETH' in view
    assert '<b>SHORT</b> $PUMP' in view and 'Margin Balance (all venues):</b> $2,713,105.03' in view

    # a wallet with no HL activity: HL inactive, Lighter only
    hl2 = FakeHLClient()
    other = '0x' + '7' * 39 + 'a'
    client.subs[other] = [{'index': 5, 'collateral': '10'}]
    client.accounts[5] = {'index': 5, 'total_asset_value': '10', 'positions': []}
    bot_data2 = {'repo': repo, 'hl': hl2, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl2), base.LIGHTER: ad}}
    out = await call(commands.add_wallet, repo, bot_data2, 5, other, 'ltr')
    assert out == ['✅ Wallet added as <b>ltr</b> · HL ✗ · Lighter ✅ (1 sub-account)']
    assert {(r['venue'], r['active']) for r in await repo.venue_accounts_of(other)} == {
        (base.HYPERLIQUID, False), (base.LIGHTER, True)}
    assert other not in {a for _, a in await repo.tracked_accounts()}       # not polled on HL
    view = (await call(commands.positions_command, repo, bot_data2, 5, 'ltr'))[0]
    assert 'Futures:' not in view and '<b>Lighter #5</b>' in view

    # /rescan: Lighter gone -> inactive; HL now active
    client.subs[other] = []
    hl2.clearinghouse[other] = clearinghouse(position('ETH', '1'), account_value='50')
    out = await call(commands.rescan_command, repo, bot_data2, 5, 'ltr')
    assert out == ['🔎 <b>ltr</b>: HL ✅ · Lighter ✗']
    assert {(r['venue'], r['active']) for r in await repo.venue_accounts_of(other)} == {
        (base.HYPERLIQUID, True), (base.LIGHTER, False)}


async def test_list_sums_account_values_over_venues(repo, clock):
    hl = FakeHLClient()
    hl.clearinghouse[L1] = clearinghouse(account_value='2000')
    ad, client = lighter_adapter_with_fixture()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.LIGHTER: ad}}
    await call(commands.add_wallet, repo, bot_data, 5, L1, 'cl')
    rows = await repo.venue_accounts_of(L1)
    await repo.save_snapshot(rows[0]['key'], {'': {}}, T0, '2000')
    await repo.save_snapshot(rows[1]['key'], {'': {}}, T0, '2711105.03')
    out = (await call(commands.list_wallets, repo, bot_data, 5))[0]
    assert '$2,713,105.03' in out


async def test_lighter_poll_sends_alerts_with_badge_and_sub_account_label(repo, clock):
    ad, client = lighter_adapter_with_fixture()
    client.trades_by_index[INDEX] = [trade(1, T0 - 5000, start='150')]
    bot = FakeBot()
    await repo.add_subscription(7, L1, 'cl', T0)
    results = await resolve_wallet(repo, {base.LIGHTER: ad}, L1, T0)
    assert resolve_summary(results) == 'Lighter ✅ (1 sub-account)'
    account = results[base.LIGHTER][0]
    # baseline cycle: snapshot stored, cursor at the newest trade, nothing sent
    await pipeline.poll_venue_account(bot, repo, ad, account)
    assert bot.sent == []
    stored = await repo.get_snapshot(account.venue_account_id)
    assert stored['']['ETH']['szi'] == '153.1986'
    # the position grows and a trade arrives: one alert with the [LTR] badge and alias#index
    acc = client.accounts[INDEX]
    eth = next(p for p in acc['positions'] if p['symbol'] == 'ETH')
    eth['position'] = '154.1986'
    client.trades_by_index[INDEX].append(trade(2, T0 + 1000, size='1', price='2700', start='153.1986'))
    clock['ms'] = T0 + 30_000
    activity, weight = await pipeline.poll_venue_account(bot, repo, ad, account)
    assert activity and weight == 1
    assert len(bot.sent) == 1
    text = bot.sent[0]['text']
    check_telegram_html(text)
    assert text.startswith('[LTR] ➕ <b><a href="https://app.lighter.xyz/">cl#702389</a></b> added to LONG $ETH')
    assert '$2.7k (1 ETH) @ 2,700' in text
    # unchanged snapshot: no trades request
    calls_before = len(client.calls)
    clock['ms'] = T0 + 60_000
    await pipeline.poll_venue_account(bot, repo, ad, account)
    assert ('trades', INDEX, None) not in client.calls[calls_before:]


async def test_diff_fills_for_venues_without_trades(clock):
    previous = {'BTC': {'szi': '1', 'entry_px': '60000'}, 'ETH': {'szi': '-2', 'entry_px': '3000'}}
    current = {'BTC': {'szi': '1.5', 'entry_px': '61000'}, 'SOL': {'szi': '10', 'entry_px': '100'}}
    fills = pipeline.diff_fills(previous, current, T0)
    by_coin = {f['coin']: f for f in fills}
    assert by_coin['BTC']['dir'] == 'Open Long' and by_coin['BTC']['sz'] == '0.5' and by_coin['BTC']['px'] == '61000'
    assert by_coin['ETH']['dir'] == 'Close Short' and by_coin['ETH']['sz'] == '2' and by_coin['ETH']['side'] == 'B'
    assert by_coin['SOL']['dir'] == 'Open Long' and by_coin['SOL']['startPosition'] == '0'
    events = hl_adapter.fill_events(1, fills)
    assert sorted(e.type.value for e in events) == ['position_close', 'position_increase', 'position_open']
    assert all(e.realized_pnl in (None, Decimal(0)) for e in events)


async def test_venue_poll_job_and_health_lines(repo, clock, monkeypatch):
    monkeypatch.setattr(poller.Config, 'ADMIN_USER_IDS', frozenset({1}))
    ad, client = lighter_adapter_with_fixture()
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.LIGHTER: ad}}
    await call(commands.add_wallet, repo, bot_data, 1, L1, 'cl')
    context = make_context(bot_data)
    await poller.venue_poll_job(context)
    state = poller.get_state(context)
    assert state.venue_accounts[base.LIGHTER] == 1 and state.venue_interval_sec[base.LIGHTER] == 40
    n = len(client.calls)
    clock['ms'] += 20_000
    await poller.venue_poll_job(context)                     # 20 s later: inside the 40 s interval
    assert len(client.calls) == n
    clock['ms'] += 25_000
    await poller.venue_poll_job(context)
    assert len(client.calls) > n
    update = make_update(1)
    await commands.health_command(update, context)
    text = update.message.replies[0]['text']
    check_telegram_html(text)
    assert 'Venues:\n- lighter: 1 accounts · REST polling · every 40s · last' in text


async def test_daily_rescan_activates_new_venues(repo, clock):
    ad, client = lighter_adapter_with_fixture()
    hl = FakeHLClient()
    client.subs[L1] = []
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.LIGHTER: ad}}
    await call(commands.add_wallet, repo, bot_data, 1, L1, 'cl')
    assert all(not r['active'] for r in await repo.venue_accounts_of(L1))
    client.subs[L1] = load_fixture('lighter_accountsByL1Address.json')['sub_accounts']
    await poller.rescan_job(make_context(bot_data))
    rows = {r['venue']: r['active'] for r in await repo.venue_accounts_of(L1)}
    assert rows == {base.HYPERLIQUID: False, base.LIGHTER: True}


async def test_reactivated_account_restarts_from_now(tmp_path, clock):
    repo = Repo(str(tmp_path / 'hm.db'))
    await repo.connect()
    key, created = await repo.ensure_venue_account(L1, base.LIGHTER, '5', True, T0)
    assert created
    await repo.save_snapshot(key, {'': {'ETH': {'szi': '1'}}}, T0, '10')
    await repo.set_cursor(key, 'trades', '{"trade_id": 9}', T0)
    assert (await repo.ensure_venue_account(L1, base.LIGHTER, '5', False, T0 + 1)) == (key, False)
    assert (await repo.ensure_venue_account(L1, base.LIGHTER, '5', True, T0 + 2)) == (key, False)
    assert await repo.get_snapshot(key) is None and await repo.get_cursor(key, 'trades') == str(T0 + 2)
    await repo.close()


def test_hl_venue_resolve_rules():
    venue = HyperliquidVenue(FakeHLClient())
    assert asyncio.run(venue.resolve(L1)) == []                                  # nothing on HL
    hl = FakeHLClient()
    hl.spot[L1] = {'balances': [{'coin': 'HYPE', 'total': '1'}]}
    assert [a.account_ref for a in asyncio.run(HyperliquidVenue(hl).resolve(L1))] == [L1]
    account = VenueAccount(base.HYPERLIQUID, L1, L1)
    assert account.label('cl') == 'cl' and VenueAccount(base.LIGHTER, '7', L1).label('cl') == 'cl#7'
    assert SimpleNamespace is not None
