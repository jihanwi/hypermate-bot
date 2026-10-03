"""Phase 2C Aster (spec 6.4): getBalance parsing (inactive fixture and the documented active shape),
privacy transition, bucket halving on 429, fills without ids or PnL, /add and polling."""

import json
from decimal import Decimal

import pytest

from hypermate.bot import commands
from hypermate.core import pipeline, poller
from hypermate.core.venues import resolve_summary, resolve_wallet
from hypermate.venues import base
from hypermate.venues.aster import adapter as aster
from hypermate.venues.aster.client import AsterAPIError, AsterClient, AsterRateLimited
from hypermate.venues.base import VenueAccount
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid.scheduler import WeightBudget
from hypermate.venues.hyperliquid.venue import HyperliquidVenue
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, load_fixture, make_context, make_update

INACTIVE = '0x84bd0dc61fea2e69393b7e5bfca71cebd7324d94'
ACTIVE = '0x690931c0000000000000000000000000000000ab'
T0 = 1_791_060_000_000

# The documented active response (github.com/asterdex/api-docs, RPC/aster-chain-rpc.md); not recorded live [?]
ACTIVE_RESULT = {
    'address': ACTIVE, 'accountPrivacy': 'disabled',
    'perpAssets': [{'asset': 'USD1', 'walletBalance': Decimal('200')}, {'asset': 'USDT', 'walletBalance': Decimal('1000')}],
    'positions': [{'tradingProduct': 'perps', 'positions': [
        {'id': '98000000000389_BTCUSDT_BOTH', 'symbol': 'BTCUSDT', 'collateral': 'USDT', 'positionAmount': '1.340',
         'entryPrice': '84490.74932115', 'unrealizedProfit': '-13990.31797537', 'notionalValue': '99227.28611496',
         'markPrice': '74050.21351863', 'leverage': 1, 'isolated': False, 'positionSide': 'BOTH',
         'marginValue': '99227.28611496'},
        {'id': 'x_ETHUSDT_BOTH', 'symbol': 'ETHUSDT', 'collateral': 'USDT', 'positionAmount': '-2.5',
         'entryPrice': '3000', 'unrealizedProfit': '10', 'notionalValue': '7500', 'markPrice': '2996',
         'leverage': 5, 'isolated': False, 'positionSide': 'BOTH', 'marginValue': '1500'},
        {'id': 'x_SOLUSDT_BOTH', 'symbol': 'SOLUSDT', 'collateral': 'USDT', 'positionAmount': '0',
         'entryPrice': '0', 'unrealizedProfit': '0', 'notionalValue': '0', 'markPrice': '100', 'leverage': 1,
         'isolated': False, 'positionSide': 'BOTH', 'marginValue': '0'},
    ]}],
    'staking': {'totalStakedAmount': None},
}


def test_inactive_fixture_and_documented_active_shape():
    inactive = load_fixture('aster_getBalance.json')['result']
    assert 'perpAssets' not in inactive and 'positions' not in inactive           # PM: keys absent
    assert not aster.is_active(inactive)
    snap = aster.parse_snapshot(inactive)
    assert snap.positions == {} and snap.account_value is None and snap.extra['privacy'] == 'disabled'

    assert aster.is_active(ACTIVE_RESULT)
    snap = aster.parse_snapshot(ACTIVE_RESULT)
    assert set(snap.positions) == {'BTC', 'ETH'}                                   # zero SOL row skipped
    assert snap.positions['BTC']['szi'] == '1.340' and snap.positions['BTC']['direction'] == 'LONG'
    assert snap.positions['ETH']['szi'] == '-2.5' and snap.positions['ETH']['direction'] == 'SHORT'
    assert snap.positions['BTC']['entry_px'] == '84490.74932115'
    assert snap.positions['BTC']['position_value'] == '99227.28611496'
    assert snap.positions['BTC']['unrealized_pnl'] == '-13990.31797537'
    assert snap.account_value == Decimal('1200') + Decimal('-13990.31797537') + Decimal('10')
    assert aster.coin_of('BTCUSDT') == 'BTC' and aster.coin_of('ASTCUSDC') == 'ASTC' and aster.coin_of('XYZ') == 'XYZ'
    # balance only, no positions: still active
    assert aster.is_active({'perpAssets': [{'asset': 'USDT', 'walletBalance': '5'}]})
    assert not aster.is_active({'perpAssets': [{'asset': 'USDT', 'walletBalance': '0'}]})
    # a SHORT positionSide with a positive amount is a short
    hedged = {'positions': [{'positions': [{'symbol': 'BTCUSDT', 'positionAmount': '1', 'positionSide': 'SHORT',
                                            'entryPrice': '1', 'notionalValue': '1', 'unrealizedProfit': '0'}]}]}
    assert aster.parse_positions(hedged)['BTC']['szi'] == '-1'


