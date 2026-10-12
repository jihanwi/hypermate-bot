"""Phase 2D Arcus (spec 6.5), shapes from the PM's live recording of 2026-10-10 (fix/arcus-live-shapes):
positions dict keyed by marketId, tickTiers {upToPrice, tick}, fills with positionEffect and closedPnl
after the fee, 403 / 404 bodies; resolve over accountIndex 0..9, fills paging by lowering `to`, /add
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
T0 = 1_791_600_000_000


class FakeTransport:
    """Stands in for ArcusClient._fetch: (path, params) -> (status, body, headers) from a table."""

    def __init__(self):
        self.calls = []
        self.accounts = {}          # (address, index) -> (status, body)
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
                                             (404, load_fixture('arcus_account_404.json')))
            return status, body, {}
        if path == '/v1/fills':
            return 200, (self.fills_pages.pop(0) if self.fills_pages else {'fills': []}), {}
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


# Parsing ---------------------------------------------------------------------------

def test_account_parsing_with_tick_tier_decimals():
    """positions is a dict keyed by marketId; side gives the sign; markPx is the mark; the entry shows the
    tick of the tier markPx falls in ({upToPrice, tick}, last tier unbounded); no liquidation price."""
    snap = arcus.parse_snapshot(load_fixture('arcus_account.json'), markets())
    assert set(snap.positions) == {'BTC', 'TSLA'}                                   # the zero ETH row is skipped
    btc, tsla = snap.positions['BTC'], snap.positions['TSLA']
    assert btc['szi'] == '12.5' and btc['direction'] == 'LONG' and tsla['szi'] == '-1200' and tsla['direction'] == 'SHORT'
    assert btc['entry_px'] == '84221' and btc['px_decimals'] == 0                   # markPx 86,100 > 50,000: tick 1
    assert tsla['entry_px'] == '241.37' and tsla['px_decimals'] == 2                 # markPx 238.9 > 100: tick 0.01
    assert btc['position_value'] == '1076256.25' and btc['unrealized_pnl'] == '23490.0'
    assert btc['leverage'] == '10' and btc['margin_mode'] == 'CROSS' and 'liquidation_px' not in btc
    assert btc['cumulative_funding_since_open'] == '-310.5'
    assert snap.account_value == Decimal('4381205.17') and snap.extra['free_collateral'] == '2210040.33'
    # another index, no tiers on that market: tickSize decimals; without markets: raw entry, market:<id> coin
    sub = arcus.parse_snapshot(load_fixture('arcus_account_index2.json'), markets())
    assert sub.positions['SOL']['entry_px'] == '148.21' and sub.positions['SOL']['px_decimals'] == 2
    bare = arcus.parse_positions(load_fixture('arcus_account_index2.json'))
    assert bare['MARKET:12']['entry_px'] == '148.21'
    assert arcus.coin_of('BTC-USD') == 'BTC' and arcus.coin_of('qqq-usd') == 'QQQ' and arcus.coin_of('XYZ') == 'XYZ'
    btc_market = markets()['6']
    assert arcus.tick_for_price(btc_market, Decimal('999')) == Decimal('0.01')
    assert arcus.tick_for_price(btc_market, Decimal('1000')) == Decimal('0.01')             # upToPrice inclusive
    assert arcus.tick_for_price(btc_market, Decimal('1000.5')) == Decimal('0.1')
    assert arcus.tick_for_price(btc_market, Decimal('90000')) == Decimal('1')               # unbounded last tier
    assert arcus.tick_for_price(markets()['12'], Decimal('150')) == Decimal('0.01')         # no tiers: tickSize


def test_fill_conversion_uses_position_effect_and_adds_the_fee_back():
    page2 = load_fixture('arcus_fills_page2.json')['fills']
    close1, close2, add_short = (arcus.fill_to_hl(f) for f in page2)
    assert close1['dir'] == 'Close Long' and close1['side'] == 'A' and close1['coin'] == 'BTC'
    assert close1['closedPnl'] == '120.0'                                             # 117.5 after a 2.5 fee
    assert close2['closedPnl'] == '-30.0'
    assert add_short['dir'] == 'Open Short' and add_short['closedPnl'] == '0.0000'    # +0.0057 after a -0.0057 rebate
    assert add_short['crossed'] is False and add_short['fee'] == '-0.0057'            # maker rebate
    assert close1['time'] == T0 - 1000 and close1['tid'] == '902000' and close1['oid'] == '501000'
    # accumulate keeps the venue's dir and only walks the position
    walked = arcus.accumulate([close1, add_short], {'BTC': {'szi': '0.5'}}, keep_dir=True)
    assert [(f['dir'], Decimal(f['startPosition'])) for f in walked] == [('Close Long', Decimal('0.5')), ('Open Short', Decimal(0))]
    assert arcus.POSITION_EFFECTS['FLIP_LONG_TO_SHORT'] == 'Long > Short'
    # funding and leaderboard fixtures parse
    funding = load_fixture('arcus_funding.json')['fundingPayments']
    assert Decimal(funding[0]['payment']) < 0 and funding[0]['marketId'] == 6
    board = load_fixture('arcus_leaderboard.json')
    assert board['entries'][0]['rank'] == 1 and board['entries'][0]['address'] == ADDR


# Resolve: 200 / 403 / 404 ----------------------------------------------------------------

async def test_resolve_walks_indexes_and_classifies_403_and_404():
    transport = FakeTransport()
    transport.accounts[(ADDR, 0)] = (200, load_fixture('arcus_account.json'))
    transport.accounts[(ADDR, 2)] = (200, load_fixture('arcus_account_index2.json'))
    transport.accounts[(OTHER, 0)] = (403, load_fixture('arcus_account_403.json'))
    ad = arcus.ArcusAdapter(client_with(transport))
    found = await ad.resolve(ADDR)
    assert [(a.account_ref, a.label('cl')) for a in found] == [(f'{ADDR}:0', 'cl'), (f'{ADDR}:2', 'cl#2')]
    assert found[0].meta['equity'] == '4381205.17'
    assert len([c for c in transport.calls if c[0] == '/v1/account']) == 10          # every index tried
    assert resolve_summary({base.ARCUS: found}) == 'Arcus ✅ (2 accounts)'
    # 403: not whitelisted, stop at the first index
    transport.calls.clear()
    assert await ad.resolve(OTHER) == [] and len(transport.calls) == 1
    assert resolve_summary({base.ARCUS: []}) == 'Arcus ✗'
    # all 404
    transport.calls.clear()
    assert await ad.resolve('0x' + 'f' * 40) == [] and len(transport.calls) == 10
    # the client surfaces each status as its own error
    client = client_with(transport)
    with pytest.raises(ArcusNotWhitelisted):
        await client.account(OTHER, 0)
    with pytest.raises(ArcusNoActivity):
        await client.account(OTHER, 5)
    # the recorded bodies classify even on an unexpected status
    transport.accounts[(OTHER, 7)] = (200, load_fixture('arcus_account_404.json'))
    with pytest.raises(ArcusNoActivity):
        await client.account(OTHER, 7)
    transport.accounts[(OTHER, 8)] = (400, load_fixture('arcus_account_403.json'))
    with pytest.raises(ArcusNotWhitelisted):
        await client.account(OTHER, 8)


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


async def test_fetch_events_follows_full_pages_by_lowering_to(clock):
    transport = FakeTransport()
    transport.fills_pages = [load_fixture('arcus_fills_page1.json'), load_fixture('arcus_fills_page2.json')]
    ad = arcus.ArcusAdapter(client_with(transport))
    account = VenueAccount(base.ARCUS, f'{ADDR}:0', ADDR, 1)
    fills, cursor = await ad.fetch_events(account, None, {})                          # baseline: cursor only
    assert fills == [] and json.loads(cursor) == {'ms': T0} and transport.calls == []
    since = T0 - 10_000
    clock['ms'] = T0 + 1_001_000
    fills, cursor = await ad.fetch_events(account, arcus.encode_cursor(since), {'BTC': {'szi': '1'}})
    first, second = [c for c in transport.calls if c[0] == '/v1/fills']
    assert first[1] == {'address': ADDR, 'accountIndex': 0, 'limit': 1000, 'from': (since + 1) * 1000}
    oldest_us = min(f['createdAt'] for f in load_fixture('arcus_fills_page1.json')['fills'])
    assert second[1]['to'] == oldest_us - 1 and second[1]['from'] == (since + 1) * 1000
    assert len(fills) == 1003 and fills[0]['tid'] == '902002' and fills[-1]['tid'] == '900000'   # oldest first
    assert [f['time'] for f in fills] == sorted(f['time'] for f in fills)
    assert fills[0]['startPosition'] == '1' and fills[0]['dir'] == 'Open Short'                  # positionEffect ADD_SHORT
    assert fills[1]['dir'] == 'Close Long' and fills[1]['closedPnl'] == '-30.0'
    assert json.loads(cursor) == {'ms': fills[-1]['time']}
    # a short page: one call, rows at or before the cursor dropped
    transport.calls.clear()
    transport.fills_pages = [{'fills': load_fixture('arcus_fills_page2.json')['fills']}]
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
    transport.accounts[(ADDR, 0)] = (200, load_fixture('arcus_account.json'))
    transport.accounts[(ADDR, 2)] = (200, load_fixture('arcus_account_index2.json'))
    ad = arcus.ArcusAdapter(client_with(transport, WeightBudget(1200)))
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.ARCUS: ad}}
    assert await call(commands.add_wallet, repo, bot_data, 5, ADDR, 'cl') == [
        '✅ Wallet added as <b>cl</b> · HL ✗ · Arcus ✅ (2 accounts)']
    view = (await call(commands.positions_command, repo, bot_data, 5, 'cl'))[0]
    assert '<b>Arcus #0</b> · account $4,381,205.17' in view and '<b>Arcus #2</b> · account $4,100.00' in view
    assert '<b>LONG</b> $BTC' in view and 'Entry: $84,221' in view and '· 10x' in view
    assert '<b>SHORT</b> $TSLA' in view and 'Entry: $241.37' in view and '<b>LONG</b> $SOL' in view
    # venue pin on an address Arcus has never seen: forced main account, no HL row
    transport.accounts[(OTHER, 0)] = (403, load_fixture('arcus_account_403.json'))
    assert await call(commands.add_wallet, repo, bot_data, 5, f'arcus:{OTHER}', 'pinned') == [
        '✅ Wallet added as <b>pinned</b> · Arcus ✅ (added as given, no activity seen)']
    rows = {r['venue']: r for r in await repo.venue_accounts_of(OTHER)}
    assert set(rows) == {base.ARCUS} and rows[base.ARCUS]['account_ref'] == f'{OTHER}:0' and rows[base.ARCUS]['active']
    # polling: baseline, then a close fill on the sub-account -> [ARC] alert with the #2 label
    bot = FakeBot()
    key = next(r['key'] for r in await repo.venue_accounts_of(ADDR) if r['account_ref'] == f'{ADDR}:2')
    account = VenueAccount(base.ARCUS, f'{ADDR}:2', ADDR, key)
    await pipeline.poll_venue_account(bot, repo, ad, account)
    assert bot.sent == []
    transport.accounts[(ADDR, 2)] = (200, {**load_fixture('arcus_account_index2.json'), 'positions': {}})
    sell_us = (T0 + 5000) * 1000
    transport.fills_pages = [{'fills': [{'tradeId': 1, 'orderId': 1, 'marketId': 12, 'marketDisplayName': 'SOL-USD',
                                         'side': 'SELL', 'price': '150', 'size': '50', 'fee': '3', 'closedPnl': '86.5',
                                         'role': 'TAKER', 'positionEffect': 'CLOSE_LONG', 'createdAt': sell_us,
                                         'sequenceNumber': 1}], 'total': 1}]
    clock['ms'] = T0 + 30_000
    activity, _ = await pipeline.poll_venue_account(bot, repo, ad, account)
    assert activity and len(bot.sent) == 1
    text = bot.sent[0]['text']
    check_telegram_html(text)
    assert text.startswith('[ARC] 🔒 <b><a href="https://arcus.xyz/">cl#2</a></b> closed LONG $SOL')
    assert 'realized 🟢 +$90' in text                                                  # 86.5 after fee + 3 fee
    # /health lists the venue with its interval and the bucket
    assert poller.venue_interval_sec(base.ARCUS, ad, 2) == 20
    assert (await ad.health()) == {'mode': 'rest', 'detail': 'REST polling, bucket 1200/min'}
    health = await poller.venue_health(make_context(bot_data))
    assert health[base.ARCUS]['detail'] == 'REST polling, bucket 1200/min' and health[base.ARCUS]['interval_sec'] is None
    results = await resolve_wallet(repo, {base.ARCUS: ad}, ADDR, clock['ms'])
    assert resolve_summary(results) == 'Arcus ✅ (2 accounts)'


async def test_market_maker_fills_enter_multi_algo_summary_on_arcus(repo, clock):
    """The leaderboard's rank 1 wallet: small orders on many markets every few seconds. Six markets with
    one 0.2% slice each every 5 s become six algos within three polls and, at multi_algo_min (5), one
    [ARC] summary message instead of per-coin alerts."""
    transport = FakeTransport()
    account_body = load_fixture('arcus_account.json')
    big = {str(i): {'side': 'LONG', 'size': '1000', 'averageEntryPrice': '100', 'markPx': '100', 'unrealizedPnl': '0',
                    'leverage': '5', 'marginMode': 'CROSS', 'positionValueNotional': '100000', 'marginUsed': '1'}
           for i in range(50, 56)}
    transport.accounts[(ADDR, 0)] = (200, {**account_body, 'positions': big})
    transport.markets = {'markets': [{'marketId': i, 'marketDisplayName': f'M{i}-USD', 'baseAsset': f'M{i}',
                                      'tickSize': '0.01', 'stepSize': '1', 'status': 'ACTIVE', 'markPrice': '100', 'tickTiers': []}
                                     for i in range(50, 56)]}
    ad = arcus.ArcusAdapter(client_with(transport, WeightBudget(1200)))
    await repo.add_subscription(5, ADDR, 'mm', T0 - 60_000)
    key, _ = await repo.ensure_venue_account(ADDR, base.ARCUS, arcus.account_ref(ADDR, 0), True, T0)
    account = VenueAccount(base.ARCUS, arcus.account_ref(ADDR, 0), ADDR, key)
    bot = FakeBot()
    await pipeline.poll_venue_account(bot, repo, ad, account)                          # baseline
    fills, tid, t_us = [], 1, (T0 + 1000) * 1000
    for cycle in range(4):                                                             # 4 polls x 6 markets x 6 orders
        page = []
        for n in range(6):
            for i in range(50, 56):
                page.append({'tradeId': tid, 'orderId': tid, 'marketId': i, 'marketDisplayName': f'M{i}-USD',
                             'side': 'BUY', 'price': '100', 'size': '2', 'fee': '0.01', 'closedPnl': '-0.01',
                             'role': 'MAKER', 'positionEffect': 'ADD_LONG', 'createdAt': t_us, 'sequenceNumber': tid})
                tid += 1
            t_us += 5_000_000
        fills.append(page)
        clock['ms'] = t_us // 1000 + 1000
        transport.fills_pages = [{'fills': list(reversed(page)), 'total': len(page)}]
        grown = {k: {**v, 'size': str(1000 + 12 * (cycle + 1))} for k, v in big.items()}
        transport.accounts[(ADDR, 0)] = (200, {**account_body, 'positions': grown})
        await pipeline.poll_venue_account(bot, repo, ad, account)
    texts = [m['text'] for m in bot.sent]
    summary = [t for t in texts if 'running TWAP-style algos' in t]
    assert len(summary) == 1 and summary[0].startswith('[ARC] 🤖') and 'on 6 coins' in summary[0]
    assert 'accumulating LONG ×6' in summary[0]
    assert not any('algo accumulating' in t for t in texts)                            # no per-coin algo messages
    assert len(await repo.active_algos(key)) == 6 and await repo.multi_algo_mode(key) is not None
