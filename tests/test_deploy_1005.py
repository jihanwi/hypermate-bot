"""Deploy review 2026-10-05: Lighter not-found classification (A), alert storm on re-add (B), RISEx
per-market trade history and venue-pinned /add (D), /add and /related labels (E)."""

import json
from decimal import Decimal

import pytest

from hypermate.bot import commands
from hypermate.config import Config
from hypermate.core import pipeline
from hypermate.core.venues import resolve_summary, resolve_wallet, split_venue_prefix
from hypermate.venues import base
from hypermate.venues.base import VenueAccount
from hypermate.venues.hyperliquid import adapter as hl_adapter
from hypermate.venues.hyperliquid.venue import HyperliquidVenue
from hypermate.venues.lighter import adapter as lighter
from hypermate.venues.lighter.client import LighterAPIError, LighterClient, LighterNotFound, LighterRateLimited
from hypermate.venues.risex import adapter as risex
from tests.helpers import FakeBot, FakeHLClient, check_telegram_html, fill, load_fixture, make_context, make_update
from tests.test_risex import ADDR as RISEX_ADDR, FakeRisexClient, rest_trade

W = '0x' + 'c' * 40
T0 = 1_791_200_000_000
CYCLE_MS = 30_000


# A. Lighter not-found -------------------------------------------------------------------

async def test_lighter_400_code_21100_is_no_account_not_an_error(monkeypatch):
    client = LighterClient('http://unused')
    responses = [(400, load_fixture('lighter_accountsByL1Address_notfound.json')), (500, None), (429, None),
                 (400, {'code': 12345, 'message': 'something else'})]

    async def fake_fetch(path, params):
        return responses.pop(0)

    monkeypatch.setattr(client, '_fetch', fake_fetch)
    assert await client.accounts_by_l1_address(W) == []                     # 400 + 21100: no account
    for _ in range(3):                                                        # 500, 429, other 400: errors
        with pytest.raises(LighterAPIError):
            await client.accounts_by_l1_address(W)

    # in the /add summary: no account -> ✗, a failing adapter -> ? (error)
    class Lighter:
        venue = base.LIGHTER

        def __init__(self, outcome):
            self.outcome = outcome

        async def resolve(self, address):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    assert resolve_summary({base.LIGHTER: []}) == 'Lighter ✗'
    assert resolve_summary({base.LIGHTER: None}) == 'Lighter ? (error)'
    with pytest.raises(LighterNotFound):
        raise LighterNotFound('x')
    assert issubclass(LighterRateLimited, LighterAPIError)


# B. Re-add of loracle ----------------------------------------------------------------------

class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms


@pytest.fixture
def clock(monkeypatch):
    c = Clock(T0)
    monkeypatch.setattr(hl_adapter, 'now_ms', c)
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


def micro_fills(coins, start, minutes, notional_usd=Decimal(300)):
    """One order per coin every 4 s, each worth about notional_usd (the $82 to $555 fills of the review)."""
    fills, oid, t, i = [], 1, start, 0
    while t < start + minutes * 60_000:
        for n, coin in enumerate(coins):
            price = Decimal(2)
            size = (notional_usd / price).quantize(Decimal('0.01'))
            fills.append(fill(coin, 'Close Short', str(size), str(price), t + n, str(Decimal(-500_000) + size * i),
                              oid=oid, closed_pnl='1'))
            oid += 1
        t += 4000
        i += 1
    return fills


