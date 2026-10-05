"""fix/maintenance-lock: startup maintenance while the poller keeps reading and writing the same
connection (deploy log 2026-10-04: "database table is locked" after the slimming step)."""

import asyncio
import json
import logging
import sqlite3

from hypermate.bot import commands
from hypermate.config import Config
from hypermate.db import backup
from hypermate.db.repo import Repo
from tests.helpers import FakeHLClient, check_telegram_html, make_context, make_update

A = '0x' + 'a' * 40
T0 = 1_791_100_000_000


async def seed(repo: Repo, va: int, rows: int) -> None:
    fat = json.dumps({'venue': 'hyperliquid', 'venue_account_id': va, 'type': 'position_increase', 'coin': 'BTC',
                      'meta': {'dir': 'Open Long', 'fills': 1, 'oid': 1},
                      'chain': {'key': ['BTC', 'Open Long'], 'meta': {'dir': 'Open Long', 'oid': 1, 'fee': '1'}}})
    await repo.db.executemany(
        "INSERT INTO events (dedupe_key, venue_account_id, type, ts_ms, payload_json, delivery, created_at) "
        "VALUES (?, ?, 'position_increase', ?, ?, ?, 1)",
        [(f'old{i}', va, T0 - i, fat, 'suppressed_algo' if i % 3 == 0 else 'sent') for i in range(rows)])
    await repo.db.execute("PRAGMA user_version = 0")
    await repo.db.commit()


async def test_busy_timeout_is_set(repo):
    cur = await repo.db.execute("PRAGMA busy_timeout")
    assert (await cur.fetchone())[0] == 30000
    await cur.close()


async def test_maintenance_runs_while_the_poller_reads_and_writes(tmp_path, caplog):
    """A concurrent writer keeps inserting events and reading through the repo methods (the poller's
    patterns) while prune, slimming, cleanup and the WAL checkpoint run. Before the fix an open read
    cursor made the DELETE / checkpoint fail with "database table is locked"."""
    caplog.set_level(logging.INFO)
    repo = Repo(str(tmp_path / 'hm.db'))
    await repo.connect()
    await repo.add_subscription(7, A, 'w', T0)
    (va, _), = await repo.tracked_accounts()
    await seed(repo, va, 6000)
    written = []
    stop = asyncio.Event()

    async def writer():
        i = 0
        while not stop.is_set():
            key = f'new{i}'
            await repo.record_event(key, va, 'position_open', T0 + i, {'coin': 'ETH', 'n': i}, 'sent', T0)
            written.append(key)
            # the poller's read patterns, interleaved with the maintenance batches
            await repo.get_snapshot(va)
            await repo.active_algos(va)
            await repo.events_since(va, T0, ['position_open'])
            await repo.get_cursor(va, 'fills')
            await repo.tracked_accounts_with_activity()
            i += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(writer())
    status = {}
    try:
        await backup.startup_maintenance(repo, T0, status)
    finally:
        stop.set()
        await task
        if not status.get('state') == 'ok':
            await repo.close()           # a failed run must not leave the connection thread alive (pytest hangs)

    assert status['state'] == 'ok' and status['attempts'] == 1, status
    # slimming walks the 6000 seeded rows (plus what the writer added before it started), then the
    # cleanup removes the 2000 suppressed ones, then the checkpoint truncates the WAL
    assert status['cleaned'] == 2000 and status['slimmed'].startswith('6000/') and status['checkpoint'] != 'busy'
    # 2000 small rows gone leaves a freelist far under 30% / 10 MB: no VACUUM, but the decision is logged
    assert status['vacuum'] == 'skipped' and status['freelist'].startswith('0 MB (') and status['db'].endswith(' MB')
    assert 'table is locked' not in caplog.text and 'failed' not in caplog.text
    assert len(written) >= 5, len(written)                  # the writer kept going between batches
    cur = await repo.db.execute("SELECT COUNT(*) FROM events WHERE dedupe_key LIKE 'new%'")
    assert (await cur.fetchone())[0] == len(written)
    await cur.close()
    cur = await repo.db.execute("SELECT COUNT(*) FROM events WHERE delivery LIKE 'suppressed%'")
    assert (await cur.fetchone())[0] == 0
    await cur.close()
    assert await repo.user_version() == Repo.SUPPRESSED_CLEANUP_VERSION
    await repo.close()


async def test_open_read_statement_no_longer_blocks_the_checkpoint(tmp_path):
    """The failure mode itself: with a read cursor left open on the same connection, a TRUNCATE
    checkpoint reports busy; the repo methods close their cursors, so checkpoint() completes."""
    repo = Repo(str(tmp_path / 'hm.db'))
    await repo.connect()
    await repo.add_subscription(7, A, 'w', T0)
    (va, _), = await repo.tracked_accounts()
    await seed(repo, va, 500)
    # an un-consumed cursor (the old pattern) keeps a read transaction open
    leaked = await repo.db.execute("SELECT event_id FROM events")
    await leaked.fetchone()
    busy, _, _ = await repo.checkpoint()
    assert busy == 1
    await leaked.close()
    busy, _, pages = await repo.checkpoint()
    assert busy == 0
    # every repo read closes its cursor: a checkpoint right after them is never busy
    await repo.events_since(va, 0)
    await repo.recent_events(va, 5)
    await repo.venue_accounts_of(A)
    await repo.db_stats(T0)
    await repo.all_active_algos()
    assert (await repo.checkpoint())[0] == 0
    await repo.close()


