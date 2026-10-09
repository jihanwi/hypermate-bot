"""Synthetic TWAP (external execution bot) detection, spec 5.2 합성 TWAP.

The loracle pattern is simulated (BTC Open Long and CASHCAT Close Short every few seconds for an hour,
no native TWAP) and replayed from the recording (hl_userFillsByTime_algo.json, 1,068 fills).
"""

from decimal import Decimal

import pytest

from hypermate.core import aggregator, pipeline
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, fill, load_fixture, make_context

W = '0x' + 'c' * 40
T0 = 1_790_000_000_000
CYCLE_MS = 30_000
SETTINGS = {'algo_window_sec': 300, 'algo_min_fills': 8, 'algo_max_slice_pct': 2,
            'algo_progress_sec': 600, 'algo_idle_sec': 600, 'debounce_sec': 60}


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms


@pytest.fixture
def clock(monkeypatch):
    c = Clock(T0)
    monkeypatch.setattr(adapter, 'now_ms', c)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    return c


def loracle_fills(minutes=60):
    """BTC Open Long 0.02-0.09 every ~5 s from 416.5 BTC; CASHCAT Close Short 300-3000 from -2,000,000."""
    fills, btc, cash, oid = [], Decimal('416.5'), Decimal('-2000000'), 1
    t = T0 + 1000
    i = 0
    while t < T0 + minutes * 60_000:
        size = Decimal('0.02') + Decimal('0.01') * (i % 8)
        fills.append(fill('BTC', 'Open Long', str(size), str(86000 + i % 50), t, str(btc), oid=oid, tid=10_000 + oid))
        btc += size
        oid += 1
        cash_size = Decimal(300 + (i * 97) % 2700)
        fills.append(fill('CASHCAT', 'Close Short', str(cash_size), '0.17', t + 1500, str(cash), oid=oid,
                          tid=10_000 + oid))
        cash += cash_size
        oid += 1
        t += 3000 + (i * 7919) % 7000          # 3-10 s apart
        i += 1
    return fills


async def run_cycles(repo, hl, bot, clock, until_ms):
    hl.now = clock
    context = make_context({'repo': repo, 'hl': hl}, bot=bot)
    while clock.ms < until_ms:
        clock.ms += CYCLE_MS
        await pipeline.monitor_transfers_job(context)


