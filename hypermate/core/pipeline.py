"""Polling jobs, event handling and alert delivery (spec 5.2).

- positions job: per-dex snapshots (HIP-3), account value, native TWAP tracking.
- fills job: fills -> one event per order -> TWAP / algo suppression -> debounce
  edit or new message; ledger events; synthetic TWAP progress and idle end.
"""

import asyncio
import logging
from decimal import Decimal
from typing import Callable, Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hypermate.config import Config
from hypermate.core import aggregator, events, related
from hypermate.core.events import POSITION_TYPES, SPOT_TYPES, Event, EventType
from hypermate.core.formatter import (algo_label, format_algo_end, format_algo_progress, format_fill_message,
                                      format_ledger_event, format_twap_end, format_twap_start)
from hypermate.core.numbers import to_decimal
from hypermate.db.repo import Repo
from hypermate.venues import base as venues
from hypermate.venues.base import VenueAccount, fill_direction
from hypermate.venues.hyperliquid import adapter, twap
from hypermate.venues.hyperliquid.client import HyperliquidClient

logger = logging.getLogger(__name__)

Render = Callable[[str], Optional[str]]

# Ledger event types that are off by default (spec 9.4: vault, account_class_transfer, dex_collateral)
OFF_BY_DEFAULT = (EventType.ACCOUNT_CLASS_TRANSFER, EventType.DEX_COLLATERAL_TRANSFER,
                  EventType.VAULT_DEPOSIT, EventType.VAULT_WITHDRAW)
LIQUIDATION_DEDUPE_MS = 5 * 60 * 1000


def settings() -> dict:
    return Config.DEFAULT_SETTINGS


# Venue of each account key (spec 6.1): filled by the poller / commands before events are rendered.
# Keys not registered are Hyperliquid (Phase 0/1 behaviour).
_ACCOUNTS: dict[int, VenueAccount] = {}


def register_account(key: int, account: VenueAccount) -> None:
    _ACCOUNTS[key] = account


def venue_of(key: int) -> str:
    account = _ACCOUNTS.get(key)
    return account.venue if account is not None else venues.HYPERLIQUID


def label_for(key: int, alias: str) -> str:
    account = _ACCOUNTS.get(key)
    return account.label(alias) if account is not None else alias


# Delivery --------------------------------------------------------------------

async def deliver(bot: Bot, repo: Repo, account_key: int, render: Render, event_id: Optional[int] = None) -> None:
    """Send to every subscriber (rendered with their alias); remember message ids for later edits."""
    subscribers = await repo.subscribers(account_key)
    for i, (user_id, alias) in enumerate(subscribers):
        text = render(label_for(account_key, alias))
        if text is None:
            continue
        try:
            message = await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            logger.info(f"Sent alert to user {user_id} ({alias})")
            if event_id is not None and message is not None:
                await repo.add_sent_message(event_id, user_id, message.chat_id, message.message_id)
        except Exception as e:
            logger.error(f"Failed to send alert to user {user_id}: {e}")
        if i < len(subscribers) - 1:
            await asyncio.sleep(1)


async def edit_or_send(bot: Bot, repo: Repo, account_key: int, target_event_id: int, render: Render) -> None:
    """Edit the messages sent for target_event_id; users without one (or whose edit fails) get a new message."""
    sent = await repo.sent_messages(target_event_id)
    subscribers = await repo.subscribers(account_key)
    for i, (user_id, alias) in enumerate(subscribers):
        text = render(label_for(account_key, alias))
        if text is None:
            continue
        if user_id in sent:
            chat_id, message_id = sent[user_id]
            try:
                await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id,
                                            parse_mode=ParseMode.HTML)
                logger.info(f"Edited alert for user {user_id} ({alias})")
                continue
            except Exception as e:
                if 'not modified' in str(e).lower():
                    continue
                logger.warning(f"Edit failed for user {user_id}, sending a new message: {e}")
        try:
            message = await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            if message is not None:
                await repo.add_sent_message(target_event_id, user_id, message.chat_id, message.message_id)
        except Exception as e:
            logger.error(f"Failed to send alert to user {user_id}: {e}")
        if i < len(subscribers) - 1:
            await asyncio.sleep(1)


