"""Database access."""

import logging

import aiosqlite

logger = logging.getLogger(__name__)

# Database file
DATABASE_FILE = 'hypermate.db'

async def init_db() -> None:
    """Initialize SQLite database and create tables if they don't exist."""
    try:
        async with aiosqlite.connect(DATABASE_FILE) as db:
            # Create tracked_wallets table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS tracked_wallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    wallet_address TEXT,
                    alias TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create created_wallets table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS created_wallets (
                    user_id TEXT PRIMARY KEY,
                    wallet_address TEXT,
                    encrypted_private_key TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create indexes for better performance
            await db.execute('''
                CREATE INDEX IF NOT EXISTS idx_tracked_wallets_user_id 
                ON tracked_wallets(user_id)
            ''')
            
            await db.execute('''
                CREATE INDEX IF NOT EXISTS idx_tracked_wallets_address 
                ON tracked_wallets(wallet_address)
            ''')
            
            await db.commit()
            logger.info("Database initialized successfully")
            
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise ValueError(f"Database initialization failed: {e}")
