"""The gated polling loop (spec 3.5): fills only on activity, tier polling, adaptive intervals,
/health, and the 50-address 30-minute simulation with no 429."""

import asyncio
import logging
from decimal import Decimal

import pytest

from hypermate.bot import commands, texts
from hypermate.config import Config
from hypermate.core import pipeline, poller
from hypermate.venues.hyperliquid import adapter, scheduler
from hypermate.venues.hyperliquid.client import HyperliquidRateLimited
from hypermate.venues.hyperliquid.scheduler import WeightBudget
from tests.helpers import (FakeBot, FakeHLClient, check_telegram_html, clearinghouse, fill, make_context,
                           make_update, position)

A = '0x' + 'a' * 40
T0 = 1_790_000_000_000
DAY = 24 * 3600 * 1000


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms

    def seconds(self):
        return self.ms / 1000


@pytest.fixture
def clock(monkeypatch):
    c = Clock(T0)
    monkeypatch.setattr(adapter, 'now_ms', c)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pipeline.asyncio, 'sleep', no_sleep)
    monkeypatch.setattr(poller.asyncio, 'sleep', no_sleep)
    return c


def calls(hl, kind):
    return [c for c in hl.calls if c[0] == kind]


async def cycle(context, clock, seconds=20):
    clock.ms += seconds * 1000
    await poller.poll_job(context)