async def test_maintenance_retries_once_and_health_shows_the_outcome(repo, monkeypatch):
    monkeypatch.setattr(backup, 'MAINTENANCE_RETRY_SEC', 0)
    monkeypatch.setattr(Config, 'ADMIN_USER_IDS', frozenset({1}))
    await repo.add_subscription(7, A, 'w', T0)
    (va, _), = await repo.tracked_accounts()
    await seed(repo, va, 30)
    calls = {'n': 0}
    original = repo.checkpoint

    async def flaky_checkpoint():
        calls['n'] += 1
        if calls['n'] == 1:
            raise sqlite3.OperationalError('database table is locked')
        return await original()

    monkeypatch.setattr(repo, 'checkpoint', flaky_checkpoint)
    status = {}
    await backup.startup_maintenance(repo, T0, status)
    assert status['state'] == 'ok' and status['attempts'] == 2 and status['error'] is None
    context = make_context({'repo': repo, 'hl': FakeHLClient(), 'maintenance': status})
    update = make_update(1)
    await commands.health_command(update, context)
    text = update.message.replies[0]['text']
    check_telegram_html(text)
    assert 'Maintenance: ok' in text and '(attempt 2, db ' in text and 'freelist' in text and 'checkpoint' in text

    # both attempts fail: status failed with the error, health says so
    async def broken():
        raise sqlite3.OperationalError('database table is locked')

    monkeypatch.setattr(repo, 'checkpoint', broken)
    await repo.db.execute("PRAGMA user_version = 0")
    await repo.db.commit()
    status = {}
    await backup.startup_maintenance(repo, T0, status)
    assert status['state'] == 'failed' and status['attempts'] == 2 and 'locked' in status['error']
    update = make_update(1)
    await commands.health_command(update, make_context({'repo': repo, 'hl': FakeHLClient(), 'maintenance': status}))
    assert 'Maintenance: failed' in update.message.replies[0]['text'] and 'error: database table is locked' in \
        update.message.replies[0]['text']


async def test_vacuum_follows_the_freelist_and_every_boot_logs_one_line(repo, caplog, monkeypatch):
    """fix/vacuum-freelist: v12 skipped VACUUM on a 200 MB file because nothing was pending in that run.
    The decision now reads PRAGMA freelist_count on every boot (>30% or >10 MB) and one Maintenance line
    is logged whatever happened."""
    import logging
    caplog.set_level(logging.INFO)
    await repo.add_subscription(7, A, 'w', T0)
    (va, _), = await repo.tracked_accounts()
    # nothing pending, nothing free: skipped, still logged
    status = {}
    await backup.startup_maintenance(repo, T0, status)
    assert status['vacuum'] == 'skipped' and status['freelist'].startswith('0 MB (0%')
    assert sum(m.startswith('Maintenance: db 0 MB, freelist 0 MB (0%), vacuum skipped') and m.endswith('ok (attempt 1)')
               for m in caplog.messages) == 1
    # rows deleted before the restart (already migrated, so no prune/slim this run): the freelist says VACUUM
    big = {'coin': 'BTC', 'blob': 'x' * 4000}
    for i in range(3000):
        await repo.record_event(f'e{i}', va, 'position_open', T0 - i, big, 'sent', T0)
    await repo.db.execute("DELETE FROM events WHERE dedupe_key != 'e0'")
    await repo.db.commit()
    free_bytes, file_bytes, pct = await repo.free_space()
    assert pct > 30 and free_bytes > 10 * 1_048_576
    caplog.clear()
    status = {}
    await backup.startup_maintenance(repo, T0, status)
    assert status['vacuum'].startswith('done (') and status['vacuum'] != 'done (0 MB freed)'
    assert (await repo.free_space())[2] == 0
    assert sum(m.startswith('Maintenance: db ') and 'vacuum done (' in m and 'MB freed' in m for m in caplog.messages) == 1
    # 10 MB absolute threshold on a file whose free share is under 30%
    monkeypatch.setattr(backup, 'VACUUM_FREE_BYTES', 0)
    for i in range(20):
        await repo.record_event(f'f{i}', va, 'position_open', T0 - i, big, 'sent', T0)
    await repo.db.execute("DELETE FROM events WHERE dedupe_key = 'f0'")
    await repo.db.commit()
    status = {}
    await backup.startup_maintenance(repo, T0, status)
    assert status['vacuum'].startswith('done (')
    # /health shows the freelist and the vacuum outcome
    monkeypatch.setattr(Config, 'ADMIN_USER_IDS', frozenset({1}))
    update = make_update(1)
    await commands.health_command(update, make_context({'repo': repo, 'hl': FakeHLClient(), 'maintenance': status}))
    text = update.message.replies[0]['text']
    check_telegram_html(text)
    assert 'freelist' in text and 'vacuum done (0 MB freed)' in text
