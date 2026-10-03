"""Lighter VenueAdapter (spec 6.2): resolve by L1 address, snapshot by sub-account index, trades
normalised to the HL fill shape so the shared pipeline (spec 5.2) handles them.

Trade fields (PM live check 2026-10-04): our account is ask_account_id (sell) or bid_account_id (buy);
we are the maker when is_maker_ask matches our side; the position before the trade is
maker_position_size_before or taker_position_size_before accordingly; closed PnL is
ask_account_pnl / bid_account_pnl. type 'liquidation' becomes a LIQUIDATION event.
"""

import json
import logging
from decimal import Decimal
from typing import Optional

from hypermate.core.numbers import to_decimal
from hypermate.venues import base
from hypermate.venues.base import AccountSnapshot, VenueAccount, fill_direction, position_entry
from hypermate.venues.hyperliquid import scheduler
from hypermate.venues.lighter.client import LighterClient

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
EXPLORER_FALLBACK = 'https://app.lighter.xyz/'      # official explorer URL format not confirmed [?]
MAX_CATCHUP_PAGES = 5
LIQUIDATION_TYPES = ('liquidation',)


def parse_sub_accounts(body: dict) -> list[int]:
    return [int(s['index']) for s in (body or {}).get('sub_accounts') or [] if s.get('index') is not None]


def parse_account(account: dict) -> AccountSnapshot:
    """Positions with a non-zero size (the API lists every market with "0" / "0.0" / "0.00")."""
    positions = {}
    for pos in account.get('positions') or []:
        size = to_decimal(pos.get('position')) or ZERO
        if size == 0:
            continue
        sign = Decimal(int(pos.get('sign') or 1))
        szi = size * sign
        coin = str(pos.get('symbol') or pos.get('market_id'))
        positions[coin] = position_entry(coin, szi, to_decimal(pos.get('avg_entry_price')),
                                         to_decimal(pos.get('position_value')), to_decimal(pos.get('unrealized_pnl')))
    value = to_decimal(account.get('total_asset_value'))
    if value is None:
        value = to_decimal(account.get('collateral'))
    return AccountSnapshot(positions, value, raw=account,
                           extra={'collateral': str(account.get('collateral', '')),
                                  'available_balance': str(account.get('available_balance', ''))})


def normalise_trade(trade: dict, index: int, symbols: dict[int, str]) -> Optional[dict]:
    """One Lighter trade -> HL-shaped fill for our account, or None if the trade is not ours."""
    ask, bid = int(trade.get('ask_account_id', -1)), int(trade.get('bid_account_id', -1))
    if index == ask:
        side, maker = 'A', bool(trade.get('is_maker_ask'))
        oid, pnl = trade.get('ask_id'), trade.get('ask_account_pnl')
    elif index == bid:
        side, maker = 'B', not bool(trade.get('is_maker_ask'))
        oid, pnl = trade.get('bid_id'), trade.get('bid_account_pnl')
    else:
        return None
    before = 'maker_position_size_before' if maker else 'taker_position_size_before'
    start = to_decimal(trade.get(before)) or ZERO
    size = to_decimal(trade.get('size')) or ZERO
    direction, _ = fill_direction(side, start, size)
    coin = symbols.get(int(trade.get('market_id', -1)), f"market:{trade.get('market_id')}")
    fill = {
        'coin': coin, 'px': str(trade.get('price')), 'sz': str(size), 'side': side,
        'time': int(trade.get('timestamp') or 0), 'startPosition': str(start), 'dir': direction,
        'oid': oid if oid is not None else trade.get('trade_id'), 'tid': trade.get('trade_id'),
        'closedPnl': str(pnl) if pnl is not None else '0', 'fee': '0', 'feeToken': 'USDC',
        'hash': trade.get('tx_hash'), 'crossed': not maker,
    }
    if trade.get('type') in LIQUIDATION_TYPES:
        fill['liquidation'] = {'method': 'market', 'markPx': str(trade.get('price'))}
    return fill


def encode_cursor(last_trade_id: int) -> str:
    return json.dumps({'trade_id': int(last_trade_id)})


def decode_cursor(cursor: Optional[str]) -> int:
    """Newest trade_id already processed; anything that is not our JSON (e.g. the ms timestamp a new
    account's cursor row starts with) means "baseline": no replay, start from the newest trade."""
    if not cursor:
        return 0
    try:
        return int(json.loads(cursor).get('trade_id', 0))
    except (ValueError, AttributeError, TypeError):
        return 0


class LighterAdapter:
    venue = base.LIGHTER

    def __init__(self, client: LighterClient, stream=None) -> None:
        self.client = client
        self.stream = stream            # LighterStream or None (REST only)

    async def resolve(self, evm_address: str) -> list[VenueAccount]:
        subs = await self.client.accounts_by_l1_address(evm_address, priority=scheduler.P_LEDGER)
        return [VenueAccount(self.venue, str(int(s['index'])), evm_address.lower(),
                             meta={'collateral': str(s.get('collateral', '')), 'status': s.get('status')})
                for s in subs if s.get('index') is not None]

    async def snapshot(self, account: VenueAccount) -> AccountSnapshot:
        index = int(account.account_ref)
        cached = self.stream.positions_for(index) if self.stream is not None else None
        if cached is not None:
            return parse_account(cached)
        raw = await self.client.account(index, priority=scheduler.P_SNAPSHOT)
        if raw is None:
            return AccountSnapshot({}, None)
        return parse_account(raw)

    async def fetch_events(self, account: VenueAccount, cursor: Optional[str]) -> tuple[list[dict], Optional[str]]:
        """Trades newer than the cursor (last trade_id seen), oldest first.

        The API pages newest-first and its next_cursor walks backwards, so the cursor stored here is
        the newest trade_id already processed; the first page is read and, when every trade on it is
        new, older pages are followed (at most MAX_CATCHUP_PAGES) until the stored id is reached.
        With the WS stream connected, the buffered trades are used and no request is made.
        """
        index = int(account.account_ref)
        last_id = decode_cursor(cursor)
        symbols = await self.client.symbols(priority=scheduler.P_FILLS)
        trades: list[dict]
        if self.stream is not None and self.stream.connected and self.stream.has_trades(index):
            trades = self.stream.drain_trades(index)
        else:
            trades, next_cursor = await self.client.trades(index, priority=scheduler.P_FILLS)
            pages = 1
            while (trades and next_cursor and pages < MAX_CATCHUP_PAGES and last_id
                   and min(int(t['trade_id']) for t in trades) > last_id):
                older, next_cursor = await self.client.trades(index, cursor=next_cursor, priority=scheduler.P_FILLS)
                if not older:
                    break
                trades += older
                pages += 1
        fresh = [t for t in trades if int(t.get('trade_id', 0)) > last_id]
        if not last_id:
            fresh = []                                  # first poll: baseline only, no history replay
        fills = [f for f in (normalise_trade(t, index, symbols) for t in fresh) if f is not None]
        fills.sort(key=lambda f: (f['time'], int(f['tid'])))
        newest = max([last_id] + [int(t.get('trade_id', 0)) for t in trades])
        return fills, encode_cursor(newest)

    def explorer_url(self, account: VenueAccount) -> str:
        return EXPLORER_FALLBACK

    def cost(self, op: str) -> int:
        return {'resolve': 1, 'snapshot': 1, 'events': 1}.get(op, 1)

    async def health(self) -> dict:
        if self.stream is None:
            return {'mode': 'rest', 'detail': 'REST polling'}
        return {'mode': 'ws' if self.stream.connected else 'rest',
                'detail': self.stream.status_line()}
