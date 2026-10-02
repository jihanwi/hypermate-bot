import sqlite3

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
    assert await repo.get_snapshot(va) == {'BTC': {'szi': '1'}}

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
    assert await reopened.get_snapshot(va) == {'ETH': {'szi': '-2.5', 'entry_px': '3000.1'}}
    assert await reopened.get_cursor(va, 'ledger') == '1700000000123'
    await reopened.close()
    repo.db = None
