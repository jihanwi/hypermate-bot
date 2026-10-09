"""Inline keyboards and their callbacks (spec 9.4 /settings, 9.5 /mute confirmation, 9.1 "Did you mean").

callback_data stays under Telegram's 64 bytes:
  s:<rowid|u>:v:<venue>:<0|1>      venue toggle (u = the user's defaults, /settings default)
  s:<rowid|u>:e:<key>:<0|1>        event category toggle
  s:<rowid|u>:n:<choice>           min_notional: auto | 100 | 1000 | 10000 | 100000 | off
  s:<rowid|u>:t:<0|1>              twap_progress (button hidden while no progress edits exist)
  m:all:<1|0>                      /mute without alias: confirm or cancel muting every wallet
  d:<command>:<alias>              "Did you mean" re-run
"""

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.bot import texts
from hypermate.core import settings as user_settings
from hypermate.core.formatter import h
from hypermate.db.repo import Repo
from hypermate.venues import base as venues
from hypermate.venues.base import NAMES
from hypermate.venues.hyperliquid.adapter import now_ms

logger = logging.getLogger(__name__)

SETTINGS_CALLBACK = 's:'
MUTE_CALLBACK = 'm:'
DYM_CALLBACK = 'd:'
USER_TARGET = 'u'

VENUE_ORDER = (venues.HYPERLIQUID, venues.LIGHTER, venues.RISEX, venues.ASTER)
EVENT_LABELS = {'position': 'Positions', 'liquidation': 'Liquidations', 'twap': 'TWAP / algo', 'spot': 'Spot',
                'transfer': 'Transfers', 'deposit_withdraw': 'Deposits / withdrawals', 'vault': 'Vaults',
                'account_class_transfer': 'Perp <-> spot moves'}
NOTIONAL_LABELS = {'auto': 'Auto', '100': '$100', '1000': '$1k', '10000': '$10k', '100000': '$100k', 'off': 'Off'}
SHOW_TWAP_PROGRESS = False       # stored only: no per-user progress edit logic yet (spec 9.4)


def _mark(on: bool) -> str:
    return '✅' if on else '⬜'


def settings_keyboard(settings: dict, target: str, active_venues: Optional[list[str]] = None) -> InlineKeyboardMarkup:
    """Toggle buttons for one subscription (target = rowid) or the user's defaults (target = 'u')."""
    rows: list[list[InlineKeyboardButton]] = []
    shown = [v for v in VENUE_ORDER if active_venues is None or v in active_venues] or list(VENUE_ORDER)
    venue_row = []
    for venue in shown:
        on = bool((settings.get('venues') or {}).get(venue, True))
        venue_row.append(InlineKeyboardButton(f"{_mark(on)} {NAMES.get(venue, venue)}",
                                              callback_data=f"{SETTINGS_CALLBACK}{target}:v:{venue}:{0 if on else 1}"))
    rows.append(venue_row)
    event_buttons = []
    for key in user_settings.EVENT_KEYS:
        on = bool((settings.get('events') or {}).get(key, False))
        event_buttons.append(InlineKeyboardButton(f"{_mark(on)} {EVENT_LABELS[key]}",
                                                  callback_data=f"{SETTINGS_CALLBACK}{target}:e:{key}:{0 if on else 1}"))
    rows.extend(event_buttons[i:i + 2] for i in range(0, len(event_buttons), 2))
    current = user_settings.min_notional_choice(settings)
    notional_row = [InlineKeyboardButton(('✅ ' if choice == current else '') + NOTIONAL_LABELS[choice],
                                         callback_data=f"{SETTINGS_CALLBACK}{target}:n:{choice}")
                    for choice in user_settings.MIN_NOTIONAL_CHOICES]
    rows.append(notional_row[:3])
    rows.append(notional_row[3:])
    if SHOW_TWAP_PROGRESS:
        on = bool(settings.get('twap_progress'))
        rows.append([InlineKeyboardButton(f"{_mark(on)} TWAP progress edits",
                                          callback_data=f"{SETTINGS_CALLBACK}{target}:t:{0 if on else 1}")])
    return InlineKeyboardMarkup(rows)


