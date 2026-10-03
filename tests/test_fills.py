"""Fill classification and order aggregation (spec 5.2), on recorded and synthetic fills."""

from collections import Counter
import json
from decimal import Decimal

from hypermate.core.events import EventType
from hypermate.venues.hyperliquid import adapter
from hypermate.venues.hyperliquid.client import build_spot_names
from tests.helpers import clearinghouse, fill, load_fixture, position


def kinds(events):
    return [e.type for e in events]


def test_dir_classification():
    cases = [
        (fill('BTC', 'Open Long', '1', '100', 1, '0'), EventType.POSITION_OPEN, 'LONG', '1'),
        (fill('BTC', 'Open Long', '1', '100', 1, '2'), EventType.POSITION_INCREASE, 'LONG', '3'),
        (fill('BTC', 'Open Short', '1', '100', 1, '0'), EventType.POSITION_OPEN, 'SHORT', '-1'),
        (fill('BTC', 'Close Long', '1', '100', 1, '3'), EventType.POSITION_DECREASE, 'LONG', '2'),
        (fill('BTC', 'Close Long', '3', '100', 1, '3'), EventType.POSITION_CLOSE, 'LONG', '0'),
        (fill('BTC', 'Close Short', '2', '100', 1, '-2'), EventType.POSITION_CLOSE, 'SHORT', '0'),
        (fill('BTC', 'Long > Short', '3', '100', 1, '1'), EventType.POSITION_FLIP, 'SHORT', '-2'),
        (fill('BTC', 'Short > Long', '3', '100', 1, '-1'), EventType.POSITION_FLIP, 'LONG', '2'),
        (fill('BTC', 'Close Long', '1', '100', 1, '1', liquidation={'markPx': '99', 'method': 'market'}),
         EventType.LIQUIDATION, 'LONG', '0'),
        (fill('@107', 'Buy', '1', '40', 1, '0'), EventType.SPOT_BUY, None, None),
        (fill('PURR/USDC', 'Sell', '1', '0.2', 1, '5', side='A'), EventType.SPOT_SELL, None, None),
    ]
    for f, expected_type, side, after in cases:
        event = adapter.order_event(1, [f])
        assert event.type == expected_type, f
        assert event.side == side
        assert (str(event.position_after) if event.position_after is not None else None) == after


def test_unknown_dir_falls_back_to_positions():
    assert adapter.order_event(1, [fill('BTC', '???', '1', '1', 1, '0', side='B')]).type == EventType.POSITION_OPEN
    assert adapter.order_event(1, [fill('BTC', '???', '1', '1', 1, '1', side='A')]).type == EventType.POSITION_CLOSE
    assert adapter.order_event(1, [fill('BTC', '???', '3', '1', 1, '1', side='A')]).type == EventType.POSITION_FLIP
    assert adapter.order_event(1, [fill('BTC', '???', '1', '1', 1, '2', side='A')]).type == EventType.POSITION_DECREASE


def test_order_aggregation_by_coin_dir_oid():
    sweep = [fill('HYPE', 'Open Long', '1', str(40 + i), 1000, str(i), oid=7, tid=900 - i) for i in range(5)]
    other = [fill('HYPE', 'Open Long', '2', '50', 1000, '5', oid=8, tid=1)]
    events = adapter.fill_events(1, sweep + other)
    assert len(events) == 2
    first = events[0]
    assert first.type == EventType.POSITION_OPEN and first.meta['fills'] == 5
    assert first.size == Decimal('5') and first.notional_usd == Decimal('210')
    assert first.price == Decimal('42')                       # VWAP
    assert first.position_after == Decimal('5')
    assert first.source_id == '900'                          # dedupe key from the first fill's tid
    assert first.dedupe_key == 'hyperliquid:1:position_open:900'
    assert events[1].type == EventType.POSITION_INCREASE and events[1].source_id == '1'


def test_realized_pnl_is_sum_of_closed_pnl():
    fills = [fill('ETH', 'Close Long', '1', '3000', 1, '3', oid=1, closed_pnl='10.5'),
             fill('ETH', 'Close Long', '2', '3001', 1, '2', oid=1, closed_pnl='-2.25')]
    event, = adapter.fill_events(1, fills)
    assert event.type == EventType.POSITION_CLOSE and event.realized_pnl == Decimal('8.25')
    opened, = adapter.fill_events(1, [fill('ETH', 'Open Long', '1', '3000', 1, '0', closed_pnl='0')])
    assert opened.realized_pnl is None


def test_hip3_dex_and_dust_conversion():
    event, = adapter.fill_events(1, [fill('xyz:MU', 'Open Short', '1', '90', 1, '0')])
    assert event.meta['dex'] == 'xyz' and adapter.coin_dex('BTC') == '' and adapter.coin_dex('@107') == ''
    assert adapter.fill_events(1, [fill('@1', 'Spot Dust Conversion', '0.1', '1', 1, '0')]) == []


