"""Polling jobs and alert delivery."""

import asyncio
import logging
from typing import Callable, Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.core import events
from hypermate.core.events import EventType
from hypermate.core.formatter import (format_position_alert, format_spot_fill_message, format_transfer_message,
                                      format_twap_end, format_twap_start)
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter, twap
from hypermate.venues.hyperliquid.client import HyperliquidClient

logger = logging.getLogger(__name__)


async def deliver(bot: Bot, repo: Repo, account_key: int, render: Callable[[str], Optional[str]]) -> None:
    """Send one alert to every subscriber of the account, rendered with that subscriber's alias."""
    subscribers = await repo.subscribers(account_key)
    for i, (user_id, alias) in enumerate(subscribers):
        text = render(alias)
        if text is None:
            continue
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            logger.info(f"Sent alert to user {user_id} ({alias})")
        except Exception as e:
            logger.error(f"Failed to send alert to user {user_id}: {e}")
        if i < len(subscribers) - 1:
            await asyncio.sleep(1)


async def emit(bot: Bot, repo: Repo, account_key: int, event_type: EventType, source_id: str, ts_ms: int,
               payload: dict, render: Callable[[str], Optional[str]], delivery: str = events.SENT) -> bool:
    """Record the event; send it only if it is new and its delivery is 'sent'. Returns True if sent."""
    key = events.dedupe_key(events.HYPERLIQUID, account_key, event_type, source_id)
    event_id = await repo.record_event(key, account_key, event_type.value, ts_ms, payload, delivery,
                                       adapter.now_ms())
    if event_id is None:
        logger.info(f"Duplicate event {key}, not sent")
        return False
    if delivery != events.SENT:
        logger.info(f"Event {key} recorded as {delivery}, not sent")
        return False
    await deliver(bot, repo, account_key, render)
    return True


async def _cursor(repo: Repo, account_key: int, kind: str) -> int:
    """Stored cursor (B6). An account without one (e.g. just migrated) starts at now, no history replay."""
    value = await repo.get_cursor(account_key, kind)
    if value is None:
        now = adapter.now_ms()
        await repo.set_cursor(account_key, kind, str(now), now)
        logger.info(f"Started {kind} cursor for {account_key} at {now}")
        return now
    return int(value)


