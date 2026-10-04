"""/related discovery (spec 7) on the PM-recorded master wallet, counterparty rules, system-address
exclusion, weight cap and userRole cache, the Track button flow, and background accumulation."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from telegram.constants import ParseMode

from hypermate.bot import commands, texts
from hypermate.core import pipeline, related
from hypermate.venues.hyperliquid import adapter
from tests.helpers import (FakeBot, FakeHLClient, check_telegram_html, clearinghouse, load_fixture,
                           make_context, make_update)

MASTER = '0x6aca4dce31f5560d880dbcc334998a9094269d9e'
SUB = '0xe5f44480001cd42e591a64d5106f1a2f3777cf08'
AGENT = '0x3ab0037816763d6dd2a08bdd8f694590f59d2553'
REFERRER = '0x41a4ec9d9b999825c8dbefc1072427ea17fc1135'
REFERRED = '0xb16039f74a0b77112a02d4b88332ed6b22086c51'
VAULT = '0x433f3e432be88068fe4e62e5f45220ff31f1d0e7'
TOP_COUNTERPARTY = '0x8ac3952bd8bb17e0c36f45df80dc7ca9618a7aa4'
T0 = 1_790_000_000_000


def addr(i: int) -> str:
    """Distinct test addresses that do not look like system addresses (no repeating tail)."""
    return f"0x{(i * 0x9e3779b97f4a7c15 + 0x1234567) % (1 << 160):040x}"


def master_client() -> FakeHLClient:
    """The recorded responses for the master wallet."""
    hl = FakeHLClient()
    hl.roles[MASTER] = load_fixture('hl_userRole_master.json')
    hl.subs[MASTER] = load_fixture('hl_subAccounts.json')
    hl.web[MASTER] = load_fixture('hl_webData2_master.json')
    hl.agents[MASTER] = load_fixture('hl_extraAgents.json')
    hl.fees[MASTER] = load_fixture('hl_userFees.json')
    hl.referrals[MASTER] = load_fixture('hl_referral.json')
    hl.vault_equities[MASTER] = load_fixture('hl_userVaultEquities.json')
    hl.vault_details[VAULT] = load_fixture('hl_vaultDetails_hlp.json')
    hl.ledger[MASTER] = load_fixture('hl_ledger_full_master.json')
    hl.clearinghouse[SUB] = clearinghouse(account_value='1234.5')
    return hl


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(adapter, 'now_ms', lambda: T0)
    monkeypatch.setattr(commands, 'now_ms', lambda: T0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)


def links_of(d: related.Discovery, address: str) -> list[related.Link]:
    return [l for l in d.links if l.related_address == address]


async def test_master_fixture_discovery(repo, clock):
    await repo.add_subscription(7, MASTER, 'master', T0)
    hl = master_client()
    d = await related.discover(hl, repo, MASTER, T0)

    # confirmed: the subaccount (spec 7.4 known master/sub pair) and the API wallet
    sub = next(l for l in links_of(d, SUB) if l.link_type == 'subaccount')
    assert (sub.confidence, sub.evidence['name']) == ('confirmed', 'Liminal')
    # the same address also received 5 spot transfers: kept as a second link on the row
    assert {l.link_type for l in links_of(d, SUB)} == {'subaccount', 'transfer_counterparty'}
    (agent,) = links_of(d, AGENT)
    assert (agent.link_type, agent.confidence) == ('agent', 'confirmed')
    # weak: referral both ways, the followed vault (userVaultEquities and the vault deposits in the ledger)
    assert ('referral', 'weak') in {(l.link_type, l.confidence) for l in links_of(d, REFERRER)}
    assert ('referral', 'weak') in {(l.link_type, l.confidence) for l in links_of(d, REFERRED)}
    assert {(l.link_type, l.confidence) for l in links_of(d, VAULT)} == {('vault_follow', 'weak')}
    # likely: the top transfer counterparty (34 transfers), and the subaccount's 5 spot transfers
    # are kept on the same address as a second link
    (top,) = links_of(d, TOP_COUNTERPARTY)
    assert (top.link_type, top.confidence) == ('transfer_counterparty', 'likely')
    assert top.evidence['in'] + top.evidence['out'] == 34
    assert all(not related.is_system_address(l.related_address) for l in d.links)
    assert MASTER not in {l.related_address for l in d.links}
    # self moves (send with user == destination) are not counterparties
    assert not any(l.related_address == MASTER for l in d.links)
    # weight cap (spec 7.4) and the account value lookups of the shown rows
    assert d.weight <= related.MAX_DISCOVERY_WEIGHT
    assert d.meter['userRole'] >= 60 and d.meter['userNonFundingLedgerUpdates'] == 20 + 239 // 20
    assert d.values[SUB] == Decimal('1234.5')


async def test_user_role_is_cached_for_seven_days(repo, clock):
    await repo.add_subscription(7, MASTER, 'master', T0)
    hl = master_client()
    first = await related.discover(hl, repo, MASTER, T0)
    second = await related.discover(hl, repo, MASTER, T0 + 3600_000)
    assert first.meter['userRole'] >= 60
    assert sum(1 for c in hl.calls if c == ('userRole', MASTER)) == 1      # served from api_cache
    assert second.weight <= first.weight
    # expired after 7 days
    await related.discover(hl, repo, MASTER, T0 + related.USER_ROLE_TTL_MS + 1)
    assert sum(1 for c in hl.calls if c == ('userRole', MASTER)) == 2


async def test_subaccount_role_expands_to_master_and_siblings(repo, clock):
    await repo.add_subscription(7, SUB, 'sub', T0)
    hl = master_client()
    hl.roles[SUB] = {'role': 'subAccount', 'data': {'master': MASTER}}
    other_sub = addr(5)
    hl.subs[MASTER] = hl.subs[MASTER] + [{'name': 'Other', 'subAccountUser': other_sub, 'master': MASTER}]
    d = await related.discover(hl, repo, SUB, T0)
    assert {(l.related_address, l.link_type, l.confidence) for l in d.links if l.confidence == 'confirmed'} == {
        (MASTER, 'master', 'confirmed'), (other_sub, 'subaccount', 'confirmed')}
    assert links_of(d, other_sub)[0].evidence['via'] == MASTER


def test_counterparty_rules_and_system_addresses():
    assert related.is_system_address('0x2000000000000000000000000000000000000000')
    assert related.is_system_address('0xfefefefefefefefefefefefefefefefefefefefe')
    assert related.is_system_address('0xDFC24B077BC1425AD1DEA75BCB6F8158E10DF303')      # HLP, any case
    # HL spot token / HIP-3 escrow addresses: 0x2 or 0x0, 28+ zeros, token index at the end
    for escrow in ('0x2000000000000000000000000000000000000079', '0x2000000000000000000000000000000000000168',
                   '0x200000000000000000000000000000000000010c', '0x0000000000000000000000000000000000000abc'):
        assert related.is_system_address(escrow), escrow
    assert not related.is_system_address('0x3000000000000000000000000000000000000000')  # not 0x2 / 0x0
    assert not related.is_system_address('0x2000000000000000000000000000abcdef000079')  # zeros broken
    assert not related.is_system_address(MASTER)

    def tx(kind, user, dest, usd, t=1):
        return {'time': t, 'delta': {'type': kind, 'user': user, 'destination': dest, 'usdcValue': usd}}
    A, B, C, D = addr(1), addr(2), addr(3), addr(4)
    updates = [
        tx('send', A, B, '100'), tx('send', B, A, '50'),                 # both ways -> likely
        tx('spotTransfer', A, C, '20000'),                               # one transfer $10k+ -> likely
        tx('send', D, A, '5'),                                           # one small transfer -> weak
        tx('send', A, A, '999'),                                         # self move, skipped
        tx('send', A, '0x2000000000000000000000000000000000000000', '7'),  # system, skipped
        {'time': 1, 'delta': {'type': 'deposit', 'usdc': '1'}},
    ]
    stats = related.counterparties(A, updates)
    assert set(stats) == {B, C, D}
    assert related.counterparty_confidence(stats[B]) == 'likely'
    assert related.counterparty_confidence(stats[C]) == 'likely'
    assert related.counterparty_confidence(stats[D]) == 'weak'
    assert stats[B] == {'in': 1, 'out': 1, 'usd': Decimal(150), 'last_ms': 1}


async def test_vault_counterparty_becomes_vault_follow(repo, clock):
    A, V = addr(1), addr(9)
    await repo.add_subscription(7, A, 'a', T0)
    hl = FakeHLClient()
    hl.ledger[A] = [{'time': T0 - i, 'delta': {'type': 'send', 'user': A, 'destination': V, 'usdcValue': '100'}}
                    for i in range(3)]
    hl.roles[V] = {'role': 'vault'}
    d = await related.discover(hl, repo, A, T0)
    (link,) = links_of(d, V)
    assert (link.link_type, link.confidence) == ('vault_follow', 'weak')
    assert ('userRole', V) in hl.calls


async def test_discovery_stays_under_300_with_many_counterparties(repo, clock):
    A = addr(1)
    await repo.add_subscription(7, A, 'a', T0)
    hl = FakeHLClient()
    hl.ledger[A] = [{'time': T0 - i, 'delta': {'type': 'send', 'user': A, 'destination': addr(100 + i),
                                               'usdcValue': '20000'}} for i in range(400)]
    d = await related.discover(hl, repo, A, T0)
    assert d.weight <= related.MAX_DISCOVERY_WEIGHT
    assert len(d.values) <= related.MAX_ROWS
    assert sum(1 for c in hl.calls if c[0] == 'userRole') <= 1 + (300 - 200) // 60 + 1


async def call(handler, repo, hl, user_id, *args, bot=None):
    update = make_update(user_id)
    await handler(update, make_context({'repo': repo, 'hl': hl}, args=args, bot=bot))
    return update


class EditableMessage:
    """A sent placeholder that /related edits in place."""

    def __init__(self, owner):
        self.owner = owner
        self.text = None
        self.reply_markup = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
        self.text, self.reply_markup = text, reply_markup
        self.owner.replies.append({'text': text, 'parse_mode': parse_mode, 'edited': True, 'markup': reply_markup})


async def test_related_command_searching_then_edit_then_cache(repo, clock, monkeypatch):
    await repo.add_subscription(7, MASTER, 'master', T0)
    hl = master_client()
    update = make_update(7)
    message = update.message
    placeholder = EditableMessage(message)

    async def reply_text(text, parse_mode=None, **kwargs):
        message.replies.append({'text': text, 'parse_mode': parse_mode, 'markup': kwargs.get('reply_markup')})
        return placeholder

    message.reply_text = reply_text
    await commands.related_command(update, make_context({'repo': repo, 'hl': hl}, args=['master']))
    first, result = message.replies
    assert 'Searching related wallets' in first['text']
    assert result.get('edited') and result['parse_mode'] == ParseMode.HTML
    check_telegram_html(result['text'])
    text = result['text']
    assert '<b>Confirmed</b>' in text and 'subaccount "Liminal"' in text and 'API wallet' in text
    assert '<b>Likely</b>' in text and '34 transfers both ways' in text
    assert '<b>Weak</b>' in text and 'referral' in text
    assert 'hypurrscan.io/address/' + SUB in text and 'acct $1.23k' in text and 'vol $' in text
    assert 'discovery weight' in text
    labels = [b.text for row in result['markup'].inline_keyboard for b in row]
    assert labels[0] == 'Track as master-1' and len(labels) == min(related.MAX_ROWS, len(labels))
    assert all(len(b.callback_data.encode()) <= 64 for row in result['markup'].inline_keyboard for b in row)

    # second call within 24 h: no placeholder, no API calls, same rows
    calls_before = len(hl.calls)
    update = make_update(7)
    await commands.related_command(update, make_context({'repo': repo, 'hl': hl}, args=['master']))
    assert len(update.message.replies) == 1 and 'Searching' not in update.message.replies[0]['text']
    assert len(hl.calls) == calls_before
    # refresh forces a new discovery
    update = make_update(7)
    update.message.reply_text = reply_text
    message.replies.clear()
    await commands.related_command(update, make_context({'repo': repo, 'hl': hl}, args=['master', 'refresh']))
    assert len(hl.calls) > calls_before
    assert await call(commands.related_command, repo, hl, 7) is not None


async def test_track_button_adds_the_wallet_and_list_shows_it(repo, clock):
    await repo.add_subscription(7, MASTER, 'master', T0)
    hl = master_client()
    links, _ = await related.links_for(hl, repo, MASTER, T0)
    text, buttons = __import__('hypermate.core.formatter', fromlist=['x']).format_related('master', MASTER, links, T0)
    label, row_id = buttons[0]
    markup = commands.track_keyboard(buttons)
    replies = []

    async def reply_text(text, parse_mode=None, **kwargs):
        replies.append(text)

    query = SimpleNamespace(data=f"rel:{row_id}", from_user=SimpleNamespace(id=7),
                            message=SimpleNamespace(reply_markup=markup, reply_text=reply_text),
                            answer=_answer)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7))
    await commands.track_callback(update, make_context({'repo': repo, 'hl': hl}))
    assert texts.WALLET_ADDED.format(alias='master-1') in replies[0]
    tracked = (await repo.link_by_row(row_id))['related_address']
    assert ('master-1', tracked) in await repo.list_subscriptions(7)
    # pressing again: alias exists
    await commands.track_callback(update, make_context({'repo': repo, 'hl': hl}))
    assert replies[1] == texts.ALIAS_EXISTS
    # stale row id
    query.data = 'rel:999999'
    await commands.track_callback(update, make_context({'repo': repo, 'hl': hl}))
    assert replies[2] == texts.TRACK_EXPIRED


async def _answer(*args, **kwargs):
    return None


async def test_background_counterparties_accumulate_as_weak_without_alerts(repo, clock):
    A, B = addr(1), addr(2)
    await repo.add_subscription(7, A, 'a', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.ledger[A] = [
        {'time': T0 + 1000, 'hash': '0x1', 'delta': {'type': 'send', 'user': B, 'destination': A, 'token': 'USDC',
                                                      'amount': '500', 'usdcValue': '500', 'sourceDex': '',
                                                      'destinationDex': ''}},
        {'time': T0 + 2000, 'hash': '0x2', 'delta': {'type': 'send', 'user': A, 'destination': B, 'token': 'USDC',
                                                      'amount': '100', 'usdcValue': '100', 'sourceDex': '',
                                                      'destinationDex': ''}},
        {'time': T0 + 3000, 'hash': '0x3', 'delta': {'type': 'send', 'user': A,
                                                      'destination': '0x2000000000000000000000000000000000000000',
                                                      'token': 'USDC', 'amount': '9', 'usdcValue': '9',
                                                      'sourceDex': '', 'destinationDex': 'xyz'}},
    ]
    hl.now = lambda: T0 + 10_000
    (va, _), = await repo.tracked_accounts()
    await pipeline.poll_ledger(bot, repo, hl, va, A)
    wallet_id = await repo.wallet_id(A)
    rows = await repo.links(wallet_id)
    assert len(rows) == 1 and rows[0]['related_address'] == B
    assert rows[0]['confidence'] == 'weak' and rows[0]['link_type'] == 'transfer_counterparty'
    assert rows[0]['evidence']['in'] == 1 and rows[0]['evidence']['out'] == 1 and rows[0]['evidence']['usd'] == '600'
    assert rows[0]['evidence'].get('background') and not rows[0]['evidence'].get('discovery')
    # the transfer alerts themselves went out as usual, nothing extra for the link
    assert len(bot.sent) == 2
    # a later full discovery is not considered cached by background rows
    assert await repo.links_discovered_at(wallet_id) is None
