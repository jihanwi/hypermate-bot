"""Phase 1 PR A: TWAP START/END and suppression (spec 5.2 TWAP), on the recorded fixtures."""

import copy
import logging
from decimal import Decimal

from hypermate.core import formatter, pipeline
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter, twap
from tests.helpers import (FakeBot, FakeHLClient, check_telegram_html, clearinghouse, fill, load_fixture,
                           make_context, position)

# Wallet whose webData2 / twapHistory were recorded
W = '0xeb6eac57da5d1333895faab287c2c7fb58ff5e5e'
FIXTURE_TWAP_IDS = {'2276387', '2276388', '2276389', '2276390', '2276392'}  # BTC STX SOL HYPE ETH, all side B


async def _no_sleep(_):
    return None


def web_with(*twap_ids, base=None):
    """The recorded webData2, keeping only the given TWAPs."""
    web = copy.deepcopy(base or load_fixture('hl_webData2.json'))
    web['twapStates'] = [t for t in web['twapStates'] if str(t[0]) in twap_ids]
    return web


async def setup(repo, monkeypatch, positions):
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    await repo.add_subscription(7, W, 'loracle', 1000)
    hl, bot = FakeHLClient(), FakeBot()
    hl.clearinghouse[W] = clearinghouse(*positions)
    hl.web[W] = load_fixture('hl_webData2.json')
    hl.twap_histories[W] = load_fixture('hl_twapHistory.json')
    context = make_context({'repo': repo, 'hl': hl}, bot=bot)
    await pipeline.monitor_positions_job(context)          # baseline, no TWAP lookup yet
    (va, _), = await repo.tracked_accounts()
    return hl, bot, context, va


async def events_by(repo, va):
    return await repo.recent_events(va, 100)


def sent_texts(bot):
    return [m['text'] for m in bot.sent]


async def fills_cycle(context):
    await pipeline.monitor_transfers_job(context)


async def test_fixture_twaps_start_once(repo, monkeypatch):
    hl, bot, context, va = await setup(repo, monkeypatch, [position('BTC', '0.01444'), position('SOL', '6.66')])
    assert bot.sent == [] and ('webData2', W) not in hl.calls

    # first TWAP slice on BTC: position grows -> webData2 -> 5 TWAP_START
    hl.clearinghouse[W] = clearinghouse(position('BTC', '0.01472'), position('SOL', '6.66'))
    await pipeline.monitor_positions_job(context)
    texts = sent_texts(bot)
    assert len(texts) == 5 and all('started TWAP BUY' in t for t in texts)
    assert set(await repo.active_twaps(va)) == FIXTURE_TWAP_IDS

    # next cycles: same twapStates -> no new START
    bot.sent.clear()
    hl.clearinghouse[W] = clearinghouse(position('BTC', '0.015'), position('SOL', '6.86'))
    await pipeline.monitor_positions_job(context)
    await pipeline.monitor_positions_job(context)  # nothing changed, active TWAPs -> webData2 again
    assert bot.sent == []
    assert sum(e['type'] == 'twap_start' for e in await events_by(repo, va)) == 5


async def test_same_direction_fills_during_twap_are_suppressed(repo, monkeypatch):
    hl, bot, context, va = await setup(repo, monkeypatch, [position('BTC', '0.01444'), position('SOL', '6.66')])
    hl.clearinghouse[W] = clearinghouse(position('BTC', '0.01472'), position('SOL', '6.66'))
    await pipeline.monitor_positions_job(context)          # 5 buy TWAPs active
    bot.sent.clear()
    now = adapter.now_ms()
    hl.fills[W] = [fill('BTC', 'Open Long', '0.001', '85000', now - 5000, '0.01472', oid=1),   # manual buy
                   fill('SOL', 'Close Long', '3', '120', now - 4000, '6.66', oid=2)]          # manual sell
    (va, _), = await repo.tracked_accounts()
    await repo.set_cursor(va, 'fills', str(now - 10_000), 1)
    await fills_cycle(context)
    texts = sent_texts(bot)
    assert len(texts) == 1 and 'reduced LONG $SOL' in texts[0]
    recorded = {e['payload']['coin']: e['delivery'] for e in await events_by(repo, va)
                if e['type'].startswith('position_')}
    assert recorded == {'BTC': 'suppressed_twap', 'SOL': 'sent'}


