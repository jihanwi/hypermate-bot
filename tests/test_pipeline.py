"""Polling jobs against a real SQLite DB, a fake HL client and a fake bot."""

import logging

from telegram.constants import ParseMode

from hypermate.core import pipeline
from hypermate.db.repo import Repo
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, clearinghouse, make_context, position

A = '0x' + 'a' * 40


async def run_jobs(repo, hl, bot):
    context = make_context({'repo': repo, 'hl': hl}, bot=bot)
    await pipeline.monitor_positions_job(context)
    await pipeline.monitor_transfers_job(context)


async def test_alert_for_alias_with_underscores_is_sent_as_html(repo, monkeypatch):
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    await repo.add_subscription(42, A, 'test_wallet_1', 1000)
    hl, bot = FakeHLClient(), FakeBot()
    await run_jobs(repo, hl, bot)          # baseline
    hl.clearinghouse[A] = clearinghouse(position('BTC', '0.5', entry_px='60000'))
    await run_jobs(repo, hl, bot)
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    assert sent['chat_id'] == 42 and sent['parse_mode'] == ParseMode.HTML
    assert 'test_wallet_1' in check_telegram_html(sent['text'])


async def test_restart_resumes_from_stored_state(tmp_path, monkeypatch, caplog):
    """Criterion: after a restart, no initial scan; polling continues from stored cursors."""
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    caplog.set_level(logging.INFO)
    path = str(tmp_path / 'hypermate.db')
    hl = FakeHLClient()
    hl.clearinghouse[A] = clearinghouse(position('ETH', '1'))

    # first process
    repo = Repo(path)
    await repo.connect()
    await repo.add_subscription(7, A, 'w', 1000)
    bot = FakeBot()
    await run_jobs(repo, hl, bot)
    assert bot.sent == []
    assert 'Baseline snapshot' in caplog.text
    hl.ledger[A] = [{'time': 2000, 'hash': '0x1', 'delta': {'type': 'deposit', 'usdc': '500'}}]
    await run_jobs(repo, hl, bot)
    assert len(bot.sent) == 1 and 'deposited $500.00' in bot.sent[0]['text']
    await repo.close()

    # while the bot is down: position grows, a withdrawal happens; the old deposit is still in the API
    hl.clearinghouse[A] = clearinghouse(position('ETH', '2'))
    hl.ledger[A].append({'time': 3000, 'hash': '0x2', 'delta': {'type': 'withdraw', 'usdc': '100'}})
    hl.calls.clear()
    caplog.clear()

    # second process, same DB file
    repo = Repo(path)
    await repo.connect()
    bot = FakeBot()
    await run_jobs(repo, hl, bot)
    assert 'Baseline' not in caplog.text and 'Started' not in caplog.text
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 2
    assert 'added' in texts[0] and '$ETH' in texts[0]
    assert 'withdrew $100.00' in texts[1]          # the deposit before the restart is not resent
    assert ('userNonFundingLedgerUpdates', A, 2001) in hl.calls

    con_cursors = await (await repo.db.execute('SELECT kind, cursor FROM cursors')).fetchall()
    assert dict(con_cursors) == {'fills': '1000', 'ledger': '3000'}
    await repo.close()


async def test_spot_fills_alert_and_cursor_covers_perp_fills(repo, monkeypatch):
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    await repo.add_subscription(1, A, 'w', 1000)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[A] = [
        {'coin': 'BTC', 'px': '60000', 'sz': '0.1', 'side': 'B', 'time': 1500, 'dir': 'Open Long'},
        {'coin': '@107', 'px': '40', 'sz': '10', 'side': 'B', 'time': 1600, 'dir': 'Buy'},
        {'coin': 'PURR/USDC', 'px': '0.2', 'sz': '100', 'side': 'A', 'time': 1700, 'dir': 'Sell'},
    ]
    await run_jobs(repo, hl, bot)
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 2
    assert 'bought 10.00 HYPE for $400.00' in texts[0]
    assert 'sold 100.00 PURR for $20.00' in texts[1]
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_cursor(va, 'fills') == '1700'
    bot.sent.clear()
    await run_jobs(repo, hl, bot)
    assert bot.sent == []
    assert ('userFillsByTime', A, 1701) in hl.calls


async def test_shared_wallet_alert_uses_each_subscribers_alias(repo, monkeypatch):
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    await repo.add_subscription(1, A, 'mine', 1000)
    await repo.add_subscription(2, A, 'their_alias', 1000)
    hl, bot = FakeHLClient(), FakeBot()
    hl.ledger[A] = [{'time': 2000, 'hash': '0x1', 'delta': {'type': 'deposit', 'usdc': '1'}}]
    await run_jobs(repo, hl, bot)
    by_user = {m['chat_id']: m['text'] for m in bot.sent}
    assert '>mine<' in by_user[1] and '>their_alias<' in by_user[2]
    assert sum(1 for c in hl.calls if c[0] == 'userNonFundingLedgerUpdates') == 1


async def test_api_error_does_not_advance_cursor(repo, monkeypatch):
    monkeypatch.setattr(pipeline.asyncio, 'sleep', _no_sleep)
    await repo.add_subscription(1, A, 'w', 1000)

    class FailingHL(FakeHLClient):
        async def ledger_updates(self, user, start_time):
            raise RuntimeError('HTTP 500')

    await run_jobs(repo, FailingHL(), FakeBot())
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_cursor(va, 'ledger') == '1000'


async def _no_sleep(_seconds):
    return None


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
