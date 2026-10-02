"""Alert formatting (Telegram HTML).

Every user- or API-supplied string goes through h() before it is placed in a
message (B4). Numbers are Decimal.
"""

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

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


_SYSTEM_ADDRESS_RE = re.compile(r'0x(?:20|00)0{38}')


def is_system_address(address: str) -> bool:
    """HL system addresses: 0x20 or 0x00 followed by zeros (e.g. 0x2000...0000)."""
    return bool(_SYSTEM_ADDRESS_RE.fullmatch(address.lower()))


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


def format_spot_fill_message(fill: dict, wallet_address: str, alias: str) -> str:
    """Spot fill -> buy/sell message. side is "B" (buy) or "A" (sell)."""
    coin = h(fill.get('display_coin') or fill.get('coin', 'Unknown'))
    px = to_decimal(fill.get('px')) or ZERO
    quantity = to_decimal(fill.get('sz')) or ZERO
    usd_value = px * quantity
    who = f"<b>{alias_link(wallet_address, alias)}</b>"
    side = str(fill.get('side', '')).upper()
    if side == 'B':
        verb, emoji = 'bought', '🟢'
    elif side == 'A':
        verb, emoji = 'sold', '🔴'
    else:
        verb, emoji = 'traded', '📊'
    return f"{emoji} {who} {verb} {quantity:,.2f} {coin} for {usd(usd_value)} @ ${px:,.4f}"


def format_position_alert(wallet_address: str, alias: str, position: dict) -> str:
    """Position alert from the v1 snapshot diff (wording kept from v1)."""
    alert_type = position.get('alert_type', 'NEW_POSITION')
    direction = position['direction']
    coin = h(position['coin'])

    if alert_type == 'NEW_POSITION':
        side_emoji, action_text = ("📈" if direction == 'LONG' else "📉"), "opened a new"
    elif alert_type == 'POSITION_INCREASE':
        side_emoji, action_text = ("📈" if direction == 'LONG' else "📉"), "added to"
    elif alert_type == 'POSITION_DECREASE':
        side_emoji, action_text = ("📉" if direction == 'LONG' else "📈"), "reduced"
    elif alert_type == 'POSITION_CLOSED':
        side_emoji, action_text = "🔒", "closed"
    elif alert_type == 'LIQUIDATION':
        side_emoji, action_text = "🔥", "was liquidated on"
    else:
        side_emoji, action_text = "📊", "updated"

    szi = to_decimal(position.get('szi'))
    position_value = to_decimal(position.get('position_value'))
    entry_px = to_decimal(position.get('entry_px'))
    if position_value is not None:
        size_str = usd(position_value, 0)
    elif szi is not None:
        size_str = f"{abs(szi):,.2f}"
    else:
        size_str = "unknown"

    header = (f"{side_emoji} <b>{h(short_address(wallet_address))}</b> "
              f"({alias_link(wallet_address, alias)})")

    if alert_type == 'POSITION_INCREASE':
        added = to_decimal(position.get('size_change')) or ZERO
        added_info = f"<b>{added:,.2f}</b>"
        price_info = ""
        if position_value is not None and szi:
            current_px = abs(position_value) / abs(szi)
            added_info += f" ({usd(added * current_px)})"
            price_info = f" at {price(current_px)}"
        details = f"Total Position Size: {size_str}"
        if entry_px is not None:
            details += f", Average Entry: {price(entry_px)}"
        return (f"{header} just added {added_info} to <b>{direction}</b> on ${coin}"
                f"{price_info} ({details}).")

    if alert_type in ('POSITION_CLOSED', 'LIQUIDATION'):
        pnl = to_decimal(position.get('closing_pnl'))
        pnl_text = ""
        if alert_type == 'POSITION_CLOSED':
            if pnl is not None:
                emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
                pnl_text = f" | {emoji} {'+' if pnl > 0 else ''}{usd(pnl)}"
            closed = to_decimal(position.get('closed_size')) or ZERO
            additional = f" (closed {closed:,.2f}{pnl_text})"
        else:
            if pnl is not None:
                pnl_text = f" | 🔴 {usd(pnl)} loss"
            liquidated = to_decimal(position.get('liquidated_size')) or ZERO
            additional = f" (liquidated {liquidated:,.2f}{pnl_text})"
        return f"{header} just {action_text} <b>{direction}</b> position on ${coin}{additional}."

    additional = ""
    if alert_type == 'NEW_POSITION' and entry_px is not None:
        additional = f" @ {price(entry_px)}"
    elif alert_type == 'POSITION_DECREASE':
        change = to_decimal(position.get('size_change')) or ZERO
        remaining = to_decimal(position.get('remaining_size')) or ZERO
        additional = f" (-{change:,.2f}, {remaining:,.2f} remaining)"
    return (f"{header} just {action_text} <b>{direction}</b> on ${coin} "
            f"with {size_str} size{additional}.")