async def sync_twaps(bot: Bot, repo: Repo, client: HyperliquidClient, key: int, address: str,
                     active: dict[str, dict], web_data2: dict) -> dict[str, dict]:
    """Reconcile twap_active with webData2.twapStates; emit TWAP_START / TWAP_END.

    Returns the TWAP states that count for suppression this cycle: everything
    active before the sync plus everything reported now, so the last slices of
    a TWAP that ends in this cycle are still suppressed.
    """
    current = twap.parse_twap_states(web_data2)
    prices = twap.mark_prices(web_data2)
    now = adapter.now_ms()

    for twap_id, state in current.items():
        started = int(state.get('timestamp') or now)
        await repo.upsert_twap(key, twap_id, state, started)
        if twap_id not in active:
            logger.info(f"TWAP {twap_id} started: {state.get('side')} {state.get('coin')} for {address}")
            await emit(bot, repo, key, EventType.TWAP_START, twap_id, started,
                       {'twap_id': twap_id, 'state': state, 'mark_px': prices.get(state.get('coin'))},
                       lambda alias, s=state: format_twap_start(address, alias, s, prices.get(s.get('coin')), now))

    ended = [twap_id for twap_id in active if twap_id not in current]
    history = None
    if ended:
        try:
            history = await client.twap_history(address)
        except Exception as e:
            logger.error(f"twapHistory failed for {address}: {e}")
    for twap_id in ended:
        last_state = {k: v for k, v in active[twap_id].items() if k != twap.END_PENDING}
        entry = twap.final_entry(history, twap_id) if history is not None else None
        if entry is None and not active[twap_id].get(twap.END_PENDING):
            # Give twapHistory one more cycle to show the final status
            await repo.upsert_twap(key, twap_id, {**last_state, twap.END_PENDING: True},
                                   int(last_state.get('timestamp') or now))
            continue
        if entry is not None:
            final_state = entry.get('state') or last_state
            status = (entry.get('status') or {}).get('status', 'unknown')
            description = (entry.get('status') or {}).get('description')
            ended_ms = int(entry.get('time', now // 1000)) * 1000
        else:
            final_state, status, description, ended_ms = last_state, 'unknown', None, now
        logger.info(f"TWAP {twap_id} ended ({status}) for {address}")
        await emit(bot, repo, key, EventType.TWAP_END, twap_id, ended_ms,
                   {'twap_id': twap_id, 'status': status, 'description': description, 'state': final_state},
                   lambda alias, s=final_state, st=status, d=description, t=ended_ms:
                       format_twap_end(address, alias, s, st, d, t))
        await repo.delete_twap(key, twap_id)

    return {**active, **current}


async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: Repo = context.bot_data['repo']
    client: HyperliquidClient = context.bot_data['hl']
    accounts = await repo.tracked_accounts()
    for i, (key, address) in enumerate(accounts):
        try:
            previous = await repo.get_snapshot(key)
            alerts, current, account_value = await adapter.check_positions(client, address, previous)
            poll_ms = adapter.now_ms()
            await repo.save_snapshot(key, current, poll_ms, account_value)

            # TWAP (spec 5.2): webData2 only when positions moved or a TWAP is being tracked
            twap_states = await repo.active_twaps(key)
            if alerts or twap_states:
                try:
                    web = await client.web_data2(address)
                except Exception as e:
                    logger.error(f"webData2 failed for {address}: {e}")
                else:
                    twap_states = await sync_twaps(context.bot, repo, client, key, address, twap_states, web)

            for alert in alerts:
                suppressed = twap.is_suppressed(alert, twap_states.values())
                await emit(context.bot, repo, key, events.position_alert_type(alert),
                           f"{alert['coin']}:{poll_ms}", poll_ms, alert,
                           lambda alias, a=alert: format_position_alert(address, alias, a),
                           events.SUPPRESSED_TWAP if suppressed else events.SENT)
        except Exception as e:
            logger.error(f"Position check failed for {address}: {e}")
        if i < len(accounts) - 1:
            await asyncio.sleep(2)
    logger.info(f"Completed position check for {len(accounts)} wallets")


async def monitor_transfers_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: Repo = context.bot_data['repo']
    client: HyperliquidClient = context.bot_data['hl']
    accounts = await repo.tracked_accounts()
    for i, (key, address) in enumerate(accounts):
        try:
            cursor = await _cursor(repo, key, 'ledger')
            updates, new_cursor = await adapter.check_ledger(client, address, cursor)
            if new_cursor != cursor:
                await repo.set_cursor(key, 'ledger', str(new_cursor), adapter.now_ms())
            for update in updates:
                event_type = events.ledger_event_type(update, address)
                # Types the formatter does not show (rewards, commissions, ...) are not events
                if event_type is None or format_transfer_message(update, address, '') is None:
                    continue
                await emit(context.bot, repo, key, event_type,
                           f"{update.get('hash')}:{update['time']}", int(update['time']), update,
                           lambda alias, u=update: format_transfer_message(u, address, alias))

            cursor = await _cursor(repo, key, 'fills')
            fills, new_cursor = await adapter.check_spot_fills(client, address, cursor)
            if new_cursor != cursor:
                await repo.set_cursor(key, 'fills', str(new_cursor), adapter.now_ms())
            for fill in fills:
                await emit(context.bot, repo, key, events.spot_fill_event_type(fill),
                           str(fill.get('tid', f"{fill['coin']}:{fill['time']}:{fill.get('sz')}")),
                           int(fill['time']), fill,
                           lambda alias, f=fill: format_spot_fill_message(f, address, alias))
        except Exception as e:
            logger.error(f"Transfer check failed for {address}: {e}")
        if i < len(accounts) - 1:
            await asyncio.sleep(2)
    logger.info(f"Completed transfer check for {len(accounts)} wallets")
