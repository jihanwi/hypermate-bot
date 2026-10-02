from decimal import Decimal

from hypermate.core import formatter
from hypermate.core.formatter import h
from tests.helpers import check_telegram_html, clearinghouse, position

ADDR = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40
TRICKY_ALIASES = ['test_wallet_1', 'a*b[c]', '<script>&', 'whale_1 [main]']


def test_h_escapes_html():
    assert h('<b>&"') == '&lt;b&gt;&amp;&quot;'
    assert h('test_wallet_1') == 'test_wallet_1'


def test_position_alerts_are_valid_html_for_tricky_aliases():
    alerts = [
        {**_pos('BTC', '1.5'), 'alert_type': 'NEW_POSITION'},
        {**_pos('BTC', '2'), 'alert_type': 'POSITION_INCREASE', 'size_change': '0.5'},
        {**_pos('BTC', '1'), 'alert_type': 'POSITION_DECREASE', 'size_change': '1', 'remaining_size': '1'},
        {**_pos('BTC', '1'), 'alert_type': 'POSITION_CLOSED', 'closed_size': '1', 'closing_pnl': '12.5'},
        {**_pos('BTC', '1'), 'alert_type': 'LIQUIDATION', 'liquidated_size': '1', 'closing_pnl': '-900'},
    ]
    for alias in TRICKY_ALIASES:
        for alert in alerts:
            text = formatter.format_position_alert(ADDR, alias, alert)
            visible = check_telegram_html(text)
            assert alias in visible


def test_new_position_message():
    text = formatter.format_position_alert(ADDR, 'test_wallet_1', {**_pos('BTC', '-2', entry='86281.5'),
                                                                 'alert_type': 'NEW_POSITION'})
    assert 'opened a new <b>SHORT</b> on $BTC' in text
    assert '@ $86,281.5' in text
    assert f'href="https://hypurrscan.io/address/{ADDR}"' in text


def test_increase_message_uses_decimal_math():
    alert = {'coin': 'ETH', 'szi': '3', 'direction': 'LONG', 'entry_px': '3000',
             'position_value': '9300.3', 'unrealized_pnl': '0', 'alert_type': 'POSITION_INCREASE',
             'size_change': '0.1'}
    text = formatter.format_position_alert(ADDR, 'w', alert)
    # current px = 9300.3 / 3 = 3100.1 exactly; added = 0.1 * 3100.1 = 310.01
    assert 'just added <b>0.10</b> ($310.01) to <b>LONG</b> on $ETH at $3,100.1' in text


def test_closed_message_pnl_sign():
    alert = {**_pos('SOL', '10'), 'alert_type': 'POSITION_CLOSED', 'closed_size': '10', 'closing_pnl': '-5'}
    assert '🔴 -$5.00' in formatter.format_position_alert(ADDR, 'w', alert)


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


def test_spot_fill_messages_side_and_display_name():
    buy = formatter.format_spot_fill_message(
        {'coin': '@107', 'display_coin': 'HYPE', 'px': '0.1', 'sz': '3', 'side': 'B', 'time': 1}, ADDR, 'a_b')
    assert 'bought 3.00 HYPE for $0.30 @ $0.1000' in buy
    sell = formatter.format_spot_fill_message(
        {'coin': 'PURR/USDC', 'display_coin': 'PURR', 'px': '2', 'sz': '5', 'side': 'A', 'time': 1}, ADDR, 'a_b')
    assert sell.startswith('🔴') and 'sold 5.00 PURR for $10.00' in sell
    check_telegram_html(sell)


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
    assert formatter.format_list_line('w_1', ADDR, perp).endswith('· $5,000.00')
    assert formatter.format_list_line('w_1', ADDR, None).endswith('· n/a')
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


def _pos(coin, szi, entry='100'):
    direction = 'LONG' if Decimal(szi) > 0 else 'SHORT'
    return {'coin': coin, 'szi': szi, 'direction': direction, 'entry_px': entry,
            'position_value': str(abs(Decimal(szi)) * 100), 'unrealized_pnl': '0'}


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