async def test_short_twap_suppresses_short_fills(repo, monkeypatch):
    """loracle case: fills in the direction of a sell TWAP building a short (CASHCAT) are not alerted."""
    hl, bot, context, va = await setup(repo, monkeypatch, [position('CASHCAT', '-1000')])
    web = web_with()
    web['twapStates'] = [[9001, {'coin': 'CASHCAT', 'user': W, 'side': 'A', 'sz': '50000', 'executedSz': '1000',
                                 'executedNtl': '12.5', 'minutes': 600, 'reduceOnly': False, 'randomize': True,
                                 'timestamp': 1790878210368}]]
    hl.web[W] = web
    hl.clearinghouse[W] = clearinghouse(position('CASHCAT', '-1500'))
    await pipeline.monitor_positions_job(context)
    texts = sent_texts(bot)
    assert len(texts) == 1 and 'started TWAP SELL $CASHCAT' in texts[0]
    now = adapter.now_ms()
    await repo.set_cursor(va, 'fills', str(now - 10_000), 1)
    hl.fills[W] = [fill('CASHCAT', 'Open Short', '500', '0.17', now - 3000 + i, str(-1500 - 500 * i), oid=10 + i)
                   for i in range(3)]
    await fills_cycle(context)
    assert len(sent_texts(bot)) == 1
    positions = [e for e in await events_by(repo, va) if e['type'].startswith('position_')]
    assert len(positions) == 3 and {e['delivery'] for e in positions} == {'suppressed_twap'}


async def test_twap_end_uses_twap_history_status(repo, monkeypatch):
    hl, bot, context, va = await setup(repo, monkeypatch, [position('BTC', '0.01')])
    # 2248297 is a BTC buy TWAP that twapHistory records as finished (100.20108 USDC for 0.00119 BTC)
    activated = next(h for h in load_fixture('hl_twapHistory.json')
                     if h['twapId'] == 2248297 and h['status']['status'] == 'activated')
    await repo.upsert_twap(va, '2248297', activated['state'], activated['state']['timestamp'])
    hl.web[W] = web_with()                                              # no longer in twapStates
    hl.clearinghouse[W] = clearinghouse(position('BTC', '0.01119'))    # its last slice
    await pipeline.monitor_positions_job(context)

    texts = sent_texts(bot)
    assert len(texts) == 1
    end = texts[0]
    check_telegram_html(end)
    assert 'TWAP done BUY $BTC' in end and 'status finished' in end
    assert 'filled $100 (0.00119 BTC) avg 84,203' in end            # 100.20108 / 0.00119 = 84202.6
    assert await repo.active_twaps(va) == {}
    end_event, = [e for e in await events_by(repo, va) if e['type'] == 'twap_end']
    assert end_event['payload']['status'] == 'finished'


async def test_error_status_shows_description(repo, monkeypatch):
    hl, bot, context, va = await setup(repo, monkeypatch, [position('SOL', '1')])
    activated = next(h for h in load_fixture('hl_twapHistory.json')
                     if h['twapId'] == 2052843 and h['status']['status'] == 'activated')
    await repo.upsert_twap(va, '2052843', activated['state'], activated['state']['timestamp'])
    hl.web[W] = web_with()
    await pipeline.monitor_positions_job(context)
    end, = sent_texts(bot)
    assert 'TWAP error BUY $SOL' in end and 'Insufficient margin to place order.' in end
    assert 'status error' in end and '4h 1m' in end   # 1784844094 s - 1784829602500 ms = 241 min