def test_fills_without_ids_or_pnl():
    docs_fills = [
        {'symbol': 'BTCUSDT', 'side': 'BUY', 'price': '69999', 'qty': '0.001', 'time': 1774233564000},
        {'symbol': 'BTCUSDT', 'side': 'SELL', 'price': '70658.6', 'qty': '0.001', 'time': 1774084612000},
        {'symbol': 'ETHUSDT', 'side': 'BUY', 'price': '1971', 'qty': '0.013', 'time': 1774084518000},
        {'symbol': 'BTCUSDT', 'side': 'BUY', 'price': '70676.7', 'qty': '0.001', 'time': 1774084489000},
    ]
    ordered = sorted(docs_fills, key=lambda f: f['time'])
    fills = aster.accumulate([aster.fill_to_hl(f, i) for i, f in enumerate(ordered)], {})
    assert [(f['coin'], f['side'], f['dir']) for f in fills] == [
        ('BTC', 'B', 'Open Long'), ('ETH', 'B', 'Open Long'), ('BTC', 'A', 'Close Long'), ('BTC', 'B', 'Open Long')]
    assert fills[0]['closedPnl'] is None and fills[0]['tid'] != fills[3]['tid']
    events = hl_adapter.fill_events(1, fills)
    close = next(e for e in events if e.type.value == 'position_close')
    assert close.realized_pnl is None and close.coin == 'BTC'                      # no PnL on Aster
    # two fills in the same ms, symbol and side share one oid (one order), distinct tids
    same_ms = [aster.fill_to_hl({'symbol': 'BTCUSDT', 'side': 'BUY', 'price': '1', 'qty': '1', 'time': 5}, i)
               for i in range(2)]
    assert same_ms[0]['oid'] == same_ms[1]['oid'] and same_ms[0]['tid'] != same_ms[1]['tid']
    assert len(hl_adapter.fill_events(1, aster.accumulate(same_ms, {}))) == 1


class FakeAsterClient:
    def __init__(self):
        self.balances = {}
        self.fills_by = {}
        self.calls = []
        self.budget = None

    async def get_balance(self, address, priority=0):
        self.calls.append(('getBalance', address))
        return self.balances.get(address.lower(), load_fixture('aster_getBalance.json')['result'])

    async def user_fills(self, address, from_ms, to_ms, symbol=None, priority=2):
        self.calls.append(('userFills', address, from_ms, to_ms))
        fills = [f for f in self.fills_by.get(address.lower(), []) if from_ms <= f['time'] <= to_ms]
        return {'accountPrivacy': 'disabled', 'fills': fills}


@pytest.fixture
def clock(monkeypatch):
    state = {'ms': T0}
    monkeypatch.setattr(hl_adapter, 'now_ms', lambda: state['ms'])
    monkeypatch.setattr(commands, 'now_ms', lambda: state['ms'])

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    return state


async def test_fetch_events_baseline_window_and_cursor(clock):
    client = FakeAsterClient()
    ad = aster.AsterAdapter(client)
    account = VenueAccount(base.ASTER, ACTIVE, ACTIVE, 1)
    fills, cursor = await ad.fetch_events(account, str(T0 - 1000), {})          # ms cursor: baseline
    assert fills == [] and json.loads(cursor) == {'ms': T0} and not client.calls
    client.fills_by[ACTIVE] = [{'symbol': 'BTCUSDT', 'side': 'SELL', 'price': '84000', 'qty': '0.34', 'time': T0 + 5000}]
    clock['ms'] = T0 + 30_000
    fills, cursor = await ad.fetch_events(account, cursor, {'BTC': {'szi': '1.340'}})
    assert client.calls[-1] == ('userFills', ACTIVE, T0 + 1, T0 + 30_000)        # window from the cursor to now
    assert [(f['dir'], f['startPosition']) for f in fills] == [('Close Long', '1.340')]
    assert json.loads(cursor) == {'ms': T0 + 5000}
    # an empty 7-day window moves the cursor forward instead of re-reading it forever
    clock['ms'] = T0 + 20 * 24 * 3600_000
    fills, cursor = await ad.fetch_events(account, cursor, {})
    assert fills == [] and json.loads(cursor)['ms'] == T0 + 5000 + 7 * 24 * 3600_000


async def test_client_halves_the_bucket_on_429(monkeypatch):
    clock = {'now': 100.0}

    async def sleep(s):
        clock['now'] += s

    budget = WeightBudget(300, clock=lambda: clock['now'], sleep=sleep)
    client = AsterClient('http://unused', budget)
    responses = [(200, {'jsonrpc': '2.0', 'id': 1, 'result': load_fixture('aster_getBalance.json')['result']}),
                 (429, None), (429, None), (200, {'jsonrpc': '2.0', 'id': 4, 'error': {'code': -32602, 'message': 'bad'}}),
                 (500, None)]
    sent = []

    async def fake_post(payload):
        sent.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(client, '_post', fake_post)
    result = await client.get_balance(INACTIVE)
    assert result['accountPrivacy'] == 'disabled'
    assert sent[0]['method'] == 'aster_getBalance' and sent[0]['params'] == [INACTIVE, 'latest']
    with pytest.raises(AsterRateLimited):
        await client.get_balance(INACTIVE)
    assert budget.per_minute == 150 and budget.paused_until > clock['now']
    budget.paused_until = 0
    with pytest.raises(AsterRateLimited):
        await client.user_fills(INACTIVE, 1, 2)
    assert budget.per_minute == 75 and sent[-1]['params'] == [INACTIVE, None, 1, 2, 'latest']
    budget.paused_until = 0
    for _ in range(5):
        budget.halve()
    assert budget.per_minute == WeightBudget.MIN_PER_MINUTE               # floor
    with pytest.raises(AsterAPIError):                                      # JSON-RPC error object
        await client.get_balance(INACTIVE)
    with pytest.raises(AsterAPIError):                                      # HTTP 500
        await client.get_balance(INACTIVE)


