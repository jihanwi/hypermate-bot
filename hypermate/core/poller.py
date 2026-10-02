"""The HL polling loop (spec 3.5): one fast cycle per POLL_FAST seconds.

Per account and cycle:
  1. clearinghouseState (main dex + HIP-3 dexs) and spotClearinghouseState, weight 2 each.
  2. userFillsByTime only when a position size or a spot balance changed, or the
     account has an active native TWAP or algo. Otherwise the fills call is skipped.
  3. webData2 when positions changed or a TWAP is being tracked (as before).
  4. ledger on its own cadence (POLL_LEDGER, stretched up to 600 s when the budget is short).
Dormant accounts (no activity for 7 days) are polled every third cycle and return to the
fast tier as soon as activity is seen.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from telegram.ext import ContextTypes

from hypermate.config import Config
from hypermate.core import pipeline
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter, scheduler
from hypermate.venues.hyperliquid.client import HyperliquidClient

logger = logging.getLogger(__name__)

HL = 'hyperliquid'


@dataclass
class PollState:
    """In-memory scheduling state (rebuilt on restart; cursors and snapshots live in the DB)."""
    started_ms: int
    cycle: int = 0
    last_cycle_ms: int = 0
    poll_fast_sec: int = Config.POLL_FAST_SEC
    ledger_interval_sec: int = Config.POLL_LEDGER_SEC
    last_ledger_ms: dict[int, int] = field(default_factory=dict)
    last_poll_ms: dict[str, int] = field(default_factory=dict)      # venue -> last completed cycle
    dormant: set[int] = field(default_factory=set)
    accounts: int = 0


def get_state(context) -> PollState:
    """context is a CallbackContext or the Application (both expose bot_data)."""
    state = context.bot_data.get('poll_state')
    if state is None:
        state = context.bot_data['poll_state'] = PollState(started_ms=adapter.now_ms())
    return state


def _is_dormant(last_activity_ms: Optional[int], now: int) -> bool:
    return last_activity_ms is not None and now - last_activity_ms >= Config.DORMANT_AFTER_MS


def _snapshot_weight(dexs: list[str]) -> int:
    """Weight of one account's first-stage poll: main dex + each HIP-3 dex + spot, 2 each."""
    return scheduler.request_cost('clearinghouseState')[0] * (1 + len(dexs)) \
        + scheduler.request_cost('spotClearinghouseState')[0]


