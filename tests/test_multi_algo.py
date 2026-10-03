"""fix/multi-algo-summary: suppressed fills leave no events rows, summary mode for accounts running many
algos at once, the one-time cleanup of old suppressed rows."""

import logging
from decimal import Decimal

import pytest

from hypermate.bot import commands
from hypermate.config import Config
from hypermate.core import pipeline
from hypermate.db import backup
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, fill, load_fixture, make_context, make_update

W = '0x' + 'c' * 40
T0 = 1_790_000_000_000
CYCLE_MS = 30_000
COINS = ['LINK', 'DOGE', 'SUI', 'ONDO', 'UNI', 'ARB', 'OP', 'AVAX', 'NEAR', 'APT', 'SEI', 'TIA', 'INJ', 'WIF',
         'PEPE', 'BTC', 'CASHCAT']           # 15 reducing SHORT, 2 accumulating LONG


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms


@pytest.fixture
def clock(monkeypatch):
    c = Clock(T0)
    monkeypatch.setattr(adapter, 'now_ms', c)
    monkeypatch.setattr(commands, 'now_ms', c)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    return c


async def run_cycles(repo, hl, bot, clock, until_ms):
    hl.now = clock
    context = make_context({'repo': repo, 'hl': hl}, bot=bot)
    while clock.ms < until_ms:
        clock.ms += CYCLE_MS
        await pipeline.monitor_transfers_job(context)


def seventeen_coin_fills(minutes, start=T0 + 1000):
    """Every 4 s one small order on each of 17 coins: 15 Close Short (reducing SHORT, +), BTC and CASHCAT
    Open Long (accumulating LONG, +). Position sizes are large, so every order is far under 2%."""
    fills, oid, t, i = [], 1, start, 0
    while t < start + minutes * 60_000:
        for n, coin in enumerate(COINS):
            if coin in ('BTC', 'CASHCAT'):
                fills.append(fill(coin, 'Open Long', '0.05', '86000' if coin == 'BTC' else '0.17', t + n,
                                  str(Decimal(400) + Decimal('0.05') * i), oid=oid))
            else:
                fills.append(fill(coin, 'Close Short', '100', '2', t + n, str(Decimal(-2_000_000) + 100 * i),
                                  oid=oid, closed_pnl='1'))
            oid += 1
        t += 4000
        i += 1
    return fills


async def test_loracle_hour_records_95_percent_fewer_rows(repo, clock):
    """Before this change the hour produced 838 events rows (21 sent + 817 suppressed_algo)."""
    fills = load_fixture('hl_userFillsByTime_algo.json')
    times = [int(f['time']) for f in fills]
    clock.ms = min(times) - 1000
    await repo.add_subscription(7, W, 'loracle', clock.ms)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = fills
    await run_cycles(repo, hl, bot, clock, max(times) + 12 * 60_000)
    cur = await repo.db.execute("SELECT COUNT(*) FROM events")
    (rows,) = await cur.fetchone()
    assert rows <= 838 * 5 // 100, rows                        # 95% fewer than 838
    cur = await repo.db.execute("SELECT COUNT(*) FROM events WHERE delivery LIKE 'suppressed%'")
    assert (await cur.fetchone())[0] == 0
    assert len(bot.sent) == 6                                 # 2 fill messages, 2 START, 2 END, as before
    (va, _), = await repo.tracked_accounts()
    ends = await repo.events_since(va, 0, ['algo_end'])
    btc = next(e for e in ends if e['payload']['coin'] == 'BTC')
    assert int(btc['payload']['fills_count']) == len({f['oid'] for f in fills if f['coin'] == 'BTC'})


