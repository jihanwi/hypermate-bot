"""Daily SQLite backup (sqlite backup API) with 7-day retention."""

import os
from datetime import datetime, timedelta, timezone

import aiosqlite

from hypermate.db import backup

T = datetime(2026, 10, 3, 4, 0, tzinfo=backup.KST)


async def test_backup_is_a_consistent_copy_and_prunes_old_files(repo, tmp_path):
    await repo.add_subscription(7, '0x' + 'a' * 40, 'w', 1)
    directory = str(tmp_path / 'backups')
    os.makedirs(directory)
    for days_ago in (10, 8, 7, 6, 1):
        open(backup.backup_path(directory, T - timedelta(days=days_ago)), 'w').close()

    path = await backup.run_backup(repo, directory, T.astimezone(timezone.utc))
    assert path.endswith('hypermate-20261003.db')
    async with aiosqlite.connect(path) as copy:
        cur = await copy.execute("SELECT alias FROM subscriptions")
        assert await cur.fetchall() == [('w',)]
    kept = sorted(os.listdir(directory))
    assert kept == ['hypermate-20260927.db', 'hypermate-20261002.db', 'hypermate-20261003.db']
    assert not any(name.endswith('.tmp') for name in kept)

    # running again on the same day overwrites the day's file
    await backup.run_backup(repo, directory, T.astimezone(timezone.utc))
    assert sorted(os.listdir(directory)) == kept


def test_backup_dir_defaults_next_to_the_db(monkeypatch):
    monkeypatch.setattr(backup.Config, 'BACKUP_DIR', '')
    assert backup.backup_dir('/data/hypermate.db') == '/data/backups'
    monkeypatch.setattr(backup.Config, 'BACKUP_DIR', '/mnt/b')
    assert backup.backup_dir('/data/hypermate.db') == '/mnt/b'


async def test_backup_job_prunes_old_events_and_checkpoints(repo, tmp_path, monkeypatch):
    from tests.helpers import make_context
    monkeypatch.setattr(backup.Config, 'BACKUP_DIR', str(tmp_path / 'b'))
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    await repo.add_subscription(7, '0x' + 'a' * 40, 'w', 1)
    (va, _), = await repo.tracked_accounts()
    await repo.record_event('old', va, 'position_open', now_ms - 31 * 86_400_000, {'coin': 'BTC'}, 'sent', 1)
    await repo.record_event('new', va, 'position_open', now_ms - 29 * 86_400_000, {'coin': 'BTC'}, 'sent', 1)

    await backup.backup_job(make_context({'repo': repo}))
    assert await repo.get_event_by_key('old') is None
    assert await repo.get_event_by_key('new') is not None
    assert os.path.getsize(repo.path + '-wal') == 0
    assert len(os.listdir(tmp_path / 'b')) == 1

    # a failed backup never prunes
    await repo.record_event('old2', va, 'position_open', now_ms - 40 * 86_400_000, {'coin': 'BTC'}, 'sent', 1)
    monkeypatch.setattr(backup.Config, 'BACKUP_DIR', '/proc/no-such-dir/x')
    await backup.backup_job(make_context({'repo': repo}))
    assert await repo.get_event_by_key('old2') is not None


async def test_startup_maintenance_prunes_then_slims_in_the_background(repo, caplog):
    import logging
    from datetime import datetime, timezone
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    await repo.add_subscription(7, '0x' + 'a' * 40, 'w', 1)
    (va, _), = await repo.tracked_accounts()
    fat = {'venue': 'hyperliquid', 'venue_account_id': va, 'coin': 'BTC'}
    await repo.record_event('old', va, 'position_open', now_ms - 40 * 86_400_000, fat, 'sent', 1)
    await repo.record_event('new', va, 'position_open', now_ms - 1000, fat, 'sent', 1)
    await repo.db.execute("PRAGMA user_version = 0")
    await repo.db.commit()
    caplog.set_level(logging.INFO)
    await backup.startup_maintenance(repo, now_ms)
    assert await repo.get_event_by_key('old') is None                 # pruned before slimming
    assert 'venue' not in (await repo.get_event_by_key('new'))['payload']
    assert 'Startup prune before slimming: removed 1 events' in caplog.text
    assert 'Slimmed 1 of 1' in caplog.text
    assert not await repo.payloads_need_slimming()
    # already migrated: no prune, no log
    await repo.record_event('old2', va, 'position_open', now_ms - 40 * 86_400_000, fat, 'sent', 1)
    await backup.startup_maintenance(repo, now_ms)
    assert await repo.get_event_by_key('old2') is not None
