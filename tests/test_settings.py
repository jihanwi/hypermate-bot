"""feat/ux-settings (spec 9): per-subscription settings, /settings keyboard, /mute, /rename, Did you mean."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from hypermate.bot import callbacks, commands, texts
from hypermate.config import Config
from hypermate.core import pipeline
from hypermate.core import settings as user_settings
from hypermate.venues import base
from hypermate.venues.base import VenueAccount
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, clearinghouse, fill, make_context, make_update
from hypermate.venues.hyperliquid import adapter as hl_adapter
from tests.test_deploy_1005 import CYCLE_MS, T0, Clock, W, run_cycles


@pytest.fixture
def clock(monkeypatch):
    c = Clock(T0)
    for module in (hl_adapter, commands, callbacks):
        monkeypatch.setattr(module, 'now_ms', c)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    return c


def ctx(repo, hl=None, bot=None, args=None):
    return make_context({'repo': repo, 'hl': hl or FakeHLClient()}, args=args, bot=bot)


def query(data, user_id, markup=None):
    """A callback_query stand-in recording answer() texts and reply-markup / text edits."""
    log = {'answers': [], 'markups': [], 'texts': []}

    async def answer(text=None, *a, **k):
        log['answers'].append(text)

    async def edit_markup(reply_markup=None):
        log['markups'].append(reply_markup)

    async def edit_text(text=None, **k):
        log['texts'].append(text)

    message = SimpleNamespace(reply_markup=markup, reply_text=make_update(user_id).message.reply_text)
    q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id), answer=answer,
                        edit_message_reply_markup=edit_markup, edit_message_text=edit_text, message=message)
    return SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=user_id)), log


# 1. model --------------------------------------------------------------------------------

def test_settings_resolve_in_three_levels():
    """subscription > user defaults > code defaults, deep merged; untouched keys keep the lower level."""
    assert user_settings.resolve(None, None) == user_settings.DEFAULTS
    user_level = {'events': {'spot': False}, 'min_notional': {'mode': 'fixed', 'usd': 10_000}}
    sub_level = {'events': {'spot': True}, 'venues': {'risex': False}}
    merged = user_settings.resolve(sub_level, user_level)
    assert merged['events']['spot'] is True and merged['events']['position'] is True       # sub wins, defaults kept
    assert merged['venues'] == {'hyperliquid': True, 'lighter': True, 'risex': False, 'aster': True}
    assert merged['min_notional'] == {'mode': 'fixed', 'usd': 10_000}                     # from the user level
    assert user_settings.resolve(None, user_level)['events']['spot'] is False
    assert user_settings.DEFAULTS['venues']['risex'] is True                                # inputs untouched


def test_threshold_modes(monkeypatch):
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_FLOOR_USD', 100)
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_PCT', Decimal('0.005'))
    auto, fixed, off = {'min_notional': {'mode': 'auto'}}, {'min_notional': {'mode': 'fixed', 'usd': 10_000}}, \
        {'min_notional': {'mode': 'off'}}
    assert user_settings.threshold_usd(auto, Decimal(483_000)) == Decimal(2415)
    assert user_settings.threshold_usd(auto, None) == Decimal(100)
    assert user_settings.threshold_usd(fixed, Decimal(483_000)) == Decimal(10_000)
    assert user_settings.threshold_usd(off, Decimal(483_000)) is None
    # closes and liquidations are never filtered by the threshold; a venue off blocks everything
    assert user_settings.allows(fixed, 'position_close', 'hyperliquid', Decimal(5), None) == (True, '')
    assert user_settings.allows(fixed, 'liquidation', 'hyperliquid', Decimal(5), None) == (True, '')
    assert user_settings.allows(fixed, 'position_open', 'hyperliquid', Decimal(5000), None) == (False, 'threshold')
    assert user_settings.allows({'venues': {'risex': False}}, 'position_close', 'risex', None, None) == (False, 'venue')
    assert user_settings.allows(user_settings.DEFAULTS, 'vault_deposit', 'hyperliquid', None, None) == (False, 'event')
    assert user_settings.allows({}, 'privacy_on', 'aster', None, None) == (True, '')


async def test_same_wallet_two_subscribers_filtered_per_user(repo, clock, monkeypatch):
    """A (off) and B (fixed $10k) track one wallet: a $5k order reaches A only, the event is 'sent' with
    one sent_messages row, B's skip is logged only."""
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_FLOOR_USD', 100)
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_PCT', Decimal('0.005'))
    await repo.add_subscription(1, W, 'a', T0)
    await repo.add_subscription(2, W, 'b', T0)
    rows = {uid: await repo.subscription_of(uid, W) for uid in (1, 2)}
    await repo.set_subscription_settings(rows[1]['rowid'], {'min_notional': {'mode': 'off'}})
    await repo.set_subscription_settings(rows[2]['rowid'], {'min_notional': {'mode': 'fixed', 'usd': 10_000}})
    hl, bot = FakeHLClient(), FakeBot()
    hl.clearinghouse[W] = clearinghouse(account_value='50000')
    hl.fills[W] = [fill('ETH', 'Open Long', '1.25', '4000', T0 + 1000, '0', oid=1)]          # $5,000
    await run_cycles(repo, hl, bot, clock, T0 + CYCLE_MS)
    assert [m['chat_id'] for m in bot.sent] == [1]
    (va, _), = await repo.tracked_accounts()
    (event,) = [e for e in await repo.events_since(va, 0) if e['type'] == 'position_open']
    assert event['delivery'] == 'sent' and set(await repo.sent_messages(event['event_id'])) == {1}


