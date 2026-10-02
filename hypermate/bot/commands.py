"""Telegram command handlers. Wallet lookups go through the DB only (B1)."""

import logging
import re
import uuid

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.bot import texts
from hypermate.core import formatter
from hypermate.core.formatter import h
from hypermate.db.repo import ADDED, ALIAS_EXISTS, Repo
from hypermate.venues.hyperliquid.adapter import now_ms
from hypermate.venues.hyperliquid.client import HyperliquidAPIError, HyperliquidClient

logger = logging.getLogger(__name__)


def _repo(context: ContextTypes.DEFAULT_TYPE) -> Repo:
    return context.bot_data['repo']


def _hl(context: ContextTypes.DEFAULT_TYPE) -> HyperliquidClient:
    return context.bot_data['hl']


async def reply(update: Update, text: str) -> None:
    """Reply in HTML, split into several messages if over Telegram's 4096 character limit."""
    for chunk in formatter.split_message(text):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def _perp_state_or_none(context: ContextTypes.DEFAULT_TYPE, address: str):
    try:
        return await _hl(context).clearinghouse_state(address)
    except HyperliquidAPIError as e:
        logger.error(f"clearinghouseState {address}: {e}")
        return None


async def reply_internal_error(update: Update, where: str, error: Exception) -> None:
    error_id = uuid.uuid4().hex[:6]
    logger.error(f"[{error_id}] {where}: {error}", exc_info=error)
    await reply(update, texts.INTERNAL_ERROR.format(error_id=error_id))


def is_valid_wallet_address(address: str) -> bool:
    """0x-prefixed, 40 hex characters."""
    return bool(address) and bool(re.fullmatch(r'0x[0-9a-fA-F]{40}', address))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, texts.WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, texts.HELP)


async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2 or not is_valid_wallet_address(context.args[0].strip()):
        await reply(update, texts.ADD_USAGE)
        return
    address = context.args[0].strip().lower()
    alias = " ".join(context.args[1:]).strip()
    user_id = update.effective_user.id
    try:
        result = await _repo(context).add_subscription(user_id, address, alias, now_ms())
    except Exception as e:
        await reply_internal_error(update, f"add_wallet user={user_id}", e)
        return
    if result == ADDED:
        logger.info(f"User {user_id} added wallet {address} as '{alias}'")
        await reply(update, texts.WALLET_ADDED.format(alias=h(alias)))
    elif result == ALIAS_EXISTS:
        await reply(update, texts.ALIAS_EXISTS)
    else:
        await reply(update, texts.ADDRESS_EXISTS)


async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        wallets = await _repo(context).list_subscriptions(user_id)
    except Exception as e:
        await reply_internal_error(update, f"list_wallets user={user_id}", e)
        return
    if not wallets:
        await reply(update, texts.NO_WALLETS)
        return
    lines = [formatter.format_list_line(alias, address, await _perp_state_or_none(context, address))
             for alias, address in wallets]
    await reply(update, texts.LIST_HEADER + "\n" + "\n".join(lines))


async def remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, texts.REMOVE_USAGE)
        return
    alias = " ".join(context.args).strip()
    user_id = update.effective_user.id
    try:
        removed = await _repo(context).remove_subscription(user_id, alias)
    except Exception as e:
        await reply_internal_error(update, f"remove_wallet user={user_id}", e)
        return
    if removed:
        logger.info(f"User {user_id} removed wallet '{alias}'")
        await reply(update, texts.WALLET_REMOVED.format(alias=h(alias)))
    else:
        await reply(update, texts.ALIAS_NOT_FOUND.format(alias=h(alias)))


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await positions_summary(update, context)
        return
    subscription = await _repo(context).find_subscription(user_id, " ".join(context.args).strip())
    if subscription is None:
        await reply(update, texts.ALIAS_NOT_FOUND.format(alias=h(" ".join(context.args))))
        return
    alias, address = subscription
    try:
        perp_state = await _hl(context).clearinghouse_state(address)
        spot_state = await _hl(context).spot_clearinghouse_state(address)
    except HyperliquidAPIError as e:
        logger.error(f"positions {address}: {e}")
        await reply(update, texts.HL_API_ERROR)
        return
    await reply(update, formatter.format_positions(alias, address, perp_state, spot_state))
    logger.info(f"User {user_id} checked positions for {address} ({alias})")


async def positions_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/positions without an alias: one line per tracked wallet."""
    wallets = await _repo(context).list_subscriptions(update.effective_user.id)
    if not wallets:
        await reply(update, texts.NO_WALLETS)
        return
    lines = [formatter.format_positions_summary_line(alias, address, await _perp_state_or_none(context, address))
             for alias, address in wallets]
    await reply(update, texts.POSITIONS_SUMMARY_HEADER + "\n" + "\n".join(lines))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, texts.STATS_USAGE)
        return
    user_id = update.effective_user.id
    subscription = await _repo(context).find_subscription(user_id, " ".join(context.args).strip())
    if subscription is None:
        await reply(update, texts.ALIAS_NOT_FOUND.format(alias=h(" ".join(context.args))))
        return
    alias, address = subscription
    try:
        portfolio = await _hl(context).portfolio(address)
    except HyperliquidAPIError as e:
        logger.error(f"stats {address}: {e}")
        await reply(update, texts.HL_API_ERROR)
        return
    message = formatter.format_stats(alias, address, portfolio)
    await reply(update, message if message is not None else texts.STATS_NOT_AVAILABLE)
    logger.info(f"User {user_id} checked stats for {address} ({alias})")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
