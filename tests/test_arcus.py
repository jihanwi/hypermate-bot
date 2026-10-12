"""Phase 2D Arcus (spec 6.5) on the PM's live recording of 2026-10-10 (leaderboard rank 1, anonymised
0xa11ce…0001; fix/arcus-live-shapes): 42-position account dict keyed by marketId, 67 markets with
tickTiers {upToPrice, tick}, 60 fills with positionEffect and closedPnl after the fee, the 403 / 404
bodies, funding and leaderboard; resolve over accountIndex 0..9, fills paging by lowering `to`, /add
pin, /positions ARC section, /health, 429 pause, multi-algo summary on Arcus."""

import json
from decimal import Decimal

import pytest

from hypermate.bot import commands
from hypermate.core import pipeline, poller
from hypermate.core.venues import resolve_summary, resolve_wallet
from hypermate.venues import base
from hypermate.venues.arcus import adapter as arcus
from hypermate.venues.arcus.client import ArcusClient, ArcusNoActivity, ArcusNotWhitelisted, ArcusRateLimited
from hypermate.venues.base import VenueAccount
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid.scheduler import WeightBudget
from hypermate.venues.hyperliquid.venue import HyperliquidVenue
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, load_fixture, make_context, make_update

ADDR = '0xa11ce00000000000000000000000000000000001'
OTHER = '0x' + 'e' * 40
T0 = 1_791_640_000_000                      # just after the recording (createdAt 1791639669797581 µs)


class FakeTransport:
    """Stands in for ArcusClient._fetch: (path, params) -> (status, body, headers) from a table."""

    def __init__(self):
        self.calls = []
        self.accounts = {}          # (address, index) -> (status, body); default: the recorded 404
        self.fills_pages = []       # bodies returned in order for /v1/fills
        self.markets = load_fixture('arcus_markets.json')
        self.rate_limit_once = False

    async def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if self.rate_limit_once:
            self.rate_limit_once = False
            return 429, None, {'Retry-After': '7'}
        if path == '/v1/account':
            status, body = self.accounts.get((params['address'], params['accountIndex']),
                                             (404, load_fixture('arcus_account_1.json')))
            return status, body, {}
        if path == '/v1/fills':
            return 200, (self.fills_pages.pop(0) if self.fills_pages else {'fills': [], 'total': 0}), {}
        if path == '/v1/funding':
            return 200, load_fixture('arcus_funding_p1.json'), {}
        if path == '/v1/leaderboard':
            return 200, load_fixture('arcus_leaderboard.json'), {}
        if path == '/v1/markets':
            return 200, self.markets, {}
        return 500, None, {}


def client_with(transport, budget=None):
    client = ArcusClient('http://unused', budget)
    client._fetch = transport
    return client


def markets():
    data = load_fixture('arcus_markets.json')['markets']
    return {str(m['marketId']): {'market_id': str(m['marketId']), 'name': m['marketDisplayName'], 'base': m['baseAsset'],
                                 'tick_size': m['tickSize'], 'step_size': m['stepSize'], 'tick_tiers': m['tickTiers'],
                                 'mark_price': m['markPrice'], 'status': m['status']} for m in data}


# Parsing (recorded shapes) ----------------------------------------------------------------

