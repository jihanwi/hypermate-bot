"""Telegram command handlers. Wallet lookups go through the DB only (B1)."""

import logging
import re
import uuid
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.bot import texts
from hypermate.config import Config
from hypermate.core import formatter, poller, related
from hypermate.core.venues import resolve_summary, resolve_wallet
from hypermate.venues import base as venues
from hypermate.venues.base import VenueAccount, as_clearinghouse_state
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


def _venues(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """{venue: adapter}; empty in tests that only wire the HL client."""
    return context.bot_data.get('venues') or {}


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
        message = await _add(context, user_id, address, alias)
    except Exception as e:
        await reply_internal_error(update, f"add_wallet user={user_id}", e)
        return
    await reply(update, message)


async def _add(context: ContextTypes.DEFAULT_TYPE, user_id: int, address: str, alias: str) -> str:
    """The /add flow (also used by the /related Track button). Returns the reply text."""
    result = await _repo(context).add_subscription(user_id, address, alias, now_ms())
    if result == ADDED:
        logger.info(f"User {user_id} added wallet {address} as '{alias}'")
        summary = await _resolve_venues(context, address)
        message = texts.WALLET_ADDED.format(alias=h(alias))
        if summary:
            message += texts.WALLET_VENUES.format(venues=h(summary))
        return message
    if result == ALIAS_EXISTS:
        return texts.ALIAS_EXISTS
    return texts.ADDRESS_EXISTS


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
    """Account value summed over the wallet's active venues from the last polls (spec 6.1);
    live clearinghouseState only if nothing was polled yet."""
    stored = to_decimal(await _repo(context).account_value_sum(address))
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
    rows = await _repo(context).venue_accounts_of(address)
    hl_active = any(r['venue'] == venues.HYPERLIQUID and r['active'] for r in rows) or not _venues(context)
    va = await _repo(context).hl_account_id(address)
    dexs = await _repo(context).get_dexs(va) if va is not None else []
    perp_states, spot_state = None, {}
    if hl_active:
        try:
            perp_states = {'': await _hl(context).clearinghouse_state(address)}
            for dex in dexs:
                perp_states[dex] = await _hl(context).clearinghouse_state(address, dex)
            spot_state = await _hl(context).spot_clearinghouse_state(address)
        except HyperliquidAPIError as e:
            logger.error(f"positions {address}: {e}")
            await reply(update, texts.HL_API_ERROR)
            return
    sections = []
    for row in rows:
        adapter_obj = _venues(context).get(row['venue'])
        if row['venue'] == venues.HYPERLIQUID or not row['active'] or adapter_obj is None:
            continue
        account = VenueAccount(row['venue'], row['account_ref'], address, row['key'])
        try:
            snap = await adapter_obj.snapshot(account)
        except Exception as e:
            logger.error(f"positions {row['venue']} {address}#{row['account_ref']}: {e}")
            continue
        title = f"{venues.NAMES.get(row['venue'], row['venue'])} #{row['account_ref']}" \
            if row['venue'] == venues.LIGHTER else venues.NAMES.get(row['venue'], row['venue'])
        sections.append((title, as_clearinghouse_state(snap)))
    dust = to_decimal(Config.DEFAULT_SETTINGS['dust_notional_usd'])
    await reply(update, formatter.format_positions(alias, address, perp_states, spot_state, dust, sections))
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


async def _resolve_venues(context: ContextTypes.DEFAULT_TYPE, address: str) -> str:
    """Run every adapter's resolve (spec 6.1) and the HL HIP-3 dex scan; returns the summary line.
    Without adapters (HL-only wiring) only the dex scan runs and the summary names the dexs."""
    adapters = _venues(context)
    dexs = await _scan_dexs(context, address)
    if not adapters:
        return ("HL ✅" + (f" ({', '.join(dexs)})" if dexs else "")) if dexs else ""
    try:
        results = await resolve_wallet(_repo(context), adapters, address, now_ms())
    except Exception as e:
        logger.error(f"resolve failed for {address}: {e}")
        return ""
    return resolve_summary(results, dexs)


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
    algos = list((await _repo(context).active_algos(va)).values()) if va is not None else []
    await reply(update, formatter.format_recent(alias, events, algos))


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
    if _venues(context):
        summary = await _resolve_venues(context, address)
        await reply(update, texts.RESCAN_VENUES.format(alias=h(alias), venues=h(summary or "no venue answered")))
        return
    dexs = await _scan_dexs(context, address)
    await reply(update, texts.RESCAN_RESULT.format(
        alias=h(alias), dexs=(" + HIP-3 " + h(", ".join(dexs))) if dexs else ", no HIP-3 dex positions"))


TRACK_CALLBACK = 'rel:'


def track_keyboard(buttons: list[tuple[str, int]]) -> Optional[InlineKeyboardMarkup]:
    """One [Track as alias-N] button per row; callback data is the wallet_links rowid (under 64 bytes)."""
    if not buttons:
        return None
    rows = [[InlineKeyboardButton(texts.TRACK_BUTTON.format(alias=label), callback_data=f"{TRACK_CALLBACK}{row_id}")]
            for label, row_id in buttons]
    return InlineKeyboardMarkup(rows)


async def related_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/related alias [refresh]: linked wallets (spec 7), cached 24 h in wallet_links."""
    args = list(context.args)
    refresh = bool(args) and args[-1].lower() == 'refresh'
    if refresh:
        args = args[:-1]
    if not args:
        await reply(update, texts.RELATED_USAGE)
        return
    subscription = await _subscription_or_reply(update, context, " ".join(args).strip())
    if subscription is None:
        return
    alias, address = subscription
    repo, hl = _repo(context), _hl(context)
    wallet_id = await repo.wallet_id(address)
    cached = (not refresh and wallet_id is not None
              and (await repo.links_discovered_at(wallet_id) or 0) > now_ms() - related.LINKS_TTL_MS)
    placeholder = None
    if not cached:
        placeholder = await update.message.reply_text(texts.RELATED_SEARCHING.format(alias=h(alias)),
                                                      parse_mode=ParseMode.HTML)
    try:
        links, discovery = await related.links_for(hl, repo, address, now_ms(), refresh)
    except Exception as e:
        await reply_internal_error(update, f"related user={update.effective_user.id}", e)
        return
    text, buttons = formatter.format_related(alias, address, links, now_ms(),
                                             discovery.weight if discovery else None)
    keyboard = track_keyboard(buttons)
    if placeholder is not None:
        await placeholder.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                    disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                        disable_web_page_preview=True)


async def track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline button from /related: add the row's address as <alias>-N through the /add flow."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        row_id = int(query.data[len(TRACK_CALLBACK):])
        link = await _repo(context).link_by_row(row_id)
    except (ValueError, TypeError):
        link = None
    if link is None:
        await query.message.reply_text(texts.TRACK_EXPIRED, parse_mode=ParseMode.HTML)
        return
    label = next((b.text for row in (query.message.reply_markup.inline_keyboard if query.message.reply_markup else [])
                  for b in row if b.callback_data == query.data), None)
    alias = label[len("Track as "):] if label and label.startswith("Track as ") else None
    if alias is None:
        base = await _repo(context).list_subscriptions(user_id)
        alias = f"related-{row_id}" if not base else f"{base[0][0]}-{row_id}"
    try:
        message = await _add(context, user_id, link['related_address'], alias)
    except Exception as e:
        logger.error(f"track_callback user={user_id}: {e}", exc_info=e)
        await query.message.reply_text(texts.INTERNAL_ERROR.format(error_id='track'), parse_mode=ParseMode.HTML)
        return
    await query.message.reply_text(message, parse_mode=ParseMode.HTML)


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/health (admins only, spec 11): polling state, weight budget, DB size, uptime."""
    if update.effective_user.id not in Config.ADMIN_USER_IDS:
        await reply(update, texts.ADMIN_ONLY)
        return
    repo = _repo(context)
    algo_rows = await repo.all_active_algos()
    modes = {}
    accounts = {key: address for key, address in await repo.tracked_accounts()}
    for key, mode in (await repo.multi_algo_modes()).items():
        address = accounts.get(key)
        if address is None:
            continue
        subscribers = await repo.subscribers(key)
        modes[address] = {'entered_ms': mode['entered_ms'],
                          'count': sum(1 for r in algo_rows if r['address'] == address),
                          'alias': subscribers[0][1] if subscribers else None}
    report = poller.health_report(context, await repo.counts(), repo.path,
                                  await repo.db_stats(now_ms()), algo_rows, modes,
                                  await poller.venue_health(context))
    await reply(update, formatter.format_health(report, now_ms()))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