def format_positions(alias: str, address: str, perp_state: dict, spot_state: dict) -> str:
    """/positions <alias> view: perp positions, spot balances, margin balance."""
    margin_balance = "N/A"
    account_value = to_decimal(perp_state.get('marginSummary', {}).get('accountValue'))
    if account_value is not None:
        margin_balance = usd(account_value)

    futures_lines = []
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
        futures_lines.append(
            f"- {side_emoji} <b>{side}</b> ${h(position.get('coin', 'Unknown'))} — Size: {size_str} "
            f"— Entry: {entry_str} — PnL: {pnl_str}{funding_str}\n")

    spot_lines = []
    for balance in spot_state.get('balances', []):
        total = to_decimal(balance.get('total')) or ZERO
        entry_ntl = to_decimal(balance.get('entryNtl')) or ZERO
        # v1 behavior: entry notional approximates USD value
        usd_val = entry_ntl if entry_ntl > 0 else total
        if usd_val > 1 and total > 0:
            spot_lines.append(f"- {h(balance.get('coin', 'Unknown'))}: {total:,.2f} ({usd(usd_val)})")

    futures = "\n".join(futures_lines) if futures_lines else "- No open futures positions\n"
    spot = "\n".join(spot_lines) if spot_lines else "- No spot assets"
    return (
        f"📊 <b>Positions for {h(alias)}</b>\n"
        f"<code>{h(address)}</code>\n\n"
        f"📈 <b>Futures:</b>\n{futures}\n"
        f"💰 <b>Spot:</b>\n{spot}\n\n"
        f"📊 <b>Margin Balance:</b> {margin_balance}"
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
    coin = h(state.get('coin', '?'))
    size = compact_usd(sz * mark_px) if mark_px is not None else f"{quantity(sz)} {coin}"
    details = [f"{size} over {minutes} min", f"ends ~{kst_time(started + minutes * 60_000, now_ms)}"]
    if state.get('reduceOnly'):
        details.append('reduce-only')
    return (f"{VENUE_BADGE_HL} ⏳ <b>{alias_link(wallet_address, alias)}</b> started TWAP {_twap_side(state)} ${coin}\n"
            + " · ".join(details))


_TWAP_END_HEADERS = {
    'finished': ('✅', 'TWAP done'),
    'terminated': ('⏹️', 'TWAP stopped'),
    'error': ('⚠️', 'TWAP error'),
}


def format_twap_end(wallet_address: str, alias: str, state: dict, status: str,
                    description: Optional[str], ended_ms: int) -> str:
    emoji, title = _TWAP_END_HEADERS.get(status, ('❔', 'TWAP ended'))
    coin = h(state.get('coin', '?'))
    executed_sz = to_decimal(state.get('executedSz')) or ZERO
    executed_ntl = to_decimal(state.get('executedNtl')) or ZERO
    started = int(state.get('timestamp') or ended_ms)
    took = max(0, (ended_ms - started) // 60_000)
    if executed_sz > 0:
        filled = (f"filled {compact_usd(executed_ntl)} ({quantity(executed_sz)} {coin}) "
                  f"avg {plain_price(executed_ntl / executed_sz)}")
    else:
        filled = "nothing filled"
    line = f"{filled} · {took} min · status {h(status)}"
    if description:
        line += f"\n{h(description)}"
    return (f"{VENUE_BADGE_HL} {emoji} <b>{alias_link(wallet_address, alias)}</b> {title} "
            f"{_twap_side(state)} ${coin}\n{line}")