async def emit(bot: Bot, repo: Repo, account_key: int, event_type: EventType, source_id: str, ts_ms: int,
               payload: dict, render: Render, delivery: str = events.SENT) -> Optional[int]:
    """Record the event; send it only if new and delivery is 'sent'. Returns the event id if recorded."""
    key = events.dedupe_key(events.HYPERLIQUID, account_key, event_type, source_id)
    event_id = await repo.record_event(key, account_key, event_type.value, ts_ms, payload, delivery,
                                       adapter.now_ms())
    if event_id is None:
        logger.info(f"Duplicate event {key}, not sent")
        return None
    if delivery != events.SENT:
        logger.info(f"Event {key} recorded as {delivery}, not sent")
        return event_id
    await deliver(bot, repo, account_key, render, event_id)
    return event_id


async def _cursor(repo: Repo, account_key: int, kind: str) -> int:
    """Stored cursor (B6). An account without one (e.g. just migrated) starts at now, no history replay."""
    value = await repo.get_cursor(account_key, kind)
    if value is None:
        now = adapter.now_ms()
        await repo.set_cursor(account_key, kind, str(now), now)
        logger.info(f"Started {kind} cursor for {account_key} at {now}")
        return now
    return int(value)


# Native TWAP (PR A) -------------------------------------------------------------

async def sync_twaps(bot: Bot, repo: Repo, client: HyperliquidClient, key: int, address: str,
                     active: dict[str, dict], web_data2: dict) -> dict[str, dict]:
    """Reconcile twap_active with webData2.twapStates; emit TWAP_START / TWAP_END."""
    current = twap.parse_twap_states(web_data2)
    prices = twap.mark_prices(web_data2)
    now = adapter.now_ms()
    tracking_since = await _cursor(repo, key, 'twap')   # set on /add, never advanced

    for twap_id, state in current.items():
        started = int(state.get('timestamp') or now)
        await repo.upsert_twap(key, twap_id, state, started)
        if twap_id not in active:
            in_progress = started < tracking_since   # already running when the wallet was added
            logger.info(f"TWAP {twap_id} {'in progress' if in_progress else 'started'}: "
                        f"{state.get('side')} {state.get('coin')} for {address}")
            await emit(bot, repo, key, EventType.TWAP_START, twap_id, started,
                       {'twap_id': twap_id, 'state': state, 'mark_px': prices.get(state.get('coin')),
                        'in_progress': in_progress},
                       lambda alias, s=state, ip=in_progress: format_twap_start(
                           address, alias, s, prices.get(s.get('coin')), now, ip))

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


# Positions job -------------------------------------------------------------------

async def _ensure_dex_scan(repo: Repo, client: HyperliquidClient, key: int, address: str) -> list[str]:
    """Accounts tracked before HIP-3 support get one perpDexs scan (spec 5.2), marked by a cursor row."""
    dexs = await repo.get_dexs(key)
    if await repo.get_cursor(key, 'dex_scan') is None:
        try:
            found = await adapter.scan_dexs(client, address)
        except Exception as e:
            logger.error(f"HIP-3 dex scan failed for {address}: {e}")
            return dexs
        dexs = sorted(set(dexs) | set(found))
        await repo.set_dexs(key, dexs)
        now = adapter.now_ms()
        await repo.set_cursor(key, 'dex_scan', str(now), now)
        logger.info(f"HIP-3 dex scan for {address}: {dexs or 'none'}")
    return dexs


async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: Repo = context.bot_data['repo']
    client: HyperliquidClient = context.bot_data['hl']
    accounts = await repo.tracked_accounts()
    for i, (key, address) in enumerate(accounts):
        try:
            dexs = await _ensure_dex_scan(repo, client, key, address)
            previous = await repo.get_snapshot(key)
            current, account_value, _ = await adapter.fetch_snapshot(client, address, dexs)
            changed = previous is not None and adapter.snapshot_changed(previous, current)
            if previous is None:
                logger.info(f"Baseline snapshot for {address}: "
                            f"{sum(len(p) for p in current.values())} positions")
            await repo.save_snapshot(key, current, adapter.now_ms(), account_value)

            # TWAP (spec 5.2): webData2 only when positions moved or a TWAP is being tracked
            twap_states = await repo.active_twaps(key)
            if changed or twap_states:
                try:
                    web = await client.web_data2(address)
                except Exception as e:
                    logger.error(f"webData2 failed for {address}: {e}")
                else:
                    await sync_twaps(context.bot, repo, client, key, address, twap_states, web)
        except Exception as e:
            logger.error(f"Position check failed for {address}: {e}")
        if i < len(accounts) - 1:
            await asyncio.sleep(2)
    logger.info(f"Completed position check for {len(accounts)} wallets")


