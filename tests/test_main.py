import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from telegram import BotCommandScopeAllPrivateChats
from telegram.ext import CommandHandler, ExtBot

from hypermate import main as hm_main
from hypermate.bot import texts
from hypermate.config import Config

ROOT = Path(__file__).resolve().parents[1]


async def test_post_init_registers_menu_and_creates_db(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'BOT_TOKEN', '123:TEST')
    monkeypatch.setattr(Config, 'DATABASE_PATH', str(tmp_path / 'new_dir' / 'hypermate.db'))
    set_my_commands = AsyncMock(return_value=True)
    monkeypatch.setattr(ExtBot, 'set_my_commands', set_my_commands)

    app = hm_main.build_application()
    await hm_main.post_init(app)
    try:
        commands_arg = set_my_commands.await_args.args[0]
        assert [(c.command, c.description) for c in commands_arg] == [
            ('add', 'Track a wallet: /add 0x... alias'),
            ('remove', 'Stop tracking: /remove alias'),
            ('list', 'Your tracked wallets with account value'),
            ('positions', 'Open positions: /positions alias (no alias = all)'),
            ('twap', 'Active TWAPs: /twap [alias]'),
            ('recent', 'Recent events: /recent alias [n]'),
            ('related', 'Find linked wallets: /related alias'),
            ('stats', 'PnL and volume: /stats alias'),
            ('settings', 'Notification settings: /settings alias'),
            ('mute', 'Mute alerts: /mute alias [1h/1d]'),
            ('unmute', 'Unmute alerts: /unmute alias'),
            ('rename', 'Rename alias: /rename old new'),
            ('digest', 'Daily summary now: /digest (on|off|hour to set)'),
            ('rescan', 'Re-detect venues for a wallet: /rescan alias'),
            ('help', 'Commands and examples'),
        ]                                                   # spec 9.1 table; /health stays out of the menu
        assert all(len(c.description) <= 256 for c in commands_arg)
        assert isinstance(set_my_commands.await_args.kwargs['scope'], BotCommandScopeAllPrivateChats)
        assert (tmp_path / 'new_dir' / 'hypermate.db').exists()
        assert app.bot_data['maintenance']['state'] == 'ok'      # ran before the jobs, nothing pending
        assert not await app.bot_data['repo'].payloads_need_slimming()
    finally:
        await hm_main.post_shutdown(app)


def test_every_menu_command_has_a_handler(monkeypatch):
    monkeypatch.setattr(Config, 'BOT_TOKEN', '123:TEST')
    app = hm_main.build_application()
    handled = {cmd for group in app.handlers.values() for h in group
               if isinstance(h, CommandHandler) for cmd in h.commands}
    assert {c for c, _ in texts.MENU_COMMANDS} <= handled


def test_config_has_no_encryption_key():
    assert not hasattr(Config, 'WALLET_ENCRYPTION_KEY')
    assert not hasattr(Config, 'DATABASE_URL')


def test_bot_py_removed():
    assert not (ROOT / 'bot.py').exists()


def test_missing_bot_token_exits_cleanly(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ('BOT_TOKEN', 'WALLET_ENCRYPTION_KEY')}
    proc = subprocess.run([sys.executable, '-m', 'hypermate.main'], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert 'BOT_TOKEN environment variable is required' in proc.stderr
    assert 'WALLET_ENCRYPTION_KEY' not in proc.stderr


def test_noisy_loggers_are_quiet_and_health_is_registered(tmp_path, monkeypatch):
    import logging
    monkeypatch.setattr(Config, 'BOT_TOKEN', '123:TEST')
    hm_main.quiet_noisy_loggers()
    assert logging.getLogger('httpx').level == logging.WARNING
    assert logging.getLogger('apscheduler').level == logging.WARNING
    app = hm_main.build_application()
    registered = {h.commands for group in app.handlers.values() for h in group if isinstance(h, CommandHandler)}
    assert frozenset({'health'}) in registered
    jobs = {j.name for j in app.job_queue.jobs()}
    assert {'poll_job', 'weight_log_job', 'backup_job'} <= jobs
