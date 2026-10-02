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
