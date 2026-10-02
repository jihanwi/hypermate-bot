"""Telegram command handlers. Wallet lookups go through the DB only (B1)."""

import logging
import re
import uuid

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.bot import texts
from hypermate.core import formatter
from hypermate.core.events import HYPERLIQUID, EventType, dedupe_key
from hypermate.core.formatter import h
from hypermate.core.pipeline import algo_source
from hypermate.core.numbers import to_decimal
from hypermate.db.repo import ADDED, ALIAS_EXISTS, Repo
from hypermate.venues.hyperliquid import adapter
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
        dexs = await _scan_dexs(context, address)
        message = texts.WALLET_ADDED.format(alias=h(alias))
        if dexs:
            message += texts.WALLET_ADDED_DEXS.format(dexs=h(", ".join(dexs)))
        await reply(update, message)
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
    lines = [formatter.format_list_line(alias, address, await _account_value(context, address))
             for alias, address in wallets]
    await reply(update, texts.LIST_HEADER + "\n" + "\n".join(lines))


async def _account_value(context: ContextTypes.DEFAULT_TYPE, address: str):
    """Account value from the last positions poll; live clearinghouseState only if not polled yet."""
    stored = to_decimal(await _repo(context).hl_account_value(address))
    if stored is not None:
        return stored
    state = await _perp_state_or_none(context, address)
    return formatter.account_value(state) if state is not None else None


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
    va = await _repo(context).hl_account_id(address)
    dexs = await _repo(context).get_dexs(va) if va is not None else []
    try:
        perp_states = {'': await _hl(context).clearinghouse_state(address)}
        for dex in dexs:
            perp_states[dex] = await _hl(context).clearinghouse_state(address, dex)
        spot_state = await _hl(context).spot_clearinghouse_state(address)
    except HyperliquidAPIError as e:
        logger.error(f"positions {address}: {e}")
        await reply(update, texts.HL_API_ERROR)
        return
    await reply(update, formatter.format_positions(alias, address, perp_states, spot_state))
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


async def _scan_dexs(context: ContextTypes.DEFAULT_TYPE, address: str) -> list[str]:
    """HIP-3 dex scan (spec 5.2) stored on the account; [] if the scan fails (the poller retries)."""
    repo = _repo(context)
    va = await repo.hl_account_id(address)
    if va is None:
        return []
    try:
        found = await adapter.scan_dexs(_hl(context), address)
    except HyperliquidAPIError as e:
        logger.error(f"HIP-3 dex scan failed for {address}: {e}")
        return await repo.get_dexs(va)
    dexs = sorted(set(await repo.get_dexs(va)) | set(found))
    await repo.set_dexs(va, dexs)
    now = now_ms()
    await repo.set_cursor(va, 'dex_scan', str(now), now)
    return dexs


async def _subscription_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, alias_text: str):
    subscription = await _repo(context).find_subscription(update.effective_user.id, alias_text)
    if subscription is None:
        await reply(update, texts.ALIAS_NOT_FOUND.format(alias=h(alias_text)))
    return subscription


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recent alias [n] (spec 9.6): last n events (default 10, max 30), including unsent ones."""
    if not context.args:
        await reply(update, texts.RECENT_USAGE)
        return
    args = list(context.args)
    count = 10
    if len(args) > 1 and args[-1].isdigit():
        count = max(1, min(30, int(args.pop())))
    subscription = await _subscription_or_reply(update, context, " ".join(args).strip())
    if subscription is None:
        return
    alias, address = subscription
    va = await _repo(context).hl_account_id(address)
    events = await _repo(context).recent_events(va, count) if va is not None else []
    await reply(update, formatter.format_recent(alias, events))


async def twap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/twap [alias] (spec 9.2a): active native TWAPs, plus synthetic (algo) executions."""
    repo = _repo(context)
    if context.args:
        subscription = await _subscription_or_reply(update, context, " ".join(context.args).strip())
        if subscription is None:
            return
        wallets = [subscription]
    else:
        wallets = await repo.list_subscriptions(update.effective_user.id)
    rows = []
    for alias, address in wallets:
        va = await repo.hl_account_id(address)
        if va is None:
            continue
        for state in (await repo.active_twaps(va)).values():
            rows.append({'kind': 'twap', 'alias': alias, 'address': address, 'state': state})
        for state in (await repo.active_algos(va)).values():
            start = await repo.get_event_by_key(dedupe_key(
                HYPERLIQUID, va, EventType.ALGO_START,
                algo_source(state['coin'], int(state['sign']), int(state['started_ms']))))
            verb, side = formatter.algo_label(int(state['sign']), None)
            if start is not None:
                verb, side = start['payload'].get('verb', verb), start['payload'].get('side', side)
            rows.append({'kind': 'algo', 'alias': alias, 'address': address, 'state': state,
                         'verb': verb, 'side': side})
    await reply(update, formatter.format_twap_list(rows, now_ms()))


async def rescan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rescan alias: Hyperliquid HIP-3 dex scan (other venues come in Phase 2)."""
    if not context.args:
        await reply(update, texts.RESCAN_USAGE)
        return
    subscription = await _subscription_or_reply(update, context, " ".join(context.args).strip())
    if subscription is None:
        return
    alias, address = subscription
    dexs = await _scan_dexs(context, address)
    await reply(update, texts.RESCAN_RESULT.format(
        alias=h(alias), dexs=(" + HIP-3 " + h(", ".join(dexs))) if dexs else ", no HIP-3 dex positions"))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
