from decimal import Decimal

from hypermate.venues.hyperliquid import client as client_mod
from hypermate.venues.hyperliquid.client import HyperliquidAPIError, HyperliquidClient

META = {'tokens': [{'name': 'USDC', 'index': 0}, {'name': 'HYPE', 'index': 150}],
        'universe': [{'name': '@107', 'tokens': [150, 0], 'index': 107}]}


async def test_spot_names_fetched_on_first_use_and_cached(monkeypatch):
    # monotonic() can be small right after boot; the first call must still fetch
    clock = {'now': 5.0}
    monkeypatch.setattr(client_mod.time, 'monotonic', lambda: clock['now'])
    hl = HyperliquidClient('http://unused', spot_meta_ttl_sec=3600)
    calls = []

    async def fake_info(payload):
        calls.append(payload['type'])
        return META

    monkeypatch.setattr(hl, '_info', fake_info)
    assert await hl.spot_display_name('@107') == 'HYPE'
    assert await hl.spot_display_name('@999') == '@999'
    assert calls == ['spotMeta']
    clock['now'] += 3601
    await hl.spot_display_name('@107')
    assert calls == ['spotMeta', 'spotMeta']


async def test_spot_names_fall_back_to_raw_coin_when_api_fails(monkeypatch):
    hl = HyperliquidClient('http://unused')

    async def failing(payload):
        raise HyperliquidAPIError('HTTP 500')

    monkeypatch.setattr(hl, '_info', failing)
    assert await hl.spot_display_name('@107') == '@107'


def test_json_floats_parse_as_decimal():
    parsed = client_mod._json_loads('{"a": 0.1, "b": "0.2", "c": 3}')
    assert parsed == {'a': Decimal('0.1'), 'b': '0.2', 'c': 3}
    assert isinstance(parsed['a'], Decimal)


async def test_perp_dexs_reads_names_from_live_shape(monkeypatch):
    """PM live check 2026-10-03: [null, {name, fullName, deployer, ...}, ...]."""
    hl = HyperliquidClient('http://unused')
    calls = []

    async def fake_info(payload):
        calls.append(payload)
        return [None,
                {'name': 'xyz', 'fullName': 'XYZ', 'deployer': '0x' + '1' * 40, 'oracleUpdater': None,
                 'feeRecipient': None, 'assetToStreamingOiCap': []},
                {'name': 'para', 'fullName': 'Para', 'deployer': '0x' + '2' * 40}]

    monkeypatch.setattr(hl, '_info', fake_info)
    assert await hl.perp_dexs() == ['xyz', 'para']
    assert await hl.perp_dexs() == ['xyz', 'para']
    assert calls == [{'type': 'perpDexs'}]