async def test_readd_with_stale_algos_and_micro_fills_sends_only_the_summary(repo, clock, monkeypatch):
    """The review case: 4 stale algo rows (last fill > 2 h ago) plus a stream of micro fills right after
    /add. Expected: 0 individual alerts, 0 stale algo_end messages, 1 summary."""
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_FLOOR_USD', 1000)
    hl, bot = FakeHLClient(), FakeBot()
    coins = ['LINK', 'DOGE', 'SUI', 'ONDO', 'UNI', 'ARB']
    from tests.helpers import clearinghouse, position
    hl.clearinghouse[W] = clearinghouse(*(position(c, '-500000', entry_px='2', position_value='1000000') for c in coins),
                                        account_value='21000000')
    # a previous life of the wallet: tracked, four algos still "active" from 3 hours ago
    await repo.add_subscription(7, W, 'loracle', T0 - 4 * 3600_000)
    (va, _), = await repo.tracked_accounts()
    for coin in coins[:4]:
        await repo.upsert_algo(va, {'coin': coin, 'sign': 1, 'started_ms': T0 - 5 * 3600_000,
                                    'last_fill_ms': T0 - 3 * 3600_000, 'fills_count': 500,
                                    'total_sz': Decimal(1000), 'total_ntl': Decimal(150_000)})
    await repo.remove_subscription(7, 'loracle')

    # re-add through the command (stale algos end silently, cursors restart at now)
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl)}}
    update = make_update(7)
    await commands.add_wallet(update, make_context(bot_data, args=[W, 'loracle']))
    assert await repo.active_algos(va) == {}
    assert len(await repo.events_since(va, 0, ['algo_end'])) == 0           # no stale algo_end alerts

    hl.fills[W] = micro_fills(coins, T0 + 1000, 10)
    await run_cycles(repo, hl, bot, clock, T0 + 6 * 60_000)
    texts = [m['text'] for m in bot.sent]
    assert sum('running TWAP-style algos' in t for t in texts) == 1
    assert len(texts) == 1, texts                                             # nothing else went out
    events = await repo.events_since(va, 0)
    fills = [e for e in events if e['type'].startswith('position_')]
    assert fills and {e['delivery'] for e in fills} <= {'filtered_threshold'}
    assert len(await repo.active_algos(va)) == 6                             # detected from filtered rows
    # /recent shows the filtered fills dimmed and the active algos
    out = make_update(7)
    await commands.recent_command(out, make_context(bot_data, args=['loracle', '30']))
    text = out.message.replies[0]['text']
    check_telegram_html(text)
    assert 'not sent (below threshold)' in text and '🤖 algo $' in text


