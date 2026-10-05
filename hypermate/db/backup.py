"""Daily SQLite backup via the sqlite backup API, kept for BACKUP_KEEP_DAYS (spec 11)."""

import asyncio
import logging
import os
import re
import time
from typing import Optional
from datetime import datetime, timedelta, timezone

from telegram.ext import ContextTypes

from hypermate.config import Config
from hypermate.db.repo import Repo

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9), 'KST')
_NAME = re.compile(r'^hypermate-(\d{8})\.db$')


def backup_dir(db_path: str = Config.DATABASE_PATH) -> str:
    return Config.BACKUP_DIR or os.path.join(os.path.dirname(os.path.abspath(db_path)), 'backups')


def backup_path(directory: str, when: datetime) -> str:
    return os.path.join(directory, f"hypermate-{when.astimezone(KST):%Y%m%d}.db")


def prune(directory: str, now: datetime, keep_days: int = Config.BACKUP_KEEP_DAYS) -> list[str]:
    """Keep the newest keep_days daily files (by the date in the file name). Returns the removed paths."""
    cutoff = (now.astimezone(KST) - timedelta(days=keep_days - 1)).strftime('%Y%m%d')
    removed = []
    for name in sorted(os.listdir(directory)):
        match = _NAME.match(name)
        if match and match.group(1) < cutoff:
            path = os.path.join(directory, name)
            os.remove(path)
            removed.append(path)
    return removed


async def run_backup(repo: Repo, directory: str, now: datetime) -> str:
    os.makedirs(directory, exist_ok=True)
    path = backup_path(directory, now)
    tmp = path + '.tmp'
    await repo.backup(tmp)
    os.replace(tmp, path)
    removed = prune(directory, now)
    logger.info(f"DB backup written to {path} ({os.path.getsize(path)} bytes), removed {len(removed)} old")
    return path


async def retention(repo: Repo, now_ms: int, days: int = Config.EVENTS_RETENTION_DAYS) -> tuple[int, int]:
    """After the backup: drop events older than `days` and fold the WAL into the main file."""
    events, messages = await repo.prune_events(now_ms - days * 24 * 3600 * 1000)
    await repo.checkpoint()
    logger.info(f"Retention: removed {events} events and {messages} sent_messages older than {days} days, "
                f"WAL checkpointed")
    return events, messages


MAINTENANCE_RETRY_SEC = 5
VACUUM_FREE_PERCENT = 30            # VACUUM when free pages exceed this share of the file ...
VACUUM_FREE_BYTES = 10 * 1_048_576  # ... or this many bytes, whichever comes first
MAINTENANCE_KEYS = ('db', 'freelist', 'vacuum', 'checkpoint', 'pruned', 'slimmed', 'cleaned')


def maintenance_summary(status: dict) -> str:
    """'db 50 MB, freelist 1 MB (2%), vacuum skipped, checkpoint 12 pages, pruned 0' for the log and /health."""
    return ", ".join(f"{k} {status[k]}" for k in MAINTENANCE_KEYS if k in status)


async def _maintenance_steps(repo: Repo, now_ms: int, status: dict) -> None:
    pending = await repo.payloads_need_slimming() or await repo.suppressed_rows_need_cleanup()
    status['pending'] = pending
    if pending:
        events, messages = await repo.prune_events(now_ms - Config.EVENTS_RETENTION_DAYS * 24 * 3600 * 1000)
        status['pruned'] = events
        logger.info(f"Startup prune before slimming: removed {events} events, {messages} sent_messages")
        seen, changed = await repo.slim_payloads()
        status['slimmed'] = f"{changed}/{seen}"
        status['cleaned'] = await repo.delete_suppressed_rows()     # fix/multi-algo-summary: old suppressed fill rows
    # Free pages are only reclaimed by VACUUM. The decision looks at the freelist on every boot, not at
    # what this run deleted: v12 skipped it on a 200 MB file whose rows were gone before the restart.
    free_bytes, file_bytes, free_pct = await repo.free_space()
    status['db'] = f"{file_bytes // 1_048_576} MB"
    status['freelist'] = f"{free_bytes // 1_048_576} MB ({free_pct}%)"
    if free_pct > VACUUM_FREE_PERCENT or free_bytes > VACUUM_FREE_BYTES:
        # runs before the poller starts, with the volume's 2x headroom
        status['vacuum'] = f"done ({await repo.vacuum() // 1_048_576} MB freed)"
    else:
        status['vacuum'] = 'skipped'
    # the checkpoint is cheap and runs on every attempt (a retry after a failed checkpoint still does it)
    busy, _, pages = await repo.checkpoint()
    status['checkpoint'] = 'busy' if busy else f"{pages} pages"


async def startup_maintenance(repo: Repo, now_ms: int, status: Optional[dict] = None) -> dict:
    """Runs in post_init before the polling jobs start: prune to the retention window first (fewer
    rows), then the batched payload slimming, the suppressed-row cleanup, VACUUM when rows went away,
    and a WAL checkpoint. Memory stays flat (batches); the bot answers commands only after it is done.
    One retry after MAINTENANCE_RETRY_SEC on failure; the outcome is kept in `status` for /health."""
    status = status if status is not None else {}
    status.update({'state': 'running', 'started_ms': now_ms, 'attempts': 0, 'error': None})
    for attempt in (1, 2):
        status['attempts'] = attempt
        try:
            await _maintenance_steps(repo, now_ms, status)
            status.update({'state': 'ok', 'finished_ms': int(time.time() * 1000), 'error': None})
            break
        except Exception as e:
            status.update({'state': 'failed', 'error': str(e), 'finished_ms': int(time.time() * 1000)})
            logger.error(f"Startup maintenance attempt {attempt} failed: {e}")
            if attempt == 1:
                await asyncio.sleep(MAINTENANCE_RETRY_SEC)
    # one line on every boot, whatever happened (v12 left no trace of a skipped VACUUM)
    logger.info(f"Maintenance: {maintenance_summary(status)} · {status['state']} (attempt {status['attempts']})"
                + (f", error: {status['error']}" if status.get('error') else ""))
    return status


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    repo: Repo = context.bot_data['repo']
    now = datetime.now(timezone.utc)
    try:
        await run_backup(repo, backup_dir(repo.path), now)
    except Exception as e:
        logger.error(f"DB backup failed: {e}")
        return   # keep the data when the backup did not succeed
    try:
        await retention(repo, int(now.timestamp() * 1000))
    except Exception as e:
        logger.error(f"Retention failed: {e}")
