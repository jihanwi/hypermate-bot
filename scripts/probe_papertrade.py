"""Papertrade (HyperEVM, chain 999) probe for spec 6.7: decode Exchange logs, read the view functions,
measure the polling options. Stdlib only, read-only, nothing here is imported by the bot.

    python scripts/probe_papertrade.py abi               # topic0 / selector keccak check of the ABI fixture
    python scripts/probe_papertrade.py sample [--write]  # decode recent open/close/liquidation logs,
                                                         # cross-check with /query, BBO check; --write saves
                                                         # tests/fixtures/papertrade_logs_sample.json
    python scripts/probe_papertrade.py views <address>   # resolve + snapshot through the view functions
    python scripts/probe_papertrade.py poll [n_wallets]  # polling (a) vs (b), catch-up chunks, rate limit

The ABI comes from tests/fixtures/papertrade_exchange_abi.json (event names brute-forced against the
implementation's topic0s, view names from 4byte/openchain; see docs/API_NOTES.md, Papertrade).
"""

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

RPC_URL = 'https://rpc.hyperliquid.xyz/evm'
QUERY_URL = 'https://exchange.papertrade.xyz/query'
HL_INFO_URL = 'https://api.hyperliquid.xyz/info'
EXCHANGE = '0x6cd5661646289fb6e65ea5c032310fded797d0a2'
MULTICALL3 = '0xca11bde05977b3631167028862be2a173976ca11'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'
ROOT = pathlib.Path(__file__).resolve().parents[1]
ABI_PATH = ROOT / 'tests' / 'fixtures' / 'papertrade_exchange_abi.json'
SAMPLE_PATH = ROOT / 'tests' / 'fixtures' / 'papertrade_logs_sample.json'
# instrumentId -> (symbol, priceScale): frontend constant (exchange.papertrade.xyz wire chunk, 2026-10-15);
# the contract's instruments(id) has the HL assetIndex but no symbol or scale
INSTRUMENTS = {0: ('BTC', 10), 1: ('ETH', 100)}
TRADE_EVENTS = ('PositionOpened', 'PositionClosed', 'Liquidated')


# --- keccak-256 (pure Python, enough for signatures) ---

_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def keccak256(data: bytes) -> bytes:
    rate = 136
    p = bytearray(data) + b'\x01'
    while len(p) % rate:
        p.append(0)
    p[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(p), rate):
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(p[off + 8 * i:off + 8 * i + 8], 'little')
        for rc in _RC:
            c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
            a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
            a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            a[0][0] ^= rc
    return b''.join(a[i % 5][i // 5].to_bytes(8, 'little') for i in range(4))


# --- transport ---

class RpcError(Exception):
    def __init__(self, error: dict):
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.code = error.get('code')


STATS = {'calls': 0, 'rate_limited': 0, 'backoff_s': 0.0}


def _post(url: str, body, timeout=30) -> tuple[object, int]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'content-type': 'application/json'})
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return json.loads(raw), len(raw)