# Fills -----------------------------------------------------------------------------

def algo_source(coin: str, sign: int, started_ms: int) -> str:
    return f"{coin}:{sign}:{started_ms}"


async def _latest_chain(repo: Repo, key: int, event_id: int, payload: dict, debounce_sec: int) -> Optional[dict]:
    """Base event of the newest earlier message this order could be merged into, or None."""
    since = int(payload['meta']['first_ms']) - debounce_sec * 1000
    types = [t.value for t in POSITION_TYPES + SPOT_TYPES]
    candidates = [e for e in await repo.events_since(key, since, types)
                  if e['event_id'] != event_id and e['delivery'] == events.SENT
                  and e['payload'].get('coin') == payload.get('coin')
                  and aggregator.debounce_key(e['payload']) == aggregator.debounce_key(payload)]
    if not candidates:
        return None
    latest = candidates[-1]
    base_id = latest['payload'].get('chain_base', latest['event_id'])
    return await repo.get_event(base_id)


async def _held_ms(repo: Repo, key: int, payload: dict) -> Optional[int]:
    if payload['type'] != EventType.POSITION_CLOSE.value:
        return None
    opened = await repo.last_event_ts(key, EventType.POSITION_OPEN.value, payload['coin'])
    return int(payload['ts_ms']) - opened if opened else None


async def _send_fill_event(bot: Bot, repo: Repo, key: int, address: str, event_id: int, payload: dict) -> None:
    """Debounce (spec 5.2): merge into the previous message for the same (coin, dir) or send a new one."""
    debounce_sec = int(settings()['debounce_sec'])
    base = await _latest_chain(repo, key, event_id, payload, debounce_sec)
    if (base is not None and base['event_id'] != event_id and 'chain' in base['payload']
            and aggregator.can_merge(base['payload']['chain'], payload, debounce_sec)):
        chain = aggregator.merge_into_chain(base['payload']['chain'], payload)
        await repo.update_event_payload(base['event_id'], {**base['payload'], 'chain': chain})
        await repo.update_event_payload(event_id, {**payload, 'chain_base': base['event_id']})
        held = await _held_ms(repo, key, {**payload, 'type': chain['type']})
        await edit_or_send(bot, repo, key, base['event_id'],
                           lambda alias: format_fill_message(address, alias, chain, held, venue_of(key)))
        return
    chain = aggregator.start_chain(payload)
    await repo.update_event_payload(event_id, {**payload, 'chain': chain})
    held = await _held_ms(repo, key, payload)
    await deliver(bot, repo, key, lambda alias: format_fill_message(address, alias, chain, held, venue_of(key)), event_id)


async def _liquidation_already_alerted(repo: Repo, key: int, coins: list[str], ts_ms: int) -> bool:
    """fills `liquidation` and ledger `liquidation` describe the same event: alert only the first (spec 5.2)."""
    recent = await repo.events_since(key, ts_ms - LIQUIDATION_DEDUPE_MS, [EventType.LIQUIDATION.value])
    return any(c and c in (e['payload'].get('coin') or '').split(',') for e in recent for c in coins)


async def _start_algo(bot: Bot, repo: Repo, key: int, address: str, algo_key: tuple[str, int],
                      orders: list[dict], now: int) -> dict:
    """ALGO_START as a new message (owner decision, PR C): the fill messages sent before detection
    stay as they are; progress edits go to the START message."""
    coin, sign = algo_key
    state = aggregator.new_algo_state(coin, sign, orders)
    position_after = to_decimal(orders[-1].get('position_after'))
    verb, side = algo_label(sign, position_after)
    await repo.upsert_algo(key, state)
    payload = {'coin': coin, 'sign': sign, 'verb': verb, 'side': side,
               'position_after': str(position_after) if position_after is not None else None,
               'started_ms': state['started_ms'], 'last_progress_ms': now}
    source = algo_source(coin, sign, state['started_ms'])
    event_id = await repo.record_event(events.dedupe_key(events.HYPERLIQUID, key, EventType.ALGO_START, source),
                                       key, EventType.ALGO_START.value, now, payload, events.SENT, now)
    logger.info(f"Algo started: {verb} {side} {coin} for {address} ({len(orders)} orders)")
    if event_id is not None:
        await deliver(bot, repo, key,
                      lambda alias: format_algo_progress(address, alias, state, verb, side, position_after, venue_of(key)),
                      event_id)
    return state


