"""Tests against recorded api.hyperliquid.xyz responses (tests/fixtures, recorded 2026-10-03, addresses anonymized)."""

from collections import Counter
from decimal import Decimal

from hypermate.core import formatter
from hypermate.venues.hyperliquid import adapter
from hypermate.venues.hyperliquid.client import build_spot_names
from tests.helpers import FakeHLClient, check_telegram_html, load_fixture

# The wallet whose ledger was recorded: it is the user or the destination of every entry
LEDGER_WALLET = '0x32b60513b79c7f02fa77777adb495d2bd32a4b97'


def test_spot_meta_names():
    meta = load_fixture('hl_spotMeta.json')
    names = build_spot_names(meta)
    assert names['@107'] == 'HYPE'
    assert names['PURR/USDC'] == 'PURR' and names['@0'] == 'PURR'
    for pair in meta['universe']:
        assert f"@{pair['index']}" in names and pair['name'] in names
    # pairs quoted in something other than USDC keep the quote, e.g. HYPE/USDT0
    assert names['@207'] == 'HYPE/USDT0'
    assert names['@232'] == 'HYPE/USDH'


def test_fills_side_dir_and_perp_classification():
    fills = load_fixture('hl_userFillsByTime.json')
    assert len(fills) == 348
    assert {f['side'] for f in fills} == {'B', 'A'}
    assert {f['dir'] for f in fills} == {'Open Long', 'Close Long', 'Open Short', 'Close Short'}
    # every fill here is a perp fill, including HIP-3 dex coins such as "xyz:GOLD"
    assert not any(adapter.is_spot_coin(f['coin']) for f in fills)
    assert any(':' in f['coin'] for f in fills)
    # userFills twapId is always null (spec 5.1)
    assert {f['twapId'] for f in fills} == {None}


async def test_fetch_fills_on_recorded_fills_advances_cursor():
    fills = load_fixture('hl_userFillsByTime.json')
    hl = FakeHLClient()
    hl.fills['0xw'] = fills
    start = min(f['time'] for f in fills) - 1
    new, cursor = await adapter.fetch_fills(hl, '0xw', start)
    assert len(new) == 348 and not any(adapter.is_spot_coin(f['coin']) for f in new)
    assert cursor == max(f['time'] for f in fills)


def test_ledger_sends_produce_alerts():
    updates = load_fixture('hl_userNonFundingLedgerUpdates.json')
    assert Counter(u['delta']['type'] for u in updates) == {'send': 10}
    messages = [formatter.format_transfer_message(u, LEDGER_WALLET, 'test_wallet_1') for u in updates]
    assert all(m is not None for m in messages)
    for m in messages:
        check_telegram_html(m)
    sent = [m for m in messages if m.startswith('↗️')]
    received = [m for m in messages if m.startswith('↘️')]
    expected_sent = sum(u['delta']['user'] == LEDGER_WALLET for u in updates)
    assert len(sent) == expected_sent and len(received) == len(updates) - expected_sent
    assert 'sent 5,000.00 USDC ($5,000.00) to <code>0xb78a...3672</code>' in ''.join(sent)
    assert 'received 3,749.60 USDC ($3,749.60) from <code>0x287b...d61a</code>' in ''.join(received)


def test_clearinghouse_state_parsing_and_views():
    state = load_fixture('hl_clearinghouseState.json')
    positions = adapter.parse_positions(state)
    assert sorted(positions) == ['BTC', 'ETH', 'HYPE', 'SOL', 'STX']
    assert positions['BTC']['szi'] == '0.01444' and positions['BTC']['direction'] == 'LONG'
    assert adapter.parse_account_value(state) == Decimal('453.698173')
    assert formatter.account_value(state) == Decimal('453.698173')

    view = formatter.format_positions('w', '0x' + 'a' * 40, state, {'balances': []})
    check_telegram_html(view)
    assert view.count('<b>LONG</b>') == 5
    assert 'Margin Balance:</b> $453.70' in view
    summary = formatter.format_positions_summary_line('w', '0x' + 'a' * 40, state)
    assert '$453.70 · 5 positions · largest LONG $BTC $1,230' in summary


def test_twap_history_statuses_recorded_for_phase1():
    history = load_fixture('hl_twapHistory.json')
    assert {h['status']['status'] for h in history} == {'activated', 'finished', 'terminated', 'error'}
    # time is in seconds, state.timestamp in ms
    assert all(h['time'] < 10**11 < h['state']['timestamp'] for h in history)


def test_webdata2_twap_states_and_slice_fills_recorded_for_phase1():
    web = load_fixture('hl_webData2.json')
    active = {twap_id: state for twap_id, state in web['twapStates']}
    assert active and all({'coin', 'side', 'sz', 'executedSz', 'executedNtl', 'minutes', 'timestamp'} <= set(s)
                          for s in active.values())
    # webData2 also carries clearinghouseState (spec 5.1)
    assert 'assetPositions' in web['clearinghouseState']

    slices = load_fixture('hl_userTwapSliceFillsByTime.json')
    assert slices and all(set(s) == {'fill', 'twapId'} for s in slices)
    # slice fills belong to the TWAPs that webData2 reports as active
    assert {s['twapId'] for s in slices} <= set(active)
