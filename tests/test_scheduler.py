"""Weight budget (spec 3.5): token bucket, priority, 429 pause, per-item cost, adaptive intervals."""

import asyncio

from hypermate.venues.hyperliquid import scheduler
from hypermate.venues.hyperliquid.client import HyperliquidClient, HyperliquidRateLimited
from hypermate.venues.hyperliquid.scheduler import WeightBudget


class FakeTime:
    """Monotonic clock advanced by the budget's own sleeps (no real waiting)."""

    def __init__(self, start=1000.0):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def make_budget(per_minute=1020, start=1000.0):
    clock = FakeTime(start)
    return WeightBudget(per_minute, clock=clock, sleep=clock.sleep), clock


async def test_bucket_refills_at_budget_rate():
    budget, clock = make_budget(600)          # 10 per second, bucket holds 100
    for _ in range(5):
        await budget.acquire(20, scheduler.P_FILLS)
    assert clock.slept == []                   # the initial bucket covers 100
    await budget.acquire(20, scheduler.P_FILLS)
    assert sum(clock.slept) == 2.0             # 20 more tokens at 10/s


async def test_rolling_minute_never_exceeds_hl_limit():
    """1020/min with a 170 bucket: any 60 s window stays under 1200."""
    budget, clock = make_budget(1020)
    stamps = []
    for _ in range(200):
        await budget.acquire(20, scheduler.P_FILLS)
        stamps.append((clock.now, 20))
    for t, _ in stamps:
        window = sum(w for s, w in stamps if t - 60 < s <= t)
        assert window <= 1200


async def test_priority_order_when_waiting():
    budget, clock = make_budget(60)            # 1 per second, bucket 10
    budget.tokens = 0
    order = []

    async def req(name, weight, priority):
        await budget.acquire(weight, priority)
        order.append(name)

    ledger = asyncio.create_task(req('ledger', 5, scheduler.P_LEDGER))
    await asyncio.sleep(0)
    snapshot = asyncio.create_task(req('snapshot', 2, scheduler.P_SNAPSHOT))
    fills = asyncio.create_task(req('fills', 3, scheduler.P_FILLS))
    await asyncio.gather(ledger, snapshot, fills)
    assert order == ['snapshot', 'fills', 'ledger']


async def test_429_pauses_the_queue_for_retry_after():
    budget, clock = make_budget(1020)
    assert budget.rate_limited(12.0) == 12.0
    start = clock.now
    await budget.acquire(2, scheduler.P_SNAPSHOT)
    assert clock.now - start >= 12.0
    assert budget.rate_limited(None) == scheduler.DEFAULT_RETRY_AFTER_SEC
    assert budget.last_hour()['rate_limited'] == 2


async def test_client_charges_per_item_and_raises_on_429(monkeypatch):
    budget, clock = make_budget(1020)
    hl = HyperliquidClient('http://unused', budget=budget)
    responses = [(200, {}, [{'time': i} for i in range(45)]), (429, {'Retry-After': '7'}, None)]

    async def fake_post(payload):
        return responses.pop(0)

    monkeypatch.setattr(hl, '_post', fake_post)
    assert len(await hl.user_fills_by_time('0xabc', 0)) == 45
    assert budget.tokens == budget.capacity - 20 - 2       # 20 base + 45 // 20 per-item
    try:
        await hl.user_fills_by_time('0xabc', 0)
    except HyperliquidRateLimited:
        pass
    else:
        raise AssertionError('expected HyperliquidRateLimited')
    assert budget.paused_until == clock.now + 7


async def test_minute_report_and_last_hour():
    budget, clock = make_budget(1020, start=60.0)
    await budget.acquire(20, scheduler.P_FILLS)
    await budget.acquire(20, scheduler.P_LEDGER)
    assert budget.take_minute_report() is None      # the minute has not completed
    clock.now += 60
    assert budget.take_minute_report() == (40, 0)
    assert budget.take_minute_report() is None      # reported once
    assert budget.recent_weight() == 40
    assert budget.recent_weight(exclude_priority=scheduler.P_LEDGER) == 20
    clock.now += 180                                 # three quiet minutes
    hour = budget.last_hour()
    assert hour['minutes'] == 4 and hour['max'] == 40 and hour['avg'] == 10


def test_adaptive_intervals():
    # 50 accounts at 4 weight each: 200 per cycle -> 200 * 60 / 408 = 29.4 -> 30 s
    assert scheduler.poll_fast_sec(200, 1020, 20) == 30
    # 10 accounts: 40 * 60 / 408 = 5.9 -> floor 20 s
    assert scheduler.poll_fast_sec(40, 1020, 20) == 20
    # dormant accounts count a third: 50 dormant -> 66.7 -> 20 s
    assert scheduler.poll_fast_sec(50 * 4 / 3, 1020, 20) == 20
    # ledger: 50 accounts * 20 = 1000 per round; with 400 spare per minute -> 150 s -> floor 180
    assert scheduler.ledger_interval_sec(50, 400, 180, 600) == 180
    # 100 spare -> 600 s cap
    assert scheduler.ledger_interval_sec(50, 100, 180, 600) == 600
    assert scheduler.ledger_interval_sec(50, 0, 180, 600) == 600
    assert scheduler.ledger_interval_sec(0, 0, 180, 600) == 180
