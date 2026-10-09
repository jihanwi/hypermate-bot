"""HyperMate entry point: python -m hypermate.main"""

import asyncio
import datetime
import logging
import os

from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from hypermate.bot import callbacks, commands, texts
from hypermate.config import Config
from hypermate.core import poller
from hypermate.db import backup
from hypermate.db.repo import Repo
from hypermate.venues import base as venues
from hypermate.venues.hyperliquid import adapter
from hypermate.venues.hyperliquid.client import HyperliquidClient
from hypermate.venues.hyperliquid.scheduler import WeightBudget
from hypermate.venues.hyperliquid.venue import HyperliquidVenue
from hypermate.venues.lighter.adapter import LighterAdapter
from hypermate.venues.lighter.client import LighterClient
from hypermate.venues.lighter.stream import LighterStream
from hypermate.venues.risex.adapter import RisexAdapter
from hypermate.venues.risex.client import RisexClient
from hypermate.venues.risex.stream import RisexStream
from hypermate.venues.aster.adapter import AsterAdapter
from hypermate.venues.aster.client import AsterClient

logger = logging.getLogger(__name__)

# v1 JSON stores. They are not read anymore; scripts/migrate_v1.py migrates the DB table.
LEGACY_JSON_FILES = ('user_wallets.json', 'generated_wallets.json', 'wallets_secure.json')


def warn_legacy_files() -> None:
    db_dir = os.path.dirname(os.path.abspath(Config.DATABASE_PATH))
    for directory in {os.getcwd(), db_dir}:
        for name in LEGACY_JSON_FILES:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                logger.warning(f"Legacy file {path} found; it is not read anymore")


def quiet_noisy_loggers() -> None:
    """httpx logs every Telegram request URL (with the bot token) at INFO; apscheduler logs each job run."""
    for name in ('httpx', 'apscheduler', 'apscheduler.executors.default', 'apscheduler.scheduler'):
        logging.getLogger(name).setLevel(logging.WARNING)


async def post_init(application: Application) -> None:
    """Runs inside PTB's event loop before polling starts (B7: no get_event_loop in our code)."""
    warn_legacy_files()
    repo = Repo(Config.DATABASE_PATH)
    await repo.connect()
    application.bot_data['repo'] = repo
    budget = WeightBudget(Config.HL_WEIGHT_BUDGET)
    application.bot_data['budget'] = budget
    client = HyperliquidClient(Config.HYPERLIQUID_API_URL, Config.SPOT_META_TTL_SEC, Config.PERP_DEXS_TTL_SEC,
                               budget)
    await client.start()
    application.bot_data['hl'] = client
    poller.get_state(application)
    await build_venues(application, client)
    # Startup maintenance (prune, batched slimming, cleanup, VACUUM, checkpoint) runs before the polling
    # jobs start so VACUUM never competes with the poller for the connection (deploy review 2026-10-05).
    # It is a no-op on a database that is already migrated.
    application.bot_data['maintenance'] = {}
    await backup.startup_maintenance(repo, adapter.now_ms(), application.bot_data['maintenance'])
    # "/" autocomplete menu in private chats (spec 9.1, commands implemented so far)
    await application.bot.set_my_commands(
        [BotCommand(command, description) for command, description in texts.MENU_COMMANDS],
        scope=BotCommandScopeAllPrivateChats())
    logger.info(f"Registered {len(texts.MENU_COMMANDS)} menu commands")


async def build_venues(application: Application, hl: HyperliquidClient) -> None:
    """Venue adapters (spec 3.2): HL wrapper plus Lighter (REST bucket 50/min, WS stream best effort)."""
    lighter_budget = WeightBudget(Config.LIGHTER_REQ_BUDGET)
    lighter = LighterClient(Config.LIGHTER_API_URL, lighter_budget)
    await lighter.start()
    stream = None
    if Config.LIGHTER_WS_ENABLED:
        stream = LighterStream(Config.LIGHTER_WS_URL)
        stream.start()
    application.bot_data['lighter'] = lighter
    application.bot_data['lighter_budget'] = lighter_budget
    application.bot_data['lighter_stream'] = stream
    risex_budget = WeightBudget(Config.RISEX_REQ_BUDGET)
    risex = RisexClient(Config.RISEX_API_URL, risex_budget)
    await risex.start()
    risex_stream = None
    if Config.RISEX_WS_ENABLED:
        risex_stream = RisexStream(Config.RISEX_WS_URL)
        risex_stream.start()
    application.bot_data['risex'] = risex
    application.bot_data['risex_stream'] = risex_stream
    aster_budget = WeightBudget(Config.ASTER_REQ_BUDGET)
    aster = AsterClient(Config.ASTER_RPC_URL, aster_budget)
    await aster.start()
    application.bot_data['aster'] = aster
    application.bot_data['venues'] = {
        venues.HYPERLIQUID: HyperliquidVenue(hl),
        venues.LIGHTER: LighterAdapter(lighter, stream),
        venues.RISEX: RisexAdapter(risex, risex_stream),
        venues.ASTER: AsterAdapter(aster),
    }


