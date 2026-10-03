import json
import sqlite3
from decimal import Decimal

from hypermate.db.repo import ADDED, ADDRESS_EXISTS, ALIAS_EXISTS, Repo

A = '0x' + 'a' * 40
B = '0x' + 'b' * 40

SPEC_TABLES = {'users', 'wallets', 'venue_accounts', 'subscriptions', 'cursors', 'snapshots',
               'twap_active', 'events', 'sent_messages', 'wallet_links', 'api_cache'}


async def test_schema_and_wal(repo, tmp_path):
    con = sqlite3.connect(repo.path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert SPEC_TABLES <= tables
    assert 'idx_events_account_ts' in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert con.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    # created the missing data/ directory
    assert (tmp_path / 'data').is_dir()


async def test_schema_is_idempotent(repo):
    again = Repo(repo.path)
    await again.connect()
    await again.close()


async def test_add_list_find_remove(repo):
    assert await repo.add_subscription(1, A, 'test_wallet_1', 1000) == ADDED
    assert await repo.add_subscription(1, B, 'Whale', 1000) == ADDED
    assert await repo.list_subscriptions(1) == [('test_wallet_1', A), ('Whale', B)]
    assert await repo.find_subscription(1, 'TEST_WALLET_1') == ('test_wallet_1', A)
    assert await repo.find_subscription(1, 'nope') is None
    assert await repo.find_subscription(2, 'test_wallet_1') is None
    assert await repo.add_subscription(1, B, 'whale', 1000) == ALIAS_EXISTS
    assert await repo.add_subscription(1, A, 'other', 1000) == ADDRESS_EXISTS
    assert await repo.remove_subscription(1, 'WHALE') is True
    assert await repo.remove_subscription(1, 'WHALE') is False
    assert await repo.list_subscriptions(1) == [('test_wallet_1', A)]


async def test_shared_wallet_polled_once_and_subscribers(repo):
    await repo.add_subscription(1, A, 'mine', 1000)
    await repo.add_subscription(2, A, 'theirs', 1000)
    await repo.add_subscription(2, B, 'b', 1000)
    accounts = await repo.tracked_accounts()
    assert [address for _, address in accounts] == [A, B]
    va_a = accounts[0][0]
    assert sorted(await repo.subscribers(va_a)) == [(1, 'mine'), (2, 'theirs')]
    await repo.remove_subscription(2, 'b')
    assert [address for _, address in await repo.tracked_accounts()] == [A]


async def test_cursors_start_at_add_time_and_reset_on_readd(repo):
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_cursor(va, 'fills') == '1000'
    assert await repo.get_cursor(va, 'ledger') == '1000'
    await repo.set_cursor(va, 'fills', '5000', 5000)
    await repo.save_snapshot(va, {'BTC': {'szi': '1'}}, 5000)

    # second subscriber: shared state untouched
    await repo.add_subscription(2, A, 'a2', 6000)
    assert await repo.get_cursor(va, 'fills') == '5000'
    assert await repo.get_snapshot(va) == {'': {'BTC': {'szi': '1'}}}   # Phase 0 shape read as main dex

    # everyone leaves, someone re-adds later: no replay of the gap
    await repo.remove_subscription(1, 'a')
    await repo.remove_subscription(2, 'a2')
    await repo.add_subscription(3, A, 'again', 9000)
    assert await repo.get_cursor(va, 'fills') == '9000'
    assert await repo.get_snapshot(va) is None


async def test_snapshot_and_cursor_roundtrip_survive_reconnect(repo):
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    await repo.save_snapshot(va, {'ETH': {'szi': '-2.5', 'entry_px': '3000.1'}}, 2000)
    await repo.set_cursor(va, 'ledger', '1700000000123', 2000)
    await repo.close()
    reopened = Repo(repo.path)
    await reopened.connect()
    assert await reopened.get_snapshot(va) == {'': {'ETH': {'szi': '-2.5', 'entry_px': '3000.1'}}}
    assert await reopened.get_cursor(va, 'ledger') == '1700000000123'
    await reopened.close()
    repo.db = None


async def test_account_value_stored_as_decimal_text(repo):
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    assert await repo.hl_account_value(A) is None
    await repo.save_snapshot(va, {}, 2000, '10500.123456789012345')
    assert await repo.hl_account_value(A) == '10500.123456789012345'
    row = await (await repo.db.execute(
        'SELECT typeof(account_value) FROM snapshots WHERE venue_account_id = ?', (va,))).fetchone()
    assert row[0] == 'text'


async def test_dex_snapshot_and_dexs_roundtrip(repo):
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    assert await repo.get_dexs(va) == []
    await repo.set_dexs(va, ['xyz', 'cash', 'xyz'])
    assert await repo.get_dexs(va) == ['cash', 'xyz']
    snap = {'': {'BTC': {'szi': '1'}}, 'xyz': {'xyz:MU': {'szi': '-3'}}}
    await repo.save_snapshot(va, snap, 2000, '10')
    assert await repo.get_snapshot(va) == snap


async def test_migration_adds_dexs_json_to_a_phase0_database(tmp_path):
    """A DB created by Phase 0 (venue_accounts without dexs_json) gets the column on connect, data intact."""
    path = tmp_path / 'old.db'
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE wallets (wallet_id INTEGER PRIMARY KEY, evm_address TEXT UNIQUE NOT NULL);
        CREATE TABLE venue_accounts (venue_account_id INTEGER PRIMARY KEY, wallet_id INTEGER NOT NULL,
          venue TEXT NOT NULL, account_ref TEXT NOT NULL, active INTEGER DEFAULT 1, last_activity_ms INTEGER,
          UNIQUE(venue, account_ref));
        INSERT INTO wallets VALUES (1, '%s');
        INSERT INTO venue_accounts (venue_account_id, wallet_id, venue, account_ref) VALUES (1, 1, 'hyperliquid', '%s');
    """ % (A, A))
    con.commit()
    con.close()
    for _ in range(2):  # idempotent
        repo = Repo(str(path))
        await repo.connect()
        assert await repo.get_dexs(1) == []
        await repo.close()
    assert 'algo_active' in {r[0] for r in sqlite3.connect(path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


async def test_events_sent_messages_and_algo_state(repo):
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    event_id = await repo.record_event('k1', va, 'position_open', 5000, {'coin': 'BTC'}, 'sent', 1)
    assert await repo.record_event('k1', va, 'position_open', 5000, {}, 'sent', 1) is None
    await repo.update_event_payload(event_id, {'coin': 'BTC', 'chain': {'orders': 2}})
    assert (await repo.get_event_by_key('k1'))['payload']['chain'] == {'orders': 2}
    assert [e['event_id'] for e in await repo.events_since(va, 4000, ['position_open'])] == [event_id]
    assert await repo.last_event_ts(va, 'position_open', 'BTC') == 5000
    await repo.add_sent_message(event_id, 1, 1, 77)
    assert await repo.sent_messages(event_id) == {1: (1, 77)}
    state = {'coin': 'BTC', 'sign': 1, 'started_ms': 1, 'last_fill_ms': 2, 'fills_count': 3,
             'total_sz': Decimal('0.5'), 'total_ntl': Decimal('43000.10')}
    await repo.upsert_algo(va, state)
    stored = (await repo.active_algos(va))[('BTC', 1)]
    assert stored['total_ntl'] == '43000.10' and stored['fills_count'] == 3
    await repo.delete_algo(va, 'BTC', 1)
    assert await repo.active_algos(va) == {}


async def test_prune_checkpoint_stats_and_algo_rows(repo):
    import os
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    old = await repo.record_event('old', va, 'position_open', 1_000, {'coin': 'BTC'}, 'sent', 1)
    new = await repo.record_event('new', va, 'position_close', 9_000, {'coin': 'BTC'}, 'sent', 1)
    await repo.add_sent_message(old, 1, 1, 7)
    await repo.add_sent_message(new, 1, 1, 8)
    stats = await repo.db_stats(10_000)
    assert stats == {'events_total': 2, 'events_24h': [('position_close', 1), ('position_open', 1)], 'sent_messages': 2}

    assert await repo.prune_events(5_000) == (1, 1)
    assert await repo.get_event_by_key('old') is None
    assert await repo.sent_messages(new) == {1: (1, 8)}
    await repo.checkpoint()
    assert os.path.getsize(repo.path + '-wal') == 0

    await repo.upsert_algo(va, {'coin': 'BTC', 'sign': -1, 'started_ms': 1, 'last_fill_ms': 2, 'fills_count': 3,
                                'total_sz': Decimal('0.5'), 'total_ntl': Decimal('43000')})
    rows = await repo.all_active_algos()
    assert [(r['address'], r['coin'], r['sign'], r['fills_count']) for r in rows] == [(A, 'BTC', -1, 3)]


async def test_slim_payloads_is_batched_and_runs_once(tmp_path, caplog):
    import json
    import logging
    path = str(tmp_path / 'hm.db')
    repo = Repo(path)
    await repo.connect()
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    fat = {'venue': 'hyperliquid', 'venue_account_id': va, 'type': 'position_open', 'coin': 'BTC',
           'meta': {'dir': 'Open Long', 'fills': 3, 'oid': 1},
           'chain': {'key': ['BTC', 'Open Long'], 'orders': 2,
                     'meta': {'dir': 'Open Long', 'oid': 1, 'fills': 3, 'first_ms': 5, 'fee': '1', 'sign': 1}}}
    for i in range(25):
        await repo.record_event(f'k{i}', va, 'position_open', 5000 + i, fat, 'sent', 1)
    await repo.db.execute("PRAGMA user_version = 0")
    await repo.db.commit()
    assert await repo.payloads_need_slimming()

    caplog.set_level(logging.INFO, logger='hypermate.db.repo')
    assert await repo.slim_payloads(batch=2) == (25, 25)          # 13 batches, progress logged at 10
    assert 'Slimming event payloads: 20 rows seen' in caplog.text
    stored = (await repo.get_event_by_key('k0'))['payload']
    assert 'venue' not in stored and 'venue_account_id' not in stored
    assert stored['meta'] == fat['meta']                                   # the order's own meta is kept
    assert stored['chain']['meta'] == {'dir': 'Open Long', 'sign': 1}    # copied meta reduced
    assert not await repo.payloads_need_slimming()
    assert await repo.slim_payloads() == (0, 0)                           # second run: nothing to do
    assert json.loads(json.dumps(stored)) == stored
    await repo.close()


async def test_slimming_200k_rows_keeps_memory_flat(tmp_path):
    """fix/migration-oom: peak RSS during the migration stays within 30 MB of the baseline."""
    import resource
    path = str(tmp_path / 'big.db')
    repo = Repo(path)
    await repo.connect()
    await repo.add_subscription(1, A, 'a', 1000)
    (va, _), = await repo.tracked_accounts()
    fat = json.dumps({'venue': 'hyperliquid', 'venue_account_id': va, 'type': 'position_increase', 'coin': 'BTC',
                      'size': '0.01', 'notional_usd': '860', 'meta': {'dir': 'Open Long', 'fills': 1, 'oid': 1},
                      'chain': {'key': ['BTC', 'Open Long'], 'orders': 3, 'size': '0.03',
                                'meta': {'dir': 'Open Long', 'oid': 1, 'fills': 1, 'first_ms': 5, 'fee': '0.1',
                                         'sign': 1, 'dex': '', 'fee_token': 'USDC'}}})
    for start in range(0, 200_000, 10_000):
        await repo.db.executemany(
            "INSERT INTO events (dedupe_key, venue_account_id, type, ts_ms, payload_json, delivery, created_at) "
            "VALUES (?, ?, 'position_increase', ?, ?, 'sent', 1)",
            [(f'k{i}', va, i, fat) for i in range(start, start + 10_000)])
    await repo.db.commit()
    await repo.db.execute("PRAGMA user_version = 0")
    await repo.db.commit()

    import gc
    gc.collect()
    baseline_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    seen, changed = await repo.slim_payloads()
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert (seen, changed) == (200_000, 200_000)
    assert peak_kb - baseline_kb < 30 * 1024, f"RSS grew {(peak_kb - baseline_kb) // 1024} MB"
    await repo.close()