async def test_venue_off_skips_that_venue_only(repo, clock):
    await repo.add_subscription(1, W, 'a', T0)
    row = await repo.subscription_of(1, W)
    await repo.set_subscription_settings(row['rowid'], {'venues': {'risex': False}})
    key, _ = await repo.ensure_venue_account(W, base.RISEX, W, True, T0)
    pipeline.register_account(key, VenueAccount(base.RISEX, W, W, key))
    bot = FakeBot()
    await pipeline.deliver(bot, repo, key, lambda alias: f'risex {alias}')
    (hl_key, _), = await repo.tracked_accounts()
    await pipeline.deliver(bot, repo, hl_key, lambda alias: f'hl {alias}')
    assert [m['text'] for m in bot.sent] == ['hl a']


# 2. /settings keyboard ------------------------------------------------------------------------

async def test_settings_keyboard_toggles_and_ignores_other_users(repo):
    await repo.add_subscription(7, W, 'jez', T0)
    await repo.ensure_venue_account(W, base.RISEX, W, True, T0)
    update = make_update(7)
    await commands.settings_command(update, ctx(repo, args=['JEZ']))
    (reply,) = update.message.replies
    check_telegram_html(reply['text'])
    kb = reply['reply_markup'].inline_keyboard
    assert [b.text for b in kb[0]] == ['✅ HL', '✅ RISEx']                 # active venues only
    assert all(len(b.callback_data.encode()) <= 64 for row in kb for b in row)
    assert any(b.text == '✅ Auto' for row in kb for b in row)
    risex_off = kb[0][1].callback_data
    row = await repo.subscription_of(7, W)
    assert risex_off == f"s:{row['rowid']}:v:risex:0"
    # another user pressing: ignored
    other, log = query(risex_off, 8)
    await callbacks.settings_callback(other, ctx(repo))
    assert log['answers'] == [texts.NOT_YOURS] and log['markups'] == []
    assert (await repo.subscription_of(7, W))['settings'] == {}
    # the owner: stored and the markup edited in place
    mine, log = query(risex_off, 7)
    await callbacks.settings_callback(mine, ctx(repo))
    assert (await repo.subscription_of(7, W))['settings'] == {'venues': {'risex': False}}
    assert [b.text for b in log['markups'][0].inline_keyboard[0]] == ['✅ HL', '⬜ RISEx']
    # min_notional choice and an event toggle
    mine, log = query(f"s:{row['rowid']}:n:10000", 7)
    await callbacks.settings_callback(mine, ctx(repo))
    assert (await repo.subscription_of(7, W))['settings']['min_notional'] == {'mode': 'fixed', 'usd': 10000}
    mine, log = query(f"s:{row['rowid']}:e:spot:0", 7)
    await callbacks.settings_callback(mine, ctx(repo))
    assert (await repo.subscription_of(7, W))['settings']['events'] == {'spot': False}
    texts_ = [b.text for r in log['markups'][0].inline_keyboard for b in r]
    assert '⬜ Spot' in texts_ and '✅ $10k' in texts_
    # /settings default edits the user level, shown with all four venues
    update = make_update(7)
    await commands.settings_command(update, ctx(repo, args=['default']))
    kb = update.message.replies[0]['reply_markup'].inline_keyboard
    assert len(kb[0]) == 4 and kb[0][0].callback_data == 's:u:v:hyperliquid:0'
    mine, log = query('s:u:e:vault:1', 7)
    await callbacks.settings_callback(mine, ctx(repo))
    assert await repo.user_settings(7) == {'events': {'vault': True}}
    assert await call_usage(commands.settings_command, repo) == texts.SETTINGS_USAGE
    # stale rowid
    stale, log = query('s:999:v:risex:0', 7)
    await callbacks.settings_callback(stale, ctx(repo))
    assert log['answers'] == [texts.SETTINGS_STALE]