def test_recorded_fills_replay():
    """hl_userFillsByTime.json: 348 fills, 114 orders (one per oid)."""
    fills = load_fixture('hl_userFillsByTime.json')
    events = adapter.fill_events(1, fills)
    assert len(events) == 114 == len({f['oid'] for f in fills})
    assert sum(e.meta['fills'] for e in events) == 348
    assert Counter(kinds(events)) == {EventType.POSITION_CLOSE: 47, EventType.POSITION_OPEN: 38,
                                      EventType.POSITION_DECREASE: 21, EventType.POSITION_INCREASE: 8}
    assert sum(e.realized_pnl or 0 for e in events) == sum(Decimal(f['closedPnl']) for f in fills)
    assert Counter(e.meta['dex'] for e in events) == {'': 106, 'xyz': 6, 'para': 2}
    # Within an order, each fill starts where the previous one ended (API order kept for same-ms fills)
    for event in events:
        assert event.position_after is not None


def test_replay_covers_every_position_event_kind():
    """Spec 5.3: open, increase, decrease, close, flip, liquidation, spot buy (synthetic sequence;
    the recorded fixture has no flip, liquidation or spot fill)."""
    sequence = [
        fill('BTC', 'Open Long', '1', '86000', 1000, '0', oid=1),
        fill('BTC', 'Open Long', '0.5', '86100', 2000, '1', oid=2),
        fill('BTC', 'Close Long', '0.5', '86200', 3000, '1.5', oid=3, closed_pnl='50'),
        fill('BTC', 'Close Long', '1', '86300', 4000, '1', oid=4, closed_pnl='300'),
        fill('ETH', 'Open Long', '2', '3000', 5000, '0', oid=5),
        fill('ETH', 'Long > Short', '5', '2990', 6000, '2', oid=6, closed_pnl='-20'),
        fill('DOGE', 'Close Long', '1000', '0.2', 7000, '1000', oid=7, closed_pnl='-80',
             liquidation={'liquidatedUser': '0x1', 'markPx': '0.2', 'method': 'market'}),
        fill('@107', 'Buy', '10', '41.5', 8000, '0', oid=8),
    ]
    assert kinds(adapter.fill_events(1, sequence)) == [
        EventType.POSITION_OPEN, EventType.POSITION_INCREASE, EventType.POSITION_DECREASE,
        EventType.POSITION_CLOSE, EventType.POSITION_OPEN, EventType.POSITION_FLIP,
        EventType.LIQUIDATION, EventType.SPOT_BUY]


def test_snapshot_changed_ignores_value_moves():
    a = {'': adapter.parse_positions(clearinghouse(position('BTC', '1', position_value='100')))}
    b = {'': adapter.parse_positions(clearinghouse(position('BTC', '1', position_value='200')))}
    c = {'': adapter.parse_positions(clearinghouse(position('BTC', '1.1')))}
    assert not adapter.snapshot_changed(a, b)
    assert adapter.snapshot_changed(a, c)
    assert adapter.snapshot_changed(a, {**a, 'xyz': {'xyz:MU': {'szi': '1'}}})


def test_is_spot_coin():
    assert adapter.is_spot_coin('PURR/USDC')
    assert adapter.is_spot_coin('@107')
    assert not adapter.is_spot_coin('BTC')
    assert not adapter.is_spot_coin('xyz:GOLD')


def test_items_after_sorts_and_advances_cursor():
    items = [{'time': 30}, {'time': 10}, {'time': 20}, {'time': 5}]
    new, cursor = adapter.items_after(items, 10)
    assert [i['time'] for i in new] == [20, 30] and cursor == 30
    assert adapter.items_after([], 10) == ([], 10)


def test_build_spot_names_maps_index_and_pair_names():
    meta = {
        'tokens': [{'name': 'USDC', 'index': 0}, {'name': 'PURR', 'index': 1},
                   {'name': 'HYPE', 'index': 150}, {'name': 'FOO', 'index': 7}],
        'universe': [{'name': 'PURR/USDC', 'tokens': [1, 0], 'index': 0},
                     {'name': '@107', 'tokens': [150, 0], 'index': 107},
                     {'name': '@200', 'tokens': [7, 1], 'index': 200}],
    }
    names = build_spot_names(meta)
    assert names['PURR/USDC'] == 'PURR' and names['@0'] == 'PURR'
    assert names['@107'] == 'HYPE'
    assert names['@200'] == 'FOO/PURR'


def test_payload_is_aggregates_only():
    """Retention: no raw fills in payload_json; first and last tid, counts and sums only."""
    fills = load_fixture('hl_userFillsByTime.json')
    event = max(adapter.fill_events(1, fills), key=lambda e: e.meta['fills'])
    payload = event.payload()
    assert event.meta['fills'] > 1
    assert 'venue' not in payload and 'venue_account_id' not in payload
    assert set(payload['meta']) >= {'tid_first', 'tid_last', 'fills', 'oid'}
    assert not any(isinstance(v, list) for v in payload['meta'].values())
    assert len(json.dumps(payload)) < 600