def test_account_parsing_with_tick_tier_decimals():
    """positions is a dict keyed by marketId; side gives the sign (size comes negative for shorts too);
    markPx is the mark; the entry shows the tick of the tier markPx falls in; no liquidation price."""
    account = load_fixture('arcus_account_0.json')
    snap = arcus.parse_snapshot(account, markets())
    assert len(snap.positions) == 42 and snap.account_value == Decimal('4383320.019300706')
    btc, mu, doge, hype = (snap.positions[c] for c in ('BTC', 'MU', 'DOGE', 'HYPE'))
    assert btc['szi'] == '0.48638939' and btc['direction'] == 'LONG'
    assert btc['entry_px'] == '82775.5' and btc['px_decimals'] == 1                 # tier up to 500,000: tick 0.1
    assert btc['position_value'] == '40241.669376345' and btc['unrealized_pnl'] == '-19.491096927'
    assert btc['leverage'] == '40' and btc['margin_mode'] == 'CROSS' and 'liquidation_px' not in btc
    assert mu['szi'] == '-83.1003286' and mu['direction'] == 'SHORT'                  # size "-83.1…", side SHORT
    assert mu['position_value'] == '85969.782946558'                                  # notional comes negative
    assert mu['entry_px'] == '1032.45' and mu['px_decimals'] == 2
    assert doge['entry_px'] == '0.08574' and doge['px_decimals'] == 5                 # tier up to 0.5: tick 0.00001
    assert hype['cumulative_funding_since_open'] == '10.454973008'                    # sign convention [?]
    assert snap.extra['free_collateral'] == '4217070.6709156'
    assert {p['direction'] for p in snap.positions.values()} == {'LONG', 'SHORT'}
    # without markets the coin still comes from marketDisplayName and the entry is raw
    bare = arcus.parse_positions(account)
    assert bare['BTC']['entry_px'] == '82775.5' and 'px_decimals' not in bare['BTC']
    assert arcus.coin_of('BTC-USD') == 'BTC' and arcus.coin_of('qqq-usd') == 'QQQ' and arcus.coin_of('XYZ') == 'XYZ'
    btc_market = markets()['1']
    assert [t['tick'] for t in btc_market['tick_tiers']][-1] == '5' and 'upToPrice' not in btc_market['tick_tiers'][-1]
    assert arcus.tick_for_price(btc_market, Decimal('82753.2')) == Decimal('0.1')
    assert arcus.tick_for_price(btc_market, Decimal('500000')) == Decimal('0.1')             # upToPrice inclusive
    assert arcus.tick_for_price(btc_market, Decimal('500000.5')) == Decimal('0.2')
    assert arcus.tick_for_price(btc_market, Decimal('99000000')) == Decimal('5')             # unbounded last tier
    assert arcus.tick_for_price({'tick_size': '0.01', 'tick_tiers': []}, Decimal('150')) == Decimal('0.01')
    assert len(markets()) == 67 and markets()['24']['name'] == 'QQQ-USD'                   # stock perps included


def test_fill_conversion_uses_position_effect_and_adds_the_fee_back():
    """60 recorded fills: five positionEffect values present (FLIP_LONG_TO_SHORT is in the full page only),
    closedPnl is after the fee so the fee is added back, maker rebates are negative fees."""
    rows = load_fixture('arcus_fills_p1.json')['fills']
    assert len(rows) == 60 and sorted({r['positionEffect'] for r in rows}) == [
        'ADD_LONG', 'ADD_SHORT', 'CLOSE_LONG', 'CLOSE_SHORT', 'FLIP_SHORT_TO_LONG']
    first = arcus.fill_to_hl(rows[0])                                                 # QQQ maker ADD_SHORT
    assert first['dir'] == 'Open Short' and first['side'] == 'A' and first['coin'] == 'QQQ'
    assert first['fee'] == '-0.005695022' and first['closedPnl'] == '0'                # +0.005695022 after a rebate
    assert first['crossed'] is False and first['sz'] == '0.3791323' and first['px'] == '751.06'
    assert first['tid'] == '15472303' and first['oid'] == '9841e1ab4d7544b7' and first['time'] == 1791639669797
    taker_close = arcus.fill_to_hl(rows[1])                                           # SUI taker CLOSE_LONG
    assert taker_close['dir'] == 'Close Long' and taker_close['closedPnl'] == '0.067752053' and taker_close['crossed']
    flip = arcus.fill_to_hl(next(r for r in rows if r['positionEffect'] == 'FLIP_SHORT_TO_LONG'))
    assert flip['dir'] == 'Short > Long' and flip['coin'] == 'UNI' and flip['closedPnl'] == '-4.672366535'
    assert arcus.POSITION_EFFECTS['FLIP_LONG_TO_SHORT'] == 'Long > Short'
    # size is this fill's size: originalSize - size = remainingSize
    assert Decimal(rows[0]['originalSize']) - Decimal(rows[0]['size']) == Decimal(rows[0]['remainingSize'])
    # accumulate keeps the venue's dir and only walks the position
    walked = arcus.accumulate([taker_close, first], {'SUI': {'szi': '900'}}, keep_dir=True)
    assert [(f['dir'], Decimal(f['startPosition'])) for f in walked] == [('Close Long', Decimal(900)), ('Open Short', Decimal(0))]
    # one order filled several times keeps one oid and distinct tids (DRAM d2cdbe53…)
    dram = [arcus.fill_to_hl(r) for r in rows if r['orderId'] == 'd2cdbe539c474874']
    assert len(dram) >= 5 and len({f['oid'] for f in dram}) == 1 and len({f['tid'] for f in dram}) == len(dram)
    # funding and leaderboard recordings parse
    funding = load_fixture('arcus_funding_p1.json')['fundingPayments']
    uni, tao = funding[0], funding[2]
    assert Decimal(uni['size']) > 0 and Decimal(uni['payment']) < 0                    # a long paid
    assert Decimal(tao['size']) < 0 and Decimal(tao['payment']) > 0                    # a short received
    board = load_fixture('arcus_leaderboard.json')
    assert len(board['entries']) == 100 and board['entries'][0]['rank'] == 1 and board['window'] == '30d'