async def test_end_waits_one_cycle_for_history_then_reports_unknown(repo, monkeypatch):
    hl, bot, context, va = await setup(repo, monkeypatch, [position('ETH', '1')])
    state = dict(web_with('2276392')['twapStates'][0][1])
    await repo.upsert_twap(va, '2276392', state, state['timestamp'])   # history only has 'activated' for it
    hl.web[W] = web_with()
    await pipeline.monitor_positions_job(context)
    assert bot.sent == [] and twap.END_PENDING in (await repo.active_twaps(va))['2276392']
    await pipeline.monitor_positions_job(context)
    end, = sent_texts(bot)
    assert 'TWAP ended BUY $ETH' in end and 'status unknown' in end
    assert await repo.active_twaps(va) == {}


async def test_restart_keeps_tracking_and_sends_end(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    path = str(tmp_path / 'hm.db')
    hl, bot = FakeHLClient(), FakeBot()
    hl.web[W] = web_with('2276387')
    hl.twap_histories[W] = load_fixture('hl_twapHistory.json')

    repo = Repo(path)
    await repo.connect()
    await repo.add_subscription(7, W, 'loracle', 1000)
    hl.clearinghouse[W] = clearinghouse(position('BTC', '0.01'))
    await pipeline.monitor_positions_job(make_context({'repo': repo, 'hl': hl}, bot=bot))
    hl.clearinghouse[W] = clearinghouse(position('BTC', '0.0103'))
    await pipeline.monitor_positions_job(make_context({'repo': repo, 'hl': hl}, bot=bot))
    assert len(bot.sent) == 1 and 'started TWAP' in bot.sent[0]['text']
    await repo.close()

    # while down the TWAP finishes; make twapHistory report it
    history = load_fixture('hl_twapHistory.json')
    done = copy.deepcopy(next(h for h in history if h['twapId'] == 2276387))
    done['status'] = {'status': 'finished'}
    done['time'] = 1790978210
    done['state'].update(executedSz='0.00119', executedNtl='101.4')
    hl.twap_histories[W] = [done] + history
    hl.web[W] = web_with()
    bot.sent.clear()

    repo = Repo(path)
    await repo.connect()
    caplog.set_level(logging.INFO)
    await pipeline.monitor_positions_job(make_context({'repo': repo, 'hl': hl}, bot=bot))
    end, = sent_texts(bot)
    assert 'TWAP done BUY $BTC' in end and 'started TWAP' not in end
    (va, _), = await repo.tracked_accounts()
    assert await repo.active_twaps(va) == {}
    await repo.close()


async def test_no_webdata2_call_without_changes_or_active_twaps(repo, monkeypatch):
    hl, bot, context, va = await setup(repo, monkeypatch, [position('BTC', '1')])
    await pipeline.monitor_positions_job(context)
    assert ('webData2', W) not in hl.calls


async def test_dedupe_key_blocks_repeat_sends(repo, monkeypatch):
    hl, bot, context, va = await setup(repo, monkeypatch, [])
    render = lambda alias: f'hi {alias}'  # noqa: E731
    first = await pipeline.emit(bot, repo, va, pipeline.EventType.TWAP_START, '42', 1, {}, render)
    second = await pipeline.emit(bot, repo, va, pipeline.EventType.TWAP_START, '42', 1, {}, render)
    assert isinstance(first, int) and second is None and len(bot.sent) == 1


async def test_ledger_and_spot_events_are_recorded_with_dedupe(repo, monkeypatch):
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    await repo.add_subscription(7, W, 'w', 1000)
    hl, bot = FakeHLClient(), FakeBot()
    send = copy.deepcopy(load_fixture('hl_userNonFundingLedgerUpdates.json')[1])   # a recorded 'send'
    send['time'] = 5000
    send['delta']['destination'] = W
    hl.ledger[W] = [send]
    hl.fills[W] = [fill('@107', 'Sell', '1', '40', 5001, '5', oid=3, tid=77)]
    context = make_context({'repo': repo, 'hl': hl}, bot=bot)
    await pipeline.monitor_transfers_job(context)
    (va, _), = await repo.tracked_accounts()
    # cursor rewound by hand: the same items come back, dedupe keeps them from being sent twice
    await repo.set_cursor(va, 'ledger', '1000', 1)
    await repo.set_cursor(va, 'fills', '1000', 1)
    await pipeline.monitor_transfers_job(context)
    assert len(bot.sent) == 2
    types = sorted(e['type'] for e in await repo.recent_events(va))
    assert types == ['spot_sell', 'transfer_in']


def test_twap_formats():
    web = load_fixture('hl_webData2.json')
    state = dict(twap.parse_twap_states(web)['2276387'])
    prices = twap.mark_prices(web)
    assert prices['BTC'] == Decimal('85206.8')
    start = formatter.format_twap_start(W, 'lo_racle', state, prices['BTC'], 1790958929641)
    check_telegram_html(start)
    # 0.00119 BTC * 85206.8 = 101.4; 10080 min = 7 days (humanized), so the end shows a date
    assert start.startswith('[HL] ⏳ <b><a href=') and 'started TWAP BUY $BTC' in start
    # started 1790878210368 ms + 7 days = 2026-10-09 03:10 KST
    assert '$101 over 7d · ends ~10-09 03:10 KST' in start
    no_price = formatter.format_twap_start(W, 'x', state, None, 1790958929641)
    assert '0.00119 BTC over 7d' in no_price
    end = formatter.format_twap_end(W, 'x', {**state, 'executedSz': '41200', 'executedNtl': '1980000'},
                                    'finished', None, int(state['timestamp']) + 118 * 60_000)
    assert 'filled $1.98M (41,200 BTC) avg 48.06 · 1h 58m · status finished' in end
    hip3 = formatter.format_twap_start(W, 'x', {**state, 'coin': 'xyz:MSTR', 'minutes': 90}, None, 1790958929641)
    assert 'started TWAP BUY $MSTR (xyz)' in hip3 and '0.00119 MSTR over 1h 30m' in hip3
    assert 'nothing filled' in formatter.format_twap_end(W, 'x', {**state, 'executedSz': '0.0'}, 'terminated',
                                                         None, int(state['timestamp']))


def test_compact_usd_and_quantity():
    assert formatter.compact_usd(Decimal('1250000')) == '$1.25M'
    assert formatter.compact_usd(Decimal('45000')) == '$45k'
    assert formatter.compact_usd(Decimal('1234')) == '$1.23k'
    assert formatter.compact_usd(Decimal('2000000')) == '$2M'
    assert formatter.quantity(Decimal('41200')) == '41,200'
    assert formatter.quantity(Decimal('0.0011945')) == '0.001195'


def test_suppression_rules():
    buy = {'coin': 'BTC', 'side': 'B'}
    sell = {'coin': 'BTC', 'side': 'A'}

    def event(kind, sign, coin='BTC'):
        return {'coin': coin, 'type': kind, 'meta': {'sign': sign}}

    assert twap.is_suppressed(event('position_increase', 1), [buy])
    assert twap.is_suppressed(event('position_open', 1), [buy])
    assert twap.is_suppressed(event('position_decrease', 1), [buy])      # buying back a short
    assert twap.is_suppressed(event('position_close', 1), [buy])
    assert not twap.is_suppressed(event('position_decrease', -1), [buy])  # selling against a buy TWAP
    assert twap.is_suppressed(event('position_decrease', -1), [sell])
    assert not twap.is_suppressed(event('liquidation', -1), [sell])
    assert not twap.is_suppressed(event('position_flip', -1), [sell])
    assert not twap.is_suppressed(event('position_increase', 1, coin='ETH'), [buy])
