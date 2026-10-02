"""Polling jobs and alert delivery."""

import asyncio
import logging
from typing import Callable, Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.core.formatter import format_position_alert, format_spot_fill_message, format_transfer_message
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter
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


async def _cursor(repo: Repo, account_key: int, kind: str) -> int:
    """Stored cursor (B6). An account without one (e.g. just migrated) starts at now, no history replay."""
    value = await repo.get_cursor(account_key, kind)
    if value is None:
        now = adapter.now_ms()
        await repo.set_cursor(account_key, kind, str(now), now)
        logger.info(f"Started {kind} cursor for {account_key} at {now}")
        return now
    return int(value)


async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: Repo = context.bot_data['repo']
    client: HyperliquidClient = context.bot_data['hl']
    accounts = await repo.tracked_accounts()
    for i, (key, address) in enumerate(accounts):
        try:
            previous = await repo.get_snapshot(key)
            alerts, current, account_value = await adapter.check_positions(client, address, previous)
            await repo.save_snapshot(key, current, adapter.now_ms(), account_value)
            for alert in alerts:
                await deliver(context.bot, repo, key,
                              lambda alias, a=alert: format_position_alert(address, alias, a))
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
                await deliver(context.bot, repo, key,
                              lambda alias, u=update: format_transfer_message(u, address, alias))

            cursor = await _cursor(repo, key, 'fills')
            fills, new_cursor = await adapter.check_spot_fills(client, address, cursor)
            if new_cursor != cursor:
                await repo.set_cursor(key, 'fills', str(new_cursor), adapter.now_ms())
            for fill in fills:
                await deliver(context.bot, repo, key,
                              lambda alias, f=fill: format_spot_fill_message(f, address, alias))
        except Exception as e:
            logger.error(f"Transfer check failed for {address}: {e}")
        if i < len(accounts) - 1:
            await asyncio.sleep(2)
    logger.info(f"Completed transfer check for {len(accounts)} wallets")