# Resolve: 200 / 403 / 404 ----------------------------------------------------------------

async def test_resolve_walks_indexes_and_classifies_403_and_404():
    transport = FakeTransport()
    account = load_fixture('arcus_account_0.json')
    transport.accounts[(ADDR, 0)] = (200, account)
    transport.accounts[(ADDR, 2)] = (200, {**account, 'accountIndex': 2, 'equity': '4100.0'})
    transport.accounts[(OTHER, 0)] = (403, load_fixture('arcus_account_403.json'))
    ad = arcus.ArcusAdapter(client_with(transport))
    found = await ad.resolve(ADDR)
    assert [(a.account_ref, a.label('cl')) for a in found] == [(f'{ADDR}:0', 'cl'), (f'{ADDR}:2', 'cl#2')]
    assert found[0].meta['equity'] == '4383320.019300706'
    assert len([c for c in transport.calls if c[0] == '/v1/account']) == 10          # every index tried (1 is a 404)
    assert resolve_summary({base.ARCUS: found}) == 'Arcus ✅ (2 accounts)'
    # 403: not whitelisted, stop at the first index
    transport.calls.clear()
    assert await ad.resolve(OTHER) == [] and len(transport.calls) == 1
    assert resolve_summary({base.ARCUS: []}) == 'Arcus ✗'
    # all 404
    transport.calls.clear()
    assert await ad.resolve('0x' + 'f' * 40) == [] and len(transport.calls) == 10
    # the client surfaces each status as its own error, and the recorded bodies classify on any status
    client = client_with(transport)
    with pytest.raises(ArcusNotWhitelisted):
        await client.account(OTHER, 0)
    with pytest.raises(ArcusNoActivity):
        await client.account(OTHER, 5)
    transport.accounts[(OTHER, 7)] = (200, load_fixture('arcus_account_1.json'))
    with pytest.raises(ArcusNoActivity):
        await client.account(OTHER, 7)
    transport.accounts[(OTHER, 8)] = (400, load_fixture('arcus_account_403.json'))
    with pytest.raises(ArcusNotWhitelisted):
        await client.account(OTHER, 8)
    assert len(await client.funding(ADDR, 0)) == 40 and (await client.leaderboard())['entries'][0]['rank'] == 1


async def test_rate_limit_pauses_the_bucket(monkeypatch):
    clock = {'now': 100.0}

    async def sleep(s):
        clock['now'] += s

    budget = WeightBudget(1200, clock=lambda: clock['now'], sleep=sleep)
    transport = FakeTransport()
    transport.rate_limit_once = True
    client = client_with(transport, budget)
    with pytest.raises(ArcusRateLimited):
        await client.account(ADDR, 0)
    assert budget.paused_until == clock['now'] + 7 and budget.per_minute == 1200       # Retry-After, no halving


# Fills paging -----------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    state = {'ms': T0}
    monkeypatch.setattr(hl_adapter, 'now_ms', lambda: state['ms'])
    monkeypatch.setattr(commands, 'now_ms', lambda: state['ms'])

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    return state


def full_page(template: dict, start_us: int, count: int = 1000) -> list[dict]:
    """A full newest-first page in the recorded row shape, one fill per second."""
    return [{**template, 'tradeId': str(20_000_000 + i), 'orderId': f'o{i:08x}', 'createdAt': start_us - i * 1_000_000,
             'sequenceNumber': 800_000_000 - i} for i in range(count)]