async def call(handler, repo, bot_data, user_id, *args):
    update = make_update(user_id)
    await handler(update, make_context(bot_data, args=args))
    for r in update.message.replies:
        check_telegram_html(r['text'])
    return [r['text'] for r in update.message.replies]


async def test_add_summary_positions_poll_and_privacy_transition(repo, clock):
    client = FakeAsterClient()
    client.balances[ACTIVE] = dict(ACTIVE_RESULT)
    ad = aster.AsterAdapter(client)
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl), base.ASTER: ad}}
    assert await call(commands.add_wallet, repo, bot_data, 5, INACTIVE, 'quiet') == [
        '✅ Wallet added as <b>quiet</b> · HL ✗ · Aster ✗']
    assert await call(commands.add_wallet, repo, bot_data, 5, ACTIVE, 'ast') == [
        '✅ Wallet added as <b>ast</b> · HL ✗ · Aster ✅ (privacy: off)']
    view = (await call(commands.positions_command, repo, bot_data, 5, 'ast'))[0]
    assert '<b>Aster</b> · account' in view and '<b>LONG</b> $BTC' in view and '<b>SHORT</b> $ETH' in view

    bot = FakeBot()
    rows = await repo.venue_accounts_of(ACTIVE)
    key = next(r['key'] for r in rows if r['venue'] == base.ASTER)
    account = VenueAccount(base.ASTER, ACTIVE, ACTIVE, key)
    await pipeline.poll_venue_account(bot, repo, ad, account)                     # baseline
    assert bot.sent == []
    # the ETH short is covered: snapshot changes, one BUY fill, alert without a realized line
    client.balances[ACTIVE] = {**ACTIVE_RESULT, 'positions': [{'tradingProduct': 'perps',
                                                               'positions': ACTIVE_RESULT['positions'][0]['positions'][:1]}]}
    client.fills_by[ACTIVE] = [{'symbol': 'ETHUSDT', 'side': 'BUY', 'price': '2990', 'qty': '2.5', 'time': T0 + 5000}]
    clock['ms'] = T0 + 30_000
    activity, _ = await pipeline.poll_venue_account(bot, repo, ad, account)
    assert activity and len(bot.sent) == 1
    text = bot.sent[0]['text']
    check_telegram_html(text)
    assert text.startswith('[ASTER] 🔒 <b><a href="https://www.asterdex.com/">ast</a></b> closed SHORT $ETH')
    assert 'realized' not in text

    # privacy turns on: PRIVACY_ON once, account inactive, no further polling or messages
    client.balances[ACTIVE] = {'address': ACTIVE, 'accountPrivacy': 'enabled', 'staking': {}}
    clock['ms'] = T0 + 60_000
    await pipeline.poll_venue_account(bot, repo, ad, account)
    assert len(bot.sent) == 2 and 'turned on Aster privacy' in bot.sent[1]['text']
    check_telegram_html(bot.sent[1]['text'])
    assert not next(r for r in await repo.venue_accounts_of(ACTIVE) if r['venue'] == base.ASTER)['active']
    assert len(await repo.events_since(key, 0, ['privacy_on'])) == 1
    clock['ms'] = T0 + 90_000
    await pipeline.poll_venue_account(bot, repo, ad, account)                     # still enabled: nothing new
    assert len(bot.sent) == 2
    # the daily rescan keeps it inactive while hidden, and brings it back once privacy is off
    await poller.rescan_job(make_context(bot_data))
    assert not next(r for r in await repo.venue_accounts_of(ACTIVE) if r['venue'] == base.ASTER)['active']
    client.balances[ACTIVE] = dict(ACTIVE_RESULT)
    await poller.rescan_job(make_context(bot_data))
    assert next(r for r in await repo.venue_accounts_of(ACTIVE) if r['venue'] == base.ASTER)['active']
    results = await resolve_wallet(repo, {base.ASTER: ad}, ACTIVE, clock['ms'])
    assert resolve_summary(results) == 'Aster ✅ (privacy: off)'
    assert poller.venue_interval_sec(base.ASTER, ad, 1) == 30
    assert (await ad.health())['detail'] == 'REST polling'