async def test_min_notional_floor_filters_small_fills_but_not_liquidations(repo, clock, monkeypatch):
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_FLOOR_USD', 1000)
    await repo.add_subscription(7, W, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [fill('ETH', 'Open Long', '0.1', '3000', T0 + 1000, '0', oid=1),                    # $300
                   fill('BTC', 'Open Long', '0.05', '86000', T0 + 2000, '0', oid=2),                 # $4,300
                   fill('DOGE', 'Close Long', '100', '0.2', T0 + 3000, '100', oid=3, closed_pnl='-5',
                        liquidation={'markPx': '0.2', 'method': 'market'})]                            # $20
    await run_cycles(repo, hl, bot, clock, T0 + CYCLE_MS)
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 2 and any('$BTC' in t for t in texts) and any('LIQUIDATED' in t for t in texts)
    (va, _), = await repo.tracked_accounts()
    eth = next(e for e in await repo.events_since(va, 0) if e['payload'].get('coin') == 'ETH')
    assert eth['delivery'] == 'filtered_threshold'


async def test_full_close_is_never_filtered_by_the_notional_floor(repo, clock, monkeypatch):
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_FLOOR_USD', 1000)
    await repo.add_subscription(7, W, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.fills[W] = [fill('ETH', 'Close Long', '0.1', '3000', T0 + 1000, '0.5', oid=1, closed_pnl='2'),   # $300 partial
                   fill('ETH', 'Close Long', '0.4', '3000', T0 + 2000, '0.4', oid=2, closed_pnl='8')]   # $1,200? no: $1,200 > floor
    hl.fills[W][1]['sz'] = '0.2'                                                                        # $600 full close
    hl.fills[W][1]['startPosition'] = '0.2'
    await run_cycles(repo, hl, bot, clock, T0 + CYCLE_MS)
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 1 and 'closed LONG $ETH' in texts[0]
    (va, _), = await repo.tracked_accounts()
    rows = {e['type']: e['delivery'] for e in await repo.events_since(va, 0)}
    assert rows == {'position_decrease': 'filtered_threshold', 'position_close': 'sent'}


def test_risex_positions_use_mark_price_for_notional_and_unrealized_pnl():
    """D-3: notional = |size| x mark, uPnL = size x (mark - entry), entry rounded to step_price,
    leverage and unsettled funding carried. Real dipper3 recording (2026-10-05 03:20Z): 150 ZEC long
    at 1311.4946 and 5M PUMP long at 0.006268; mark uses the markets fixture value (ZEC 1340.258925),
    so ZEC uPnL = +$4,315."""
    data = load_fixture('risex_markets_dipper3.json')['data']['markets']
    markets = {str(m['market_id']): {'name': m['config']['name'], 'mark_price': m['mark_price'],
                                     'step_price': m['config']['step_price'], 'step_size': m['config']['step_size']}
               for m in data}
    rest_rows = load_fixture('risex_positions_dipper3.json')['data']['positions']
    positions = risex.parse_rest_positions({'positions': rest_rows}, markets)
    assert set(positions) == {'ZEC', 'PUMP'}
    zec = positions['ZEC']
    assert Decimal(zec['szi']) == 150
    assert Decimal(zec['position_value']) == Decimal(150) * Decimal('1340.258925173239075868')     # 201,038.84
    assert abs(Decimal(zec['unrealized_pnl']) - Decimal('4315')) < 1      # 150 x (1340.258925 - 1311.494614)
    assert zec['entry_px'] == '1311.49' and zec['leverage'] == '10'
    assert Decimal(zec['unsettled_funding']) == Decimal('87.79075841250791025')
    assert Decimal(zec['funding_pnl']) == Decimal('-87.79075841250791025')      # a long pays funding
    pump = positions['PUMP']
    assert Decimal(pump['szi']) == 5_000_000 and pump['entry_px'] == '0.006268'
    assert zec['px_decimals'] == 2 and pump['px_decimals'] == 6                 # from step_price
    assert abs(Decimal(pump['unrealized_pnl']) - Decimal('520')) < 1       # 5M x (0.006372 - 0.006268)
    # a short: PnL sign follows the signed size
    short = risex.parse_rest_positions({'positions': [{**rest_rows[0], 'size': '-' + rest_rows[0]['size'], 'side': 'SELL'}]},
                                       markets)['ZEC']
    assert abs(Decimal(short['unrealized_pnl']) + Decimal('4315')) < 1
    assert Decimal(short['funding_pnl']) == Decimal('87.79075841250791025')     # a short receives it [?]
    # the same numbers through the human-unit WS path
    ws = risex.parse_ws_positions([{**rest_rows[0], 'size': '150', 'avg_entry_price': '1311.494614892857142853',
                                    'leverage': '10', 'unsettled_funding': '87.79075841250791025'}], markets)['ZEC']
    assert ws['unrealized_pnl'] == zec['unrealized_pnl'] and ws['position_value'] == zec['position_value']
    # rendering: leverage and funding on the line
    from hypermate.core.formatter import format_positions
    from hypermate.venues.base import AccountSnapshot, as_clearinghouse_state
    state = as_clearinghouse_state(AccountSnapshot(positions, Decimal(50000)))
    text = format_positions('w', W, None, {}, Decimal(10), [('RISEx', state)])
    check_telegram_html(text)
    assert 'Entry: $0.006268' in text                                            # (5) step_price decimals, not $0.0063
    assert 'Entry: $1,311.49' in text and 'Size: $201,039' in text and '🟢 $4,314.65' in text and '· 10x' in text and '· funding -$87.79' in text
    # no mark price: the quote amount stands in and PnL is unknown
    nomark = risex.parse_rest_positions({'positions': rest_rows[:1]}, {'8': {'name': 'ZEC/USDC'}})['ZEC']
    assert Decimal(nomark['position_value']) == Decimal('195730.439897416756007236') and nomark['unrealized_pnl'] == 'N/A'


async def test_fills_after_an_algo_end_wait_for_re_detection(repo, clock):
    """After ALGO_END, fills on that coin within ALGO_REARM_SEC go to the buffer: one summed message
    when nothing re-detects, nothing when an algo starts again."""
    await repo.add_subscription(7, W, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    (va, _), = await repo.tracked_accounts()
    await repo.record_event('end1', va, 'algo_end', T0 - 60_000, {'coin': 'BTC', 'sign': 1, 'verb': 'accumulating',
                                                                  'side': 'LONG', 'total_ntl': '1'}, 'sent', T0)
    hl.fills[W] = [fill('BTC', 'Open Long', '0.05', '86000', T0 + 1000 + i * 90_000, str(Decimal(400) + Decimal('0.05') * i),
                        oid=10 + i) for i in range(3)]                       # 3 orders over 3 minutes
    await run_cycles(repo, hl, bot, clock, T0 + 5 * 60_000)
    assert bot.sent == []
    assert {e['delivery'] for e in await repo.events_since(va, T0) if e['type'].startswith('position_')} == {'rearm_buffer'}
    await run_cycles(repo, hl, bot, clock, T0 + 11 * 60_000)                 # window (10 min from the end) passed
    assert len(bot.sent) == 1
    text = bot.sent[0]['text']
    check_telegram_html(text)
    assert 'added to LONG $BTC' in text and '· 3 fills' in text
    rows = [e for e in await repo.events_since(va, T0) if e['type'].startswith('position_')]
    assert {e['delivery'] for e in rows} == {'sent'} and sum('chain' in e['payload'] for e in rows) == 1

    # re-detection: a fresh burst right after another END starts an algo and absorbs the buffer silently
    bot.sent.clear()
    t = clock.ms
    await repo.record_event('end2', va, 'algo_end', t, {'coin': 'ETH', 'sign': 1, 'verb': 'accumulating',
                                                        'side': 'LONG', 'total_ntl': '1'}, 'sent', t)
    hl.fills[W] += [fill('ETH', 'Open Long', '0.5', '3000', t + 1000 + i * 15_000, str(Decimal(100) + Decimal('0.5') * i),
                         oid=100 + i) for i in range(12)]                     # 12 orders in 3 minutes
    await run_cycles(repo, hl, bot, clock, t + 12 * 60_000)
    texts = [m['text'] for m in bot.sent]
    assert sum('algo accumulating LONG $ETH' in x for x in texts) == 1 and len(texts) == 1
    eth_rows = [e for e in await repo.events_since(va, t) if e['payload'].get('coin') == 'ETH'
                and e['type'].startswith('position_')]
    assert eth_rows and {e['delivery'] for e in eth_rows} == {'summarized'}


# D. RISEx ----------------------------------------------------------------------------------

async def test_risex_rest_paths_always_pass_a_market_id():
    client = FakeRisexClient()
    ad = risex.RisexAdapter(client)
    account = VenueAccount(base.RISEX, RISEX_ADDR, RISEX_ADDR, 1)
    client.positions_by[RISEX_ADDR] = load_fixture('risex_positions.json')['data']['positions']   # market 1
    client.trades_by[RISEX_ADDR] = [rest_trade('t1', T0, 'BUY', '0.01')]
    await ad.fetch_events(account, json.dumps({'id': 't0', 'ms': T0 - 1}), {'BTC': {'szi': '-0.02'}})
    history = [c for c in client.calls if c[0] == 'trade-history']
    assert history and all(c[3] is not None for c in history)                 # never market_id=None
    assert {c[3] for c in history} == {'1'}                                   # the markets the account holds
    # resolve without positions sweeps every market with limit=1
    client.calls.clear()
    client.positions_by.pop(RISEX_ADDR)
    client.trades_by[RISEX_ADDR] = []
    assert await ad.resolve(RISEX_ADDR) == []
    sweep = [c for c in client.calls if c[0] == 'trade-history']
    assert len(sweep) == 38 and all(c[2] == 1 and c[3] is not None for c in sweep)


def test_venue_prefix_parsing():
    assert split_venue_prefix('risex:0xABC') == (base.RISEX, '0xABC')
    assert split_venue_prefix('LTR:0xabc') == (base.LIGHTER, '0xabc')
    assert split_venue_prefix('0xabc') == (None, '0xabc')
    assert split_venue_prefix('foo:0xabc') == (None, 'foo:0xabc')


async def test_add_with_venue_prefix_pins_one_venue_and_list_shows_badges(repo, clock):
    rclient = FakeRisexClient()
    hl = FakeHLClient()
    bot_data = {'repo': repo, 'hl': hl, 'venues': {base.HYPERLIQUID: HyperliquidVenue(hl),
                                                    base.RISEX: risex.RisexAdapter(rclient)}}
    update = make_update(5)
    await commands.add_wallet(update, make_context(bot_data, args=[f"risex:{RISEX_ADDR}", 'rise']))
    text = update.message.replies[0]['text']
    check_telegram_html(text)
    assert text == '✅ Wallet added as <b>rise</b> · RISEx ✅ (added as given, no activity seen)'
    rows = {r['venue']: r['active'] for r in await repo.venue_accounts_of(RISEX_ADDR)}
    assert rows == {base.RISEX: True}                                   # exclusive: no HL row, no dex scan
    assert ('positions', RISEX_ADDR) in rclient.calls and not any(c[0] in ('clearinghouseState', 'perpDexs')
                                                                   for c in hl.calls)
    assert RISEX_ADDR not in {a for _, a in await repo.tracked_accounts()}
    update = make_update(5)
    await commands.list_wallets(update, make_context(bot_data))
    assert '· [RISE]' in update.message.replies[0]['text'] and '[HL]' not in update.message.replies[0]['text']
    update = make_update(5)
    await commands.add_wallet(update, make_context(bot_data, args=['bogus:0x12', 'x']))
    assert 'Usage: /add' in update.message.replies[0]['text'] and 'risex:' in update.message.replies[0]['text']


# E. Labels ---------------------------------------------------------------------------------

def test_add_summary_labels():
    hl_account = [VenueAccount(base.HYPERLIQUID, W, W)]
    assert resolve_summary({base.HYPERLIQUID: hl_account}, ['xyz', 'para']) == 'HL ✅ · dex: xyz, para'
    assert resolve_summary({base.HYPERLIQUID: hl_account}) == 'HL ✅'
    lighter_account = [VenueAccount(base.LIGHTER, '7', W)]
    assert resolve_summary({base.HYPERLIQUID: [], base.LIGHTER: lighter_account}) == 'HL ✗ · Lighter ✅ (1 sub-account)'
    assert lighter.EXPLORER_FALLBACK.startswith('https://')


async def test_related_row_separates_volume_and_account_value(repo, clock):
    from hypermate.core.formatter import format_related
    links = [{'row_id': 1, 'related_address': '0x' + '1' * 39 + 'a', 'link_type': 'transfer_counterparty',
              'confidence': 'likely', 'evidence': {'in': 5, 'out': 5, 'usd': '113000000', 'last_ms': T0,
                                                   'account_value': '3150000', 'discovery': True}}]
    text, buttons = format_related('w', W, links, T0)
    check_telegram_html(text)
    assert '10 transfers both ways · vol $113M' in text and '· acct $3.15M' in text
    assert buttons == [('w-1', 1)]
    assert await resolve_wallet(repo, {}, W, T0) == {}


def test_min_notional_scales_with_the_account_value(monkeypatch):
    """post-deploy 1005b (4): loracle-2's $999.99 clips passed a fixed $1,000 by one cent. The threshold is
    max(floor $100, 0.5% of the account value): smb $483k -> $2,415, iroh $79k -> $397, a $5k wallet -> $100."""
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_FLOOR_USD', 100)
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_PCT', Decimal('0.005'))
    assert pipeline.min_notional_usd(Decimal(483_000)) == Decimal(2415)
    assert pipeline.min_notional_usd(Decimal(79_400)) == Decimal(397)
    assert pipeline.min_notional_usd(Decimal(5_000)) == Decimal(100)
    assert pipeline.min_notional_usd(None) == Decimal(100)          # no snapshot yet: the floor


async def test_notional_threshold_uses_the_snapshot_account_value(repo, clock, monkeypatch):
    """A $483k account (smb): a $999.99 clip and a $2,000 order are filtered, a $3,000 one is sent."""
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_FLOOR_USD', 100)
    monkeypatch.setattr(Config, 'MIN_NOTIONAL_PCT', Decimal('0.005'))
    await repo.add_subscription(7, W, 'smb', T0)
    hl, bot = FakeHLClient(), FakeBot()
    from tests.helpers import clearinghouse
    hl.clearinghouse[W] = clearinghouse(account_value='483000')
    hl.fills[W] = [fill('LINK', 'Open Long', '50', '19.9998', T0 + 1000, '0', oid=1),        # $999.99
                   fill('ETH', 'Open Long', '0.5', '4000', T0 + 2000, '0', oid=2),           # $2,000
                   fill('BTC', 'Open Long', '0.05', '60000', T0 + 3000, '0', oid=3)]         # $3,000
    hl.now = clock
    await pipeline.monitor_positions_job(make_context({'repo': repo, 'hl': hl}, bot=bot))   # snapshot: $483k
    await run_cycles(repo, hl, bot, clock, T0 + CYCLE_MS)
    texts = [m['text'] for m in bot.sent]
    assert len(texts) == 1 and '$BTC' in texts[0]
    (va, _), = await repo.tracked_accounts()
    rows = {e['payload']['coin']: e['delivery'] for e in await repo.events_since(va, 0)}
    assert rows == {'LINK': 'filtered_threshold', 'ETH': 'filtered_threshold', 'BTC': 'sent'}