async def poll_account(context: ContextTypes.DEFAULT_TYPE, key: int, address: str, state: PollState,
                       ledger_due: bool) -> int:
    """One fast-cycle pass over one account. Returns the first-stage weight it used."""
    repo: Repo = context.bot_data['repo']
    client: HyperliquidClient = context.bot_data['hl']
    bot = context.bot
    now = adapter.now_ms()

    dexs = await pipeline._ensure_dex_scan(repo, client, key, address)
    previous = await repo.get_snapshot(key)
    previous_spot = await repo.get_spot_snapshot(key)
    current, account_value, _ = await adapter.fetch_snapshot(client, address, dexs)
    spot = adapter.parse_spot_balances(await client.spot_clearinghouse_state(address))
    positions_changed = previous is not None and adapter.snapshot_changed(previous, current)
    spot_moved = adapter.spot_changed(previous_spot, spot)
    if previous is None:
        logger.info(f"Baseline snapshot for {address}: {sum(len(p) for p in current.values())} positions")
    await repo.save_snapshot(key, current, now, account_value, spot)

    twap_states = await repo.active_twaps(key)
    if positions_changed or twap_states:
        try:
            web = await client.web_data2(address)
        except Exception as e:
            logger.error(f"webData2 failed for {address}: {e}")
        else:
            twap_states = await pipeline.sync_twaps(bot, repo, client, key, address, twap_states, web)

    # Second stage (spec 3.5): fills only when something moved, or while a TWAP / algo is tracked
    active = bool(twap_states) or bool(await repo.active_algos(key))
    activity = positions_changed or spot_moved
    if activity or active:
        fetched = await pipeline.poll_fills(bot, repo, client, key, address)
        activity = activity or fetched
    if ledger_due:
        state.last_ledger_ms[key] = now
        if await pipeline.poll_ledger(bot, repo, client, key, address):
            activity = True
    await pipeline.maintain_algos(bot, repo, key, address, adapter.now_ms())

    if activity:
        await repo.touch_activity(key, now)
        if key in state.dormant:
            state.dormant.discard(key)
            logger.info(f"{address} is active again, back to fast polling")
    return _snapshot_weight(dexs)


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every POLL_FAST_SEC; skips the cycle while the computed interval has not elapsed."""
    repo: Repo = context.bot_data['repo']
    state = get_state(context)
    now = adapter.now_ms()
    if now - state.last_cycle_ms < state.poll_fast_sec * 1000 - 500:
        return
    state.last_cycle_ms = now
    state.cycle += 1

    accounts = await repo.tracked_accounts_with_activity()
    state.accounts = len(accounts)
    state.dormant = {key for key, _, last in accounts if _is_dormant(last, now)}
    due = [(key, address) for key, address, _ in accounts
           if key not in state.dormant or state.cycle % 3 == key % 3]
    budget: Optional[scheduler.WeightBudget] = context.bot_data.get('budget')

    cycle_weight = 0
    polled = 0
    for i, (key, address) in enumerate(due):
        ledger_due = now - state.last_ledger_ms.get(key, 0) >= state.ledger_interval_sec * 1000
        try:
            cycle_weight += await poll_account(context, key, address, state, ledger_due)
            polled += 1
        except Exception as e:
            logger.error(f"Poll failed for {address}: {e}")
        if i < len(due) - 1 and budget is None:
            await asyncio.sleep(2)   # without a budget keep the Phase 0 pacing
    state.last_poll_ms[HL] = adapter.now_ms()

    # Adaptive intervals (spec 3.5): fast polling may use 40% of the budget; ledger takes the spare
    full_cycle_weight = sum(_snapshot_weight([]) * (1 / 3 if key in state.dormant else 1)
                            for key, _, _ in accounts) if accounts else 0
    full_cycle_weight = max(full_cycle_weight, cycle_weight)
    state.poll_fast_sec = scheduler.poll_fast_sec(full_cycle_weight, Config.HL_WEIGHT_BUDGET, Config.POLL_FAST_SEC)
    spare = Config.HL_WEIGHT_BUDGET - full_cycle_weight * 60 / state.poll_fast_sec
    if budget is not None:
        spare -= budget.recent_weight(exclude_priority=scheduler.P_LEDGER)
    state.ledger_interval_sec = scheduler.ledger_interval_sec(
        len(accounts), spare, Config.POLL_LEDGER_SEC, Config.POLL_LEDGER_MAX_SEC)
    logger.info(f"Cycle {state.cycle}: polled {polled}/{len(accounts)} accounts "
                f"({len(state.dormant)} dormant), fast={state.poll_fast_sec}s ledger={state.ledger_interval_sec}s")


async def weight_log_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One INFO line per minute: weight used and 429s in the last completed minute (spec 3.5)."""
    budget: Optional[scheduler.WeightBudget] = context.bot_data.get('budget')
    if budget is None:
        return
    report = budget.take_minute_report()
    if report is not None:
        weight, rate_limited = report
        logger.info(f"HL weight last minute: {weight}/{budget.per_minute}, 429s: {rate_limited}")


def health_report(context: ContextTypes.DEFAULT_TYPE, counts: dict, db_path: str) -> dict:
    state = get_state(context)
    budget: Optional[scheduler.WeightBudget] = context.bot_data.get('budget')
    try:
        db_bytes = os.path.getsize(db_path)
    except OSError:
        db_bytes = None
    return {
        'last_poll_ms': dict(state.last_poll_ms),
        'accounts': state.accounts,
        'dormant': len(state.dormant),
        'poll_fast_sec': state.poll_fast_sec,
        'ledger_interval_sec': state.ledger_interval_sec,
        'weight': budget.last_hour() if budget is not None else None,
        'twaps': counts.get('twaps', 0),
        'algos': counts.get('algos', 0),
        'db_bytes': db_bytes,
        'uptime_ms': adapter.now_ms() - state.started_ms,
        'started_ms': state.started_ms,
    }
