"""HyperMate entry point: python -m hypermate.main"""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler

from hypermate.config import Config

# Load environment variables
Config.load_env()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, Config.LOG_LEVEL.upper())
)
logger = logging.getLogger(__name__)

from hypermate.bot import commands  # noqa: E402
from hypermate.core import pipeline  # noqa: E402
from hypermate.db import repo  # noqa: E402

def main() -> None:
    """Start the bot."""
    # Validate configuration
    try:
        Config.validate_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Please set the BOT_TOKEN environment variable")
        return
    
    # Initialize database
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(repo.init_db())
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return
    
    # Create the Application
    application = Application.builder().token(Config.BOT_TOKEN).build()
    pipeline.app_instance = application

    # Register handlers
    application.add_handler(CommandHandler("start", commands.start))
    application.add_handler(CommandHandler("add", commands.add_wallet))
    application.add_handler(CommandHandler("list", commands.list_wallets))
    application.add_handler(CommandHandler("remove", commands.remove_wallet))
    application.add_handler(CommandHandler("positions", commands.positions_command))
    application.add_handler(CommandHandler("stats", commands.stats_command))
    
    # Register error handler
    application.add_error_handler(commands.error_handler)

    # Schedule the position monitoring job to run every 30 seconds
    job_queue = application.job_queue
    job_queue.run_repeating(pipeline.monitor_positions_job, interval=30, first=10)
    
    # Schedule the transfer monitoring job to run every 30 seconds (offset by 15 seconds)
    job_queue.run_repeating(pipeline.monitor_transfers_job, interval=30, first=25)

    # Run the bot until the user presses Ctrl-C
    logger.info("Starting HyperMate bot...")
    logger.info("Position monitoring will start in 10 seconds...")
    logger.info("Transfer monitoring will start in 25 seconds...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main() 