def rpc(method: str, params: list, retries: int = 8) -> tuple[object, int]:
    """(result, response bytes). -32005 (rate limited) backs off 1 s, 2 s, 4 s ... capped at 16 s."""
    delay = 1.0
    for attempt in range(retries):
        STATS['calls'] += 1
        out, size = _post(RPC_URL, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        err = out.get('error')
        if err and err.get('code') == -32005 and attempt < retries - 1:
            STATS['rate_limited'] += 1
            STATS['backoff_s'] += delay
            time.sleep(delay)
            delay = min(delay * 2, 16)
            continue
        if err:
            raise RpcError(err)
        return out['result'], size
    raise RpcError({'code': -32005, 'message': 'rate limited after retries'})


def rpc_batch(calls: list[tuple[str, list]]) -> tuple[list, int]:
    body = [{'jsonrpc': '2.0', 'id': i, 'method': m, 'params': p} for i, (m, p) in enumerate(calls)]
    STATS['calls'] += 1
    out, size = _post(RPC_URL, body)
    out = sorted(out, key=lambda r: r['id'])
    return [r.get('result', r.get('error')) for r in out], size


def multicall(calls: list[str], to: str = EXCHANGE) -> tuple[list[str], int]:
    """Multicall3.aggregate3 over `to` in one eth_call (the RPC caps a JSON-RPC batch at 20 calls)."""
    def enc_bytes(b: bytes) -> str:
        return format(len(b), '064x') + b.hex() + '00' * ((32 - len(b) % 32) % 32)
    elems = []
    for data in calls:
        body = bytes.fromhex(data[2:])
        elems.append(format(int(to, 16), '064x') + format(1, '064x') + format(0x60, '064x') + enc_bytes(body))
    offsets, pos = [], 32 * len(elems)
    for e in elems:
        offsets.append(format(pos, '064x'))
        pos += len(e) // 2
    payload = '0x82ad56cb' + format(0x20, '064x') + format(len(elems), '064x') + ''.join(offsets) + ''.join(elems)
    raw, size = rpc('eth_call', [{'to': MULTICALL3, 'data': payload}, 'latest'])
    n = _word(raw, 1)
    out = []
    for i in range(n):
        base = 64 + _word(raw, 2 + i)                       # byte offset of the (success, bytes) tuple
        ok = _word(raw, base // 32)
        blen = _word(raw, base // 32 + 2)
        start = 2 + 2 * (base + 96)
        out.append('0x' + raw[start:start + 2 * blen] if ok else None)
    return out, size


def query(path: str):
    req = urllib.request.Request(f"{QUERY_URL}{path}", headers={'User-Agent': UA, 'accept': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=20))


def hl_info(body: dict):
    return _post(HL_INFO_URL, body)[0]


# --- ABI ---

def load_abi() -> dict:
    return json.loads(ABI_PATH.read_text())


def signature(entry: dict) -> str:
    return f"{entry['name']}({','.join(i['type'] for i in entry['inputs'])})"


def topic0(entry: dict) -> str:
    return '0x' + keccak256(signature(entry).encode()).hex()


def selector(entry: dict) -> str:
    return '0x' + keccak256(signature(entry).encode()).hex()[:8]


def events_by_topic(abi: dict) -> dict[str, dict]:
    return {topic0(e): e for e in abi['abi'] if e['type'] == 'event'}


def view(abi: dict, name: str) -> dict:
    return next(e for e in abi['abi'] if e['type'] == 'function' and e['name'] == name)


def _word(data: str, i: int) -> int:
    return int(data[2 + 64 * i:2 + 64 * (i + 1)], 16)


def _value(kind: str, raw: int):
    if kind == 'address':
        return '0x' + format(raw, '040x')[-40:]
    if kind == 'bool':
        return bool(raw)
    if kind.startswith('int') and raw >> 255:
        return str(raw - (1 << 256))
    return str(raw) if kind.startswith(('uint', 'int')) else hex(raw)


def decode_log(log: dict, by_topic: dict) -> dict:
    entry = by_topic.get(log['topics'][0])
    if entry is None:
        return {'event': None, 'topic0': log['topics'][0]}
    topics = iter(log['topics'][1:])
    data_i = 0
    args = {}
    for inp in entry['inputs']:
        if inp.get('indexed'):
            args[inp['name']] = _value(inp['type'], int(next(topics), 16))
        else:
            args[inp['name']] = _value(inp['type'], _word(log['data'], data_i))
            data_i += 1
    return {'event': entry['name'], 'args': args}


def decode_output(entry: dict, data: str):
    if entry['outputs'] and entry['outputs'][0]['type'].endswith('[]'):
        n = _word(data, 1)
        return [str(_word(data, 2 + i)) for i in range(n)]
    return {o['name']: _value(o['type'], _word(data, i)) for i, o in enumerate(entry['outputs'])}


def call_data(entry: dict, *args) -> str:
    return selector(entry) + ''.join(format(int(a, 16) if isinstance(a, str) else int(a), '064x') for a in args)


def human(decoded: dict) -> dict:
    """Scaled values: prices / priceScale, margin / pnl / fees / 1e18 USD, notional = margin x leverage."""
    a = decoded['args']
    sym, scale = INSTRUMENTS.get(int(a.get('instrumentId', -1)), (None, None))
    out = {}
    if decoded['event'] == 'PositionOpened':
        margin = int(a['margin']) / 1e18
        out = {'coin': sym, 'side': 'long' if a['isLong'] else 'short', 'entry_px': int(a['entryPrice']) / scale,
               'margin_usd': margin, 'leverage': int(a['leverage']), 'notional_usd': margin * int(a['leverage'])}
        out['size'] = out['notional_usd'] / out['entry_px']
    elif decoded['event'] == 'PositionClosed':
        out = {'exit_px_raw': a['exitPrice'], 'raw_pnl_usd': int(a['rawPnl']) / 1e18,
               'adjusted_pnl_usd': int(a['adjustedPnl']) / 1e18, 'fee_usd': int(a['fee']) / 1e18}
    elif decoded['event'] == 'Liquidated':
        out = {'px_raw': a['price'], 'bust_px_raw': a['bustPrice'], 'fee_a_usd': int(a['feeA']) / 1e18,
               'fee_b_usd': int(a['feeB']) / 1e18}
    return out


# --- commands ---

def cmd_abi() -> None:
    abi = load_abi()
    seen = set(abi.get('observed_topic0', {}))
    print(f"{'topic0':<14} {'signature':<72} match")
    for e in abi['abi']:
        if e['type'] != 'event':
            continue
        t = topic0(e)
        print(f"{t[:12]:<14} {signature(e):<72} {'yes' if t in seen else 'not observed'}")
    for t, note in abi.get('unknown_topic0', {}).items():
        print(f"{t[:12]:<14} {'[?] ' + note:<72} no signature")
    bad = [e['name'] for e in abi['abi'] if e['type'] == 'function' and selector(e) != e.get('selector', selector(e))]
    print('function selectors ok' if not bad else f"selector mismatch: {bad}")


def recent_trade_logs(abi: dict, blocks: int = 40) -> tuple[int, list[dict]]:
    by_topic = events_by_topic(abi)
    topics = [t for t, e in by_topic.items() if e['name'] in TRADE_EVENTS]
    tip = int(rpc('eth_blockNumber', [])[0], 16)
    logs, _ = rpc('eth_getLogs', [{'address': EXCHANGE, 'fromBlock': hex(tip - blocks + 1), 'toBlock': hex(tip),
                                   'topics': [topics]}])
    return tip, logs


def cmd_sample(write: bool) -> None:
    abi = load_abi()
    by_topic = events_by_topic(abi)
    tip, logs = recent_trade_logs(abi, 60)
    decoded = [(lg, decode_log(lg, by_topic)) for lg in logs]
    # cross-check with the public /query feed: seq = block * 1e6 + logIndex
    feed = {int(e['seq']): e for e in query('/protocol/trades/recent')['events']}
    checked = 0
    for lg, d in decoded:
        e = feed.get(int(lg['blockNumber'], 16) * 10 ** 6 + int(lg['logIndex'], 16))
        if not e:
            continue
        h = human(d)
        a = d['args']
        if d['event'] == 'PositionOpened':
            assert e['kind'] == 'open' and e['positionId'] == a['positionId'], e
            assert abs(e['entry'] - h['entry_px']) < 1e-9 and e['leverage'] == h['leverage'], (e, h)
            assert abs(e['notionalUsd'] - h['notional_usd']) <= 1, (e, h)
        elif d['event'] == 'PositionClosed':
            scale = INSTRUMENTS[e['instrumentId']][1]
            assert e['kind'] == 'close' and abs(e['exit'] - int(a['exitPrice']) / scale) < 1e-9, (e, a)
            assert abs(e['pnlUsd'] - h['adjusted_pnl_usd']) < 0.01, (e, h)
        checked += 1
    print(f"tip {tip}: {len(logs)} trade logs in 60 blocks, {checked} matched /query/protocol/trades/recent")

    # BBO: the newest open per coin vs HyperCore mid right now (opens execute at the BBO mid)
    mids = hl_info({'type': 'allMids'})
    for coin in ('BTC', 'ETH'):
        opens = [human(d) for _, d in decoded if d['event'] == 'PositionOpened' and human(d)['coin'] == coin]
        if opens:
            px, mid = opens[-1]['entry_px'], float(mids[coin])
            print(f"{coin}: newest open entry {px} vs HL mid {mid} ({abs(px - mid) / mid * 100:.3f}%)")

    if write:
        pick, counts = [], {'PositionOpened': 12, 'PositionClosed': 6, 'Liquidated': 2}
        for lg, d in decoded:
            if counts.get(d['event'], 0) > 0:
                counts[d['event']] -= 1
                pick.append((lg, d))
        if counts['Liquidated']:
            pick += liquidation_samples(abi, by_topic, counts['Liquidated'])
        SAMPLE_PATH.write_text(json.dumps({
            '_note': 'Papertrade Exchange logs recorded by scripts/probe_papertrade.py sample --write; '
                     'public chain, addresses as is. decoded = ABI fixture names, human = scaled values.',
            'recorded_at_block': tip, 'instruments': {str(k): {'symbol': v[0], 'priceScale': v[1]}
                                                     for k, v in INSTRUMENTS.items()},
            'logs': [{'log': {k: lg[k] for k in ('blockNumber', 'blockTimestamp', 'logIndex',
                                                         'transactionHash', 'topics', 'data')},
                      'decoded': d, 'human': human(d)} for lg, d in pick]}, indent=1) + '\n')
        print(f"wrote {len(pick)} logs to {SAMPLE_PATH.relative_to(ROOT)}")


def liquidation_samples(abi: dict, by_topic: dict, n: int) -> list:
    """Liquidations are rare in a 60-block window: walk back in 250-block chunks."""
    liq = next(t for t, e in by_topic.items() if e['name'] == 'Liquidated')
    tip = int(rpc('eth_blockNumber', [])[0], 16)
    out = []
    for i in range(20):
        logs, _ = rpc('eth_getLogs', [{'address': EXCHANGE, 'fromBlock': hex(tip - 250 * (i + 1) + 1),
                                       'toBlock': hex(tip - 250 * i), 'topics': [liq]}])
        out += [(lg, decode_log(lg, by_topic)) for lg in logs[:n - len(out)]]
        if len(out) >= n:
            break
        time.sleep(1)
    return out


def cmd_views(address: str) -> None:
    abi = load_abi()
    address = address.lower()
    ids_fn, bal_fn, pos_fn = view(abi, 'getUserOpenPositionIds'), view(abi, 'balances'), view(abi, 'positions')
    t = time.time()
    (ids_raw, bal_raw), size = rpc_batch([
        ('eth_call', [{'to': EXCHANGE, 'data': call_data(ids_fn, address)}, 'latest']),
        ('eth_call', [{'to': EXCHANGE, 'data': call_data(bal_fn, address)}, 'latest'])])
    ids = decode_output(ids_fn, ids_raw)
    print(f"resolve (1 batch of 2 eth_call, {size} B, {time.time() - t:.2f}s): "
          f"{len(ids)} open positions, balance {int(bal_raw, 16) / 1e18:.6f} USD")
    if not ids:
        return
    t = time.time()
    rows, size = multicall([call_data(pos_fn, int(i)) for i in ids])
    print(f"snapshot (1 Multicall3 eth_call of {len(rows)} positions(id), {size:,} B, {time.time() - t:.2f}s)")
    if len(ids) <= 20:
        t = time.time()
        _, bsize = rpc_batch([('eth_call', [{'to': EXCHANGE, 'data': call_data(pos_fn, int(i))}, 'latest'])
                              for i in ids])
        print(f"  same as a JSON-RPC batch: {bsize:,} B, {time.time() - t:.2f}s (batch limit 20)")
    by_coin: dict[str, dict] = {}
    for i, raw in zip(ids, rows):
        p = decode_output(pos_fn, raw)
        sym, scale = INSTRUMENTS[int(p['instrumentId'])]
        notional = int(p['margin']) / 1e18 * int(p['leverage'])
        key = f"{sym} {'long' if p['isLong'] else 'short'}"
        agg = by_coin.setdefault(key, {'positions': 0, 'notional_usd': 0.0, 'margin_usd': 0.0})
        agg['positions'] += 1
        agg['notional_usd'] += notional
        agg['margin_usd'] += int(p['margin']) / 1e18
    for k, v in by_coin.items():
        print(f"  {k}: {v['positions']} positions, notional {v['notional_usd']:,.0f}, margin {v['margin_usd']:,.2f}")


def cmd_poll(n_wallets: int) -> None:
    abi = load_abi()
    by_topic = events_by_topic(abi)
    topics = [t for t, e in by_topic.items() if e['name'] in TRADE_EVENTS]
    tip = int(rpc('eth_blockNumber', [])[0], 16)
    frm = tip - 19
    t = time.time()
    logs, size_a = rpc('eth_getLogs', [{'address': EXCHANGE, 'fromBlock': hex(frm), 'toBlock': hex(tip),
                                        'topics': [topics]}])
    wallets = sorted({lg['topics'][2] for lg in logs})
    print(f"(a) 20 blocks, trade topic0s, no user filter: 1 call, {len(logs)} logs, {size_a:,} B, "
          f"{time.time() - t:.2f}s, {len(wallets)} distinct wallets")
    time.sleep(1)
    sizes, t = [], time.time()
    for w in wallets[:n_wallets]:
        out, size = rpc('eth_getLogs', [{'address': EXCHANGE, 'fromBlock': hex(frm), 'toBlock': hex(tip),
                                         'topics': [topics, None, w]}])
        sizes.append(size)
    if sizes:
        print(f"(b) 20 blocks, per wallet: {len(sizes)} calls, {sum(sizes):,} B total, "
              f"{sum(sizes) // len(sizes):,} B avg, {time.time() - t:.2f}s, rate-limited {STATS['rate_limited']}x, "
              f"backoff {STATS['backoff_s']:.0f}s")
    time.sleep(2)
    for span in (1000, 500, 250):
        try:
            t = time.time()
            logs, size = rpc('eth_getLogs', [{'address': EXCHANGE, 'fromBlock': hex(tip - span + 1),
                                              'toBlock': hex(tip), 'topics': [topics]}], retries=4)
            print(f"catch-up (a) {span} blocks: {len(logs)} logs, {size:,} B, {time.time() - t:.2f}s")
            break
        except RpcError as e:
            print(f"catch-up (a) {span} blocks: error {e}")
        time.sleep(2)
    if wallets:
        time.sleep(2)
        t = time.time()
        logs, size = rpc('eth_getLogs', [{'address': EXCHANGE, 'fromBlock': hex(tip - 999), 'toBlock': hex(tip),
                                          'topics': [topics, None, wallets[0]]}])
        print(f"catch-up (b) 1000 blocks, one wallet: {len(logs)} logs, {size:,} B, {time.time() - t:.2f}s")
    # back-to-back blockNumber to see when -32005 starts and how long it lasts
    burst, t0, first_limit = 0, time.time(), None
    while time.time() - t0 < 20:
        out, _ = _post(RPC_URL, {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_blockNumber'})
        if 'error' in out:
            first_limit = burst
            break
        burst += 1
    if first_limit is None:
        print(f"burst: {burst} calls in 20 s without -32005")
        return
    t1 = time.time()
    while 'error' in _post(RPC_URL, {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_blockNumber'})[0]:
        time.sleep(0.25)
    print(f"burst: -32005 after {first_limit} back-to-back calls ({t1 - t0:.1f}s), "
          f"cleared after {time.time() - t1:.1f}s")


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in ('abi', 'sample', 'views', 'poll'):
        print(__doc__)
        sys.exit(2)
    if argv[0] == 'abi':
        cmd_abi()
    elif argv[0] == 'sample':
        cmd_sample('--write' in argv)
    elif argv[0] == 'views':
        cmd_views(argv[1])
    else:
        cmd_poll(int(argv[1]) if len(argv) > 1 else 10)
    print(f"rpc calls {STATS['calls']}, -32005 {STATS['rate_limited']}x, backoff {STATS['backoff_s']:.0f}s")


if __name__ == '__main__':
    main(sys.argv[1:])