async def _update_algo_position(repo: Repo, key: int, k: tuple, state: dict, payload: dict) -> None:
    start = await repo.get_event_by_key(events.dedupe_key(
        events.HYPERLIQUID, key, EventType.ALGO_START, algo_source(k[0], k[1], int(state['started_ms']))))
    if start is not None:
        await repo.update_event_payload(start['event_id'],
                                        {**start['payload'], 'position_after': payload.get('position_after')})


async def process_fills(bot: Bot, repo: Repo, client: HyperliquidClient, key: int, address: str,
                        fills: list[dict], poll_ms: int) -> None:
    fill_events: list[Event] = adapter.fill_events(key, fills)
    if not fill_events:
        return

    # HIP-3: a fill on a dex we do not poll yet adds it to the account (spec 5.2)
    dexs = await repo.get_dexs(key)
    new_dexs = {e.meta['dex'] for e in fill_events if e.meta.get('dex')} - set(dexs)
    if new_dexs:
        await repo.set_dexs(key, list(set(dexs) | new_dexs))
        logger.info(f"Added HIP-3 dexs {sorted(new_dexs)} for {address}")

    for event in fill_events:
        event.meta['poll_ms'] = poll_ms
        if event.type in SPOT_TYPES and client is not None:
            event.meta['display_coin'] = await client.spot_display_name(event.coin)

    twap_states = list((await repo.active_twaps(key)).values())
    algos = await repo.active_algos(key)
    st = settings()
    payloads = [e.payload() for e in fill_events]
    twap_hit = [twap.is_suppressed(p, twap_states) for p in payloads]

    # Synthetic TWAP entry (spec 5.2): this poll's orders plus the ones sent earlier in the window
    batches: dict[tuple, list[dict]] = {}
    for p, hit in zip(payloads, twap_hit):
        k = aggregator.algo_key(p)
        if k is not None and not hit and k not in algos:
            batches.setdefault(k, []).append(p)
    window_since = poll_ms - int(st['algo_window_sec']) * 1000
    started_now = set()
    for k, batch in batches.items():
        recorded = [dict(e['payload'], _event_id=e['event_id'], _delivery=e['delivery'])
                    for e in await repo.events_since(key, window_since, list(aggregator.ALGO_TYPES))
                    if aggregator.algo_key(e['payload']) == k and e['delivery'] == events.SENT]
        orders = sorted(recorded + batch, key=lambda o: int(o['ts_ms']))
        if aggregator.should_start_algo(orders, st):
            algos[k] = await _start_algo(bot, repo, key, address, k, orders, poll_ms)
            started_now.add(k)

    for event, p, hit in zip(fill_events, payloads, twap_hit):
        k = aggregator.algo_key(p)
        delivery = events.SENT
        if hit:
            delivery = events.SUPPRESSED_TWAP
        elif k in algos:
            if k not in started_now:  # orders of the START cycle are already in its totals
                algos[k] = aggregator.add_to_algo(algos[k], p)
                await repo.upsert_algo(key, algos[k])
                await _update_algo_position(repo, key, k, algos[k], p)
            if p['type'] != EventType.POSITION_CLOSE.value:  # a full close is always alerted
                delivery = events.SUPPRESSED_ALGO
        if event.type == EventType.LIQUIDATION and \
                await _liquidation_already_alerted(repo, key, [event.coin], event.ts_ms):
            logger.info(f"Liquidation {event.coin} already alerted from the ledger, skipping")
            continue
        event_id = await repo.record_event(event.dedupe_key, key, p['type'], event.ts_ms, p, delivery,
                                           adapter.now_ms())
        if event_id is None:
            logger.info(f"Duplicate event {event.dedupe_key}, not sent")
            continue
        if delivery != events.SENT:
            logger.info(f"Event {event.dedupe_key} recorded as {delivery}, not sent")
            continue
        await _send_fill_event(bot, repo, key, address, event_id, p)


