"""feat/daily-digest (spec 12): one message per user per day: header with the 24 h change, per-wallet
sections (active wallets only), quiet wallets, exposure over all wallets; hourly scheduling with
last_digest_day; /digest now and /digest on|off|<hour>; the Digest rows on /settings default."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from hypermate.bot import callbacks, commands, texts
from hypermate.core import digest, pipeline
from hypermate.core import settings as user_settings
from hypermate.venues.hyperliquid import adapter as hl_adapter
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, make_context, make_update

W1, W2, W3 = ('0x' + c * 40 for c in 'abc')
# 2026-10-11 09:30 KST = 00:30 UTC
NOW = int(datetime(2026, 10, 11, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
HOUR = 3600_000


@pytest.fixture
def clock(monkeypatch):
    state = {'ms': NOW}
    monkeypatch.setattr(hl_adapter, 'now_ms', lambda: state['ms'])
    monkeypatch.setattr(commands, 'now_ms', lambda: state['ms'])

    async def no_sleep(_):
        return None

    monkeypatch.setattr(digest.asyncio, 'sleep', no_sleep)
    return state


def pos(coin, szi, value, entry='100'):
    return {'szi': str(szi), 'direction': 'LONG' if Decimal(szi) > 0 else 'SHORT', 'entry_px': entry,
            'position_value': str(value), 'unrealized_pnl': '0', 'coin': coin}


async def fixture_wallets(repo, user_id=7):
    """Three wallets: two with activity in the last 24 h, one quiet. Returns {alias: venue_account_id}."""
    keys = {}
    for alias, address in (('whale', W1), ('iroh', W2), ('anteater', W3)):
        await repo.add_subscription(user_id, address, alias, NOW - 3 * 24 * HOUR)
        (va,) = [r['key'] for r in await repo.venue_accounts_of(address)]
        keys[alias] = va
    # whale: ETH short grown, BTC long closed with profit, two algos running; value 50M (was 49.4M)
    await repo.save_snapshot(keys['whale'], {'': {'ETH': pos('ETH', '-800', '2600000', '3250')}}, NOW, '50000000')
    await repo.record_event('w1', keys['whale'], 'position_increase', NOW - 5 * HOUR,
                            {'type': 'position_increase', 'coin': 'ETH', 'side': 'SHORT', 'notional_usd': '1200000',
                             'size': '370', 'position_after': '-800', 'realized_pnl': None,
                             'chain': {'notional_usd': '1200000', 'realized_pnl': None}}, 'sent', NOW)
    await repo.record_event('w2', keys['whale'], 'position_close', NOW - 2 * HOUR,
                            {'type': 'position_close', 'coin': 'BTC', 'side': 'LONG', 'notional_usd': '4000000',
                             'size': '46', 'position_after': '0', 'realized_pnl': '18000'}, 'sent', NOW)
    await repo.record_event('w3', keys['whale'], 'position_decrease', NOW - 30 * HOUR,
                            {'type': 'position_decrease', 'coin': 'SOL', 'side': 'LONG', 'notional_usd': '999999',
                             'realized_pnl': '5'}, 'sent', NOW)                                   # outside 24 h
    for coin, ntl in (('LINK', '7000000'), ('DOGE', '6100000')):
        await repo.upsert_algo(keys['whale'], {'coin': coin, 'sign': 1, 'started_ms': NOW - HOUR,
                                               'last_fill_ms': NOW - 60_000, 'fills_count': 500,
                                               'total_sz': '1', 'total_ntl': ntl})
    # iroh: ETH long reduced; value 2.1M, no stored value yesterday
    await repo.save_snapshot(keys['iroh'], {'': {'ETH': pos('ETH', '500', '1600000', '3200'),
                                                 'IREN': pos('xyz:IREN', '-1000', '36000', '36')}}, NOW, '2100000')
    await repo.record_event('i1', keys['iroh'], 'position_decrease', NOW - 3 * HOUR,
                            {'type': 'position_decrease', 'coin': 'ETH', 'side': 'LONG', 'notional_usd': '250000',
                             'realized_pnl': '-4000', 'position_after': '500'}, 'sent', NOW)
    # anteater: quiet, small long
    await repo.save_snapshot(keys['anteater'], {'': {'ETH': pos('ETH', '10', '32000', '3200')}}, NOW, '40000')
    await repo.record_daily_values(digest.utc_day(NOW, 1))                        # yesterday's values = today's
    await repo.db.execute("UPDATE account_value_daily SET value = '49400000' WHERE venue_account_id = ?", (keys['whale'],))
    await repo.db.execute("DELETE FROM account_value_daily WHERE venue_account_id = ?", (keys['iroh'],))
    await repo.db.commit()
    return keys


async def test_digest_renders_header_sections_quiet_line_and_exposure(repo, clock):
    await fixture_wallets(repo)
    (text,) = await digest.build_digest(repo, 7, NOW)
    check_telegram_html(text)
    lines = text.split('\n')
    assert lines[0] == '📰 <b>Daily · Oct 11</b> · 3 wallets · total $52.1M (+5.5% 24h)'   # vs 49.4M + 40k stored
    whale = text.index('<b>whale</b>')
    iroh = text.index('<b>iroh</b>')
    assert whale < iroh                                                                   # value descending
    assert '<b>whale</b> · $50M (+1.2% 24h)' in text
    assert '  ETH short +$1.2M → $2.6M' in text and '  BTC long closed, realized +$18k' in text
    assert '  realized +$18k total' in text and '  algos: 2 coins $13.1M' in text and '  largest: BTC LONG $4M' in text
    assert 'SOL' not in text                                                               # outside the window
    assert '<b>iroh</b> · $2.1M\n' in text and '  ETH long -$250k → $1.6M' in text and 'realized -$4k total' in text
    assert 'quiet: anteater' in text
    assert '<b>Exposure</b>' in text
    exposure = text.split('<b>Exposure</b>\n')[1].split('\n')
    assert exposure[0] == 'ETH net short $968k (3 wallets)' and exposure[1] == 'IREN net short $36k (1 wallet)'


def test_exposure_sums_signed_values_and_keeps_the_top_five():
    wallets = [{'ETH': pos('ETH', '-800', '2600000'), 'BTC': pos('BTC', '1', '90000')},
               {'ETH': pos('ETH', '500', '1600000'), 'SOL': pos('SOL', '10', '1000'), 'LINK': pos('LINK', '1', '10'),
                'DOGE': pos('DOGE', '1', '20'), 'UNI': pos('UNI', '-1', '30'), 'ARB': pos('ARB', '1', '5')},
               {'ETH': pos('ETH', '10', '32000'), 'BTC': pos('BTC', '-2', '90000')}]
    lines = digest.exposure_lines(wallets)
    assert lines[0] == 'ETH net short $968k (3 wallets)'
    assert 'BTC' not in '\n'.join(lines)                                                  # 90k long vs 90k short: flat
    assert len(lines) == 5 and lines[1] == 'SOL net long $1k (1 wallet)' and 'ARB' not in '\n'.join(lines)
    # single wallet: no exposure section
    assert digest.exposure_lines([{'ETH': pos('ETH', '1', '10')}]) == ['ETH net long $10 (1 wallet)']


async def test_single_wallet_has_no_exposure_and_many_lines_fold(repo, clock):
    await repo.add_subscription(9, W1, 'solo', NOW - HOUR)
    (va,) = [r['key'] for r in await repo.venue_accounts_of(W1)]
    coins = ['ETH', 'BTC', 'SOL', 'LINK', 'DOGE', 'UNI', 'ARB', 'OP']
    await repo.save_snapshot(va, {'': {c: pos(c, '1', '1000') for c in coins}}, NOW, '8000')
    for i, coin in enumerate(coins):
        await repo.record_event(f's{i}', va, 'position_increase', NOW - HOUR, {'type': 'position_increase', 'coin': coin,
                                'side': 'LONG', 'notional_usd': str(1000 - i)}, 'sent', NOW)
    (text,) = await digest.build_digest(repo, 9, NOW)
    assert 'Exposure' not in text and '1 wallet ·' in text
    assert text.count('\n  ') == 7 and '  +3 more' in text                                 # 6 lines + the fold


async def test_hourly_schedule_by_user_hour_and_no_double_send_after_restart(repo, clock):
    """Users at 06, 09 (default) and 18 KST; at 09:30 KST the 06 and 09 users get theirs, the 18 one waits;
    a second run the same day (a restart) sends nothing; the next day everyone is due again."""
    await fixture_wallets(repo, user_id=7)                                                  # default: 09
    await repo.add_subscription(8, W1, 'early', NOW - HOUR)
    await repo.add_subscription(9, W1, 'late', NOW - HOUR)
    await repo.add_subscription(10, W1, 'off', NOW - HOUR)
    await repo.set_user_settings(8, {'digest': {'hour_kst': 6}}, NOW)
    await repo.set_user_settings(9, {'digest': {'hour_kst': 18}}, NOW)
    await repo.set_user_settings(10, {'digest': {'enabled': False}}, NOW)
    users = await repo.users_with_subscriptions()
    assert digest.due_users(users, NOW) == [7, 8]
    bot = FakeBot()
    await digest.digest_job(make_context({'repo': repo}, bot=bot))
    assert sorted(m['chat_id'] for m in bot.sent) == [7, 8]
    assert {u['user_id']: u['last_digest_day'] for u in await repo.users_with_subscriptions()} == {
        7: '2026-10-11', 8: '2026-10-11', 9: None, 10: None}
    await digest.digest_job(make_context({'repo': repo}, bot=bot))                          # restart, same hour
    assert len(bot.sent) == 2
    clock['ms'] = NOW + 9 * HOUR                                                            # 18:30 KST
    await digest.digest_job(make_context({'repo': repo}, bot=bot))
    assert [m['chat_id'] for m in bot.sent[2:]] == [9]
    clock['ms'] = NOW + 24 * HOUR                                                           # next day 09:30
    assert sorted(digest.due_users(await repo.users_with_subscriptions(), clock['ms'])) == [7, 8]
    # the 00:00 UTC job stores every active account's value for the day
    assert await repo.record_daily_values('2026-10-12') == 3
    assert await repo.daily_value((await repo.venue_accounts_of(W1))[0]['key'], '2026-10-12') == '50000000'


async def test_digest_command_now_and_settings(repo, clock):
    await fixture_wallets(repo)
    update = make_update(7)
    await commands.digest_command(update, make_context({'repo': repo, 'hl': FakeHLClient()}, args=[]))
    (reply,) = update.message.replies
    assert reply['text'].startswith('📰 <b>Daily · Oct 11</b>')
    assert (await repo.users_with_subscriptions())[0]['last_digest_day'] is None            # /digest now does not count
    for args, expected in ((['off'], '📰 Daily digest off.'), (['18'], '📰 Daily digest on, every day at 18:00 KST.'),
                           (['on'], '📰 Daily digest on, every day at 18:00 KST.'), (['25'], texts.DIGEST_USAGE)):
        update = make_update(7)
        await commands.digest_command(update, make_context({'repo': repo, 'hl': FakeHLClient()}, args=args))
        assert update.message.replies[0]['text'] == expected
    assert digest.digest_settings(await repo.user_settings(7)) == {'enabled': True, 'hour_kst': 18}
    update = make_update(11)
    await commands.digest_command(update, make_context({'repo': repo, 'hl': FakeHLClient()}, args=[]))
    assert update.message.replies[0]['text'] == texts.DIGEST_EMPTY
    # /settings default: Digest toggle and the hour row; a wallet keyboard has neither
    update = make_update(7)
    await commands.settings_command(update, make_context({'repo': repo, 'hl': FakeHLClient()}, args=['default']))
    kb = update.message.replies[0]['reply_markup'].inline_keyboard
    labels = [[b.text for b in row] for row in kb]
    assert ['✅ Daily digest'] in labels and ['06 KST', '09 KST', '12 KST', '✅ 18 KST', '21 KST'] in labels
    off = next(b for row in kb for b in row if b.text == '✅ Daily digest').callback_data
    assert off == 's:u:g:off'
    assert callbacks.apply_change(user_settings.DEFAULTS, 'g', ['12'])['digest'] == {'enabled': True, 'hour_kst': 12}
    assert callbacks.apply_change(user_settings.DEFAULTS, 'g', ['7']) is None               # not a menu hour
    update = make_update(7)
    await commands.settings_command(update, make_context({'repo': repo, 'hl': FakeHLClient()}, args=['whale']))
    assert not any('digest' in b.text.lower() for row in update.message.replies[0]['reply_markup'].inline_keyboard for b in row)


async def test_blocked_user_gets_muted(repo, clock):
    await fixture_wallets(repo)

    class BlockingBot(FakeBot):
        async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
            raise RuntimeError('Forbidden: bot was blocked by the user')

    assert await digest.send_digest(BlockingBot(), repo, 7, NOW) is False
    rows = [await repo.subscription_of(7, a) for a in (W1, W2, W3)]
    assert all(pipeline.is_muted(r['muted_until_ms'], NOW) for r in rows)
    assert (await repo.users_with_subscriptions())[0]['last_digest_day'] is None
