"""v1 snapshot diff logic (kept in Phase 0, replaced in Phase 1)."""

from hypermate.venues.hyperliquid import adapter
from hypermate.venues.hyperliquid.client import build_spot_names
from tests.helpers import clearinghouse, position


def snap(*positions):
    return adapter.parse_positions(clearinghouse(*positions))


def test_parse_positions_skips_zero_and_keeps_strings():
    parsed = snap(position('BTC', '0.5'), position('ETH', '0.0'), position('SOL', '-3'))
    assert set(parsed) == {'BTC', 'SOL'}
    assert parsed['BTC']['szi'] == '0.5' and parsed['BTC']['direction'] == 'LONG'
    assert parsed['SOL']['direction'] == 'SHORT'


def test_diff_new_increase_decrease():
    prev = snap(position('BTC', '1'), position('ETH', '-2'))
    curr = snap(position('BTC', '1.5'), position('ETH', '-1'), position('SOL', '3'))
    alerts = {a['coin']: a for a in adapter.diff_positions(prev, curr)}
    assert alerts['SOL']['alert_type'] == 'NEW_POSITION'
    assert alerts['BTC']['alert_type'] == 'POSITION_INCREASE' and alerts['BTC']['size_change'] == '0.5'
    assert alerts['ETH']['alert_type'] == 'POSITION_DECREASE'
    assert alerts['ETH']['size_change'] == '1' and alerts['ETH']['remaining_size'] == '1'


def test_diff_unchanged_and_short_increase():
    prev = snap(position('BTC', '1'), position('ETH', '-2'))
    assert adapter.diff_positions(prev, prev) == []
    alerts = adapter.diff_positions(prev, snap(position('BTC', '1'), position('ETH', '-2.25')))
    assert [(a['coin'], a['alert_type'], a['size_change']) for a in alerts] == [('ETH', 'POSITION_INCREASE', '0.25')]


def test_diff_close_and_liquidation_heuristic():
    prev = snap(position('BTC', '1', position_value='1000', upnl='50'),
                position('ETH', '-1', position_value='1000', upnl='-151'),
                position('SOL', '1', position_value='1000', upnl='-150'))
    alerts = {a['coin']: a for a in adapter.diff_positions(prev, {})}
    assert alerts['BTC']['alert_type'] == 'POSITION_CLOSED' and alerts['BTC']['closing_pnl'] == '50'
    # B3 heuristic (Phase 1 replaces it): loss beyond 15% of position value
    assert alerts['ETH']['alert_type'] == 'LIQUIDATION' and alerts['ETH']['liquidated_size'] == '1'
    assert alerts['SOL']['alert_type'] == 'POSITION_CLOSED'


def test_flip_produces_no_alert_in_v1_logic():
    # v1 has no flip type: LONG 1 -> SHORT 1 is neither increase nor decrease, so no alert.
    # Documented here so Phase 1 (POSITION_FLIP) changes this test deliberately.
    assert adapter.diff_positions(snap(position('BTC', '1')), snap(position('BTC', '-1'))) == []


def test_is_spot_coin():
    assert adapter.is_spot_coin('PURR/USDC')
    assert adapter.is_spot_coin('@107')
    assert not adapter.is_spot_coin('BTC')
    assert not adapter.is_spot_coin('kPEPE')


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