async def test_fills_fetched_only_when_positions_or_spot_change(repo, clock):
    await repo.add_subscription(7, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.now = clock
    hl.clearinghouse[A] = clearinghouse(position('BTC', '1'))
    context = make_context({'repo': repo, 'hl': hl}, bot=bot)

    await cycle(context, clock)                  # baseline
    await cycle(context, clock)                  # nothing moved
    assert len(calls(hl, 'clearinghouseState')) == 2 and len(calls(hl, 'spotClearinghouseState')) == 2
    assert calls(hl, 'userFillsByTime') == []

    hl.clearinghouse[A] = clearinghouse(position('BTC', '2'))
    hl.fills[A] = [fill('BTC', 'Open Long', '1', '100', clock.ms + 15_000, '1', oid=5)]
    await cycle(context, clock)
    assert len(calls(hl, 'userFillsByTime')) == 1
    assert len(bot.sent) == 1 and 'added to LONG $BTC' in bot.sent[0]['text']

    await cycle(context, clock)                  # positions same as the stored snapshot again
    assert len(calls(hl, 'userFillsByTime')) == 1

    hl.spot[A] = {'balances': [{'coin': 'HYPE', 'total': '10', 'entryNtl': '400'}]}
    hl.fills[A].append(fill('@107', 'Buy', '10', '40', clock.ms + 15_000, '0', oid=6))
    await cycle(context, clock)                  # spot balance moved -> fills fetched
    assert len(calls(hl, 'userFillsByTime')) == 2
    assert 'bought 10 $HYPE' in bot.sent[1]['text']


async def test_active_algo_or_twap_keeps_fills_every_cycle(repo, clock):
    await repo.add_subscription(7, A, 'w', T0)
    hl, bot = FakeHLClient(), FakeBot()
    hl.now = clock
    context = make_context({'repo': repo, 'hl': hl}, bot=bot)
    await cycle(context, clock)
    (va, _), = await repo.tracked_accounts()
    await repo.upsert_algo(va, {'coin': 'BTC', 'sign': 1, 'started_ms': clock.ms, 'last_fill_ms': clock.ms,
                                'fills_count': 9, 'total_sz': Decimal(1), 'total_ntl': Decimal(100)})
    await cycle(context, clock)
    await cycle(context, clock)
    assert len(calls(hl, 'userFillsByTime')) == 2
    await repo.delete_algo(va, 'BTC', 1)
    await repo.upsert_twap(va, '1', {'coin': 'ETH', 'side': 'B', 'sz': '1', 'minutes': 60}, clock.ms)
    await cycle(context, clock)
    assert len(calls(hl, 'userFillsByTime')) == 3 and len(calls(hl, 'webData2')) == 1


async def test_ledger_runs_on_its_own_interval(repo, clock):
    await repo.add_subscription(7, A, 'w', T0)
    hl = FakeHLClient()
    hl.now = clock
    context = make_context({'repo': repo, 'hl': hl})
    for _ in range(10):                          # 200 s
        await cycle(context, clock)
    # first cycle and then every POLL_LEDGER_SEC (180 s): cycles at +20 s and +200 s
    assert len(calls(hl, 'userNonFundingLedgerUpdates')) == 2


async def test_dormant_accounts_poll_every_third_cycle_and_wake_on_activity(repo, clock):
    await repo.add_subscription(7, A, 'old', T0 - 8 * DAY)
    B = '0x' + 'b' * 40
    await repo.add_subscription(7, B, 'fresh', T0)
    hl = FakeHLClient()
    hl.now = clock
    context = make_context({'repo': repo, 'hl': hl})
    for _ in range(6):
        await cycle(context, clock)
    assert len([c for c in calls(hl, 'clearinghouseState') if c[1] == B]) == 6
    assert len([c for c in calls(hl, 'clearinghouseState') if c[1] == A]) == 2
    state = poller.get_state(context)
    (va_a, _), _ = await repo.tracked_accounts()
    assert va_a in state.dormant

    # activity on the dormant wallet: it is polled every cycle from the next one
    hl.clearinghouse[A] = clearinghouse(position('ETH', '3'))
    hl.fills[A] = [fill('ETH', 'Open Long', '3', '100', clock.ms + 5_000, '0', oid=1)]
    for _ in range(3):
        await cycle(context, clock)
        if va_a not in state.dormant:
            break
    assert va_a not in state.dormant
    before = len([c for c in calls(hl, 'clearinghouseState') if c[1] == A])
    for _ in range(3):
        await cycle(context, clock)
    assert len([c for c in calls(hl, 'clearinghouseState') if c[1] == A]) == before + 3


async def test_cycle_waits_for_the_adaptive_interval(repo, clock, monkeypatch):
    for i in range(60):
        await repo.add_subscription(7, f"0x{i:040x}", f"w{i}", T0)
    hl = FakeHLClient()
    hl.now = clock
    context = make_context({'repo': repo, 'hl': hl})
    await cycle(context, clock, 20)
    state = poller.get_state(context)
    # 60 accounts * 4 weight = 240 per cycle -> 240 * 60 / 408 = 35.3 -> 36 s
    assert state.poll_fast_sec == 36
    n = len(calls(hl, 'clearinghouseState'))
    await cycle(context, clock, 20)              # too early: skipped
    assert len(calls(hl, 'clearinghouseState')) == n
    await cycle(context, clock, 20)              # 40 s since the last cycle: runs
    assert len(calls(hl, 'clearinghouseState')) == 2 * n


async def test_health_command_is_admin_only(repo, clock, monkeypatch):
    monkeypatch.setattr(Config, 'ADMIN_USER_IDS', frozenset({1}))
    monkeypatch.setattr(commands, 'now_ms', clock)
    await repo.add_subscription(7, A, 'w', T0)
    hl = FakeHLClient()
    hl.now = clock
    budget = WeightBudget(1020, clock=clock.seconds)
    context = make_context({'repo': repo, 'hl': hl, 'budget': budget})
    await cycle(context, clock)
    budget.charge(100)
    clock.ms += 60_000

    update = make_update(7)
    await commands.health_command(update, context)
    assert update.message.replies[0]['text'] == texts.ADMIN_ONLY

    update = make_update(1)
    await commands.health_command(update, context)
    text = update.message.replies[0]['text']
    check_telegram_html(text)
    assert 'hyperliquid:' in text and '1 active (0 dormant)' in text
    assert 'max 100' in text and '429s: 0' in text and 'DB:' in text and 'Uptime: 1m' in text
    assert 'WAL' in text and 'Events: 0 rows' in text and 'algo_active' not in text
    (va, _), = await repo.tracked_accounts()
    await repo.upsert_algo(va, {'coin': 'xyz:MU', 'sign': 1, 'started_ms': T0, 'last_fill_ms': clock.ms - 120_000,
                                'fills_count': 12, 'total_ntl': Decimal(41000), 'total_sz': Decimal(1)})
    await repo.record_event('k', va, 'position_open', clock.ms, {'coin': 'BTC'}, 'sent', clock.ms)
    update = make_update(1)
    await commands.health_command(update, context)
    text = update.message.replies[0]['text']
    check_telegram_html(text)
    assert 'Events: 1 rows · last 24h: position_open 1' in text
    assert 'algo_active:\n- 0xaaaa...aaaa $MU · xyz + · 12 fills $41k · last fill 2m ago' in text


# 50 addresses, 30 minutes, no 429 (spec 5.3) ------------------------------------------

class RateLimitedHL(FakeHLClient):
    """Counts weight like HL (1200 per rolling minute per IP) and answers 429 above it."""

    LIMIT = 1200

    def __init__(self, budget, clock):
        super().__init__()
        self.budget = budget
        self.clock = clock
        self.log = []                      # (seconds, weight)
        self.rejections = 0

    async def _charged(self, request_type, items=0):
        weight, priority = scheduler.request_cost(request_type)
        await self.budget.acquire(weight, priority)
        now = self.clock.seconds()
        used = sum(w for t, w in self.log if now - 60 < t <= now)
        if used + weight > self.LIMIT:
            self.rejections += 1
            self.budget.rate_limited(None)
            raise HyperliquidRateLimited(f"{request_type} returned HTTP 429")
        self.log.append((now, weight))
        extra = scheduler.item_weight(request_type, items)
        if extra:
            self.log.append((now, extra))
            self.budget.charge(extra, priority)

    async def clearinghouse_state(self, user, dex=''):
        await self._charged('clearinghouseState')
        return await super().clearinghouse_state(user, dex)

    async def spot_clearinghouse_state(self, user):
        await self._charged('spotClearinghouseState')
        return await super().spot_clearinghouse_state(user)

    async def user_fills_by_time(self, user, start_time):
        fills = await super().user_fills_by_time(user, start_time)
        await self._charged('userFillsByTime', len(fills))
        return fills

    async def ledger_updates(self, user, start_time):
        await self._charged('userNonFundingLedgerUpdates')
        return await super().ledger_updates(user, start_time)

    async def web_data2(self, user):
        await self._charged('webData2')
        return await super().web_data2(user)

    async def twap_history(self, user):
        await self._charged('twapHistory')
        return await super().twap_history(user)


async def test_fifty_addresses_thirty_minutes_without_429(repo, clock, monkeypatch, caplog):
    """50 wallets, 10 of them trading every cycle, 30 simulated minutes: no 429, budget respected."""
    caplog.set_level(logging.INFO, logger='hypermate.core.poller')
    addresses = [f"0x{i + 1:040x}" for i in range(50)]
    for i, address in enumerate(addresses):
        await repo.add_subscription(7, address, f"w{i}", T0)
    budget = WeightBudget(Config.HL_WEIGHT_BUDGET, clock=clock.seconds, sleep=_advance(clock))
    hl = RateLimitedHL(budget, clock)
    hl.now = clock
    bot = FakeBot()
    context = make_context({'repo': repo, 'hl': hl, 'budget': budget}, bot=bot)
    busy = addresses[:10]
    size = {a: Decimal(0) for a in busy}
    oid = 0

    state = poller.get_state(context)
    end = clock.ms + 30 * 60_000
    while clock.ms < end:
        # the busy wallets add to BTC every cycle (one order of 3 fills each)
        for address in busy:
            oid += 1
            t = clock.ms + 1000
            size[address] += 1
            hl.clearinghouse[address] = clearinghouse(position('BTC', str(size[address])))
            hl.fills.setdefault(address, []).extend(
                fill('BTC', 'Open Long', '0.3', '100', t + j, str(size[address] - 1 + Decimal('0.3') * j), oid=oid)
                for j in range(3))
        await cycle(context, clock, state.poll_fast_sec)
        await poller.weight_log_job(context)

    assert hl.rejections == 0
    assert budget.last_hour()['rate_limited'] == 0
    assert budget.last_hour()['max'] <= Config.HL_WEIGHT_BUDGET + Config.HL_WEIGHT_BUDGET // 6
    # the busy wallets alerted, the quiet ones never cost a fills call
    assert len(bot.sent) > 0
    quiet_fills = [c for c in hl.calls if c[0] == 'userFillsByTime' and c[1] not in busy]
    assert quiet_fills == []
    assert state.poll_fast_sec == 30             # 50 * 4 = 200 per cycle -> 30 s (spec 3.5 formula)
    weight_lines = [r.message for r in caplog.records if 'HL weight last minute' in r.message]
    assert len(weight_lines) >= 25


def _advance(clock):
    async def sleep(seconds):
        clock.ms += int(seconds * 1000)
        await asyncio.sleep(0)
    return sleep