async def test_fetch_events_follows_full_pages_by_lowering_to(clock):
    recorded = load_fixture('arcus_fills_p1.json')['fills']                           # the older, short page
    newest_us = 1_791_650_000_000_000
    page1 = full_page(recorded[0], newest_us)
    transport = FakeTransport()
    transport.fills_pages = [{'fills': page1, 'total': 1060}, {'fills': recorded, 'total': 1060}]
    ad = arcus.ArcusAdapter(client_with(transport))
    account = VenueAccount(base.ARCUS, f'{ADDR}:0', ADDR, 1)
    fills, cursor = await ad.fetch_events(account, None, {})                          # baseline: cursor only
    assert fills == [] and json.loads(cursor) == {'ms': T0} and transport.calls == []
    since = 1_791_639_300_000                                                          # before the recording
    clock['ms'] = newest_us // 1000 + 1000
    fills, cursor = await ad.fetch_events(account, arcus.encode_cursor(since), {'QQQ': {'szi': '-265'}})
    first, second = [c for c in transport.calls if c[0] == '/v1/fills']
    assert first[1] == {'address': ADDR, 'accountIndex': 0, 'limit': 1000, 'from': (since + 1) * 1000}
    assert second[1]['to'] == min(f['createdAt'] for f in page1) - 1 and second[1]['from'] == (since + 1) * 1000
    assert len(fills) == 1060 and fills[0]['tid'] == '15471391' and fills[-1]['tid'] == '20000000'   # oldest first
    assert [f['time'] for f in fills] == sorted(f['time'] for f in fills)
    qqq = [f for f in fills if f['coin'] == 'QQQ']
    assert qqq[0]['startPosition'] == '-265' and qqq[0]['dir'] == 'Open Short'       # positionEffect, snapshot walk
    assert json.loads(cursor) == {'ms': fills[-1]['time']}
    # a short page: one call, rows at or before the cursor dropped
    transport.calls.clear()
    transport.fills_pages = [{'fills': recorded, 'total': 60}]
    fills, cursor2 = await ad.fetch_events(account, cursor, {})
    assert fills == [] and cursor2 == cursor and len(transport.calls) == 1


# Commands and polling ------------------------------------------------------------------------

async def call(handler, repo, bot_data, user_id, *args):
    update = make_update(user_id)
    await handler(update, make_context(bot_data, args=args))
    for r in update.message.replies:
        check_telegram_html(r['text'])
    return [r['text'] for r in update.message.replies]


async def test_add_pin_positions_section_health_and_polling(repo, clock):
    transport = FakeTransport()
    account = load_fixture('arcus_account_0.json')
    transport.accounts[(ADDR, 0)] = (200, account)
    ad = arcus.ArcusAdapter(client_with(transport, WeightBudget(1200)))
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.ARCUS: ad}}
    assert await call(commands.add_wallet, repo, bot_data, 5, ADDR, 'cl') == [
        '✅ Wallet added as <b>cl</b> · HL ✗ · Arcus ✅ (1 account)']
    view = "\n".join(await call(commands.positions_command, repo, bot_data, 5, 'cl'))
    assert '<b>Arcus #0</b> · account $4,383,320.02' in view
    assert '<b>LONG</b> $BTC' in view and 'Entry: $82,775.5' in view and '· 40x' in view
    assert '<b>SHORT</b> $MU' in view and 'Entry: $1,032.45' in view and '<b>SHORT</b> $QQQ' in view
    # venue pin on an address Arcus has never seen: forced main account, no HL row
    transport.accounts[(OTHER, 0)] = (403, load_fixture('arcus_account_403.json'))
    assert await call(commands.add_wallet, repo, bot_data, 5, f'arcus:{OTHER}', 'pinned') == [
        '✅ Wallet added as <b>pinned</b> · Arcus ✅ (added as given, no activity seen)']
    rows = {r['venue']: r for r in await repo.venue_accounts_of(OTHER)}
    assert set(rows) == {base.ARCUS} and rows[base.ARCUS]['account_ref'] == f'{OTHER}:0' and rows[base.ARCUS]['active']
    # polling: baseline, then the recorded SUI close -> [ARC] alert with the fee added back
    bot = FakeBot()
    key = next(r['key'] for r in await repo.venue_accounts_of(ADDR) if r['venue'] == base.ARCUS)
    venue_account = VenueAccount(base.ARCUS, f'{ADDR}:0', ADDR, key)
    await pipeline.poll_venue_account(bot, repo, ad, venue_account)
    assert bot.sent == []
    sui_close = next(r for r in load_fixture('arcus_fills_p1.json')['fills'] if r['tradeId'] == '15472281')
    sui_close = {**sui_close, 'size': '913.409362', 'createdAt': (T0 + 5000) * 1000}        # the whole SUI long
    positions = {k: v for k, v in account['positions'].items() if k != '59'}
    transport.accounts[(ADDR, 0)] = (200, {**account, 'positions': positions})
    transport.fills_pages = [{'fills': [sui_close], 'total': 1}]
    clock['ms'] = T0 + 30_000
    activity, _ = await pipeline.poll_venue_account(bot, repo, ad, venue_account)
    assert activity and len(bot.sent) == 1
    text = bot.sent[0]['text']
    check_telegram_html(text)
    assert text.startswith('[ARC] 🔒 <b><a href="https://arcus.xyz/">cl</a></b> closed LONG $SUI')
    assert '(913.4 SUI) @ 1.12' in text and 'realized 🟢 +$0' in text               # 0.0671 + 0.00065 fee
    # /health lists the venue with its interval and the bucket
    assert poller.venue_interval_sec(base.ARCUS, ad, 1) == 20
    assert (await ad.health()) == {'mode': 'rest', 'detail': 'REST polling, bucket 1200/min'}
    health = await poller.venue_health(make_context(bot_data))
    assert health[base.ARCUS]['detail'] == 'REST polling, bucket 1200/min' and health[base.ARCUS]['interval_sec'] is None
    results = await resolve_wallet(repo, {base.ARCUS: ad}, ADDR, clock['ms'])
    assert resolve_summary(results) == 'Arcus ✅ (1 account)'


