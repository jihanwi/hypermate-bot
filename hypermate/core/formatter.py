"""Alert formatting (Telegram HTML).

Every user- or API-supplied string goes through h() before it is placed in a
message (B4). Numbers are Decimal.
"""

import html
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from hypermate.core.events import is_system_address  # noqa: F401  (re-exported)
from hypermate.core.links import hl_address_url
from hypermate.core.numbers import to_decimal

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


def h(value) -> str:
    """Escape any value for Telegram HTML."""
    return html.escape(str(value))


def usd(value: Decimal, places: int = 2) -> str:
    """$1,234.56 (sign before the dollar sign for negatives)."""
    sign = '-' if value < 0 else ''
    return f"{sign}${abs(value):,.{places}f}"


def price(value: Decimal) -> str:
    """$1,234.5678 with trailing zeros removed."""
    return f"${value:,.4f}".rstrip('0').rstrip('.')


def short_address(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address


def counterparty(address: str) -> str:
    if is_system_address(address):
        return "Hyperliquid system"
    return f"<code>{h(short_address(address))}</code>"


def alias_link(address: str, alias: str) -> str:
    return f'<a href="{h(hl_address_url(address))}">{h(alias)}</a>'


def format_transfer_message(transfer: dict, wallet_address: str, alias: str) -> Optional[str]:
    """Ledger update -> message, or None for types that are not shown."""
    delta = transfer.get('delta', {})
    transfer_type = delta.get('type', 'unknown')
    who = f"<b>{alias_link(wallet_address, alias)}</b>"

    if transfer_type in ('spotTransfer', 'send'):
        # 'send' is how current transfers arrive (B11); spotTransfer is kept for older history
        token = h(delta.get('token', 'Unknown'))
        amount = to_decimal(delta.get('amount')) or ZERO
        usd_value = to_decimal(delta.get('usdcValue')) or ZERO
        destination = delta.get('destination', '')
        user = delta.get('user', '')
        if user.lower() == wallet_address.lower():
            return (f"↗️ {who} sent {amount:,.2f} {token} ({usd(usd_value)}) "
                    f"to {counterparty(destination)}")
        if destination.lower() == wallet_address.lower():
            return (f"↘️ {who} received {amount:,.2f} {token} ({usd(usd_value)}) "
                    f"from {counterparty(user)}")
        return None

    if transfer_type == 'accountClassTransfer':
        amount = to_decimal(delta.get('usdc')) or ZERO
        if delta.get('toPerp', True):
            return f"🔄 {who} transferred {usd(amount)} from Spot to Perp"
        return f"🔄 {who} transferred {usd(amount)} from Perp to Spot"

    if transfer_type == 'deposit':
        return f"💰 {who} deposited {usd(to_decimal(delta.get('usdc')) or ZERO)}"

    if transfer_type == 'withdraw':
        return f"💸 {who} withdrew {usd(to_decimal(delta.get('usdc')) or ZERO)}"

    if transfer_type == 'vaultWithdraw':
        return f"🏦 {who} withdrew {usd(to_decimal(delta.get('netWithdrawnUsd')) or ZERO)} from vault"

    if transfer_type == 'vaultDeposit':
        # v1 read delta['usd']; the documented field is 'usdc'. Phase 1 confirms against live data.
        amount = to_decimal(delta.get('usdc', delta.get('usd'))) or ZERO
        return f"🏦 {who} deposited {usd(amount)} to vault"

    if transfer_type == 'liquidation':
        return f"⚠️ {who} was liquidated"

    # vaultLeaderCommission, rewardsClaim, funding and anything else: not shown
    return None


def _futures_lines(perp_state: dict) -> list[str]:
    lines = []
    for pos in perp_state.get('assetPositions', []):
        position = pos.get('position')
        if not position:
            continue
        szi = to_decimal(position.get('szi')) or ZERO
        if szi == 0:
            continue
        side = "LONG" if szi > 0 else "SHORT"
        side_emoji = "📈" if side == "LONG" else "📉"
        position_value = to_decimal(position.get('positionValue'))
        size_str = usd(position_value, 0) if position_value is not None else f"{abs(szi):,.2f}"
        entry_px = to_decimal(position.get('entryPx'))
        entry_str = price(entry_px) if entry_px is not None else "N/A"
        upnl = to_decimal(position.get('unrealizedPnl'))
        pnl_str = f"{'🟢' if upnl >= 0 else '🔴'} {usd(upnl)}" if upnl is not None else "N/A"
        funding_str = ""
        funding = to_decimal((position.get('cumFunding') or {}).get('sinceOpen'))
        if funding:
            funding_text = f"Received {usd(abs(funding))}" if funding < 0 else f"Paid {usd(abs(funding))}"
            funding_str = f"\n🔁 Funding PnL: {funding_text}"
        lines.append(
            f"- {side_emoji} <b>{side}</b> ${h(base_coin(position.get('coin', 'Unknown')))} — Size: {size_str} "
            f"— Entry: {entry_str} — PnL: {pnl_str}{funding_str}\n")
    return lines


def format_positions(alias: str, address: str, perp_states, spot_state: dict) -> str:
    """/positions <alias> view: perp positions per dex (main + HIP-3), spot balances, account value.

    perp_states is {dex: clearinghouseState} ("" = main dex) or a single main-dex state.
    """
    if 'assetPositions' in perp_states or 'marginSummary' in perp_states:
        perp_states = {'': perp_states}

    sections = []
    total = ZERO
    has_value = False
    for dex in sorted(perp_states, key=lambda d: (d != '', d)):
        state = perp_states[dex]
        value = account_value(state)
        if value is not None:
            total += value
            has_value = True
        lines = _futures_lines(state)
        if dex == '':
            body = "\n".join(lines) if lines else "- No open futures positions\n"
            sections.append(f"📈 <b>Futures:</b>\n{body}")
        elif lines or (value or ZERO) > 0:
            value_str = usd(value) if value is not None else "N/A"
            body = "\n".join(lines) if lines else "- No open positions\n"
            sections.append(f"📈 <b>Futures ({h(dex)} dex)</b> · account {value_str}\n{body}")

    spot_lines = []
    for balance in spot_state.get('balances', []):
        total_bal = to_decimal(balance.get('total')) or ZERO
        entry_ntl = to_decimal(balance.get('entryNtl')) or ZERO
        # v1 behavior: entry notional approximates USD value
        usd_val = entry_ntl if entry_ntl > 0 else total_bal
        if usd_val > 1 and total_bal > 0:
            spot_lines.append(f"- {h(balance.get('coin', 'Unknown'))}: {total_bal:,.2f} ({usd(usd_val)})")
    spot = "\n".join(spot_lines) if spot_lines else "- No spot assets"
    margin_balance = usd(total) if has_value else "N/A"
    label = "Margin Balance" if len(perp_states) == 1 else "Margin Balance (all dexs)"
    return (
        f"📊 <b>Positions for {h(alias)}</b>\n"
        f"<code>{h(address)}</code>\n\n"
        + "\n".join(sections) + "\n"
        f"💰 <b>Spot:</b>\n{spot}\n\n"
        f"📊 <b>{label}:</b> {margin_balance}"
    )


def format_stats(alias: str, address: str, portfolio: list) -> Optional[str]:
    """/stats view from the portfolio endpoint's allTime entry, or None if missing."""
    all_time = None
    for period in portfolio:
        if len(period) >= 2 and period[0] == "allTime":
            all_time = period[1]
            break
    if not all_time:
        return None

    pnl_str = "N/A"
    pnl_history = all_time.get('pnlHistory', [])
    pnl = to_decimal(pnl_history[-1][1]) if pnl_history and len(pnl_history[-1]) >= 2 else ZERO
    if pnl is not None:
        pnl_str = f"{'🟢' if pnl >= 0 else '🔴'} {'+' if pnl >= 0 else ''}{usd(pnl)}"

    volume_str = "N/A"
    volume = to_decimal(all_time.get('vlm', '0'))
    if volume is not None:
        if volume >= 1_000_000:
            volume_str = f"${volume / 1_000_000:.1f}M"
        elif volume >= 1_000:
            volume_str = f"${volume / 1_000:.1f}K"
        else:
            volume_str = f"${volume:,.0f}"

    return (
        f"📊 <b>Stats for {h(alias)}</b>\n"
        f"<code>{h(address)}</code>\n\n"
        f"🧮 <b>PnL:</b> {pnl_str}\n"
        f"📈 <b>Volume:</b> {volume_str}\n"
        f"📝 <b>Note:</b> Win/loss data not available from API"
    )


TELEGRAM_MESSAGE_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split on line boundaries into chunks of at most limit characters."""
    chunks, current = [], ''
    for line in text.split('\n'):
        while len(line) > limit:  # a single overlong line is cut hard
            if current:
                chunks.append(current)
                current = ''
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def account_value(perp_state: dict) -> Optional[Decimal]:
    return to_decimal(perp_state.get('marginSummary', {}).get('accountValue'))


def format_list_line(alias: str, address: str, value: Optional[Decimal]) -> str:
    """/list row. value None means unknown (not polled yet and the API call failed)."""
    value_str = usd(value) if value is not None else "n/a"
    return f"• {alias_link(address, alias)}: {h(address)} · {value_str}"


def format_positions_summary_line(alias: str, address: str, perp_state: Optional[dict]) -> str:
    """/positions (no alias) row: account value, position count, largest position."""
    if perp_state is None:
        return f"• {alias_link(address, alias)}: Hyperliquid API error"
    value = account_value(perp_state)
    value_str = usd(value) if value is not None else "n/a"
    open_positions = []
    for pos in perp_state.get('assetPositions', []):
        position = pos.get('position') or {}
        szi = to_decimal(position.get('szi')) or ZERO
        if szi != 0:
            notional = abs(to_decimal(position.get('positionValue')) or ZERO)
            open_positions.append((notional, 'LONG' if szi > 0 else 'SHORT', position.get('coin', '?')))
    line = f"• {alias_link(address, alias)}: {value_str} · {len(open_positions)} positions"
    if open_positions:
        notional, side, coin = max(open_positions, key=lambda p: p[0])
        line += f" · largest {side} ${h(coin)} {usd(notional, 0)}"
    return line


# TWAP alerts (spec 10 format) -----------------------------------------------

KST = timezone(timedelta(hours=9), 'KST')
VENUE_BADGE_HL = '[HL]'


def _sig(value: Decimal, digits: int) -> str:
    """Round to `digits` significant digits, plain notation, no trailing zeros."""
    if value == 0:
        return '0'
    quantized = value.quantize(Decimal(1).scaleb(value.adjusted() - digits + 1), rounding=ROUND_HALF_UP)
    text = format(quantized, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def compact_usd(value: Decimal) -> str:
    """$1.2k / $45k / $1.25M style (3 significant digits)."""
    sign = '-' if value < 0 else ''
    v = abs(value)
    for size, suffix in ((Decimal(10) ** 9, 'B'), (Decimal(10) ** 6, 'M'), (Decimal(10) ** 3, 'k')):
        if v >= size:
            return f"{sign}${_sig(v / size, 3)}{suffix}"
    return f"{sign}${_sig(v, 3)}"


def quantity(value: Decimal) -> str:
    """Up to 4 significant digits, thousands separators for the integer part."""
    text = _sig(value, 4)
    whole, _, frac = text.partition('.')
    whole = f"{int(whole):,}" if whole.lstrip('-').isdigit() else whole
    return f"{whole}.{frac}" if frac else whole


def plain_price(value: Decimal) -> str:
    """86,281 / 48.06 / 0.3633 (no dollar sign)."""
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return _sig(value, 4)


def kst_time(ms: int, now_ms: Optional[int] = None) -> str:
    """HH:MM KST, with the date when it is not within the next 24 hours."""
    moment = datetime.fromtimestamp(ms / 1000, tz=KST)
    if now_ms is not None and abs(ms - now_ms) >= 24 * 3600 * 1000:
        return moment.strftime('%m-%d %H:%M KST')
    return moment.strftime('%H:%M KST')


def _twap_side(state: dict) -> str:
    return 'BUY' if state.get('side') == 'B' else 'SELL'


def format_twap_start(wallet_address: str, alias: str, state: dict,
                      mark_px: Optional[Decimal], now_ms: int) -> str:
    sz = to_decimal(state.get('sz')) or ZERO
    minutes = int(state.get('minutes') or 0)
    started = int(state.get('timestamp') or now_ms)
    coin = state.get('coin', '?')
    size = compact_usd(sz * mark_px) if mark_px is not None else f"{quantity(sz)} {h(base_coin(coin))}"
    details = [f"{size} over {humanize_minutes(minutes)}", f"ends ~{kst_time(started + minutes * 60_000, now_ms)}"]
    if state.get('reduceOnly'):
        details.append('reduce-only')
    return (f"{VENUE_BADGE_HL} ⏳ <b>{alias_link(wallet_address, alias)}</b> started TWAP {_twap_side(state)} {coin_label(coin)}\n"
            + " · ".join(details))


_TWAP_END_HEADERS = {
    'finished': ('✅', 'TWAP done'),
    'terminated': ('⏹️', 'TWAP stopped'),
    'error': ('⚠️', 'TWAP error'),
}


def format_twap_end(wallet_address: str, alias: str, state: dict, status: str,
                    description: Optional[str], ended_ms: int) -> str:
    emoji, title = _TWAP_END_HEADERS.get(status, ('❔', 'TWAP ended'))
    coin = state.get('coin', '?')
    executed_sz = to_decimal(state.get('executedSz')) or ZERO
    executed_ntl = to_decimal(state.get('executedNtl')) or ZERO
    started = int(state.get('timestamp') or ended_ms)
    took = max(0, (ended_ms - started) // 60_000)
    if executed_sz > 0:
        filled = (f"filled {compact_usd(executed_ntl)} ({quantity(executed_sz)} {h(base_coin(coin))}) "
                  f"avg {plain_price(executed_ntl / executed_sz)}")
    else:
        filled = "nothing filled"
    line = f"{filled} · {humanize_minutes(took)} · status {h(status)}"
    if description:
        line += f"\n{h(description)}"
    return (f"{VENUE_BADGE_HL} {emoji} <b>{alias_link(wallet_address, alias)}</b> {title} "
            f"{_twap_side(state)} {coin_label(coin)}\n{line}")


# Durations and coins -------------------------------------------------------------

def humanize_minutes(minutes: int) -> str:
    """10080 -> 7d, 8302 -> 5d 18h, 90 -> 1h 30m, 5 -> 5m (two largest non-zero units, spec 10)."""
    minutes = max(0, int(minutes))
    parts = []
    for unit, size in (('d', 1440), ('h', 60), ('m', 1)):
        value, minutes = divmod(minutes, size)
        if value:
            parts.append(f"{value}{unit}")
    return ' '.join(parts[:2]) if parts else '0m'


def humanize_ms(ms: int) -> str:
    return humanize_minutes(int(ms) // 60_000)


def base_coin(coin: str) -> str:
    """"xyz:MU" -> "MU"; other coins unchanged."""
    return coin.split(':', 1)[1] if ':' in coin else coin


def coin_label(coin: str, display: Optional[str] = None) -> str:
    """$BTC, HIP-3 "xyz:MU" -> $MU (xyz) (spec 5.2), spot uses its display name."""
    if display:
        return f"${h(display)}"
    if ':' in coin:
        dex, name = coin.split(':', 1)
        return f"${h(name)} ({h(dex)})"
    return f"${h(coin)}"


def _signed_usd(value: Decimal) -> str:
    return f"{'+' if value > 0 else ''}{usd(value, 0)}"


def _realized(pnl: Optional[Decimal]) -> Optional[str]:
    if pnl is None:
        return None
    emoji = '🟢' if pnl > 0 else '🔴' if pnl < 0 else '⚪'
    return f"realized {emoji} {_signed_usd(pnl)}"


# Fill events (spec 10) -------------------------------------------------------------

_POSITION_HEADERS = {
    'position_open': (None, 'opened'),
    'position_increase': ('➕', 'added to'),
    'position_decrease': ('➖', 'reduced'),
    'position_close': ('🔒', 'closed'),
    'position_flip': ('🔄', 'flipped to'),
    'liquidation': ('🔥', 'LIQUIDATED'),
}


def format_fill_message(wallet_address: str, alias: str, chain: dict, held_ms: Optional[int] = None) -> str:
    """Position or spot event, possibly several orders merged by debounce (aggregator chain dict)."""
    event_type = chain['type']
    coin = chain.get('coin') or '?'
    display = (chain.get('meta') or {}).get('display_coin')
    size = to_decimal(chain.get('size')) or ZERO
    notional = to_decimal(chain.get('notional_usd')) or ZERO
    px = notional / size if size else None
    qty = f"{quantity(size)} {h(display or base_coin(coin))}"
    at = f" @ {plain_price(px)}" if px is not None else ""
    who = f"<b>{alias_link(wallet_address, alias)}</b>"
    fills = int(chain.get('fills') or 1)
    tail = f" · {fills} fills" if fills > 1 else ""

    if event_type in ('spot_buy', 'spot_sell'):
        emoji, verb = ('🟢', 'bought') if event_type == 'spot_buy' else ('🔴', 'sold')
        return (f"{VENUE_BADGE_HL} {emoji} {who} {verb} {qty} {coin_label(coin, display)}\n"
                f"{compact_usd(notional)}{at}{tail}")

    side = chain.get('side') or ''
    emoji, verb = _POSITION_HEADERS[event_type]
    if emoji is None:
        emoji = '📈' if side == 'LONG' else '📉'
    header = f"{VENUE_BADGE_HL} {emoji} {who} {verb} {side} {coin_label(coin)}"
    details = []
    after = to_decimal(chain.get('position_after'))
    now_value = abs(after) * px if after is not None and px is not None else None
    if event_type == 'position_increase':
        details.append(f"+{compact_usd(notional)} ({qty}){at}")
    elif event_type == 'position_decrease':
        details.append(f"-{compact_usd(notional)} ({qty}){at}")
    else:
        details.append(f"{compact_usd(notional)} ({qty}){at}")
    if event_type in ('position_increase', 'position_decrease', 'position_flip') and now_value is not None:
        details.append(f"now {compact_usd(now_value)}")
    realized = _realized(to_decimal(chain.get('realized_pnl')))
    if realized and event_type != 'position_open':
        details.append(realized)
    if event_type == 'position_close' and held_ms is not None:
        details.append(f"held {humanize_ms(held_ms)}")
    return f"{header}\n{' · '.join(details)}{tail}"


def format_ledger_event(wallet_address: str, alias: str, payload: dict) -> Optional[str]:
    """Ledger events reuse the transfer formatter; HIP-3 collateral moves get their own line."""
    meta = payload.get('meta') or {}
    delta = meta.get('delta') or {}
    if payload.get('type') == 'dex_collateral_transfer':
        amount = to_decimal(delta.get('usdcValue') or delta.get('amount')) or ZERO
        target = delta.get('destinationDex') or 'main'
        source = delta.get('sourceDex') or 'main'
        return (f"{VENUE_BADGE_HL} 🔁 <b>{alias_link(wallet_address, alias)}</b> moved {usd(amount)} "
                f"{h(delta.get('token', 'USDC'))} collateral {h(source)} → {h(target)} dex")
    return format_transfer_message({'delta': delta}, wallet_address, alias)


# Synthetic TWAP (algo) ---------------------------------------------------------------

def algo_label(sign: int, position_after: Optional[Decimal]) -> tuple[str, str]:
    """('accumulating', 'LONG') or ('reducing', 'SHORT') from the fill sign and the position side."""
    if position_after is None or position_after == 0:
        side = 'LONG' if sign > 0 else 'SHORT'
    else:
        side = 'LONG' if position_after > 0 else 'SHORT'
    verb = 'accumulating' if (sign > 0) == (side == 'LONG') else 'reducing'
    return verb, side


def format_algo_progress(wallet_address: str, alias: str, state: dict, verb: str, side: str,
                         position_after: Optional[Decimal], now_ms: int) -> str:
    """ALGO_START text; the same message is edited with fresh numbers every algo_progress_sec."""
    total = to_decimal(state.get('total_ntl')) or ZERO
    total_sz = to_decimal(state.get('total_sz')) or ZERO
    vwap = total / total_sz if total_sz else None
    signed = f"{'+' if int(state['sign']) > 0 else '-'}{compact_usd(total)}"
    line = f"{int(state['fills_count'])} fills {signed} in {humanize_ms(now_ms - int(state['started_ms']))}"
    if position_after is not None and vwap is not None:
        line += f" · pos {compact_usd(abs(position_after) * vwap)}"
    if vwap is not None:
        line += f" avg {plain_price(vwap)}"
    return (f"{VENUE_BADGE_HL} 🤖 <b>{alias_link(wallet_address, alias)}</b> algo {verb} {side} "
            f"{coin_label(state['coin'])}\n{line}")


def format_algo_end(wallet_address: str, alias: str, state: dict, verb: str, side: str) -> str:
    total = to_decimal(state.get('total_ntl')) or ZERO
    total_sz = to_decimal(state.get('total_sz')) or ZERO
    vwap = total / total_sz if total_sz else None
    signed = f"{'+' if int(state['sign']) > 0 else '-'}{compact_usd(total)}"
    label = side if verb == 'accumulating' else f"{verb} {side}"
    line = f"{signed} ({quantity(total_sz)} {h(base_coin(state['coin']))})"
    if vwap is not None:
        line += f" avg {plain_price(vwap)}"
    line += (f" · {int(state['fills_count'])} fills · "
             f"{humanize_ms(int(state['last_fill_ms']) - int(state['started_ms']))}")
    return (f"{VENUE_BADGE_HL} ✅ <b>{alias_link(wallet_address, alias)}</b> algo done {label} "
            f"{coin_label(state['coin'])}\n{line}")


# /recent and /twap ------------------------------------------------------------------

_DELIVERY_NOTES = {
    'suppressed_twap': 'TWAP', 'suppressed_algo': 'algo', 'filtered_settings': 'off in settings',
    'filtered_threshold': 'below threshold', 'muted': 'muted',
}


def _recent_line(event: dict) -> str:
    payload = event['payload']
    event_type = event['type']
    coin = payload.get('coin')
    notional = to_decimal(payload.get('notional_usd'))
    parts = [kst_time(event['ts_ms']).replace(' KST', ''), event_type.replace('_', ' ')]
    if payload.get('side'):
        parts.append(payload['side'])
    if coin:
        parts.append(coin_label(coin, (payload.get('meta') or {}).get('display_coin')))
    if notional:
        parts.append(compact_usd(notional))
    if event_type in ('twap_start', 'twap_end'):
        state = payload.get('state') or {}
        parts.append(coin_label(state.get('coin', '?')))
    line = ' '.join(parts)
    note = _DELIVERY_NOTES.get(event['delivery'])
    return f"<i>{line} · not sent ({note})</i>" if note else line


def format_recent(alias: str, events: list[dict]) -> str:
    if not events:
        return f"No events recorded for <b>{h(alias)}</b> yet."
    lines = [_recent_line(e) for e in sorted(events, key=lambda e: (e['ts_ms'], e['event_id']))]
    return f"🕘 <b>Recent events for {h(alias)}</b> (KST)\n" + "\n".join(lines)


def format_twap_list(rows: list[dict], now_ms: int) -> str:
    """rows: {'alias', 'address', 'kind': 'twap'|'algo', ...} for /twap."""
    if not rows:
        return "No active TWAPs."
    lines = []
    for row in rows:
        who = f"<b>{alias_link(row['address'], row['alias'])}</b>"
        if row['kind'] == 'twap':
            state = row['state']
            sz = to_decimal(state.get('sz')) or ZERO
            done = to_decimal(state.get('executedSz')) or ZERO
            pct = f"{(done / sz * 100):.0f}%" if sz else "?"
            started = int(state.get('timestamp') or now_ms)
            ends = started + int(state.get('minutes') or 0) * 60_000
            lines.append(f"{VENUE_BADGE_HL} ⏳ {who} {_twap_side(state)} {coin_label(state.get('coin', '?'))} · "
                         f"{pct} ({quantity(done)}/{quantity(sz)}) · ends ~{kst_time(ends, now_ms)}")
        else:
            state = row['state']
            lines.append(f"{VENUE_BADGE_HL} 🤖 {who} algo {row['verb']} {row['side']} {coin_label(state['coin'])} · "
                         f"{int(state['fills_count'])} fills {compact_usd(to_decimal(state['total_ntl']) or ZERO)} · "
                         f"since {kst_time(int(state['started_ms']), now_ms)}")
    return "⏳ <b>Active TWAPs</b>\n" + "\n".join(lines)