def apply_change(settings: dict, kind: str, parts: list[str]) -> Optional[dict]:
    """New settings dict for one callback, or None when the callback is malformed."""
    if kind == 'v' and len(parts) == 2 and parts[0] in VENUE_ORDER and parts[1] in ('0', '1'):
        return user_settings.set_path(settings, ('venues', parts[0]), parts[1] == '1')
    if kind == 'e' and len(parts) == 2 and parts[0] in user_settings.EVENT_KEYS and parts[1] in ('0', '1'):
        return user_settings.set_path(settings, ('events', parts[0]), parts[1] == '1')
    if kind == 'n' and len(parts) == 1 and parts[0] in user_settings.MIN_NOTIONAL_CHOICES:
        return user_settings.set_path(settings, ('min_notional',), user_settings.min_notional_from_choice(parts[0]))
    if kind == 't' and len(parts) == 1 and parts[0] in ('0', '1'):
        return user_settings.set_path(settings, ('twap_progress',), parts[0] == '1')
    return None


async def effective_settings(repo: Repo, user_id: int, subscription: Optional[dict]) -> dict:
    """What the keyboard shows: the merged view for a subscription, the user defaults merged over code
    defaults for /settings default."""
    user_level = await repo.user_settings(user_id)
    return user_settings.resolve(subscription['settings'] if subscription else None, user_level)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    repo: Repo = context.bot_data['repo']
    user_id = query.from_user.id
    parts = (query.data or '')[len(SETTINGS_CALLBACK):].split(':')
    if len(parts) < 3:
        await query.answer()
        return
    target, kind, rest = parts[0], parts[1], parts[2:]
    subscription = None
    if target != USER_TARGET:
        try:
            subscription = await repo.subscription_by_rowid(int(target))
        except (TypeError, ValueError):
            subscription = None
        if subscription is None:
            await query.answer(texts.SETTINGS_STALE)
            return
        if subscription['user_id'] != user_id:
            await query.answer(texts.NOT_YOURS)              # someone else's keyboard: ignored
            return
    current = await effective_settings(repo, user_id, subscription)
    changed = apply_change(current, kind, rest)
    if changed is None:
        await query.answer()
        return
    if subscription is not None:
        stored = apply_change(subscription['settings'], kind, rest)
        await repo.set_subscription_settings(subscription['rowid'], stored)
        active = [r['venue'] for r in await repo.venue_accounts_of(subscription['address']) if r['active']]
    else:
        stored = apply_change(await repo.user_settings(user_id), kind, rest)
        await repo.set_user_settings(user_id, stored, now_ms())
        active = None
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=settings_keyboard(changed, target, active))
    except Exception as e:
        if 'not modified' not in str(e).lower():
            logger.warning(f"settings keyboard edit failed for user {user_id}: {e}")


def settings_text(alias: Optional[str]) -> str:
    return texts.SETTINGS_DEFAULT_HEADER if alias is None else texts.SETTINGS_HEADER.format(alias=h(alias))


def mute_all_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(texts.MUTE_ALL_YES, callback_data=f"{MUTE_CALLBACK}all:1"),
                                  InlineKeyboardButton(texts.MUTE_ALL_NO, callback_data=f"{MUTE_CALLBACK}all:0")]])


async def mute_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """m:all:1 mutes every wallet of the user who pressed it (no end); m:all:0 cancels."""
    query = update.callback_query
    repo: Repo = context.bot_data['repo']
    await query.answer()
    if (query.data or '') == f"{MUTE_CALLBACK}all:1":
        from hypermate.core.pipeline import MUTE_FOREVER_MS
        count = await repo.mute_all(query.from_user.id, MUTE_FOREVER_MS, now_ms())
        text = texts.MUTE_ALL_DONE.format(count=count)
    else:
        text = texts.MUTE_ALL_CANCELLED
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"mute_all edit failed: {e}")
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)


def did_you_mean_keyboard(command: str, alias: str) -> Optional[InlineKeyboardMarkup]:
    data = f"{DYM_CALLBACK}{command}:{alias}"
    if len(data.encode()) > 64:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(texts.DYM_BUTTON.format(command=command, alias=alias),
                                                       callback_data=data)]])


async def answer_markup(update: Update, text: str, markup: Optional[InlineKeyboardMarkup]) -> None:
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
