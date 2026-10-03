"""Daily SQLite backup via the sqlite backup API, kept for BACKUP_KEEP_DAYS (spec 11)."""

import logging
import os
import re
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


async def startup_maintenance(repo: Repo, now_ms: int) -> None:
    """Background task after post_init: prune to the retention window first (fewer rows), then the
    batched payload slimming. Never blocks startup; errors are logged."""
    try:
        if await repo.payloads_need_slimming():
            events, messages = await repo.prune_events(now_ms - Config.EVENTS_RETENTION_DAYS * 24 * 3600 * 1000)
            logger.info(f"Startup prune before slimming: removed {events} events, {messages} sent_messages")
            await repo.slim_payloads()
            await repo.checkpoint()
    except Exception as e:
        logger.error(f"Startup maintenance failed: {e}")


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