async def call_usage(handler, repo):
    update = make_update(7)
    await handler(update, ctx(repo, args=[]))
    return update.message.replies[0]['text']


# 3. /mute --------------------------------------------------------------------------------------

async def test_mute_for_an_hour_then_alerts_resume(repo, clock):
    await repo.add_subscription(1, W, 'a', T0)
    update = make_update(1)
    await commands.mute_command(update, ctx(repo, args=['a', '1h']))
    assert update.message.replies[0]['text'] == '🔇 <b>a</b> muted for 1h.'
    listed = make_update(1)
    await commands.list_wallets(listed, ctx(repo))
    assert '🔇 1h' in listed.message.replies[0]['text']
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [fill('ETH', 'Open Long', '1', '4000', T0 + 1000, '0', oid=1)]
    await run_cycles(repo, hl, bot, clock, T0 + CYCLE_MS)
    assert bot.sent == []                                                     # muted: recorded, not sent
    (va, _), = await repo.tracked_accounts()
    assert [e['delivery'] for e in await repo.events_since(va, 0) if e['type'] == 'position_open'] == ['sent']
    clock.ms = T0 + 3600_000 + 1000                                           # the hour passed
    hl.fills[W].append(fill('BTC', 'Open Long', '1', '60000', clock.ms, '0', oid=2))
    await run_cycles(repo, hl, bot, clock, clock.ms + CYCLE_MS)
    assert len(bot.sent) == 1 and '$BTC' in bot.sent[0]['text']
    # explicit unmute while muted: the count of events recorded meanwhile, nothing re-sent
    await commands.mute_command(make_update(1), ctx(repo, args=['a']))
    hl.fills[W].append(fill('SOL', 'Open Long', '10', '100', clock.ms + 1000, '0', oid=3))
    await run_cycles(repo, hl, bot, clock, clock.ms + CYCLE_MS)
    update = make_update(1)
    await commands.unmute_command(update, ctx(repo, args=['a']))
    assert update.message.replies[0]['text'] == '🔔 <b>a</b> unmuted. 1 events while muted, see /recent a.'
    assert len(bot.sent) == 1
    assert await call_usage(commands.mute_command, repo) == texts.MUTE_USAGE      # user 7 has no wallets
    assert await call_usage(commands.unmute_command, repo) == texts.UNMUTE_USAGE


