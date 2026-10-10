"""Daily digest (spec 12 backlog item, feat/daily-digest): one message per user per day with the last
24 h of every wallet the user tracks, on every venue, muted wallets included and user filters ignored.

Data: the events table (24 h window), the current snapshots, and account_value_daily (the value stored
at 00:00 UTC) for the 24 h change. Sending: chunks under the Telegram limit, one second between users.
Schedule: an hourly job sends to every user whose hour has come today (KST) and who has not received
today's digest yet (users.last_digest_day), so a restart never sends twice and a missed hour is caught up.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.core import settings as user_settings
from hypermate.core.events import EventType
from hypermate.core.formatter import KST, base_coin, compact_usd, h, split_message
from hypermate.core.numbers import to_decimal
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid import adapter as hl_adapter

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
WINDOW_MS = 24 * 3600 * 1000
MAX_WALLET_LINES = 6
EXPOSURE_TOP = 5
DIGEST_HOURS = (6, 9, 12, 18, 21)
POSITION_TYPES = {EventType.POSITION_OPEN.value, EventType.POSITION_INCREASE.value, EventType.POSITION_DECREASE.value,
                  EventType.POSITION_CLOSE.value, EventType.POSITION_FLIP.value, EventType.LIQUIDATION.value}
ADDING = {EventType.POSITION_OPEN.value, EventType.POSITION_INCREASE.value}


def kst_day(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=KST).strftime('%Y-%m-%d')


def utc_day(now_ms: int, days_back: int = 0) -> str:
    return (datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')


def digest_settings(user_level: dict) -> dict:
    merged = user_settings.resolve(None, user_level)
    return merged.get('digest') or user_settings.DEFAULTS['digest']


def _signed_compact(value: Decimal) -> str:
    return f"{'+' if value > 0 else '-' if value < 0 else ''}{compact_usd(abs(value))}"


def _event_notional(payload: dict) -> Decimal:
    chain = payload.get('chain') or {}
    return to_decimal(chain.get('notional_usd')) or to_decimal(payload.get('notional_usd')) or ZERO


def _event_realized(payload: dict) -> Decimal:
    chain = payload.get('chain') or {}
    if chain.get('realized_pnl') is not None:
        return to_decimal(chain.get('realized_pnl')) or ZERO
    return to_decimal(payload.get('realized_pnl')) or ZERO


def wallet_activity(events: list[dict], snapshot: Optional[dict], algos: dict) -> dict:
    """Per-coin net notional change and realized PnL from the 24 h events (chain bases only, merged
    orders carry chain_base), the largest single order, and the algo totals."""
    coins: dict[str, dict] = {}
    realized_total = ZERO
    largest: Optional[tuple[Decimal, dict]] = None
    for e in events:
        p = e['payload']
        if e['type'] not in POSITION_TYPES or p.get('chain_base') is not None:
            continue
        coin = p.get('coin') or '?'
        notional = _event_notional(p)
        sign = 1 if e['type'] in ADDING else -1
        if e['type'] == EventType.POSITION_FLIP.value:
            sign = 1
        entry = coins.setdefault(coin, {'net': ZERO, 'realized': ZERO, 'closed': False, 'liquidated': False,
                                        'side': str(p.get('side') or '').lower()})
        entry['net'] += sign * notional
        entry['realized'] += _event_realized(p)
        realized_total += _event_realized(p)
        if e['type'] == EventType.POSITION_CLOSE.value:
            entry['closed'] = True
        if e['type'] == EventType.LIQUIDATION.value:
            entry['liquidated'] = True
        if largest is None or notional > largest[0]:
            largest = (notional, {**p, 'type': e['type']})
    positions: dict[str, dict] = {}
    for dex in ((snapshot or {}).get('positions') or {}).values():
        for coin, pos in (dex or {}).items():
            positions[coin] = pos
    algo_ntl = sum((to_decimal(s.get('total_ntl')) or ZERO for s in algos.values()), ZERO)
    return {'coins': coins, 'positions': positions, 'realized': realized_total, 'largest': largest,
            'algo_coins': len({c for c, _ in algos}), 'algo_ntl': algo_ntl}


def wallet_lines(activity: dict) -> list[str]:
    lines = []
    positions = activity['positions']
    ordered = sorted(activity['coins'].items(), key=lambda kv: -abs(kv[1]['net']))
    for coin, info in ordered:
        pos = positions.get(coin)
        name = h(base_coin(coin))
        if pos is not None and (to_decimal(pos.get('szi')) or ZERO) != 0:
            side = 'long' if (to_decimal(pos.get('szi')) or ZERO) > 0 else 'short'
            value = abs(to_decimal(pos.get('position_value')) or ZERO)
            lines.append(f"{name} {side} {_signed_compact(info['net'])} → {compact_usd(value)}")
        else:
            verb = 'liquidated' if info['liquidated'] else 'closed'
            pnl = f", realized {_signed_compact(info['realized'])}" if info['realized'] else ''
            side = f"{info['side']} " if info['side'] else ''
            lines.append(f"{name} {side}{verb}{pnl}")
    if activity['realized']:
        lines.append(f"realized {_signed_compact(activity['realized'])} total")
    if activity['algo_coins']:
        lines.append(f"algos: {activity['algo_coins']} coin{'s' if activity['algo_coins'] != 1 else ''} "
                     f"{compact_usd(activity['algo_ntl'])}")
    if activity['largest'] is not None and activity['largest'][0] > 0:
        notional, p = activity['largest']
        lines.append(f"largest: {h(base_coin(p.get('coin') or '?'))} {p.get('side', '')} {compact_usd(notional)}".replace('  ', ' '))
    return lines


def exposure_lines(wallet_positions: list[dict[str, dict]]) -> list[str]:
    """Net long / short per coin over every wallet, top EXPOSURE_TOP by size, with the wallet count."""
    totals: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for positions in wallet_positions:
        for coin, pos in positions.items():
            szi = to_decimal(pos.get('szi')) or ZERO
            value = abs(to_decimal(pos.get('position_value')) or ZERO)
            if szi == 0 or value == 0:
                continue
            totals[coin] = totals.get(coin, ZERO) + (value if szi > 0 else -value)
            counts[coin] = counts.get(coin, 0) + 1
    ranked = sorted(totals.items(), key=lambda kv: -abs(kv[1]))[:EXPOSURE_TOP]
    return [f"{h(base_coin(coin))} net {'long' if net > 0 else 'short'} {compact_usd(abs(net))} "
            f"({counts[coin]} wallet{'s' if counts[coin] != 1 else ''})" for coin, net in ranked if net != 0]


async def build_digest(repo: Repo, user_id: int, now_ms: int) -> list[str]:
    """The digest text for one user as message chunks; [] when the user tracks nothing."""
    wallets = await repo.list_subscriptions(user_id)
    if not wallets:
        return []
    since = now_ms - WINDOW_MS
    yesterday = utc_day(now_ms, 1)
    sections, quiet = [], []
    total_now, total_then, have_then = ZERO, ZERO, False
    all_positions: list[dict[str, dict]] = []
    for alias, address in wallets:
        accounts = [r for r in await repo.venue_accounts_of(address) if r['active']]
        value_now, value_then, then_known = ZERO, ZERO, False
        events: list[dict] = []
        algos: dict = {}
        snapshot_positions: dict[str, dict] = {}
        merged_snapshot = {'positions': {}}
        for account in accounts:
            row = await repo.snapshot_row(account['key'])
            if row is not None:
                value_now += to_decimal(row.get('account_value')) or ZERO
                for dex, positions in (row.get('positions') or {}).items():
                    merged_snapshot['positions'][f"{account['key']}:{dex}"] = positions
                    snapshot_positions.update(positions or {})
            then = to_decimal(await repo.daily_value(account['key'], yesterday))
            if then is not None:
                value_then += then
                then_known = True
            events += await repo.events_since(account['key'], since)
            algos.update({(account['key'], k): v for k, v in (await repo.active_algos(account['key'])).items()})
        total_now += value_now
        if then_known:
            total_then += value_then
            have_then = True
        all_positions.append(snapshot_positions)
        activity = wallet_activity(events, merged_snapshot, {k[1]: v for k, v in algos.items()})
        lines = wallet_lines(activity)
        if not lines:
            quiet.append(alias)
            continue
        change = ''
        if then_known and value_then > 0:
            pct = (value_now - value_then) / value_then * 100
            change = f" ({'+' if pct >= 0 else ''}{pct:.1f}% 24h)"
        head = f"<b>{h(alias)}</b> · {compact_usd(value_now)}{change}"
        shown = lines[:MAX_WALLET_LINES]
        if len(lines) > MAX_WALLET_LINES:
            shown.append(f"+{len(lines) - MAX_WALLET_LINES} more")
        sections.append((value_now, head + "\n" + "\n".join(f"  {line}" for line in shown)))
    sections.sort(key=lambda s: -s[0])
    day = datetime.fromtimestamp(now_ms / 1000, tz=KST).strftime('%b %d')
    total_change = ''
    if have_then and total_then > 0:
        pct = (total_now - total_then) / total_then * 100
        total_change = f" ({'+' if pct >= 0 else ''}{pct:.1f}% 24h)"
    n = len(wallets)
    text = f"📰 <b>Daily · {day}</b> · {n} wallet{'s' if n != 1 else ''} · total {compact_usd(total_now)}{total_change}"
    for _, section in sections:
        text += "\n\n" + section
    if quiet:
        text += "\n\nquiet: " + ", ".join(h(a) for a in quiet)
    if n > 1:
        exposure = exposure_lines(all_positions)
        if exposure:
            text += "\n\n<b>Exposure</b>\n" + "\n".join(exposure)
    return split_message(text)


async def send_digest(bot: Bot, repo: Repo, user_id: int, now_ms: int, mark: bool = True) -> bool:
    """Build and send one user's digest; marks users.last_digest_day when mark is True. A user who blocked
    the bot (Forbidden) gets every wallet muted, like an alert would have done."""
    chunks = await build_digest(repo, user_id, now_ms)
    if not chunks:
        return False
    for chunk in chunks:
        try:
            await bot.send_message(chat_id=user_id, text=chunk, parse_mode=ParseMode.HTML)
        except Exception as e:
            if 'forbidden' in str(e).lower() or 'blocked' in str(e).lower():
                from hypermate.core.pipeline import MUTE_FOREVER_MS
                await repo.mute_all(user_id, MUTE_FOREVER_MS, now_ms)
                logger.warning(f"Digest: user {user_id} blocked the bot, wallets muted")
            else:
                logger.error(f"Digest send failed for user {user_id}: {e}")
            return False
    if mark:
        await repo.set_last_digest_day(user_id, kst_day(now_ms))
    logger.info(f"Digest sent to user {user_id} ({len(chunks)} messages)")
    return True


def due_users(users: list[dict], now_ms: int) -> list[int]:
    """Users whose digest hour (KST) has come today and who have not received today's digest."""
    now = datetime.fromtimestamp(now_ms / 1000, tz=KST)
    today = now.strftime('%Y-%m-%d')
    out = []
    for user in users:
        conf = digest_settings(user.get('settings') or {})
        if not conf.get('enabled', True) or user.get('last_digest_day') == today:
            continue
        if int(conf.get('hour_kst', 9)) <= now.hour:
            out.append(user['user_id'])
    return out


async def digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly: send to every due user, one second apart."""
    repo: Repo = context.bot_data['repo']
    now = hl_adapter.now_ms()
    users = due_users(await repo.users_with_subscriptions(), now)
    for i, user_id in enumerate(users):
        await send_digest(context.bot, repo, user_id, now)
        if i < len(users) - 1:
            await asyncio.sleep(1)


async def daily_value_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """00:00 UTC: store every active account's current value for tomorrow's 24 h change."""
    repo: Repo = context.bot_data['repo']
    day = utc_day(hl_adapter.now_ms())
    rows = await repo.record_daily_values(day)
    logger.info(f"Daily account values stored for {day}: {rows} accounts")