async def test_seventeen_coins_enter_summary_mode_edit_hourly_and_exit(repo, clock):
    await repo.add_subscription(7, W, 'loracle', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = seventeen_coin_fills(70)
    (va, _), = await repo.tracked_accounts()

    await run_cycles(repo, hl, bot, clock, T0 + 5 * 60_000)
    texts = [m['text'] for m in bot.sent]
    summaries = [t for t in texts if 'running TWAP-style algos' in t]
    assert len(summaries) == 1
    summary = summaries[0]
    check_telegram_html(summary)
    assert 'running TWAP-style algos on 17 coins' in summary
    assert 'reducing SHORT ×15 (' in summary and '/24h): ' in summary and ' +10' in summary
    assert 'accumulating LONG ×2 (' in summary and 'BTC, CASHCAT' in summary
    assert await repo.multi_algo_mode(va) is not None
    assert len(await repo.active_algos(va)) == 17
    # no per-coin START went out: the threshold was crossed inside one cycle
    assert not any('algo accumulating' in t or 'algo reducing' in t for t in texts)
    starts = await repo.events_since(va, 0, ['algo_start'])
    assert len(starts) == 17 and {e['delivery'] for e in starts} == {'summarized'}

    # one large single order ($150k) is still alerted while in summary mode
    t = clock.ms + 1000
    hl.fills[W].append(fill('LINK', 'Close Short', '75000', '2', t, '-1500000', oid=777_777, closed_pnl='5'))
    before = len(bot.sent)
    await run_cycles(repo, hl, bot, clock, clock.ms + CYCLE_MS)
    assert len(bot.sent) == before + 1 and 'reduced SHORT $LINK' in bot.sent[-1]['text']
    assert '$150k' in bot.sent[-1]['text']

    # an hour in: the summary message was edited at least once, no per-coin messages
    await run_cycles(repo, hl, bot, clock, T0 + 65 * 60_000)
    edits = [e for e in bot.edits if 'running TWAP-style algos' in e['text']]
    assert len(edits) >= 1 and 'summary mode for 1h' in edits[-1]['text']
    assert len(bot.sent) == before + 1
    assert not any('algo done' in m['text'] for m in bot.sent)

    # the fills stop: all 17 algos end (summarized), then the mode exits after 30 idle minutes
    await run_cycles(repo, hl, bot, clock, T0 + 70 * 60_000 + 10 * 60_000 + CYCLE_MS)
    assert await repo.active_algos(va) == {}
    ends = await repo.events_since(va, 0, ['algo_end'])
    assert len(ends) == 17 and {e['delivery'] for e in ends} == {'summarized'}
    assert not any('algo done' in m['text'] for m in bot.sent)
    await run_cycles(repo, hl, bot, clock, clock.ms + 30 * 60_000 + CYCLE_MS)
    exits = [m['text'] for m in bot.sent if 'algos wound down' in m['text']]
    assert len(exits) == 1 and '24h total $' in exits[0]
    check_telegram_html(exits[0])
    assert await repo.multi_algo_mode(va) is None
    assert len(await repo.events_since(va, 0, ['multi_algo_enter'])) == 1
    assert len(await repo.events_since(va, 0, ['multi_algo_exit'])) == 1


async def test_summary_mode_survives_restart(tmp_path, clock):
    path = str(tmp_path / 'hm.db')
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = seventeen_coin_fills(30)
    repo = Repo(path)
    await repo.connect()
    await repo.add_subscription(7, W, 'loracle', T0)
    await run_cycles(repo, hl, bot, clock, T0 + 5 * 60_000)
    (va, _), = await repo.tracked_accounts()
    mode = await repo.multi_algo_mode(va)
    assert mode is not None
    await repo.close()

    repo = Repo(path)
    await repo.connect()
    await run_cycles(repo, hl, bot, clock, T0 + 25 * 60_000)
    assert (await repo.multi_algo_mode(va))['entered_ms'] == mode['entered_ms']
    assert sum('running TWAP-style algos' in m['text'] for m in bot.sent) == 1
    assert not any('algo accumulating' in m['text'] or 'algo done' in m['text'] for m in bot.sent)
    await repo.close()


async def test_health_folds_summary_mode_accounts(repo, clock, monkeypatch):
    monkeypatch.setattr(Config, 'ADMIN_USER_IDS', frozenset({1}))
    await repo.add_subscription(7, W, 'loracle', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = seventeen_coin_fills(10)
    await run_cycles(repo, hl, bot, clock, T0 + 5 * 60_000)
    clock.ms += 3 * 3600_000
    update = make_update(1)
    await commands.health_command(update, make_context({'repo': repo, 'hl': hl}))
    text = update.message.replies[0]['text']
    check_telegram_html(text)
    assert '- loracle: 17 algos (summary mode since 3h' in text
    assert text.count('last fill') == 0


async def test_startup_cleanup_removes_old_suppressed_rows_once(repo, caplog):
    await repo.add_subscription(7, W, 'w', 1)
    (va, _), = await repo.tracked_accounts()
    now_ms = T0
    for i in range(12):
        await repo.record_event(f's{i}', va, 'position_increase', now_ms - i, {'coin': 'BTC'},
                                'suppressed_algo' if i % 2 else 'suppressed_twap', 1)
    kept = await repo.record_event('k', va, 'position_open', now_ms, {'coin': 'BTC'}, 'sent', 1)
    await repo.add_sent_message(kept, 7, 7, 1)
    await repo.add_sent_message(await repo.get_event_by_key('s1') and (await repo.get_event_by_key('s1'))['event_id'],
                                7, 7, 2)
    await repo.db.execute("PRAGMA user_version = 1")
    await repo.db.commit()
    caplog.set_level(logging.INFO)
    await backup.startup_maintenance(repo, now_ms)
    cur = await repo.db.execute("SELECT delivery, COUNT(*) FROM events GROUP BY delivery")
    assert await cur.fetchall() == [('sent', 1)]
    cur = await repo.db.execute("SELECT COUNT(*) FROM sent_messages")
    assert (await cur.fetchone())[0] == 1
    assert 'Removed 12 suppressed fill rows' in caplog.text
    assert await repo.user_version() == Repo.SUPPRESSED_CLEANUP_VERSION
    assert await repo.delete_suppressed_rows() == 0