async def test_mute_without_alias_asks_then_mutes_everything(repo, clock):
    await repo.add_subscription(1, W, 'a', T0)
    await repo.add_subscription(1, '0x' + 'd' * 40, 'b', T0)
    update = make_update(1)
    await commands.mute_command(update, ctx(repo))
    (reply,) = update.message.replies
    assert reply['text'] == texts.MUTE_ALL_CONFIRM and reply['reply_markup'].inline_keyboard[0][0].callback_data == 'm:all:1'
    cancel, log = query('m:all:0', 1)
    await callbacks.mute_all_callback(cancel, ctx(repo))
    assert log['texts'] == [texts.MUTE_ALL_CANCELLED]
    yes, log = query('m:all:1', 1)
    await callbacks.mute_all_callback(yes, ctx(repo))
    assert log['texts'] == ['🔇 Muted 2 wallets until /unmute.']
    listed = make_update(1)
    await commands.list_wallets(listed, ctx(repo))
    assert listed.message.replies[0]['text'].count('· 🔇') == 2


# 4. /rename ------------------------------------------------------------------------------------

async def test_rename_rules(repo):
    await repo.add_subscription(7, W, 'whale1', T0)
    await repo.add_subscription(7, '0x' + 'd' * 40, 'jez', T0)
    await repo.add_subscription(7, '0x' + 'e' * 40, 'jez-1', T0)             # a /related derivative stays

    async def rename(*args):
        update = make_update(7)
        await commands.rename_command(update, ctx(repo, args=list(args)))
        return update.message.replies[0]['text']

    assert await rename('whale1') == texts.RENAME_USAGE
    assert await rename('whale1', 'JEZ') == 'You already have a wallet named <b>jez</b>.'
    assert await rename('whale1', 'x' * 33) == texts.RENAME_INVALID
    assert await rename('nope', 'n') == texts.ALIAS_NOT_FOUND.format(alias='nope')
    assert await rename('WHALE1', 'Whale') == '✅ Renamed <b>whale1</b> → <b>Whale</b>.'
    assert await rename('jez', 'jez2') == '✅ Renamed <b>jez</b> → <b>jez2</b>.'
    assert [a for a, _ in await repo.list_subscriptions(7)] == ['jez-1', 'jez2', 'Whale']


# 5. Did you mean ---------------------------------------------------------------------------------

async def test_fuzzy_alias_suggestion_and_rerun_button(repo):
    hl = FakeHLClient()
    hl.clearinghouse[W] = clearinghouse(account_value='10')
    await repo.add_subscription(7, W, 'jez', T0)
    update = make_update(7)
    await commands.positions_command(update, ctx(repo, hl, args=['jeez']))
    (reply,) = update.message.replies
    assert reply['text'] == 'Alias <b>jeez</b> not found. Did you mean <code>jez</code>?'
    button = reply['reply_markup'].inline_keyboard[0][0]
    assert button.text == '/positions jez' and button.callback_data == 'd:positions:jez'
    pressed, log = query(button.callback_data, 7)
    context = ctx(repo, hl)
    await callbacks.did_you_mean_callback(pressed, context)
    assert 'Positions for jez' in pressed.callback_query.message.reply_text.__self__.replies[0]['text']
    # nothing close: the plain not-found text, no button
    update = make_update(7)
    await commands.recent_command(update, ctx(repo, hl, args=['zzzzzz']))
    assert update.message.replies[0]['text'] == texts.ALIAS_NOT_FOUND.format(alias='zzzzzz')
    assert update.message.replies[0]['reply_markup'] is None


@pytest.mark.parametrize('handler,usage', [(commands.rename_command, texts.RENAME_USAGE),
                                           (commands.settings_command, texts.SETTINGS_USAGE),
                                           (commands.unmute_command, texts.UNMUTE_USAGE)])
async def test_usage_without_arguments(repo, handler, usage):
    assert await call_usage(handler, repo) == usage
