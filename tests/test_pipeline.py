"""Polling jobs against a real SQLite DB, a fake HL client, a fake bot and a fake clock."""

import logging

import pytest
from telegram.constants import ParseMode

from hypermate.core import pipeline
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, clearinghouse, fill, make_context, position

A = '0x' + 'a' * 40
T0 = 1_790_000_000_000


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


async def fills_cycle(repo, hl, bot):
    await pipeline.monitor_transfers_job(make_context({'repo': repo, 'hl': hl}, bot=bot))


async def positions_cycle(repo, hl, bot):
    await pipeline.monitor_positions_job(make_context({'repo': repo, 'hl': hl}, bot=bot))


async def test_fill_alert_for_alias_with_underscores_is_sent_as_html(repo, clock):
    await repo.add_subscription(42, A, 'test_wallet_1', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [fill('BTC', 'Open Long', '0.5', '60000', T0 + 1000, '0')]
    clock.ms = T0 + 5000
    await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent['chat_id'] == 42 and sent['parse_mode'] == ParseMode.HTML
    assert 'test_wallet_1' in check_telegram_html(sent['text'])
    assert 'opened LONG $BTC' in sent['text']


async def test_sweep_of_one_order_is_one_message(repo, clock):
    """Spec 5.2 체결 집계: one market order hitting many levels -> 1 message (synthetic stand-in for
    hl_userFillsByTime_sweep.json until that fixture is provided)."""
    await repo.add_subscription(1, A, 'cl', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [fill('@107', 'Buy', '1.5', str(41 + i / 100), T0 + 1000, '0', oid=11, tid=500 + i)
                   for i in range(60)]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 1
    assert '60 fills' in bot.sent[0]['text'] and 'bought 90' in bot.sent[0]['text']


async def test_split_buys_one_minute_apart_edit_one_message(repo, clock):
    """Spec 5.3: 10 manual buys 1 minute apart -> one message edited with the running totals."""
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    for i in range(10):
        hl.fills[A] = hl.fills.get(A, []) + [fill('ETH', 'Open Long', '1', '3000', T0 + 1000 + 60_000 * i, str(i),
                                                   oid=100 + i)]
        clock.ms = T0 + 60_000 * i + 20_000
        await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 1
    assert len(bot.edits) == 9
    text = bot.sent[0]['text']
    assert 'opened LONG $ETH\n$30k (10 ETH) @ 3,000 · 10 fills' in text


async def test_debounce_window_expires(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [fill('ETH', 'Open Long', '1', '3000', T0 + 1000, '0', oid=1)]
    clock.ms = T0 + 10_000
    await fills_cycle(repo, hl, bot)
    hl.fills[A].append(fill('ETH', 'Open Long', '1', '3000', T0 + 62_000, '1', oid=2))
    clock.ms = T0 + 70_000
    await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 2 and bot.edits == []


async def test_restart_resumes_from_stored_state(tmp_path, clock, caplog):
    """After a restart, no baseline/initial scan; polling continues from the stored cursors."""
    caplog.set_level(logging.INFO)
    path = str(tmp_path / 'hypermate.db')
    hl = FakeHLClient()
    hl.clearinghouse[A] = clearinghouse(position('ETH', '1'))

    repo = Repo(path)
    await repo.connect()
    await repo.add_subscription(7, A, 'w', T0)
    bot = FakeBot()
    await positions_cycle(repo, hl, bot)
    await fills_cycle(repo, hl, bot)
    assert bot.sent == [] and 'Baseline snapshot' in caplog.text
    hl.ledger[A] = [{'time': T0 + 2000, 'hash': '0x1', 'delta': {'type': 'deposit', 'usdc': '500'}}]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 1 and 'deposited $500.00' in bot.sent[0]['text']
    await repo.close()

    # while the bot is down: a fill and a withdrawal; the old deposit is still in the API
    hl.fills[A] = [fill('ETH', 'Open Long', '1', '3000', T0 + 40_000, '1', oid=9)]
    hl.ledger[A].append({'time': T0 + 41_000, 'hash': '0x2', 'delta': {'type': 'withdraw', 'usdc': '100'}})
    hl.calls.clear()
    caplog.clear()
    clock.ms = T0 + 300_000

    repo = Repo(path)
    await repo.connect()
    bot = FakeBot()
    await positions_cycle(repo, hl, bot)
    await fills_cycle(repo, hl, bot)
    assert 'Baseline' not in caplog.text and 'Started' not in caplog.text
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 2
    assert 'added to LONG $ETH' in texts[0]
    assert 'withdrew $100.00' in texts[1]       # the deposit before the restart is not resent
    assert ('userNonFundingLedgerUpdates', A, T0 + 2001) in hl.calls
    await repo.close()


async def test_spot_fills_with_display_names(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [
        fill('BTC', 'Open Long', '0.1', '60000', T0 + 500, '0', oid=1),
        fill('@107', 'Buy', '10', '40', T0 + 600, '0', oid=2),
        fill('PURR/USDC', 'Sell', '100', '0.2', T0 + 700, '100', oid=3, side='A'),
    ]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 3
    assert 'bought 10 HYPE $HYPE\n$400 @ 40.00' in texts[1]
    assert 'sold 100 PURR $PURR\n$20 @ 0.2' in texts[2]
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_cursor(va, 'fills') == str(T0 + 700)
    bot.sent.clear()
    await fills_cycle(repo, hl, bot)
    assert bot.sent == []
    assert ('userFillsByTime', A, T0 + 701) in hl.calls


async def test_duplicate_fill_from_rewound_cursor_is_not_resent(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [fill('BTC', 'Open Long', '1', '60000', T0 + 500, '0', oid=1, tid=77)]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    (va, _), = await repo.tracked_accounts()
    await repo.set_cursor(va, 'fills', str(T0), 1)
    await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 1


async def test_hip3_fill_adds_dex_and_positions_poll_it(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [fill('xyz:MU', 'Open Short', '100', '95', T0 + 500, '0', oid=1)]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    assert 'opened SHORT $MU (xyz)' in bot.sent[0]['text']
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_dexs(va) == ['xyz']
    hl.clearinghouse[(A, 'xyz')] = clearinghouse(position('xyz:MU', '-100'), account_value='900')
    hl.clearinghouse[A] = clearinghouse(account_value='100')
    await positions_cycle(repo, hl, bot)
    assert ('clearinghouseState', A, 'xyz') in hl.calls
    snapshot = await repo.get_snapshot(va)
    assert snapshot['xyz']['xyz:MU']['szi'] == '-100' and snapshot[''] == {}
    assert await repo.hl_account_value(A) == '1000'


async def test_existing_account_gets_one_dex_scan(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.dexs = ['xyz', 'cash']
    hl.clearinghouse[(A, 'cash')] = clearinghouse(position('cash:CASHCAT', '-1000'))
    await positions_cycle(repo, hl, bot)
    await positions_cycle(repo, hl, bot)
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_dexs(va) == ['cash']
    assert sum(1 for c in hl.calls if c == ('perpDexs',)) == 1


async def test_liquidation_from_fills_and_ledger_alerts_once(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [fill('DOGE', 'Close Long', '1000', '0.2', T0 + 500, '1000', oid=1, closed_pnl='-80',
                        liquidation={'liquidatedUser': A, 'markPx': '0.2', 'method': 'market'})]
    hl.ledger[A] = [{'time': T0 + 600, 'hash': '0xliq', 'delta': {
        'type': 'liquidation', 'accountValue': '12', 'leverageType': 'Cross',
        'liquidatedPositions': [{'coin': 'DOGE', 'szi': '1000'}]}}]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 1 and 'LIQUIDATED LONG $DOGE' in bot.sent[0]['text']
    (va, _), = await repo.tracked_accounts()
    assert [e['type'] for e in await repo.recent_events(va)] == ['liquidation']


async def test_off_by_default_ledger_types_are_recorded_not_sent(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.ledger[A] = [
        {'time': T0 + 1, 'hash': '0xa', 'delta': {'type': 'accountClassTransfer', 'usdc': '5', 'toPerp': True}},
        {'time': T0 + 2, 'hash': '0xb', 'delta': {
            'type': 'send', 'user': A, 'destination': '0x2000000000000000000000000000000000000000',
            'sourceDex': '', 'destinationDex': 'xyz', 'token': 'USDC', 'amount': '500', 'usdcValue': '500',
            'fee': '0', 'nativeTokenFee': '0', 'nonce': 1, 'feeToken': ''}},
        {'time': T0 + 3, 'hash': '0xc', 'delta': {'type': 'deposit', 'usdc': '7'}},
    ]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    assert len(bot.sent) == 1 and 'deposited $7.00' in bot.sent[0]['text']
    (va, _), = await repo.tracked_accounts()
    recorded = {e['type']: e['delivery'] for e in await repo.recent_events(va)}
    assert recorded == {'account_class_transfer': 'filtered_settings',
                        'dex_collateral_transfer': 'filtered_settings', 'deposit': 'sent'}


async def test_shared_wallet_alert_uses_each_subscribers_alias(repo, clock):
    await repo.add_subscription(1, A, 'mine', T0)
    await repo.add_subscription(2, A, 'their_alias', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.ledger[A] = [{'time': T0 + 2000, 'hash': '0x1', 'delta': {'type': 'deposit', 'usdc': '1'}}]
    clock.ms = T0 + 30_000
    await fills_cycle(repo, hl, bot)
    by_user = {m['chat_id']: m['text'] for m in bot.sent}
    assert '>mine<' in by_user[1] and '>their_alias<' in by_user[2]
    assert sum(1 for c in hl.calls if c[0] == 'userNonFundingLedgerUpdates') == 1


async def test_api_error_does_not_advance_cursor(repo, clock):
    await repo.add_subscription(1, A, 'w', T0)

    class FailingHL(FakeHLClient):
        async def ledger_updates(self, user, start_time):
            raise RuntimeError('HTTP 500')

    await fills_cycle(repo, FailingHL(), FakeBot())
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_cursor(va, 'ledger') == str(T0)


async def test_deliver_sleeps_only_between_subscribers(repo, monkeypatch):
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(pipeline.asyncio, 'sleep', record_sleep)
    for user_id in (1, 2, 3):
        await repo.add_subscription(user_id, A, f'w{user_id}', 1000)
    (va, _), = await repo.tracked_accounts()
    bot = FakeBot()
    await pipeline.deliver(bot, repo, va, lambda alias: f'hi {alias}')
    assert len(bot.sent) == 3 and sleeps == [1, 1]

    await repo.remove_subscription(2, 'w2')
    await repo.remove_subscription(3, 'w3')
    sleeps.clear()
    await pipeline.deliver(bot, repo, va, lambda alias: f'hi {alias}')
    assert sleeps == []
