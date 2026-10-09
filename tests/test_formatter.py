from decimal import Decimal

from hypermate.core import formatter
from hypermate.core.formatter import h
from hypermate.core import aggregator
from hypermate.core.events import EventType
from hypermate.venues.hyperliquid import adapter
from tests.helpers import check_telegram_html, clearinghouse, fill, position

ADDR = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40
TRICKY_ALIASES = ['test_wallet_1', 'a*b[c]', '<script>&', 'whale_1 [main]']


def test_h_escapes_html():
    assert h('<b>&"') == '&lt;b&gt;&amp;&quot;'
    assert h('test_wallet_1') == 'test_wallet_1'


def test_transfer_messages():
    def msg(delta):
        return formatter.format_transfer_message({'time': 1, 'hash': '0x', 'delta': delta}, ADDR, 'test_wallet_1')

    assert 'deposited $1,234.50' in msg({'type': 'deposit', 'usdc': '1234.5'})
    assert 'withdrew $10.00' in msg({'type': 'withdraw', 'usdc': '10', 'nonce': 1, 'fee': '1'})
    out = msg({'type': 'spotTransfer', 'token': 'HYPE', 'amount': '3', 'usdcValue': '150',
               'user': ADDR, 'destination': OTHER, 'fee': '0'})
    assert out.startswith('↗️') and 'sent 3.00 HYPE ($150.00) to <code>0xbbbb...bbbb</code>' in out
    inc = msg({'type': 'spotTransfer', 'token': 'HYPE', 'amount': '3', 'usdcValue': '150',
               'user': OTHER, 'destination': ADDR, 'fee': '0'})
    assert inc.startswith('↘️') and 'received' in inc
    assert 'from Perp to Spot' in msg({'type': 'accountClassTransfer', 'usdc': '5', 'toPerp': False})
    assert 'deposited $7.00 to vault' in msg({'type': 'vaultDeposit', 'vault': OTHER, 'usdc': '7'})
    assert 'withdrew $8.00 from vault' in msg({'type': 'vaultWithdraw', 'vault': OTHER, 'netWithdrawnUsd': '8'})
    assert msg({'type': 'vaultLeaderCommission', 'usdc': '1'}) is None
    assert msg({'type': 'rewardsClaim', 'amount': '1'}) is None
    for out in (msg({'type': 'deposit', 'usdc': '1'}), inc):
        check_telegram_html(out)


def test_positions_view():
    perp = clearinghouse(position('BTC', '0.5', entry_px='60000', position_value='31000', upnl='1000'),
                         position('ETH', '-2', entry_px='3000', position_value='6100', upnl='-100'),
                         account_value='12345.678')
    spot = {'balances': [{'coin': 'USDC', 'total': '50', 'entryNtl': '0'},
                         {'coin': 'HYPE', 'total': '0.001', 'entryNtl': '0.04'}]}
    text = formatter.format_positions('test_wallet_1', ADDR, perp, spot)
    check_telegram_html(text)
    assert '<b>LONG</b> $BTC — Size: $31,000 — Entry: $60,000 — PnL: 🟢 $1,000.00' in text
    assert '<b>SHORT</b> $ETH' in text and '🔴 -$100.00' in text
    assert '- USDC: 50.00 ($50.00)' in text and 'HYPE' not in text
    assert 'Margin Balance:</b> $12,345.68' in text


def test_stats_view():
    portfolio = [['day', {'vlm': '1'}],
                 ['allTime', {'vlm': '2500000', 'pnlHistory': [[1, '0'], [2, '-1234.5']]}]]
    text = formatter.format_stats('w_1', ADDR, portfolio)
    assert '🔴 -$1,234.50' in text and '$2.5M' in text
    assert formatter.format_stats('w', ADDR, [['day', {}]]) is None