async def test_market_maker_fills_enter_multi_algo_summary_on_arcus(repo, clock):
    """The rank 1 wallet: small orders on many markets every few seconds. Six markets with one 0.2% slice
    each every 5 s become six algos within three polls and, at multi_algo_min (5), one [ARC] summary
    message instead of per-coin alerts."""
    transport = FakeTransport()
    account = load_fixture('arcus_account_0.json')
    big = {str(i): {'side': 'LONG', 'size': '1000', 'averageEntryPrice': '100', 'markPx': '100', 'unrealizedPnl': '0',
                    'leverage': '5', 'marginMode': 'CROSS', 'positionValueNotional': '100000', 'marginUsed': '1',
                    'marketDisplayName': f'M{i}-USD'} for i in range(50, 56)}
    transport.accounts[(ADDR, 0)] = (200, {**account, 'positions': big})
    transport.markets = {'markets': [{'marketId': i, 'marketDisplayName': f'M{i}-USD', 'baseAsset': f'M{i}',
                                      'tickSize': '0.01', 'stepSize': '1', 'status': 'ONLINE', 'markPrice': '100', 'tickTiers': []}
                                     for i in range(50, 56)]}
    ad = arcus.ArcusAdapter(client_with(transport, WeightBudget(1200)))
    await repo.add_subscription(5, ADDR, 'mm', T0 - 60_000)
    key, _ = await repo.ensure_venue_account(ADDR, base.ARCUS, arcus.account_ref(ADDR, 0), True, T0)
    venue_account = VenueAccount(base.ARCUS, arcus.account_ref(ADDR, 0), ADDR, key)
    bot = FakeBot()
    await pipeline.poll_venue_account(bot, repo, ad, venue_account)                    # baseline
    tid, t_us = 1, (T0 + 1000) * 1000
    for cycle in range(4):                                                             # 4 polls x 6 markets x 6 orders
        page = []
        for n in range(6):
            for i in range(50, 56):
                page.append({'tradeId': str(tid), 'orderId': f'o{tid}', 'marketId': i, 'marketDisplayName': f'M{i}-USD',
                             'side': 'BUY', 'price': '100', 'size': '2', 'fee': '0.01', 'closedPnl': '-0.01',
                             'role': 'MAKER', 'positionEffect': 'ADD_LONG', 'createdAt': t_us, 'sequenceNumber': tid})
                tid += 1
            t_us += 5_000_000
        clock['ms'] = t_us // 1000 + 1000
        transport.fills_pages = [{'fills': list(reversed(page)), 'total': len(page)}]
        grown = {k: {**v, 'size': str(1000 + 12 * (cycle + 1))} for k, v in big.items()}
        transport.accounts[(ADDR, 0)] = (200, {**account, 'positions': grown})
        await pipeline.poll_venue_account(bot, repo, ad, venue_account)
    texts = [m['text'] for m in bot.sent]
    summary = [t for t in texts if 'running TWAP-style algos' in t]
    assert len(summary) == 1 and summary[0].startswith('[ARC] 🤖') and 'on 6 coins' in summary[0]
    assert 'accumulating LONG ×6' in summary[0]
    assert not any('algo accumulating' in t for t in texts)                            # no per-coin algo messages
    assert len(await repo.active_algos(key)) == 6 and await repo.multi_algo_mode(key) is not None
