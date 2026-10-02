"""HyperMate entry point: python -m hypermate.main"""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler

from hypermate.bot import commands
from hypermate.config import Config
from hypermate.core import pipeline
from hypermate.db.repo import Repo
from hypermate.venues.hyperliquid.client import HyperliquidClient

logger = logging.getLogger(__name__)

DATABASE_FILE = 'hypermate.db'


async def post_init(application: Application) -> None:
    """Runs inside PTB's event loop before polling starts (B7: no get_event_loop in our code)."""
    repo = Repo(DATABASE_FILE)
    await repo.connect()
    application.bot_data['repo'] = repo
    client = HyperliquidClient(Config.HYPERLIQUID_API_URL)
    await client.start()
    application.bot_data['hl'] = client


async def post_shutdown(application: Application) -> None:
    if 'hl' in application.bot_data:
        await application.bot_data['hl'].close()
    if 'repo' in application.bot_data:
        await application.bot_data['repo'].close()


def build_application() -> Application:
    application = (Application.builder().token(Config.BOT_TOKEN)
                   .post_init(post_init).post_shutdown(post_shutdown).build())

    application.add_handler(CommandHandler("start", commands.start))
    application.add_handler(CommandHandler("add", commands.add_wallet))
    application.add_handler(CommandHandler("list", commands.list_wallets))
    application.add_handler(CommandHandler("remove", commands.remove_wallet))
    application.add_handler(CommandHandler("positions", commands.positions_command))
    application.add_handler(CommandHandler("stats", commands.stats_command))
    application.add_error_handler(commands.error_handler)

    job_queue = application.job_queue
    job_queue.run_repeating(pipeline.monitor_positions_job, interval=30, first=10)
    job_queue.run_repeating(pipeline.monitor_transfers_job, interval=30, first=25)
    return application


def main() -> None:
    Config.load_env()
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=getattr(logging, Config.LOG_LEVEL.upper())
    )
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