async def test_loracle_hour_gives_two_algo_starts_and_two_ends(repo, clock):
    await repo.add_subscription(7, W, 'loracle', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = loracle_fills(60)
    (va, _), = await repo.tracked_accounts()

    await run_cycles(repo, hl, bot, clock, T0 + 60 * 60_000)
    events = await repo.events_since(va, 0)
    starts = [e for e in events if e['type'] == 'algo_start']
    assert sorted((e['payload']['coin'], e['payload']['sign']) for e in starts) == [('BTC', 1), ('CASHCAT', 1)]
    assert {e['payload']['coin']: (e['payload']['verb'], e['payload']['side']) for e in starts} == {
        'BTC': ('accumulating', 'LONG'), 'CASHCAT': ('closing', 'SHORT')}

    # Per algo: the debounced fill message of the cycles before detection, then the START as a new
    # message (owner decision); every later order is suppressed_algo
    assert len(bot.sent) == 4
    texts = [m['text'] for m in bot.sent]
    assert sum('algo accumulating LONG $BTC' in t for t in texts) == 1
    assert sum('algo closing SHORT $CASHCAT' in t for t in texts) == 1
    assert sum('added to LONG $BTC' in t for t in texts) == 1
    assert sum('reduced SHORT $CASHCAT' in t for t in texts) == 1
    for text in texts:
        check_telegram_html(text)
    # suppressed orders leave no rows: only the orders sent before detection remain
    position_events = [e for e in events if e['type'].startswith('position_')]
    assert all(e['delivery'] == 'sent' for e in position_events) and len(position_events) < 40
    # progress edits about every 10 minutes
    assert len([e for e in bot.edits if 'algo' in e['text']]) >= 8

    # idle 10 minutes after the last fill -> two ENDs
    await run_cycles(repo, hl, bot, clock, T0 + 72 * 60_000)
    ends = [m['text'] for m in bot.sent[4:]]
    assert len(ends) == 2
    assert any('algo done accumulating LONG $BTC' in t for t in ends)
    assert any('algo done closing SHORT $CASHCAT' in t for t in ends)
    assert await repo.active_algos(va) == {}
    end_btc = next(e for e in await repo.events_since(va, 0, ['algo_end']) if e['payload']['coin'] == 'BTC')
    btc_orders = [f for f in hl.fills[W] if f['coin'] == 'BTC']
    assert end_btc['payload']['fills_count'] == len(btc_orders)
    assert Decimal(end_btc['payload']['total_sz']) == sum(Decimal(f['sz']) for f in btc_orders)


async def test_opposite_side_and_full_close_still_alert_during_algo(repo, clock):
    await repo.add_subscription(7, W, 'loracle', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [f for f in loracle_fills(10) if f['coin'] == 'BTC']
    await run_cycles(repo, hl, bot, clock, T0 + 10 * 60_000)
    assert len(bot.sent) == 2 and 'algo accumulating LONG $BTC' in bot.sent[1]['text']
    t = clock.ms
    hl.fills[W].append(fill('BTC', 'Close Long', '50', '86500', t + 1000, '420', oid=9001, closed_pnl='100'))
    hl.fills[W].append(fill('DOGE', 'Close Long', '10', '0.2', t + 2000, '10', oid=9002,
                            liquidation={'markPx': '0.2', 'method': 'market'}))
    await run_cycles(repo, hl, bot, clock, t + CYCLE_MS)
    texts = [m['text'] for m in bot.sent[2:]]
    assert any('reduced LONG $BTC' in x for x in texts)          # opposite direction
    assert any('LIQUIDATED LONG $DOGE' in x for x in texts)
    (va, _), = await repo.tracked_accounts()
    assert ('BTC', 1) in await repo.active_algos(va)              # state kept


async def test_algo_survives_restart(tmp_path, clock):
    path = str(tmp_path / 'hm.db')
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [f for f in loracle_fills(20) if f['coin'] == 'BTC']
    repo = Repo(path)
    await repo.connect()
    await repo.add_subscription(7, W, 'loracle', T0)
    await run_cycles(repo, hl, bot, clock, T0 + 10 * 60_000)
    await repo.close()

    repo = Repo(path)
    await repo.connect()
    await run_cycles(repo, hl, bot, clock, T0 + 32 * 60_000)
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 3 and 'added to LONG $BTC' in texts[0] and 'algo accumulating' in texts[1] \
        and 'algo done accumulating LONG $BTC' in texts[2]
    (va, _), = await repo.tracked_accounts()
    assert len(await repo.events_since(va, 0, ['algo_start'])) == 1
    await repo.close()


async def test_manual_trades_do_not_start_an_algo(repo, clock):
    """Ten buys a minute apart: debounced into one message, never an algo (only 5 orders per window)."""
    await repo.add_subscription(7, W, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [fill('ETH', 'Open Long', '1', '3000', T0 + 1000 + 60_000 * i, str(i), oid=i + 1)
                   for i in range(10)]
    await run_cycles(repo, hl, bot, clock, T0 + 11 * 60_000)
    (va, _), = await repo.tracked_accounts()
    assert await repo.events_since(va, 0, ['algo_start']) == []
    assert len(bot.sent) == 1


async def test_recorded_algo_fixture_sends_two_starts_and_two_ends(repo, clock):
    """PM expectation: 4 algo sends (2 START, 2 END) plus the one fill message per key sent before
    detection (BTC 1, CASHCAT 1) = 6, and nothing else."""
    fills = load_fixture('hl_userFillsByTime_algo.json')
    times = [int(f['time']) for f in fills]
    assert len(fills) == 1068 and all(f.get('twapId') is None for f in fills)
    clock.ms = min(times) - 1000
    await repo.add_subscription(7, W, 'loracle', clock.ms)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = fills
    (va, _), = await repo.tracked_accounts()

    await run_cycles(repo, hl, bot, clock, max(times) + 12 * 60_000)
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 6
    assert sum('added to LONG $BTC' in t for t in texts) == 1
    assert sum('reduced SHORT $CASHCAT' in t for t in texts) == 1
    assert sum('algo accumulating LONG $BTC' in t for t in texts) == 1
    assert sum('algo closing SHORT $CASHCAT' in t for t in texts) == 1
    assert sum('algo done accumulating LONG $BTC' in t for t in texts) == 1
    assert sum('algo done closing SHORT $CASHCAT' in t for t in texts) == 1
    for text in texts:
        check_telegram_html(text)
    events = await repo.events_since(va, 0)
    assert sorted(e['type'] for e in events if e['type'].startswith('algo_')) == [
        'algo_end', 'algo_end', 'algo_start', 'algo_start']
    # every order of the hour is counted in its algo (834 oids)
    ends = {e['payload']['coin']: e['payload'] for e in events if e['type'] == 'algo_end'}
    assert sum(int(p['fills_count']) for p in ends.values()) == len({f['oid'] for f in fills})
    for coin, payload in ends.items():
        coin_fills = [f for f in fills if f['coin'] == coin]
        assert Decimal(payload['total_sz']) == sum(Decimal(f['sz']) for f in coin_fills)
    assert await repo.active_algos(va) == {}

    # the recorded positions at the end of the hour match the fills: both keys only move the position
    # up (+ sign), so the final size is the largest startPosition + sz
    state = load_fixture('hl_clearinghouseState_algo.json')
    final = {p['position']['coin']: Decimal(p['position']['szi']) for p in state['assetPositions']}
    for coin in ('BTC', 'CASHCAT'):
        assert max(Decimal(f['startPosition']) + Decimal(f['sz']) for f in fills if f['coin'] == coin) == final[coin]


def _order(ts, poll, notional, start='1', after='2', price='100', size='1'):
    return {'type': 'position_increase', 'coin': 'BTC', 'ts_ms': ts, 'notional_usd': notional,
            'position_after': after, 'price': price, 'size': size,
            'meta': {'poll_ms': poll, 'start_position': start, 'sign': 1, 'first_ms': ts}}


def test_entry_rule():
    # 9 orders over 3 polls, each $100 against a $20k position (0.5%) -> start
    orders = [_order(i, i // 3, '100', after='200', price='100') for i in range(9)]
    assert aggregator.should_start_algo(orders, SETTINGS)
    # only 2 polls
    assert not aggregator.should_start_algo([_order(i, i // 5, '100', after='200') for i in range(9)], SETTINGS)
    # fewer than algo_min_fills orders
    assert not aggregator.should_start_algo(orders[:7], SETTINGS)
    # slices too big for the position (median $1000 vs 2% of $20k = $400)
    assert not aggregator.should_start_algo([_order(i, i // 3, '1000', after='200') for i in range(9)], SETTINGS)
    # position started from 0: the slice rule is skipped (owner decision)
    fresh = [_order(i, i // 3, '1000', start='0' if i == 0 else '1', after='200') for i in range(9)]
    assert aggregator.should_start_algo(fresh, SETTINGS)


async def test_backlog_in_one_cycle_does_not_start_an_algo(repo, clock):
    """Item 2c: the 3-cycle rule counts poll cycles, not fill times. 20 orders spread over 5 minutes
    that all arrive in one poll (after a gap) are one cycle and must not start an algo."""
    await repo.add_subscription(7, W, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [fill('BTC', 'Open Long', '0.01', '86000', T0 + 1000 + 15_000 * i, str(400 + Decimal('0.01') * i),
                        oid=i + 1) for i in range(20)]
    (va, _), = await repo.tracked_accounts()
    hl.now = clock
    clock.ms = T0 + 6 * 60_000                          # one poll sees all 20 orders at once
    await pipeline.monitor_transfers_job(make_context({'repo': repo, 'hl': hl}, bot=bot))
    assert await repo.events_since(va, 0, ['algo_start']) == []
    assert len(bot.sent) == 1                           # one debounced message
    polls = {e['payload']['meta']['poll_ms'] for e in await repo.events_since(va, 0)}
    assert len(polls) == 1


async def test_algo_key_is_never_duplicated_and_ends_on_idle(repo, clock):
    """Item 2a/2b: one row per (coin, sign); END exactly when algo_idle_sec has passed since last_fill_ms."""
    await repo.add_subscription(7, W, 'loracle', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [f for f in loracle_fills(10) if f['coin'] == 'BTC']
    (va, _), = await repo.tracked_accounts()
    await run_cycles(repo, hl, bot, clock, T0 + 10 * 60_000)
    assert list(await repo.active_algos(va)) == [('BTC', 1)]
    starts = await repo.events_since(va, 0, ['algo_start'])
    assert len(starts) == 1
    # more cycles with the same key active: no second row, no second START
    hl.fills[W].append(fill('BTC', 'Open Long', '0.05', '86000', clock.ms + 1000, '420', oid=5000))
    await run_cycles(repo, hl, bot, clock, clock.ms + 3 * CYCLE_MS)
    assert list(await repo.active_algos(va)) == [('BTC', 1)]
    assert len(await repo.events_since(va, 0, ['algo_start'])) == 1
    last_fill = int((await repo.active_algos(va))[('BTC', 1)]['last_fill_ms'])
    assert last_fill == hl.fills[W][-1]['time']
    # idle: no END before algo_idle_sec (600 s) from the last fill, END on the first cycle after
    await run_cycles(repo, hl, bot, clock, last_fill + 600_000 - CYCLE_MS)
    assert await repo.events_since(va, 0, ['algo_end']) == []
    await run_cycles(repo, hl, bot, clock, last_fill + 600_000 + CYCLE_MS)
    assert len(await repo.events_since(va, 0, ['algo_end'])) == 1
    assert await repo.active_algos(va) == {}


async def test_algo_label_follows_the_position_effect(repo, clock):
    """2026-10-09 review: 'algo accumulating SHORT $IREN … pos $0' was a long being sold down. A sell algo on
    a long is 'closing LONG' until the position crosses 0, 'accumulating SHORT' from then on; the START
    message shows 'closed' instead of 'pos $0' at the crossing."""
    await repo.add_subscription(7, W, 'iroh', T0)
    hl, bot = FakeHLClient(), FakeBot()
    (va, _), = await repo.tracked_accounts()
    fills, position, t, oid = [], Decimal(100), T0 + 1000, 1
    while position > 0:                                                   # 100 x 1 BTC sells: 1% slices
        fills.append(fill('BTC', 'Close Long', '1', '86000', t, str(position), oid=oid, closed_pnl='5'))
        position -= 1
        t += 5000
        oid += 1
    hl.fills[W] = fills
    await run_cycles(repo, hl, bot, clock, T0 + 3 * 60_000)              # detected while still long
    start = next(e for e in await repo.events_since(va, 0, ['algo_start']))
    assert (start['payload']['verb'], start['payload']['side']) == ('closing', 'LONG')
    assert any('algo closing LONG $BTC' in m['text'] for m in bot.sent)
    await run_cycles(repo, hl, bot, clock, t)                              # position reached 0
    start = await repo.get_event(start['event_id'])
    assert (start['payload']['verb'], start['payload']['side']) == ('closing', 'LONG')
    assert Decimal(start['payload']['position_after']) == 0
    for _ in range(20):                                                    # keeps selling: now a short
        fills.append(fill('BTC', 'Open Short', '1', '86000', t, str(-(oid - 101)), oid=oid))
        t += 5000
        oid += 1
    await run_cycles(repo, hl, bot, clock, t + 11 * 60_000)                # progress edit after 10 min
    start = await repo.get_event(start['event_id'])
    assert (start['payload']['verb'], start['payload']['side']) == ('accumulating', 'SHORT')
    algo_edits = [e['text'] for e in bot.edits if 'algo' in e['text']]
    from hypermate.core import formatter
    at_zero = formatter.format_algo_progress(W, 'iroh', {'coin': 'BTC', 'sign': -1, 'started_ms': 0, 'last_fill_ms': 60_000,
                                                          'fills_count': 100, 'total_sz': '100', 'total_ntl': '8600000'},
                                             'closing', 'LONG', Decimal(0))
    assert '· closed' in at_zero and 'pos $0' not in at_zero
    assert 'algo accumulating SHORT $BTC' in algo_edits[-1] and 'pos $1.72M' in algo_edits[-1]
    assert algo_edits[-1].count('pos $0') == 0
    # the END keeps the final label
    await run_cycles(repo, hl, bot, clock, t + 25 * 60_000)
    assert any('algo done accumulating SHORT $BTC' in m['text'] for m in bot.sent)