def test_list_and_summary_lines():
    perp = clearinghouse(position('BTC', '1', position_value='90000'), position('ETH', '-1', position_value='3000'),
                         account_value='5000')
    row = formatter.format_list_row({'alias': 'w_1', 'address': ADDR, 'value': Decimal('5000'), 'venues': ['hyperliquid'],
                                     'positions': 2, 'mute': ''})
    assert row.startswith('• <b><a href=') and row.endswith('</a></b> · $5k · HL\n<code>0xaaaa…aaaa</code>')
    assert formatter.format_list_row({'alias': 'w', 'address': ADDR, 'value': None, 'venues': [], 'positions': 0,
                                      'mute': ''}).endswith('· n/a (idle)')
    summary = formatter.format_positions_summary_line('w_1', ADDR, perp)
    assert '$5,000.00 · 2 positions · largest LONG $BTC $90,000' in summary
    assert 'API error' in formatter.format_positions_summary_line('w_1', ADDR, None)


def test_split_message():
    lines = [f"line {i} " + 'x' * 90 for i in range(100)]
    chunks = formatter.split_message('\n'.join(lines), limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert '\n'.join(chunks) == '\n'.join(lines)
    assert formatter.split_message('short') == ['short']
    assert all(len(c) <= 10 for c in formatter.split_message('y' * 25, limit=10))


def test_usd_and_price_helpers():
    assert formatter.usd(Decimal('-1234.567')) == '-$1,234.57'
    assert formatter.price(Decimal('3100.1000')) == '$3,100.1'
    assert formatter.price(Decimal('2')) == '$2'


def _send(user, destination, token='USDC', amount='500000.0', usdc_value='500000.0'):
    # field set observed live for delta.type == 'send' (docs/API_NOTES.md)
    return {'time': 1, 'hash': '0x1', 'delta': {
        'type': 'send', 'user': user, 'destination': destination, 'sourceDex': '', 'destinationDex': '',
        'token': token, 'amount': amount, 'usdcValue': usdc_value, 'fee': '0.0',
        'nativeTokenFee': '0.0', 'nonce': 1, 'feeToken': ''}}


def test_send_out_and_in():
    out = formatter.format_transfer_message(_send(ADDR, OTHER), ADDR, 'test_wallet_1')
    assert out.startswith('↗️') and 'sent 500,000.00 USDC ($500,000.00) to <code>0xbbbb...bbbb</code>' in out
    inc = formatter.format_transfer_message(_send(OTHER, ADDR, token='HYPE', amount='10', usdc_value='450'),
                                            ADDR, 'test_wallet_1')
    assert inc.startswith('↘️') and 'received 10.00 HYPE ($450.00) from <code>0xbbbb...bbbb</code>' in inc
    for text in (out, inc):
        check_telegram_html(text)
    assert formatter.format_transfer_message(_send(OTHER, '0x' + 'c' * 40), ADDR, 'w') is None


def test_send_with_system_address_counterparty():
    system = '0x2000000000000000000000000000000000000000'
    out = formatter.format_transfer_message(_send(ADDR, system), ADDR, 'w')
    assert out.endswith('to Hyperliquid system')
    inc = formatter.format_transfer_message(_send('0x' + '0' * 40, ADDR), ADDR, 'w')
    assert inc.endswith('from Hyperliquid system')


def test_is_system_address():
    assert formatter.is_system_address('0x2000000000000000000000000000000000000000')
    assert formatter.is_system_address('0x0000000000000000000000000000000000000000')
    assert not formatter.is_system_address('0x2000000000000000000000000000000000000001')
    assert not formatter.is_system_address('0x1000000000000000000000000000000000000000')
    assert not formatter.is_system_address(OTHER)


def chain_of(*fills):
    """Debounce chain built from orders, the way the pipeline builds messages."""
    payloads = [adapter.order_event(1, [f]).payload() for f in fills]
    chain = aggregator.start_chain(payloads[0])
    for p in payloads[1:]:
        chain = aggregator.merge_into_chain(chain, p)
    return chain


def test_fill_messages_are_valid_html_for_tricky_aliases():
    chains = [
        chain_of(fill('BTC', 'Open Long', '14.5', '86281', 1, '0')),
        chain_of(fill('ETH', 'Open Long', '102', '3140', 1, '250')),
        chain_of(fill('ETH', 'Close Long', '102', '3140', 1, '352', closed_pnl='1200')),
        chain_of(fill('SOL', 'Close Short', '3000', '180', 1, '-3000', closed_pnl='18420')),
        chain_of(fill('BTC', 'Long > Short', '2', '86000', 1, '1')),
        chain_of(fill('DOGE', 'Close Long', '1000000', '0.21', 1, '1000000', closed_pnl='-5000',
                      liquidation={'markPx': '0.21', 'method': 'market'})),
        chain_of(fill('@107', 'Buy', '10', '41.5', 1, '0')),
    ]
    for alias in TRICKY_ALIASES:
        for chain in chains:
            assert alias in check_telegram_html(formatter.format_fill_message(ADDR, alias, chain))


def test_open_increase_reduce_close_flip_liquidation_texts():
    text = formatter.format_fill_message(ADDR, 'w', chain_of(fill('BTC', 'Open Long', '14.5', '86281', 1, '0')))
    assert text.startswith('[HL] 📈 <b><a href=') and 'opened LONG $BTC\n$1.25M (14.5 BTC) @ 86,281' in text
    text = formatter.format_fill_message(ADDR, 'w', chain_of(fill('ETH', 'Open Long', '102', '3140', 1, '250')))
    assert '➕' in text and 'added to LONG $ETH\n+$320k (102 ETH) @ 3,140 · now $1.11M' in text
    text = formatter.format_fill_message(ADDR, 'w', chain_of(
        fill('ETH', 'Close Long', '102', '3140', 1, '352', closed_pnl='1200')))
    assert 'reduced LONG $ETH\n-$320k (102 ETH) @ 3,140 · now $785k · realized 🟢 +$1,200' in text
    text = formatter.format_fill_message(ADDR, 'w', chain_of(
        fill('SOL', 'Close Short', '3000', '180', 1, '-3000', closed_pnl='18420')), held_ms=(2 * 24 + 4) * 3_600_000)
    assert '🔒' in text and 'closed SHORT $SOL\n$540k (3,000 SOL) @ 180.00 · realized 🟢 +$18,420 · held 2d 4h' in text
    text = formatter.format_fill_message(ADDR, 'w', chain_of(fill('BTC', 'Long > Short', '2', '86000', 1, '1')))
    assert 'flipped to SHORT $BTC' in text and 'now $86k' in text
    text = formatter.format_fill_message(ADDR, 'w', chain_of(fill(
        'DOGE', 'Close Long', '1000000', '0.21', 1, '1000000', closed_pnl='-5000',
        liquidation={'markPx': '0.21', 'method': 'market'})))
    assert '🔥' in text and 'LIQUIDATED LONG $DOGE' in text and 'realized 🔴 -$5,000' in text


def test_hip3_coin_label_and_spot_display_name():
    text = formatter.format_fill_message(ADDR, 'w', chain_of(fill('xyz:MU', 'Open Short', '100', '95.5', 1, '0')))
    assert 'opened SHORT $MU · xyz\n$9.55k (100 MU) @ 95.50' in text
    chain = chain_of(fill('@107', 'Buy', '10', '41.5', 1, '0', side='B'))
    chain['meta']['display_coin'] = 'HYPE'
    text = formatter.format_fill_message(ADDR, 'w', chain)
    assert '🟢' in text and 'bought 10 $HYPE\n$415 @ 41.50' in text


def test_merged_chain_shows_totals_and_fill_count():
    chain = chain_of(*[fill('HYPE', 'Open Long', '10', '40', 1000 * i, str(10 * i), oid=i) for i in range(3)])
    text = formatter.format_fill_message(ADDR, 'w', chain)
    assert 'opened LONG $HYPE\n$1.2k (30 HYPE) @ 40.00 · 3 fills' in text


def test_humanize_minutes():
    assert formatter.humanize_minutes(10080) == '7d'
    assert formatter.humanize_minutes(8302) == '5d 18h'
    assert formatter.humanize_minutes(90) == '1h 30m'
    assert formatter.humanize_minutes(5) == '5m'
    assert formatter.humanize_minutes(0) == '0m'
    assert formatter.humanize_minutes(1441) == '1d 1m'


def test_algo_messages():
    state = {'coin': 'BTC', 'sign': 1, 'started_ms': 0, 'last_fill_ms': 300_000, 'fills_count': 12,
             'total_sz': '0.4757', 'total_ntl': '41000'}
    verb, side = formatter.algo_label(1, Decimal('416.5'))
    assert (verb, side) == ('accumulating', 'LONG')
    text = formatter.format_algo_progress(ADDR, 'loracle', state, verb, side, Decimal('416.5'))
    check_telegram_html(text)
    assert '🤖' in text and 'algo accumulating LONG $BTC\n12 fills +$41k in 5m · pos $35.9M avg 86,189' in text
    assert formatter.algo_label(1, Decimal('-5000')) == ('closing', 'SHORT')
    end = formatter.format_algo_end(ADDR, 'loracle', {**state, 'last_fill_ms': 58 * 60_000}, verb, side)
    assert 'algo done accumulating LONG $BTC\n+$41k (0.4757 BTC) avg 86,189 · 12 fills · 58m' in end
    end = formatter.format_algo_end(ADDR, 'x', {**state, 'coin': 'CASHCAT'}, 'closing', 'SHORT')
    assert 'algo done closing SHORT $CASHCAT' in end


def test_recent_view_marks_unsent_events():
    events = [
        {'event_id': 1, 'type': EventType.POSITION_OPEN.value, 'ts_ms': 1_790_000_000_000, 'delivery': 'sent',
         'payload': {'coin': 'BTC', 'side': 'LONG', 'notional_usd': '1250000'}},
        {'event_id': 2, 'type': EventType.POSITION_INCREASE.value, 'ts_ms': 1_790_000_060_000,
         'delivery': 'summarized', 'payload': {'coin': 'BTC', 'side': 'LONG', 'notional_usd': '5000'}},
    ]
    text = formatter.format_recent('w_1', events)
    check_telegram_html(text)
    lines = text.split('\n')[1:]
    assert 'position open LONG $BTC $1.25M' in lines[0] and '<i>' not in lines[0]
    assert lines[1].startswith('<i>') and 'not sent (summary mode)' in lines[1]
    assert 'No events' in formatter.format_recent('w', [])


def test_positions_folds_dust_and_puts_funding_on_the_line():
    perp = clearinghouse(
        position('BTC', '1', position_value='90000', upnl='100'),
        position('ENA', '-13', position_value='3.06'),
        position('PONS', '-14', position_value='6.5'),
    )
    perp['assetPositions'][0]['position']['cumFunding'] = {'sinceOpen': '-49800'}   # negative = received
    text = formatter.format_positions('w', ADDR, perp, {'balances': []}, Decimal(10))
    check_telegram_html(text)
    assert 'LONG</b> $BTC' in text and '· funding +$49.8k' in text
    assert '$ENA' not in text and '$PONS' not in text
    assert '- + 2 dust positions (under $10)' in text
    perp['assetPositions'][0]['position']['cumFunding'] = {'sinceOpen': '120'}
    assert '· funding -$120' in formatter.format_positions('w', ADDR, perp, {'balances': []}, Decimal(10))
    # threshold 0 shows everything
    assert 'dust' not in formatter.format_positions('w', ADDR, perp, {'balances': []}, Decimal(0))


def test_algo_progress_elapsed_runs_to_the_last_fill():
    state = {'coin': 'BTC', 'sign': 1, 'started_ms': 0, 'last_fill_ms': 52 * 60_000, 'fills_count': 411,
             'total_sz': Decimal('8.382'), 'total_ntl': Decimal('709000')}
    progress = formatter.format_algo_progress(ADDR, 'loracle', state, 'accumulating', 'LONG', Decimal('429'))
    end = formatter.format_algo_end(ADDR, 'loracle', state, 'accumulating', 'LONG')
    assert '411 fills +$709k in 52m' in progress and '· 411 fills · 52m' in end


def test_price_keeps_the_venue_step_price_decimals_but_hl_logic_unchanged():
    """post-deploy 1005b (5): PUMP entry 0.006268 (step_price 0.000001) rendered $0.006268, not $0.0063."""
    from hypermate.core.formatter import price
    assert price(Decimal('0.006268'), 6) == '$0.006268' and price(Decimal('1311.49'), 2) == '$1,311.49'
    assert price(Decimal('0.000000012345'), 12) == '$0.00000001'          # capped at 8
    assert price(Decimal('0.00631234')) == '$0.0063' and price(Decimal('60000')) == '$60,000'   # HL as before


def test_list_sorts_abbreviates_folds_idle_and_splits():
    """fix/list-format (2026-10-09): header with count and total, value descending then alias, $1.2k / $47.4M
    amounts, 🐳 / • / · markers, '(idle)' instead of the address line at $0 with nothing open, 🔇 marker,
    rows never split across messages."""
    def row(alias, value, venues=('hyperliquid',), positions=1, mute='', address=ADDR):
        return {'alias': alias, 'address': address, 'value': Decimal(value) if value is not None else None,
                'venues': list(venues), 'positions': positions, 'mute': mute}

    rows = [row('bob', '1234'), row('whale', '47400000', ('hyperliquid', 'risex')), row('amy', '1234'),
            row('idle', '0', ('lighter',), positions=0), row('muted', '950000', mute='🔇 5h'),
            row('unknown', None, (), positions=0), row('flat', '0', positions=1)]
    (text,) = formatter.format_list(rows)
    check_telegram_html(text)
    lines = text.split('\n')
    assert lines[0] == '<b>Tracked wallets (7)</b> · total $48.4M'
    names = [line.split('</a></b>')[0].split('>')[-1] for line in lines if line.startswith(('🐳', '•', '·'))]
    assert names == ['whale', 'muted', 'amy', 'bob', 'flat', 'idle', 'unknown']       # value desc, then alias
    assert lines[1].startswith('🐳 ') and '· $47.4M · HL RISE' in lines[1] and lines[2] == f'<code>{formatter.ellipsis_address(ADDR)}</code>'
    assert '• <b>' in lines[3] and '· $950k · HL · 🔇 5h' in lines[3]
    assert any(l.startswith('· <b>') and l.endswith('· $0 · LTR (idle)') for l in lines)
    assert any(l.startswith('· <b>') and l.endswith('· $0 · HL') for l in lines)       # $0 but a position: address shown
    assert any(l.endswith('· n/a (idle)') for l in lines)
    assert text.count('<code>') == 5 and formatter.ellipsis_address('0xefe41234567890abcdef1234567890abcdef194b') == '0xefe4…194b'
    # 25 wallets: split under the limit at row boundaries, header once
    many = [row(f'w{i:02d}', str(1000 + i)) for i in range(25)]
    chunks = formatter.format_list(many, limit=700)
    assert len(chunks) > 1 and all(len(c) <= 700 for c in chunks)
    assert chunks[0].startswith('<b>Tracked wallets (25)</b>') and not any(c.startswith('<b>Tracked') for c in chunks[1:])
    assert all(c.startswith(('•', '<b>Tracked')) and c.endswith('</code>') for c in chunks)
    assert sum(c.count('<code>') for c in chunks) == 25