async def maintain_algos(bot: Bot, repo: Repo, key: int, address: str, now: int) -> None:
    """Progress edits every algo_progress_sec; ALGO_END after algo_idle_sec without fills (spec 5.2)."""
    st = settings()
    for (coin, sign), state in (await repo.active_algos(key)).items():
        source = algo_source(coin, sign, int(state['started_ms']))
        start = await repo.get_event_by_key(events.dedupe_key(events.HYPERLIQUID, key, EventType.ALGO_START, source))
        meta = start['payload'] if start else {}
        verb = meta.get('verb', 'accumulating')
        side = meta.get('side', 'LONG' if sign > 0 else 'SHORT')
        if aggregator.algo_is_idle(state, now, st):
            logger.info(f"Algo ended: {verb} {side} {coin} for {address}")
            await emit(bot, repo, key, EventType.ALGO_END, source, now,
                       {**state, 'verb': verb, 'side': side},
                       lambda alias, s=state, v=verb, sd=side: format_algo_end(address, alias, s, v, sd, venue_of(key)))
            await repo.delete_algo(key, coin, sign)
            continue
        if start and now - int(meta.get('last_progress_ms', 0)) >= int(st['algo_progress_sec']) * 1000:
            position_after = to_decimal(meta.get('position_after'))
            await repo.update_event_payload(start['event_id'], {**meta, 'last_progress_ms': now})
            await edit_or_send(bot, repo, key, start['event_id'],
                               lambda alias, s=state, v=verb, sd=side, pa=position_after:
                                   format_algo_progress(address, alias, s, v, sd, pa, venue_of(key)))


# Other venues (spec 6.1): snapshot, then fills through the shared pipeline, or a snapshot diff ---------

def diff_fills(previous: dict[str, dict], current: dict[str, dict], now_ms: int) -> list[dict]:
    """Synthetic fills from two snapshots for venues without a fills endpoint (spec 6.1):
    one fill per coin whose size changed, priced at the new entry price (or the old one on a close),
    no realized PnL, oid/tid derived from the time so the event has a dedupe key."""
    fills = []
    for coin in sorted(set(previous) | set(current)):
        before = to_decimal((previous.get(coin) or {}).get('szi')) or Decimal(0)
        after = to_decimal((current.get(coin) or {}).get('szi')) or Decimal(0)
        if before == after:
            continue
        delta = after - before
        side = 'B' if delta > 0 else 'A'
        price = to_decimal((current.get(coin) or previous.get(coin) or {}).get('entry_px'))
        direction, _ = fill_direction(side, before, abs(delta))
        fills.append({'coin': coin, 'px': str(price or 0), 'sz': str(abs(delta)), 'side': side, 'time': now_ms,
                      'startPosition': str(before), 'dir': direction, 'oid': f"diff:{coin}:{now_ms}",
                      'tid': f"diff:{coin}:{now_ms}", 'closedPnl': '0', 'fee': '0', 'feeToken': 'USDC',
                      'synthetic': True})
    return fills


async def poll_venue_account(bot: Bot, repo: Repo, adapter_obj, account: VenueAccount) -> tuple[bool, int]:
    """One poll of a non-HL venue account. Returns (activity seen, snapshot weight).

    Fills are fetched when a position size changed or an algo is active (spec 3.5 gating);
    an adapter without fills (fetch_events returns None) gets snapshot-diff fills instead.
    """
    key = account.venue_account_id
    register_account(key, account)
    client = None
    now = adapter.now_ms()
    previous = await repo.get_snapshot(key)
    snap = await adapter_obj.snapshot(account)
    current = {'': snap.positions}
    changed = previous is not None and adapter.snapshot_changed(previous, current)
    if previous is None:
        logger.info(f"Baseline {account.venue} snapshot for {account.address}#{account.account_ref}: "
                    f"{len(snap.positions)} positions")
    await repo.save_snapshot(key, current, now, str(snap.account_value) if snap.account_value is not None else None)
    activity = changed
    active_algo = bool(await repo.active_algos(key))
    if changed or active_algo or previous is None:      # baseline poll sets the trades cursor
        cursor = await repo.get_cursor(key, 'trades')
        result = await adapter_obj.fetch_events(account, cursor)
        if result is None:
            fills = diff_fills(previous.get('', {}) if previous else {}, snap.positions, now) if changed else []
        else:
            fills, new_cursor = result
            if new_cursor != cursor:
                await repo.set_cursor(key, 'trades', str(new_cursor), now)
        if fills:
            await process_fills(bot, repo, client, key, account.address, fills, now)
            activity = True
    await maintain_algos(bot, repo, key, account.address, adapter.now_ms())
    if activity:
        await repo.touch_activity(key, now)
    return activity, adapter_obj.cost('snapshot')


