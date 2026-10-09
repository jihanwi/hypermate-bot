from telegram.constants import ParseMode

from hypermate.bot import commands, texts
from tests.helpers import FakeHLClient, check_telegram_html, clearinghouse, make_context, make_update, position

A = '0x' + 'a' * 40


async def call(handler, repo, hl, user_id, *args):
    update = make_update(user_id)
    await handler(update, make_context({'repo': repo, 'hl': hl}, args=args))
    for r in update.message.replies:
        assert r['parse_mode'] == ParseMode.HTML
        check_telegram_html(r['text'])
    return [r['text'] for r in update.message.replies]


async def test_new_user_add_then_positions(repo):
    """Criterion: a new user can /add and immediately /positions <alias> (B1)."""
    hl = FakeHLClient()
    hl.clearinghouse[A] = clearinghouse(position('BTC', '1', entry_px='60000', position_value='61000'),
                                        account_value='2000')
    assert await call(commands.add_wallet, repo, hl, 555, A.upper().replace('0X', '0x'), 'test_wallet_1') == \
        ['✅ Wallet added as <b>test_wallet_1</b>']
    out = await call(commands.positions_command, repo, hl, 555, 'Test_Wallet_1')
    assert len(out) == 1
    assert 'Positions for test_wallet_1' in out[0] and '<b>LONG</b> $BTC' in out[0]
    stats = await call(commands.stats_command, repo, hl, 555, 'test_wallet_1')
    assert stats == [texts.STATS_NOT_AVAILABLE]   # empty portfolio from the fake


async def test_add_validation_and_duplicates(repo):
    hl = FakeHLClient()
    assert await call(commands.add_wallet, repo, hl, 1) == [texts.ADD_USAGE]
    assert await call(commands.add_wallet, repo, hl, 1, '0x123', 'x') == [texts.ADD_USAGE]
    await call(commands.add_wallet, repo, hl, 1, A, 'Whale')
    assert await call(commands.add_wallet, repo, hl, 1, '0x' + 'b' * 40, 'whale') == [texts.ALIAS_EXISTS]
    assert await call(commands.add_wallet, repo, hl, 1, A, 'other') == [texts.ADDRESS_EXISTS]


async def test_list_remove_and_summary(repo):
    hl = FakeHLClient()
    hl.clearinghouse[A] = clearinghouse(position('ETH', '-3', position_value='9000'), account_value='777')
    assert await call(commands.list_wallets, repo, hl, 9) == [texts.NO_WALLETS]
    await call(commands.add_wallet, repo, hl, 9, A, '<evil>_alias')
    listed = await call(commands.list_wallets, repo, hl, 9)
    assert '&lt;evil&gt;_alias' in listed[0] and '$777.00' in listed[0]
    summary = await call(commands.positions_command, repo, hl, 9)
    assert '1 positions · largest SHORT $ETH $9,000' in summary[0]
    assert await call(commands.remove_wallet, repo, hl, 9, '<EVIL>_ALIAS') == \
        [texts.WALLET_REMOVED.format(alias='&lt;EVIL&gt;_ALIAS')]
    assert 'not found' in (await call(commands.positions_command, repo, hl, 9, 'gone'))[0]


async def test_usage_and_help(repo):
    hl = FakeHLClient()
    assert await call(commands.remove_wallet, repo, hl, 1) == [texts.REMOVE_USAGE]
    assert await call(commands.stats_command, repo, hl, 1) == [texts.STATS_USAGE]
    help_text = (await call(commands.help_command, repo, hl, 1))[0]
    assert len(help_text.split()) <= 200
    assert (await call(commands.start, repo, hl, 1))[0] == texts.WELCOME


async def test_hl_api_error_message(repo):
    from hypermate.venues.hyperliquid.client import HyperliquidAPIError

    class DownHL(FakeHLClient):
        async def clearinghouse_state(self, user):
            raise HyperliquidAPIError('HTTP 502')

    hl = DownHL()
    await call(commands.add_wallet, repo, hl, 1, A, 'w')
    assert await call(commands.positions_command, repo, hl, 1, 'w') == [texts.HL_API_ERROR]
    assert '· n/a' in (await call(commands.list_wallets, repo, hl, 1))[0]


async def test_list_uses_stored_account_value_after_poll(repo, monkeypatch):
    from hypermate.core import pipeline
    from tests.helpers import FakeBot

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    hl = FakeHLClient()
    hl.clearinghouse[A] = clearinghouse(account_value='1234.5678')
    await call(commands.add_wallet, repo, hl, 3, A, 'w')
    # not polled yet: falls back to a live call
    assert '$1,234.57' in (await call(commands.list_wallets, repo, hl, 3))[0]
    assert ('clearinghouseState', A) in hl.calls

    await pipeline.monitor_positions_job(make_context({'repo': repo, 'hl': hl}, bot=FakeBot()))
    hl.clearinghouse[A] = clearinghouse(account_value='99')   # live value now differs
    hl.calls.clear()
    assert '$1,234.57' in (await call(commands.list_wallets, repo, hl, 3))[0]
    assert hl.calls == []


