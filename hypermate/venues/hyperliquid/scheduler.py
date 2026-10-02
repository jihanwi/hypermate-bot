"""Weight budget for the Hyperliquid info API (spec 3.5).

HL allows 1200 weight per minute per IP. Every request waits for tokens from one
bucket that refills at HL_WEIGHT_BUDGET per minute (default 1020, 15% margin).
The bucket holds at most a sixth of a minute's budget, so any rolling 60 s window
sees at most budget + budget/6 = 1190 weight.

When several requests wait, the one with the best priority goes first:
snapshot polling > TWAP webData2 > fills > ledger. A 429 pauses the whole
queue for Retry-After seconds (30 s without the header).
"""

import asyncio
import heapq
import itertools
import logging
import math
import time
from collections import deque
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Priorities, lower goes first (spec 3.5)
P_SNAPSHOT = 0
P_TWAP = 1
P_FILLS = 2
P_LEDGER = 3

# (weight, priority) per request type. Weights from the HL docs (spec 3.5 table):
# clearinghouseState / spotClearinghouseState 2, userRole 60, everything else 20.
REQUESTS = {
    'clearinghouseState': (2, P_SNAPSHOT),
    'spotClearinghouseState': (2, P_SNAPSHOT),
    'webData2': (20, P_TWAP),
    'twapHistory': (20, P_TWAP),
    'userFillsByTime': (20, P_FILLS),
    'userNonFundingLedgerUpdates': (20, P_LEDGER),
}
DEFAULT_REQUEST = (20, P_TWAP)          # perpDexs, spotMeta, portfolio
# Responses of these types cost 1 more per 20 items returned
PER_ITEM_TYPES = ('userFillsByTime', 'twapHistory')

DEFAULT_RETRY_AFTER_SEC = 30
HISTORY_MINUTES = 60


def request_cost(request_type: str) -> tuple[int, int]:
    return REQUESTS.get(request_type, DEFAULT_REQUEST)


def item_weight(request_type: str, items: int) -> int:
    return items // 20 if request_type in PER_ITEM_TYPES else 0


class WeightBudget:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        self.per_minute = per_minute
        self.rate = per_minute / 60
        self.capacity = per_minute / 6
        self.clock = clock
        self.sleep = sleep
        self.tokens = self.capacity
        self.updated = clock()
        self.paused_until = 0.0
        self._waiters: list[tuple[int, int]] = []
        self._seq = itertools.count()
        # per-minute stats: (minute index, total weight, 429s, {priority: weight})
        self._current_minute = self._minute()
        self._current = self._empty()
        self.history: deque[tuple] = deque(maxlen=HISTORY_MINUTES)
        self._reported_minute: Optional[int] = None

    # Stats --------------------------------------------------------------------

    def _minute(self) -> int:
        return int(self.clock() // 60)

    @staticmethod
    def _empty() -> list:
        return [0, 0, {}]

    def _roll(self) -> None:
        minute = self._minute()
        if minute != self._current_minute:
            self.history.append((self._current_minute, *self._current))
            # minutes without any request count as 0
            for empty in range(self._current_minute + 1, min(minute, self._current_minute + HISTORY_MINUTES + 1)):
                self.history.append((empty, *self._empty()))
            self._current_minute = minute
            self._current = self._empty()

    def _record(self, weight: int = 0, rate_limited: int = 0, priority: int = P_TWAP) -> None:
        self._roll()
        self._current[0] += weight
        self._current[1] += rate_limited
        if weight:
            self._current[2][priority] = self._current[2].get(priority, 0) + weight

    def recent_weight(self, exclude_priority: Optional[int] = None) -> int:
        """Weight of the last completed minute, optionally without one priority class."""
        self._roll()
        if not self.history:
            return 0
        _, total, _, by_priority = self.history[-1]
        return total - (by_priority.get(exclude_priority, 0) if exclude_priority is not None else 0)

    def take_minute_report(self) -> Optional[tuple[int, int]]:
        """(weight, 429s) of the last completed minute, once per minute (for the INFO log line)."""
        self._roll()
        if not self.history or self._reported_minute == self.history[-1][0]:
            return None
        self._reported_minute = self.history[-1][0]
        return self.history[-1][1], self.history[-1][2]

    def last_hour(self) -> dict:
        """Average and max weight per minute and 429 count over the completed minutes of the last hour."""
        self._roll()
        weights = [entry[1] for entry in self.history]
        return {
            'minutes': len(weights),
            'avg': (sum(weights) // len(weights)) if weights else 0,
            'max': max(weights) if weights else 0,
            'rate_limited': sum(entry[2] for entry in self.history) + self._current[1],
        }

    # Bucket ---------------------------------------------------------------------

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def _wait_time(self, weight: int) -> float:
        now = self.clock()
        if now < self.paused_until:
            return self.paused_until - now
        # a request bigger than the bucket goes when the bucket is full
        need = min(weight, self.capacity) - self.tokens
        if need <= 1e-6:
            return 0.0
        return max(0.01, need / self.rate)      # floor: float refills never get stuck below the target

    async def acquire(self, weight: int, priority: int) -> None:
        ticket = (priority, next(self._seq))
        heapq.heappush(self._waiters, ticket)
        try:
            while True:
                self._refill()
                if self._waiters[0] == ticket:
                    wait = self._wait_time(weight)
                    if wait <= 0:
                        self.tokens -= weight
                        self._record(weight, priority=priority)
                        return
                else:
                    # someone with a better priority (or earlier) is first; check again shortly
                    wait = max(self._wait_time(weight), 0.05)
                await self.sleep(min(wait, 1.0))
        finally:
            self._waiters.remove(ticket)
            heapq.heapify(self._waiters)

    def charge(self, weight: int, priority: int = P_TWAP) -> None:
        """Extra weight known only after the response (per-item cost). May leave the bucket in debt."""
        if weight > 0:
            self._refill()
            self.tokens -= weight
            self._record(weight, priority=priority)

    def rate_limited(self, retry_after: Optional[float]) -> float:
        seconds = retry_after if retry_after and retry_after > 0 else DEFAULT_RETRY_AFTER_SEC
        self.paused_until = max(self.paused_until, self.clock() + seconds)
        self._record(rate_limited=1)
        logger.warning(f"HL returned 429, pausing the HL queue for {seconds:g}s")
        return seconds


def poll_fast_sec(cycle_weight: float, budget: int, minimum: int) -> int:
    """Spec 3.5: snapshot polling may use at most 40% of the budget.

    cycle_weight is the weight of one full fast cycle (dormant accounts count 1/3,
    they are polled every third cycle). P = max(POLL_FAST, ceil(w * 60 / (budget * 0.4))).
    """
    return max(minimum, math.ceil(cycle_weight * 60 / (budget * 0.4)))


def ledger_interval_sec(n_accounts: int, spare_per_minute: float, base: int, maximum: int) -> int:
    """Ledger polls every `base` seconds, stretched up to `maximum` when the spare budget is short."""
    if n_accounts == 0:
        return base
    if spare_per_minute <= 0:
        return maximum
    needed = math.ceil(n_accounts * request_cost('userNonFundingLedgerUpdates')[0] * 60 / spare_per_minute)
    return min(maximum, max(base, needed))