async def process_ledger(bot: Bot, repo: Repo, key: int, address: str, updates: list[dict]) -> None:
    for event in adapter.ledger_events(key, address, updates):
        payload = event.payload()
        if format_ledger_event(address, '', payload) is None:
            continue  # transfers not involving the wallet, or types the formatter does not show
        if event.type == EventType.LIQUIDATION and event.coin and \
                await _liquidation_already_alerted(repo, key, event.coin.split(','), event.ts_ms):
            logger.info(f"Liquidation {event.coin} already alerted from fills, skipping")
            continue
        delivery = events.FILTERED_SETTINGS if event.type in OFF_BY_DEFAULT else events.SENT
        event_id = await emit(bot, repo, key, event.type, event.source_id, event.ts_ms, payload,
                              lambda alias, p=payload: format_ledger_event(address, alias, p), delivery)
        if event_id is not None and event.type in (EventType.TRANSFER_IN, EventType.TRANSFER_OUT):
            await _accumulate_counterparty(repo, address, event)


async def _accumulate_counterparty(repo: Repo, address: str, event: Event) -> None:
    """Spec 7.3 background: a transfer counterparty becomes a weak wallet_links row, no alert."""
    other = str(event.meta.get('counterparty') or '').lower()
    if not other or related.is_system_address(other) or other == address.lower():
        return
    wallet_id = await repo.wallet_id(address)
    if wallet_id is None:
        return
    delta = event.meta.get('delta') or {}
    usd = delta.get('usdcValue') or delta.get('amount') or '0'
    direction = 'in' if event.type == EventType.TRANSFER_IN else 'out'
    await repo.add_weak_counterparty(wallet_id, other, direction, str(usd), event.ts_ms)


async def poll_fills(bot: Bot, repo: Repo, client: HyperliquidClient, key: int, address: str) -> bool:
    """Fetch and process new fills. Returns True if any fill arrived."""
    poll_ms = adapter.now_ms()
    cursor = await _cursor(repo, key, 'fills')
    fills, new_cursor = await adapter.fetch_fills(client, address, cursor)
    await process_fills(bot, repo, client, key, address, fills, poll_ms)
    if new_cursor != cursor:
        await repo.set_cursor(key, 'fills', str(new_cursor), adapter.now_ms())
    return bool(fills)


async def poll_ledger(bot: Bot, repo: Repo, client: HyperliquidClient, key: int, address: str) -> bool:
    """Fetch and process new ledger updates. Returns True if any arrived."""
    cursor = await _cursor(repo, key, 'ledger')
    updates, new_cursor = await adapter.fetch_ledger(client, address, cursor)
    await process_ledger(bot, repo, key, address, updates)
    if new_cursor != cursor:
        await repo.set_cursor(key, 'ledger', str(new_cursor), adapter.now_ms())
    return bool(updates)


async def monitor_transfers_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fills, ledger and algo upkeep for every account, unconditionally.

    The production loop is poller.poll_job, which fetches fills only on activity (spec 3.5);
    this job is the ungated path used by the replay tests.
    """
    repo: Repo = context.bot_data['repo']
    client: HyperliquidClient = context.bot_data['hl']
    accounts = await repo.tracked_accounts()
    for i, (key, address) in enumerate(accounts):
        try:
            await poll_fills(context.bot, repo, client, key, address)
            await poll_ledger(context.bot, repo, client, key, address)
            await maintain_algos(context.bot, repo, key, address, adapter.now_ms())
        except Exception as e:
            logger.error(f"Transfer check failed for {address}: {e}")
        if i < len(accounts) - 1:
            await asyncio.sleep(2)
    logger.info(f"Completed transfer check for {len(accounts)} wallets")