async def test_add_scans_hip3_dexs_and_positions_show_them(repo):
    hl = FakeHLClient()
    hl.dexs = ['xyz', 'cash', 'para']
    hl.clearinghouse[(A, 'xyz')] = clearinghouse(position('xyz:MU', '-100', entry_px='95', position_value='9500'),
                                                 account_value='1500')
    hl.clearinghouse[A] = clearinghouse(position('BTC', '1', position_value='61000'), account_value='2000')
    out = await call(commands.add_wallet, repo, hl, 5, A, 'w')
    assert out[0] == '✅ Wallet added as <b>w</b> · HL ✅ · dex: xyz'
    va = await repo.hl_account_id(A)
    assert await repo.get_dexs(va) == ['xyz'] and await repo.get_cursor(va, 'dex_scan') is not None
    view = (await call(commands.positions_command, repo, hl, 5, 'w'))[0]
    assert '<b>LONG</b> $BTC' in view
    assert 'Futures (xyz dex)</b> · account $1,500.00' in view and '<b>SHORT</b> $MU' in view
    assert 'Margin Balance (all dexs):</b> $3,500.00' in view


async def test_rescan_and_recent(repo):
    hl = FakeHLClient()
    await call(commands.add_wallet, repo, hl, 5, A, 'whale_1')
    hl.dexs = ['cash']
    hl.clearinghouse[(A, 'cash')] = clearinghouse(position('cash:CASHCAT', '-5'))
    assert 'HIP-3 cash' in (await call(commands.rescan_command, repo, hl, 5, 'WHALE_1'))[0]
    assert await call(commands.rescan_command, repo, hl, 5) == [texts.RESCAN_USAGE]

    va = await repo.hl_account_id(A)
    for i in range(12):
        await repo.record_event(f'k{i}', va, 'position_increase', 1_790_000_000_000 + i * 1000,
                                {'coin': 'BTC', 'side': 'LONG', 'notional_usd': '5000'},
                                'summarized' if i % 2 else 'sent', 1)
    out = (await call(commands.recent_command, repo, hl, 5, 'whale_1'))[0]
    assert out.count('\n') == 10                        # header + 10 events (default)
    out = (await call(commands.recent_command, repo, hl, 5, 'whale_1', '3'))[0]
    assert out.count('\n') == 3 and 'not sent (summary mode)' in out
    # active algos appear as one summary line each instead of their (unrecorded) fills
    await repo.upsert_algo(va, {'coin': 'DOGE', 'sign': 1, 'started_ms': 1, 'last_fill_ms': 2, 'fills_count': 18423,
                                'total_sz': '1', 'total_ntl': '1520000'})
    out = (await call(commands.recent_command, repo, hl, 5, 'whale_1', '3'))[0]
    assert out.endswith('🤖 algo $DOGE · 18,423 fills $1.52M (active)')
    assert await call(commands.recent_command, repo, hl, 5) == [texts.RECENT_USAGE]


async def test_twap_lists_native_twaps_and_algos(repo):
    hl = FakeHLClient()
    await call(commands.add_wallet, repo, hl, 5, A, 'loracle')
    assert await call(commands.twap_command, repo, hl, 5) == ['No active TWAPs.']
    va = await repo.hl_account_id(A)
    await repo.upsert_twap(va, '1', {'coin': 'BTC', 'side': 'B', 'sz': '1', 'executedSz': '0.45',
                                     'minutes': 120, 'timestamp': 1_790_000_000_000}, 1_790_000_000_000)
    await repo.upsert_algo(va, {'coin': 'CASHCAT', 'sign': 1, 'started_ms': 1_790_000_000_000,
                                'last_fill_ms': 1_790_000_100_000, 'fills_count': 40,
                                'total_sz': '50000', 'total_ntl': '8500'})
    await repo.record_event('hyperliquid:%d:algo_start:CASHCAT:1:1790000000000' % va, va, 'algo_start',
                            1_790_000_000_000, {'verb': 'closing', 'side': 'SHORT'}, 'sent', 1)
    out = (await call(commands.twap_command, repo, hl, 5, 'loracle'))[0]
    assert 'BUY $BTC · 45% (0.45/1)' in out
    assert 'algo closing SHORT $CASHCAT · 40 fills $8.5k' in out