async def post_shutdown(application: Application) -> None:
    for name in ('lighter_stream', 'risex_stream'):
        if application.bot_data.get(name) is not None:
            await application.bot_data[name].stop()
    for name in ('risex', 'aster'):
        if name in application.bot_data:
            await application.bot_data[name].close()
    if 'lighter' in application.bot_data:
        await application.bot_data['lighter'].close()
    if 'hl' in application.bot_data:
        await application.bot_data['hl'].close()
    if 'repo' in application.bot_data:
        await application.bot_data['repo'].close()


def build_application() -> Application:
    application = (Application.builder().token(Config.BOT_TOKEN)
                   .post_init(post_init).post_shutdown(post_shutdown).build())

    application.add_handler(CommandHandler("start", commands.start))
    application.add_handler(CommandHandler("help", commands.help_command))
    application.add_handler(CommandHandler("add", commands.add_wallet))
    application.add_handler(CommandHandler("list", commands.list_wallets))
    application.add_handler(CommandHandler("remove", commands.remove_wallet))
    application.add_handler(CommandHandler("positions", commands.positions_command))
    application.add_handler(CommandHandler("stats", commands.stats_command))
    application.add_handler(CommandHandler("recent", commands.recent_command))
    application.add_handler(CommandHandler("twap", commands.twap_command))
    application.add_handler(CommandHandler("rescan", commands.rescan_command))
    application.add_handler(CommandHandler("health", commands.health_command))
    application.add_handler(CommandHandler("related", commands.related_command))
    application.add_handler(CommandHandler("settings", commands.settings_command))
    application.add_handler(CommandHandler("mute", commands.mute_command))
    application.add_handler(CommandHandler("unmute", commands.unmute_command))
    application.add_handler(CallbackQueryHandler(callbacks.mute_all_callback, pattern=f"^{callbacks.MUTE_CALLBACK}"))
    application.add_handler(CallbackQueryHandler(commands.track_callback, pattern=f"^{commands.TRACK_CALLBACK}"))
    application.add_handler(CallbackQueryHandler(callbacks.settings_callback, pattern=f"^{callbacks.SETTINGS_CALLBACK}"))
    application.add_error_handler(commands.error_handler)

    job_queue = application.job_queue
    # poll_job itself skips cycles while the adaptive interval (spec 3.5) has not elapsed
    job_queue.run_repeating(poller.poll_job, interval=Config.POLL_FAST_SEC, first=10)
    job_queue.run_repeating(poller.venue_poll_job, interval=Config.POLL_FAST_SEC, first=15)
    job_queue.run_repeating(poller.weight_log_job, interval=60, first=60)
    job_queue.run_daily(poller.rescan_job, time=datetime.time(hour=Config.VENUE_RESCAN_HOUR_KST,
                                                              minute=Config.VENUE_RESCAN_MINUTE, tzinfo=backup.KST))
    job_queue.run_daily(backup.backup_job, time=datetime.time(hour=Config.BACKUP_HOUR_KST, tzinfo=backup.KST))
    return application


def main() -> None:
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=getattr(logging, Config.LOG_LEVEL.upper())
    )
    quiet_noisy_loggers()
    try:
        Config.validate_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return

    application = build_application()

    # run_polling() calls asyncio.get_event_loop() internally; on Python 3.12+ that warns
    # when no loop is set, so set a fresh one explicitly first (B7).
    asyncio.set_event_loop(asyncio.new_event_loop())
    logger.info("Starting HyperMate bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
