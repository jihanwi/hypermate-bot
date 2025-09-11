#!/usr/bin/env python3
"""
HyperMate - Telegram Bot for Hyperliquid
A bot focused on tracking wallets and trading on HyperCore and HyperEVM
"""

import asyncio
import aiohttp
import json
import logging
import os
import re
import time
from typing import Dict, List, Set
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from config import Config
from eth_account import Account
from cryptography.fernet import Fernet
import secrets
import base64
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import hashlib
import aiosqlite

# Load environment variables
Config.load_env()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, Config.LOG_LEVEL.upper())
)
logger = logging.getLogger(__name__)

# Data file path
DATA_FILE = 'user_wallets.json'

# In-memory storage for tracked wallets per user
# Structure: {user_id: [{"address": "0x...", "alias": "name"}, ...]}
user_wallets: Dict[int, List[Dict[str, str]]] = {}

# Track previously seen positions per wallet to detect new ones
# Structure: {wallet_address: {position_id: position_data}}
previous_positions: Dict[str, Dict[str, dict]] = {}

# Track if we've done the initial scan for each wallet (to avoid alerting on existing positions)
initial_scan_done: Dict[str, bool] = {}

# Track TWAP order states per wallet
# Structure: {wallet_address: {twap_id: twap_status}}
previous_twap_states: Dict[str, Dict[str, str]] = {}

# Track last seen transfer timestamp per wallet
# Structure: {wallet_address: last_timestamp}
last_transfer_timestamps: Dict[str, int] = {}

# Track if we've done the initial transfer scan for each wallet (to avoid alerting on existing transfers)
initial_transfer_scan_done: Dict[str, bool] = {}

# Store user-generated wallets (encrypted private keys)
# Structure: {user_id: {"address": "0x...", "encrypted_key": "encrypted_private_key"}}
user_generated_wallets: Dict[int, Dict[str, str]] = {}

# Data file for generated wallets
GENERATED_WALLETS_FILE = 'generated_wallets.json'

# Data file for secure wallet storage
SECURE_WALLETS_FILE = 'wallets_secure.json'

# Database file
DATABASE_FILE = 'hypermate.db'

# Encryption setup for private keys
try:
    # Load encryption key from environment
    encryption_key = Config.WALLET_ENCRYPTION_KEY
    if not encryption_key:
        raise ValueError("WALLET_ENCRYPTION_KEY environment variable is required")
    
    # Ensure the key is properly formatted for Fernet
    if len(encryption_key) != 44:  # Base64 encoded 32-byte key
        # If it's not a proper Fernet key, hash it to create one
        key_bytes = hashlib.sha256(encryption_key.encode()).digest()
        encryption_key = base64.urlsafe_b64encode(key_bytes).decode()
    
    fernet = Fernet(encryption_key)
    logger.info("Encryption initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize encryption: {e}")
    raise ValueError(f"Invalid encryption configuration: {e}")

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

# Global application instance for sending messages
app_instance = None

def load_wallets() -> None:
    """Load wallet data from JSON file."""
    global user_wallets
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                # JSON keys are strings, but we need integer user_ids
                data = json.load(f)
                user_wallets = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded {len(user_wallets)} users' wallet data from {DATA_FILE}")
        else:
            user_wallets = {}
            logger.info(f"No existing data file found, starting with empty wallet list")
    except Exception as e:
        logger.error(f"Error loading wallet data: {e}")
        user_wallets = {}

def save_wallets() -> None:
    """Save wallet data to JSON file."""
    try:
        with open(DATA_FILE, 'w') as f:
            # Convert integer user_ids to strings for JSON serialization
            data = {str(k): v for k, v in user_wallets.items()}
            json.dump(data, f, indent=2)
            logger.info(f"Saved wallet data to {DATA_FILE}")
    except Exception as e:
        logger.error(f"Error saving wallet data: {e}")

def load_generated_wallets() -> None:
    """Load generated wallet data from JSON file."""
    global user_generated_wallets
    try:
        if os.path.exists(GENERATED_WALLETS_FILE):
            with open(GENERATED_WALLETS_FILE, 'r') as f:
                # JSON keys are strings, but we need integer user_ids
                data = json.load(f)
                user_generated_wallets = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded {len(user_generated_wallets)} generated wallets from {GENERATED_WALLETS_FILE}")
        else:
            user_generated_wallets = {}
            logger.info(f"No existing generated wallets file found")
    except Exception as e:
        logger.error(f"Error loading generated wallets: {e}")
        user_generated_wallets = {}

def save_generated_wallets() -> None:
    """Save generated wallet data to JSON file."""
    try:
        with open(GENERATED_WALLETS_FILE, 'w') as f:
            # Convert integer user_ids to strings for JSON serialization
            data = {str(k): v for k, v in user_generated_wallets.items()}
            json.dump(data, f, indent=2)
            logger.info(f"Saved generated wallets to {GENERATED_WALLETS_FILE}")
    except Exception as e:
        logger.error(f"Error saving generated wallets: {e}")

def load_secure_wallets() -> None:
    """Load securely encrypted wallet data from JSON file."""
    global user_generated_wallets
    try:
        if os.path.exists(SECURE_WALLETS_FILE):
            with open(SECURE_WALLETS_FILE, 'r') as f:
                data = json.load(f)
                user_generated_wallets = {int(k): v for k, v in data.items()}
                logger.info(f"Loaded {len(user_generated_wallets)} secure wallets from {SECURE_WALLETS_FILE}")
        else:
            user_generated_wallets = {}
            logger.info(f"No existing secure wallets file found")
    except Exception as e:
        logger.error(f"Error loading secure wallets: {e}")
        user_generated_wallets = {}

def save_secure_wallets() -> None:
    """Save securely encrypted wallet data to JSON file."""
    try:
        with open(SECURE_WALLETS_FILE, 'w') as f:
            # Convert integer user_ids to strings for JSON serialization
            data = {str(k): v for k, v in user_generated_wallets.items()}
            json.dump(data, f, indent=2)
            logger.info(f"Saved secure wallets to {SECURE_WALLETS_FILE}")
    except Exception as e:
        logger.error(f"Error saving secure wallets: {e}")

def encrypt_private_key(private_key: str) -> str:
    """Encrypt a private key using Fernet encryption."""
    try:
        encrypted_key = fernet.encrypt(private_key.encode()).decode()
        return encrypted_key
    except Exception as e:
        logger.error(f"Error encrypting private key: {e}")
        raise ValueError(f"Failed to encrypt private key: {e}")

def decrypt_private_key(encrypted_key: str) -> str:
    """Decrypt a private key using Fernet encryption."""
    try:
        decrypted_key = fernet.decrypt(encrypted_key.encode()).decode()
        return decrypted_key
    except Exception as e:
        logger.error(f"Error decrypting private key: {e}")
        raise ValueError(f"Failed to decrypt private key: {e}")

async def store_user_wallet(user_id: int, address: str, private_key: str) -> None:
    """Store a user's wallet with encrypted private key in database."""
    try:
        encrypted_key = encrypt_private_key(private_key)
        user_id_str = str(user_id)  # Convert to string for database storage
        
        # Store in database
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute(
                """INSERT OR REPLACE INTO created_wallets 
                   (user_id, wallet_address, encrypted_private_key) 
                   VALUES (?, ?, ?)""",
                (user_id_str, address, encrypted_key)
            )
            await db.commit()
        
        logger.info(f"Stored encrypted wallet for user {user_id}: {address}")
    except Exception as e:
        logger.error(f"Error storing wallet for user {user_id}: {e}")
        raise

async def get_user_private_key(user_id: int) -> str:
    """Retrieve and decrypt a user's private key from database."""
    try:
        user_id_str = str(user_id)  # Convert to string for database storage
        
        # Get from database
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT encrypted_private_key FROM created_wallets WHERE user_id = ?",
                (user_id_str,)
            )
            result = await cur.fetchone()
            
            if not result:
                raise ValueError(f"No wallet found for user {user_id}")
            
            encrypted_key = result[0]
            private_key = decrypt_private_key(encrypted_key)
            return private_key
    except Exception as e:
        logger.error(f"Error retrieving private key for user {user_id}: {e}")
        raise

async def migrate_tracked_wallets_to_db() -> None:
    """Migrate tracked wallets from user_wallets.json to SQLite database."""
    try:
        if not user_wallets:
            logger.info("No tracked wallets to migrate")
            return
            
        logger.info(f"Migrating {len(user_wallets)} users' tracked wallets to database...")
        
        migrated_count = 0
        async with aiosqlite.connect(DATABASE_FILE) as db:
            for user_id, wallets_list in user_wallets.items():
                user_id_str = str(user_id)
                
                for wallet in wallets_list:
                    address = wallet.get('address', '').lower()
                    alias = wallet.get('alias', '')
                    
                    if address and alias:
                        try:
                            # Check if this wallet already exists in database
                            cur = await db.execute(
                                "SELECT COUNT(*) FROM tracked_wallets WHERE user_id = ? AND wallet_address = ? AND alias = ?",
                                (user_id_str, address, alias)
                            )
                            count = await cur.fetchone()
                            
                            if count[0] == 0:
                                # Insert into database
                                await db.execute(
                                    "INSERT INTO tracked_wallets (user_id, wallet_address, alias) VALUES (?, ?, ?)",
                                    (user_id_str, address, alias)
                                )
                                migrated_count += 1
                                logger.info(f"Migrated wallet for user {user_id}: {alias} ({address})")
                            else:
                                logger.debug(f"Wallet already exists in database for user {user_id}: {alias}")
                                
                        except Exception as wallet_error:
                            logger.error(f"Error migrating wallet {alias} for user {user_id}: {wallet_error}")
                            
            await db.commit()
            
        if migrated_count > 0:
            logger.info(f"Successfully migrated {migrated_count} tracked wallets to database")
        else:
            logger.info("No new wallets needed migration")
            
    except Exception as e:
        logger.error(f"Error during tracked wallets migration: {e}")
        # Don't raise - allow bot to continue even if migration fails

def migrate_old_wallets() -> None:
    """Migrate wallets from old generated_wallets.json to secure storage."""
    try:
        if os.path.exists(GENERATED_WALLETS_FILE) and not os.path.exists(SECURE_WALLETS_FILE):
            logger.info("Migrating wallets from old storage to secure storage...")
            
            # Load old wallet data
            with open(GENERATED_WALLETS_FILE, 'r') as f:
                old_data = json.load(f)
            
            # Migrate each wallet
            migrated_count = 0
            for user_id_str, wallet_data in old_data.items():
                try:
                    user_id = int(user_id_str)
                    address = wallet_data.get('address')
                    encrypted_key = wallet_data.get('encrypted_key')
                    
                    if address and encrypted_key:
                        # Try to decrypt with the old key and re-encrypt with new key
                        try:
                            # First try to decrypt (this may fail if the old encryption was different)
                            private_key = fernet.decrypt(encrypted_key.encode()).decode()
                            
                            # Store using new secure storage
                            store_user_wallet(user_id, address, private_key)
                            migrated_count += 1
                            logger.info(f"Migrated wallet for user {user_id}: {address}")
                        except Exception as decrypt_error:
                            logger.warning(f"Could not decrypt wallet for user {user_id}: {decrypt_error}")
                            # Store the encrypted key as-is for manual recovery
                            user_generated_wallets[user_id] = {
                                "address": address,
                                "encrypted_key": encrypted_key
                            }
                            logger.info(f"Preserved encrypted wallet for user {user_id} (manual recovery needed)")
                            
                except Exception as user_error:
                    logger.error(f"Error migrating wallet for user {user_id_str}: {user_error}")
            
            # Save migrated data
            if migrated_count > 0:
                save_secure_wallets()
                logger.info(f"Successfully migrated {migrated_count} wallets to secure storage")
                
                # Backup old file and remove it
                backup_file = f"{GENERATED_WALLETS_FILE}.backup"
                os.rename(GENERATED_WALLETS_FILE, backup_file)
                logger.info(f"Old wallet file backed up to {backup_file}")
            
    except Exception as e:
        logger.error(f"Error during wallet migration: {e}")
        # Don't raise - allow bot to continue even if migration fails

async def get_spot_transfers(wallet_address: str) -> list:
    """Query Hyperliquid API for spot transfers."""
    try:
        # Try the suggested endpoint first
        url = f"https://api.hyperliquid.xyz/spot/user/transfers/{wallet_address}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"Successfully fetched spot transfers for {wallet_address}")
                    return data if isinstance(data, list) else []
                elif response.status == 404:
                    # Try alternative endpoint using info API
                    info_url = "https://api.hyperliquid.xyz/info"
                    payload = {
                        "type": "userNonFundingLedgerUpdates",
                        "user": wallet_address,
                        "startTime": last_transfer_timestamps.get(wallet_address, 0)
                    }
                    
                    async with session.post(info_url, json=payload) as info_response:
                        if info_response.status == 200:
                            data = await info_response.json()
                            logger.debug(f"Successfully fetched transfers via info API for {wallet_address}")
                            return data if isinstance(data, list) else []
                        else:
                            logger.error(f"Info API request failed for {wallet_address}: {info_response.status}")
                            return []
                else:
                    logger.error(f"Spot transfers API request failed for {wallet_address}: {response.status}")
                    return []
    except Exception as e:
        logger.error(f"Error fetching spot transfers for {wallet_address}: {e}")
        return []

async def get_spot_fills(wallet_address: str) -> list:
    """Query Hyperliquid API for recent spot fills to detect buy/sell activities."""
    try:
        url = "https://api.hyperliquid.xyz/info"
        payload = {
            "type": "userFills",
            "user": wallet_address,
            "startTime": last_transfer_timestamps.get(wallet_address, 0)
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"Successfully fetched spot fills for {wallet_address}")
                    # Filter for spot fills only
                    spot_fills = [fill for fill in data if fill.get('coin', '').endswith('USDC')]
                    return spot_fills
                else:
                    logger.error(f"Spot fills API request failed for {wallet_address}: {response.status}")
                    return []
    except Exception as e:
        logger.error(f"Error fetching spot fills for {wallet_address}: {e}")
        return []

def format_transfer_message(transfer: dict, wallet_address: str, alias: str) -> str:
    """Format a transfer into a readable message."""
    try:
        transfer_type = transfer.get('delta', {}).get('type', 'unknown')
        delta = transfer.get('delta', {})
        
        # Skip unwanted notification types
        if transfer_type in ['vaultLeaderCommission', 'rewardsClaim']:
            return None
        
        # Create clickable alias link
        clickable_alias = f"[{alias}](https://hypurrscan.io/address/{wallet_address})"
        
        if transfer_type == 'spotTransfer':
            token = delta.get('token', 'Unknown')
            amount = float(delta.get('amount', 0))
            usd_value = float(delta.get('usdcValue', 0))
            destination = delta.get('destination', '')
            user = delta.get('user', '')
            
            # Determine if it's incoming or outgoing
            if user.lower() == wallet_address.lower():
                # Outgoing transfer
                dest_short = f"{destination[:6]}...{destination[-4:]}" if len(destination) > 10 else destination
                return f"↗️ **{clickable_alias}** sent {amount:.2f} {token} (${usd_value:,.2f}) to `{dest_short}`"
            elif destination.lower() == wallet_address.lower():
                # Incoming transfer
                user_short = f"{user[:6]}...{user[-4:]}" if len(user) > 10 else user
                return f"↘️ **{clickable_alias}** received {amount:.2f} {token} (${usd_value:,.2f}) from `{user_short}`"
        
        elif transfer_type == 'accountClassTransfer':
            usdc_amount = float(delta.get('usdc', 0))
            to_perp = delta.get('toPerp', True)
            
            if to_perp:
                return f"🔄 **{clickable_alias}** transferred ${usdc_amount:,.2f} from Spot to Perp"
            else:
                return f"🔄 **{clickable_alias}** transferred ${usdc_amount:,.2f} from Perp to Spot"
        
        elif transfer_type == 'deposit':
            usdc_amount = float(delta.get('usdc', 0))
            return f"💰 **{clickable_alias}** deposited ${usdc_amount:,.2f}"
        
        elif transfer_type == 'withdraw':
            usdc_amount = float(delta.get('usdc', 0))
            return f"💸 **{clickable_alias}** withdrew ${usdc_amount:,.2f}"
        
        elif transfer_type == 'vaultWithdraw':
            requested_usd = float(delta.get('requestedUsd', 0))
            net_withdrawn = float(delta.get('netWithdrawnUsd', 0))
            return f"🏦 **{clickable_alias}** withdrew ${net_withdrawn:,.2f} from vault"
        
        elif transfer_type == 'vaultDeposit':
            usd_amount = float(delta.get('usd', 0))
            return f"🏦 **{clickable_alias}** deposited ${usd_amount:,.2f} to vault"
        
        elif transfer_type == 'liquidation':
            # Handle liquidation events
            return f"⚠️ **{clickable_alias}** was liquidated"
        
        # Skip verbose/technical transfer types that users don't need to see
        elif transfer_type in ['vaultLeaderCommission', 'rewardsClaim', 'funding']:
            return None
        
        # For any remaining unhandled types, return None to skip them
        # This prevents verbose technical messages
        return None
        
    except Exception as e:
        logger.error(f"Error formatting transfer message: {e}")
        return f"📊 **[{alias}](https://hypurrscan.io/address/{wallet_address})** had a transfer activity"

def format_spot_fill_message(fill: dict, wallet_address: str, alias: str) -> str:
    """Format a spot fill into a buy/sell message."""
    try:
        coin = fill.get('coin', 'Unknown').replace('USDC', '')  # Remove USDC suffix for cleaner display
        side = fill.get('side', 'unknown')
        price = float(fill.get('px', 0))
        quantity = float(fill.get('sz', 0))
        usd_value = price * quantity
        
        # Create clickable alias link
        clickable_alias = f"[{alias}](https://hypurrscan.io/address/{wallet_address})"
        
        if side.lower() == 'b':  # Buy
            return f"🟢 **{clickable_alias}** bought {quantity:.2f} {coin} for ${usd_value:,.2f} @ ${price:.4f}"
        elif side.lower() == 's':  # Sell
            return f"🔴 **{clickable_alias}** sold {quantity:.2f} {coin} for ${usd_value:,.2f} @ ${price:.4f}"
        else:
            return f"📊 **{clickable_alias}** traded {quantity:.2f} {coin} for ${usd_value:,.2f} @ ${price:.4f}"
        
    except Exception as e:
        logger.error(f"Error formatting spot fill message: {e}")
        return f"📊 **[{alias}](https://hypurrscan.io/address/{wallet_address})** had a spot trading activity"

async def check_new_transfers(wallet_address: str, alias: str) -> list:
    """Check for new transfers and spot fills, return formatted messages."""
    # Get both transfers and fills
    transfers = await get_spot_transfers(wallet_address)
    fills = await get_spot_fills(wallet_address)
    
    # Check if this is the first scan for this wallet
    is_initial_scan = wallet_address not in initial_transfer_scan_done
    
    # Get the last seen timestamp for this wallet
    last_timestamp = last_transfer_timestamps.get(wallet_address, 0)
    new_messages = []
    latest_timestamp = last_timestamp
    
    # Process transfers
    for transfer in transfers:
        transfer_time = transfer.get('time', 0)
        latest_timestamp = max(latest_timestamp, transfer_time)
        
        # Only process transfers newer than last seen (and not on initial scan)
        if transfer_time > last_timestamp and not is_initial_scan:
            message = format_transfer_message(transfer, wallet_address, alias)
            if message is not None:  # Skip filtered out message types
                new_messages.append(message)
    
    # Process spot fills (buy/sell activities)
    for fill in fills:
        fill_time = fill.get('time', 0)
        latest_timestamp = max(latest_timestamp, fill_time)
        
        # Only process fills newer than last seen (and not on initial scan)
        if fill_time > last_timestamp and not is_initial_scan:
            message = format_spot_fill_message(fill, wallet_address, alias)
            new_messages.append(message)
    
    # Update the last seen timestamp
    if latest_timestamp > last_timestamp:
        last_transfer_timestamps[wallet_address] = latest_timestamp
    
    # Mark initial scan as done
    if is_initial_scan:
        initial_transfer_scan_done[wallet_address] = True
        # If no transfers found, set timestamp to current time to avoid processing old data
        if latest_timestamp == 0:
            latest_timestamp = int(time.time() * 1000)  # Current time in milliseconds
            last_transfer_timestamps[wallet_address] = latest_timestamp
        logger.info(f"Initial transfer scan for {wallet_address} ({alias}) - recorded latest timestamp: {latest_timestamp}")
    
    return new_messages

async def get_wallet_positions(wallet_address: str) -> dict:
    """Query Hyperliquid API for wallet positions."""
    try:
        # Add small delay to avoid rate limiting
        await asyncio.sleep(0.5)
        
        url = "https://api.hyperliquid.xyz/info"
        payload = {
            "type": "clearinghouseState",
            "user": wallet_address
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.debug(f"Successfully fetched positions for {wallet_address}")
                    return data
                else:
                    logger.error(f"API request failed for {wallet_address}: {response.status}")
                    return {}
    except Exception as e:
        logger.error(f"Error fetching positions for {wallet_address}: {e}")
        return {}

async def check_new_positions(wallet_address: str, alias: str) -> list:
    """Check for position changes and return list of alerts."""
    current_positions = await get_wallet_positions(wallet_address)
    
    if not current_positions or 'assetPositions' not in current_positions:
        logger.debug(f"No positions data for {wallet_address}")
        return []
    
    # Check if this is the first scan for this wallet
    is_initial_scan = wallet_address not in initial_scan_done
    
    # Get current asset positions
    asset_positions = current_positions.get('assetPositions', [])
    logger.debug(f"Found {len(asset_positions)} asset positions for {wallet_address}")
    
    # Track active TWAP orders to suppress regular position alerts for TWAP-related changes
    active_twap_coins = set()
    if 'twapOrders' in current_positions:
        for twap in current_positions['twapOrders']:
            if twap.get('status') == 'active':
                active_twap_coins.add(twap.get('coin', ''))
    
    # Create current position mapping
    current_position_map = {}
    for pos in asset_positions:
        if 'position' in pos:
            position = pos['position']
            coin = position.get('coin', '')
            szi = position.get('szi', '0')
            if float(szi) != 0:
                current_position_map[coin] = {
                    'szi': szi,
                    'direction': 'LONG' if float(szi) > 0 else 'SHORT',
                    'entry_px': position.get('entryPx', 'N/A'),
                    'position_value': position.get('positionValue', 'N/A'),
                    'coin': coin,
                    'unrealized_pnl': position.get('unrealizedPnl', 'N/A')
                }
    
    # Get previous positions for this wallet
    previous_position_map = previous_positions.get(wallet_address, {})
    
    position_alerts = []
    
    if is_initial_scan:
        # First scan - record positions but don't alert
        logger.info(f"Initial scan for {wallet_address} ({alias}) - recording {len(current_position_map)} positions")
        initial_scan_done[wallet_address] = True
    else:
        # Check for NEW positions and SIZE INCREASES
        for coin, current_pos in current_position_map.items():
            # Skip position alerts if there's an active TWAP order for this coin
            is_twap_related = coin in active_twap_coins
            
            if coin not in previous_position_map:
                # Completely new position
                if not is_twap_related:
                    logger.info(f"New position detected: {coin} for {wallet_address} ({alias})")
                    position_alerts.append({
                        **current_pos,
                        'alert_type': 'NEW_POSITION'
                    })
                else:
                    logger.info(f"New position detected for {coin} but suppressed due to active TWAP")
            else:
                # Position exists - check for size changes
                prev_szi = float(previous_position_map[coin]['szi'])
                curr_szi = float(current_pos['szi'])
                
                # Check if position size increased (same direction)
                if ((prev_szi > 0 and curr_szi > prev_szi) or 
                    (prev_szi < 0 and curr_szi < prev_szi)):
                    size_increase = abs(curr_szi - prev_szi)
                    if not is_twap_related:
                        logger.info(f"Position size increase detected: {coin} for {wallet_address} ({alias}) - added {size_increase}")
                        position_alerts.append({
                            **current_pos,
                            'alert_type': 'POSITION_INCREASE',
                            'size_change': size_increase
                        })
                    else:
                        logger.info(f"Position size increase detected for {coin} but suppressed due to active TWAP")
                
                # Check if position size decreased (partial close)
                elif ((prev_szi > 0 and curr_szi < prev_szi and curr_szi > 0) or 
                      (prev_szi < 0 and curr_szi > prev_szi and curr_szi < 0)):
                    size_decrease = abs(prev_szi - curr_szi)
                    if not is_twap_related:
                        logger.info(f"Position size decrease detected: {coin} for {wallet_address} ({alias}) - reduced by {size_decrease}")
                        position_alerts.append({
                            **current_pos,
                            'alert_type': 'POSITION_DECREASE',
                            'size_change': size_decrease,
                            'remaining_size': abs(curr_szi)
                        })
                    else:
                        logger.info(f"Position size decrease detected for {coin} but suppressed due to active TWAP")
        
        # Check for CLOSED positions and LIQUIDATIONS
        for coin, prev_pos in previous_position_map.items():
            if coin not in current_position_map:
                # Position completely closed
                prev_szi = float(prev_pos['szi'])
                position_size = abs(prev_szi)
                prev_direction = prev_pos['direction']
                
                # Get the closing PnL from the previous position
                closing_pnl = None
                pnl_value = 0
                if prev_pos.get('unrealized_pnl') and prev_pos['unrealized_pnl'] != 'N/A':
                    try:
                        pnl_value = float(prev_pos['unrealized_pnl'])
                        closing_pnl = pnl_value
                    except (ValueError, TypeError):
                        pass
                
                # Try to determine if this was a liquidation
                # We'll look for rapid position changes or large unrealized losses
                is_liquidation = False
                if closing_pnl is not None:
                    # If PnL was very negative (>15% loss), might be liquidation
                    try:
                        position_value = abs(float(prev_pos.get('position_value', 0)))
                        if position_value > 0 and pnl_value < -0.15 * position_value:
                            is_liquidation = True
                    except (ValueError, TypeError):
                        pass
                
                if is_liquidation:
                    logger.info(f"Potential liquidation detected: {coin} for {wallet_address} ({alias}) - PnL: {closing_pnl}")
                    position_alerts.append({
                        **prev_pos,
                        'alert_type': 'LIQUIDATION',
                        'liquidated_size': position_size,
                        'closing_pnl': closing_pnl
                    })
                else:
                    logger.info(f"Position closed: {coin} for {wallet_address} ({alias}) - PnL: {closing_pnl}")
                    position_alerts.append({
                        **prev_pos,
                        'alert_type': 'POSITION_CLOSED',
                        'closed_size': position_size,
                        'closing_pnl': closing_pnl
                    })
    
    # Check for TWAP orders (if present in the API response)
    if 'twapOrders' in current_positions:
        twap_alerts = await check_twap_orders(wallet_address, alias, current_positions['twapOrders'])
        position_alerts.extend(twap_alerts)
    
    # Update stored positions
    previous_positions[wallet_address] = current_position_map
    
    return position_alerts

async def check_twap_orders(wallet_address: str, alias: str, twap_orders: list) -> list:
    """Check for TWAP order status changes."""
    twap_alerts = []
    
    # Create current TWAP state mapping
    current_twap_states = {}
    for twap in twap_orders:
        twap_id = twap.get('id', f"{twap.get('coin', 'unknown')}_{twap.get('startTime', 'unknown')}")
        current_twap_states[twap_id] = {
            'status': twap.get('status', 'unknown'),
            'coin': twap.get('coin', 'Unknown'),
            'direction': 'LONG' if float(twap.get('sz', 0)) > 0 else 'SHORT',
            'size': twap.get('sz', '0'),
            'filled': twap.get('filled', '0'),
            'remaining': twap.get('remaining', '0')
        }
    
    # Get previous TWAP states for this wallet
    previous_twap_state_map = previous_twap_states.get(wallet_address, {})
    
    # Check for TWAP status changes
    for twap_id, current_twap in current_twap_states.items():
        if twap_id not in previous_twap_state_map:
            # New TWAP order detected
            if current_twap['status'] == 'active':
                logger.info(f"New TWAP order started: {current_twap['coin']} for {wallet_address} ({alias})")
                twap_alerts.append({
                    **current_twap,
                    'alert_type': 'TWAP_STARTED'
                })
        else:
            # TWAP exists, check for status changes
            prev_status = previous_twap_state_map[twap_id]['status']
            curr_status = current_twap['status']
            
            if prev_status != curr_status:
                if curr_status == 'completed':
                    logger.info(f"TWAP order completed: {current_twap['coin']} for {wallet_address} ({alias})")
                    twap_alerts.append({
                        **current_twap,
                        'alert_type': 'TWAP_COMPLETED'
                    })
                elif curr_status == 'cancelled':
                    logger.info(f"TWAP order cancelled: {current_twap['coin']} for {wallet_address} ({alias})")
                    twap_alerts.append({
                        **current_twap,
                        'alert_type': 'TWAP_CANCELLED'
                    })
                elif curr_status == 'active' and prev_status in ['paused', 'stopped']:
                    logger.info(f"TWAP order resumed: {current_twap['coin']} for {wallet_address} ({alias})")
                    twap_alerts.append({
                        **current_twap,
                        'alert_type': 'TWAP_RESUMED'
                    })
                elif curr_status in ['paused', 'stopped']:
                    logger.info(f"TWAP order paused/stopped: {current_twap['coin']} for {wallet_address} ({alias})")
                    twap_alerts.append({
                        **current_twap,
                        'alert_type': 'TWAP_PAUSED'
                    })
    
    # Update stored TWAP states
    previous_twap_states[wallet_address] = current_twap_states
    
    return twap_alerts

async def send_position_alert(wallet_address: str, alias: str, position: dict):
    """Send a position alert to all users tracking this wallet."""
    if not app_instance:
        return
    
    # Find all users tracking this wallet
    users_to_notify = []
    
    # Check old in-memory storage (legacy support)
    for user_id, wallets in user_wallets.items():
        for wallet in wallets:
            if wallet['address'] == wallet_address:
                users_to_notify.append(user_id)
                break
    
    # Check database (new storage)
    try:
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT user_id FROM tracked_wallets WHERE wallet_address = ?",
                (wallet_address,)
            )
            db_users = await cur.fetchall()
            for (user_id_str,) in db_users:
                user_id = int(user_id_str)  # Convert back to int
                if user_id not in users_to_notify:
                    users_to_notify.append(user_id)
    except Exception as db_error:
        logger.error(f"Error reading users from database for alerts: {db_error}")
    
    if not users_to_notify:
        return
    
    # Format the message based on alert type
    alert_type = position.get('alert_type', 'NEW_POSITION')
    
    if alert_type == 'NEW_POSITION':
        side_emoji = "📈" if position['direction'] == 'LONG' else "📉"
        action_text = "opened a new"
    elif alert_type == 'POSITION_INCREASE':
        side_emoji = "📈" if position['direction'] == 'LONG' else "📉"
        action_text = "added to"
    elif alert_type == 'POSITION_DECREASE':
        side_emoji = "📉" if position['direction'] == 'LONG' else "📈"
        action_text = "reduced"
    elif alert_type == 'POSITION_CLOSED':
        side_emoji = "🔒"
        action_text = "closed"
    elif alert_type == 'LIQUIDATION':
        side_emoji = "🔥"
        action_text = "was liquidated on"
    elif alert_type == 'TWAP_STARTED':
        side_emoji = "⏰"
        action_text = "started TWAP"
    elif alert_type == 'TWAP_COMPLETED':
        side_emoji = "✅"
        action_text = "completed TWAP"
    elif alert_type == 'TWAP_CANCELLED':
        side_emoji = "❌"
        action_text = "cancelled TWAP"
    elif alert_type == 'TWAP_RESUMED':
        side_emoji = "▶️"
        action_text = "resumed TWAP"
    elif alert_type == 'TWAP_PAUSED':
        side_emoji = "⏸️"
        action_text = "paused TWAP"
    else:
        side_emoji = "📊"
        action_text = "updated"
    
    # Handle position value formatting
    if position.get('position_value') and position['position_value'] != 'N/A':
        try:
            pos_value = float(position['position_value'])
            size_str = f"${pos_value:,.0f}"
        except (ValueError, TypeError):
            if position.get('szi'):
                size_str = f"{abs(float(position['szi'])):.2f}"
            else:
                size_str = "unknown"
    else:
        if position.get('szi'):
            size_str = f"{abs(float(position['szi'])):.2f}"
        elif position.get('size'):
            size_str = f"{abs(float(position['size'])):.2f}"
        else:
            size_str = "unknown"
    
    # Additional info for different alert types
    additional_info = ""
    if alert_type == 'NEW_POSITION':
        # Show entry price for new positions
        entry_price = ""
        if position.get('entry_px') and position['entry_px'] != 'N/A':
            try:
                entry_px = float(position['entry_px'])
                entry_price = f" @ ${entry_px:,.4f}"
            except (ValueError, TypeError):
                pass
        additional_info = entry_price
    elif alert_type == 'POSITION_INCREASE' and 'size_change' in position:
        # Show entry price for position increases too
        entry_price = ""
        if position.get('entry_px') and position['entry_px'] != 'N/A':
            try:
                entry_px = float(position['entry_px'])
                entry_price = f" @ ${entry_px:,.4f}"
            except (ValueError, TypeError):
                pass
        # Show the added amount more clearly
        additional_info = f" (+{position['size_change']:.2f} added){entry_price}"
    elif alert_type == 'POSITION_DECREASE' and 'size_change' in position:
        additional_info = f" (-{position['size_change']:.2f}, {position.get('remaining_size', 0):.2f} remaining)"
    elif alert_type == 'POSITION_CLOSED' and 'closed_size' in position:
        pnl_text = ""
        if position.get('closing_pnl') is not None:
            pnl = position['closing_pnl']
            if pnl > 0:
                pnl_text = f" | 🟢 +${pnl:,.2f}"
            elif pnl < 0:
                pnl_text = f" | 🔴 ${pnl:,.2f}"
            else:
                pnl_text = f" | ⚪ ${pnl:,.2f}"
        additional_info = f" (closed {position['closed_size']:.2f}{pnl_text})"
    elif alert_type == 'LIQUIDATION' and 'liquidated_size' in position:
        pnl_text = ""
        if position.get('closing_pnl') is not None:
            pnl = position['closing_pnl']
            pnl_text = f" | 🔴 ${pnl:,.2f} loss"
        additional_info = f" (liquidated {position['liquidated_size']:.2f}{pnl_text})"
    elif alert_type == 'TWAP_COMPLETED' and position.get('filled'):
        additional_info = f" (filled: {position['filled']})"
    
    # Special handling for different alert types
    if alert_type.startswith('TWAP_'):
        # For TWAP orders, show TWAP-specific size information
        twap_size = position.get('size', position.get('szi', 'unknown'))
        try:
            twap_size_val = abs(float(twap_size))
            twap_size_str = f"{twap_size_val:.2f}"
        except (ValueError, TypeError):
            twap_size_str = str(twap_size)
        
        message = (
            f"{side_emoji} **{wallet_address[:6]}...{wallet_address[-4:]}** ([{alias}](https://hypurrscan.io/address/{wallet_address})) "
            f"just {action_text} **{position['direction']}** on ${position['coin']} "
            f"(TWAP size: {twap_size_str}){additional_info}."
        )
    elif alert_type in ['POSITION_CLOSED', 'LIQUIDATION']:
        message = (
            f"{side_emoji} **{wallet_address[:6]}...{wallet_address[-4:]}** ([{alias}](https://hypurrscan.io/address/{wallet_address})) "
            f"just {action_text} **{position['direction']}** position on ${position['coin']}"
            f"{additional_info}."
        )
    else:
        # Format message differently based on alert type for clarity
        if alert_type == 'POSITION_INCREASE':
            # For position increases, show detailed breakdown
            added_amount = position.get('size_change', 0)
            
            # Calculate USD value of added amount and current price
            added_usd_value = ""
            current_price = ""
            avg_entry_price = ""
            
            # Try to get average entry price
            if position.get('entry_px') and position['entry_px'] != 'N/A':
                try:
                    entry_px = float(position['entry_px'])
                    avg_entry_price = f"${entry_px:,.4f}".rstrip('0').rstrip('.')
                except (ValueError, TypeError):
                    avg_entry_price = "N/A"
            
            # Calculate current price from position value and size
            if position.get('position_value') and position['position_value'] != 'N/A' and position.get('szi'):
                try:
                    pos_value = abs(float(position['position_value']))
                    total_size = abs(float(position['szi']))
                    if total_size > 0:
                        current_px = pos_value / total_size
                        current_price = f"${current_px:,.4f}".rstrip('0').rstrip('.')
                        # Calculate USD value of added amount
                        added_value = added_amount * current_px
                        added_usd_value = f"${added_value:,.2f}"
                except (ValueError, TypeError, ZeroDivisionError):
                    pass
            
            # Build the enhanced message
            added_info = f"**{added_amount:.2f}**"
            if added_usd_value:
                added_info += f" ({added_usd_value})"
            
            price_info = ""
            if current_price:
                price_info = f" at {current_price}"
            
            position_details = f"Total Position Size: {size_str}"
            if avg_entry_price and avg_entry_price != "N/A":
                position_details += f", Average Entry: {avg_entry_price}"
            
            message = (
                f"{side_emoji} **{wallet_address[:6]}...{wallet_address[-4:]}** ([{alias}](https://hypurrscan.io/address/{wallet_address})) "
                f"just added {added_info} to **{position['direction']}** on ${position['coin']}"
                f"{price_info} ({position_details})."
            )
        elif alert_type == 'NEW_POSITION':
            message = (
                f"{side_emoji} **{wallet_address[:6]}...{wallet_address[-4:]}** ([{alias}](https://hypurrscan.io/address/{wallet_address})) "
                f"just {action_text} **{position['direction']}** on ${position['coin']} "
                f"with {size_str} size{additional_info}."
            )
        else:
            message = (
                f"{side_emoji} **{wallet_address[:6]}...{wallet_address[-4:]}** ([{alias}](https://hypurrscan.io/address/{wallet_address})) "
                f"just {action_text} **{position['direction']}** on ${position['coin']} "
                f"with {size_str} size{additional_info}."
            )
    
    # Send to all users tracking this wallet
    for user_id in users_to_notify:
        try:
            await app_instance.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown'
            )
            logger.info(f"Sent position alert to user {user_id} for wallet {wallet_address}")
        except Exception as e:
            logger.error(f"Failed to send alert to user {user_id}: {e}")

async def send_transfer_alert(wallet_address: str, alias: str, message: str):
    """Send a transfer alert to all users tracking this wallet."""
    if not app_instance:
        return
    
    # Find all users tracking this wallet
    users_to_notify = []
    
    # Check old in-memory storage (legacy support)
    for user_id, wallets in user_wallets.items():
        for wallet in wallets:
            if wallet['address'] == wallet_address:
                users_to_notify.append(user_id)
                break
    
    # Check database (new storage)
    try:
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT user_id FROM tracked_wallets WHERE wallet_address = ?",
                (wallet_address,)
            )
            db_users = await cur.fetchall()
            for (user_id_str,) in db_users:
                user_id = int(user_id_str)  # Convert back to int
                if user_id not in users_to_notify:
                    users_to_notify.append(user_id)
    except Exception as db_error:
        logger.error(f"Error reading users from database for alerts: {db_error}")
    
    if not users_to_notify:
        return
    
    # Send to all users tracking this wallet
    for user_id in users_to_notify:
        try:
            await app_instance.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown'
            )
            logger.info(f"Sent transfer alert to user {user_id} for wallet {wallet_address}")
        except Exception as e:
            logger.error(f"Failed to send transfer alert to user {user_id}: {e}")

async def monitor_transfers_job(context: ContextTypes.DEFAULT_TYPE):
    """Job function to monitor wallet transfers every 30 seconds."""
    try:
        # Get all unique wallet addresses from database
        all_wallets = set()
        
        # Get from old in-memory storage (legacy support)
        for user_wallets_list in user_wallets.values():
            for wallet in user_wallets_list:
                all_wallets.add((wallet['address'], wallet['alias']))
        
        # Get from database (new storage)
        try:
            async with aiosqlite.connect(DATABASE_FILE) as db:
                cur = await db.execute(
                    "SELECT DISTINCT wallet_address, alias FROM tracked_wallets"
                )
                db_wallets = await cur.fetchall()
                for wallet_address, alias in db_wallets:
                    all_wallets.add((wallet_address, alias))
        except Exception as db_error:
            logger.error(f"Error reading wallets from database: {db_error}")
        
        # Check transfers for each wallet with rate limiting
        for i, (wallet_address, alias) in enumerate(all_wallets):
            new_transfer_messages = await check_new_transfers(wallet_address, alias)
            
            # Send alerts for new transfers
            for message in new_transfer_messages:
                await send_transfer_alert(wallet_address, alias, message)
                await asyncio.sleep(1)  # Small delay between messages
            
            # Add delay between API calls to avoid rate limiting (except for last wallet)
            if i < len(all_wallets) - 1:
                await asyncio.sleep(2)  # 2 second delay between wallet checks
        
        logger.info(f"Completed transfer check for {len(all_wallets)} wallets")
        
    except Exception as e:
        logger.error(f"Error in transfer monitoring: {e}")

async def get_user_positions(wallet_address: str) -> dict:
    """Query Hyperliquid API for user state (positions and margin)."""
    try:
        url = "https://api.hyperliquid.xyz/info"
        
        # Get perpetuals data
        perp_payload = {
            "type": "clearinghouseState",
            "user": wallet_address
        }
        
        # Get spot data
        spot_payload = {
            "type": "spotClearinghouseState", 
            "user": wallet_address
        }
        
        async with aiohttp.ClientSession() as session:
            # Fetch both futures and spot data in parallel
            perp_task = session.post(url, json=perp_payload)
            spot_task = session.post(url, json=spot_payload)
            
            perp_response = await perp_task
            spot_response = await spot_task
            
            combined_data = {}
            
            if perp_response.status == 200:
                perp_data = await perp_response.json()
                combined_data.update(perp_data)
                logger.debug(f"Successfully fetched perp data for {wallet_address}")
            else:
                logger.error(f"Perp API request failed for {wallet_address}: {perp_response.status}")
                
            if spot_response.status == 200:
                spot_data = await spot_response.json()
                # Add spot data under spotPositions key to match expected format
                combined_data['spotPositions'] = spot_data.get('balances', [])
                logger.debug(f"Successfully fetched spot data for {wallet_address}")
            else:
                logger.error(f"Spot API request failed for {wallet_address}: {spot_response.status}")
            
            return combined_data
            
    except Exception as e:
        logger.error(f"Error fetching user state for {wallet_address}: {e}")
        return {}

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show trading statistics for a tracked wallet by alias."""
    user_id = update.effective_user.id
    
    # Check if user provided an alias
    if not context.args:
        await update.message.reply_text(
            "❌ *Please provide an alias.*\n\n"
            "*Usage:* /stats <alias>\n\n"
            "*Examples:*\n"
            "• /stats WhaleTrader\n"
            "• /stats Big Trader\n\n"
            "📝 *Note:* Use `/mywallet` for your own HyperMate wallet!\n"
            "Use /list to see your tracked wallets and their aliases.",
            parse_mode='Markdown'
        )
        return
    
    # Get the alias (join all args in case alias has spaces)
    alias_to_find = ' '.join(context.args)
    
    # Check if user has any tracked wallets
    if user_id not in user_wallets or not user_wallets[user_id]:
        await update.message.reply_text(
            "📭 *No wallets tracked yet.*\n\n"
            "Add a wallet first with:\n"
            "• /add <wallet_address> <alias>\n\n"
            "📝 *Note:* Use `/mywallet` for your own HyperMate wallet!\n"
            "This is for tracking external wallets.",
            parse_mode='Markdown'
        )
        return
    
    # Find the wallet with the given alias
    wallet_address = None
    for wallet in user_wallets[user_id]:
        if wallet['alias'] == alias_to_find:
            wallet_address = wallet['address']
            break
    
    if not wallet_address:
        await update.message.reply_text("Alias not found.")
        return
    
    # Query the Hyperliquid API for portfolio stats
    try:
        url = "https://api.hyperliquid.xyz/info"
        payload = {
            "type": "portfolio",
            "user": wallet_address
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Successfully fetched stats for {wallet_address}")
                    logger.info(f"API Response: {data}")
                    
                    if not data:
                        await update.message.reply_text("Stats not available for this wallet.")
                        return
                    
                    # Find allTime data from the portfolio response
                    all_time_data = None
                    for period_data in data:
                        if len(period_data) >= 2 and period_data[0] == "allTime":
                            all_time_data = period_data[1]
                            break
                    
                    if not all_time_data:
                        await update.message.reply_text("Stats not available for this wallet.")
                        return
                    
                    # Extract total volume
                    total_volume = all_time_data.get('vlm', '0')
                    
                    # Calculate total PnL from pnlHistory
                    pnl_history = all_time_data.get('pnlHistory', [])
                    total_pnl = 0
                    if pnl_history:
                        # Get the latest PnL value (last entry in history)
                        latest_pnl = pnl_history[-1]
                        if len(latest_pnl) >= 2:
                            total_pnl = float(latest_pnl[1])
                    
                    # Note: Individual trade wins/losses are not available from portfolio endpoint
                    # We can only provide volume and PnL from this endpoint
                    
                    # Format the values
                    try:
                        pnl_val = float(total_pnl)
                        pnl_emoji = "🟢" if pnl_val >= 0 else "🔴"
                        pnl_sign = "+" if pnl_val >= 0 else ""
                        pnl_str = f"{pnl_emoji} {pnl_sign}${pnl_val:,.2f}"
                    except (ValueError, TypeError):
                        pnl_str = "N/A"
                    
                    try:
                        volume_val = float(total_volume)
                        if volume_val >= 1000000:
                            volume_str = f"${volume_val/1000000:.1f}M"
                        elif volume_val >= 1000:
                            volume_str = f"${volume_val/1000:.1f}K"
                        else:
                            volume_str = f"${volume_val:,.0f}"
                    except (ValueError, TypeError):
                        volume_str = "N/A"
                    
                    # Format the final message
                    message = (
                        f"📊 *Stats for {alias_to_find}*\n"
                        f"`{wallet_address}`\n\n"
                        f"🧮 *PnL:* {pnl_str}\n"
                        f"📈 *Volume:* {volume_str}\n"
                        f"📝 *Note:* Win/loss data not available from API"
                    )
                    
                    await update.message.reply_text(message, parse_mode='Markdown')
                    logger.info(f"User {user_id} checked stats for wallet {wallet_address} ({alias_to_find})")
                else:
                    logger.error(f"API request failed for {wallet_address}: {response.status}")
                    await update.message.reply_text("Stats not available for this wallet.")
    except Exception as e:
        logger.error(f"Error fetching stats for {wallet_address}: {e}")
        await update.message.reply_text("Stats not available for this wallet.")

async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current positions for a tracked wallet by alias."""
    user_id = update.effective_user.id
    
    # Check if user provided an alias
    if not context.args:
        await update.message.reply_text(
            "❌ **Please provide an alias.**\n\n"
            "**Usage:** `/positions <alias>`\n\n"
            "**Examples:**\n"
            "• `/positions WhaleTrader`\n"
            "• `/positions Big Trader`\n\n"
            "📝 **Note:** Use `/mywallet` for your own HyperMate wallet!\n"
            "Use `/list` to see your tracked wallets and their aliases.",
            parse_mode='Markdown'
        )
        return
    
    # Get the alias (join all args in case alias has spaces)
    alias_to_find = ' '.join(context.args)
    
    # Check if user has any tracked wallets
    if user_id not in user_wallets or not user_wallets[user_id]:
        await update.message.reply_text(
            "📭 **No wallets tracked yet.**\n\n"
            "Add your first wallet with:\n"
            "• `/add <wallet_address> <alias>`\n\n"
            "**Examples:**\n"
            "• `/add 0x1234567890abcdef1234567890abcdef12345678 WhaleTrader`\n"
            "• `/add 0x1234567890abcdef1234567890abcdef12345678 Big Trader`\n\n"
            "📝 **Note:** Use `/mywallet` for your own HyperMate wallet!\n"
            "This is for tracking external wallets.",
            parse_mode='Markdown'
        )
        return
    
    # Find the wallet with the given alias
    wallet_address = None
    for wallet in user_wallets[user_id]:
        if wallet['alias'] == alias_to_find:
            wallet_address = wallet['address']
            break
    
    if not wallet_address:
        await update.message.reply_text(
            "❌ **Alias not found.**\n\n"
            f"Alias '{alias_to_find}' is not in your tracked wallets.\n"
            "Use `/list` to see your tracked wallets.",
            parse_mode='Markdown'
        )
        return
    
    # Query the API for user state
    user_state = await get_user_positions(wallet_address)
    
    if not user_state:
        await update.message.reply_text(
            "❌ **Failed to fetch position data.**\n\n"
            "There was an error querying the Hyperliquid API.",
            parse_mode='Markdown'
        )
        return
    
    # Extract margin balance
    margin_balance = "N/A"
    if 'marginSummary' in user_state:
        margin_summary = user_state['marginSummary']
        # Try different possible field names for account value
        account_value_field = None
        for field in ['accountValue', 'usdcValue', 'totalValue']:
            if field in margin_summary:
                account_value_field = field
                break
        
        if account_value_field:
            try:
                balance = float(margin_summary[account_value_field])
                margin_balance = f"${balance:,.2f}"
            except (ValueError, TypeError):
                margin_balance = "N/A"
    
    # Extract futures positions
    futures_text = ""
    futures_count = 0
    
    if 'assetPositions' in user_state:
        for pos in user_state['assetPositions']:
            if 'position' in pos:
                position = pos['position']
                coin = position.get('coin', 'Unknown')
                szi = position.get('szi', '0')
                entry_px = position.get('entryPx', 'N/A')
                position_value = position.get('positionValue', 'N/A')
                
                # Only show positions with non-zero size
                if float(szi) != 0:
                    side = "LONG" if float(szi) > 0 else "SHORT"
                    side_emoji = "📈" if side == "LONG" else "📉"
                    
                    # Format position value
                    if position_value != 'N/A':
                        try:
                            pos_val = float(position_value)
                            size_str = f"${pos_val:,.0f}"
                        except (ValueError, TypeError):
                            size_str = f"{abs(float(szi)):.2f}"
                    else:
                        size_str = f"{abs(float(szi)):.2f}"
                    
                    # Format entry price
                    if entry_px != 'N/A':
                        try:
                            entry_price = float(entry_px)
                            entry_str = f"${entry_price:,.4f}".rstrip('0').rstrip('.')
                        except (ValueError, TypeError):
                            entry_str = "N/A"
                    else:
                        entry_str = "N/A"
                    
                    # Format unrealized PnL
                    unrealized_pnl = position.get('unrealizedPnl', 'N/A')
                    if unrealized_pnl != 'N/A':
                        try:
                            pnl_val = float(unrealized_pnl)
                            pnl_emoji = "🟢" if pnl_val >= 0 else "🔴"
                            pnl_str = f"{pnl_emoji} ${pnl_val:,.2f}"
                        except (ValueError, TypeError):
                            pnl_str = "N/A"
                    else:
                        pnl_str = "N/A"
                    
                    # Format funding PnL
                    cum_funding = position.get('cumFunding', {})
                    funding_pnl_str = ""
                    if cum_funding:
                        since_open_funding = cum_funding.get('sinceOpen', 'N/A')
                        if since_open_funding != 'N/A':
                            try:
                                funding_val = float(since_open_funding)
                                if funding_val != 0:
                                    if funding_val < 0:
                                        funding_text = f"Received ${abs(funding_val):.2f}"
                                    else:
                                        funding_text = f"Paid ${abs(funding_val):.2f}"
                                    funding_pnl_str = f"\n🔁 Funding PnL: {funding_text}"
                            except (ValueError, TypeError):
                                pass
                    
                    futures_text += f"- {side_emoji} **{side}** ${coin} — Size: {size_str} — Entry: {entry_str} — PnL: {pnl_str}{funding_pnl_str}\n\n"
                    futures_count += 1
    
    # Extract spot positions
    spot_text = ""
    spot_count = 0
    
    if 'spotPositions' in user_state:
        for spot_pos in user_state['spotPositions']:
            coin = spot_pos.get('coin', 'Unknown')
            total = spot_pos.get('total', '0')
            entry_ntl = spot_pos.get('entryNtl', '0')
            
            try:
                total_val = float(total)
                entry_ntl_val = float(entry_ntl)
                
                # Calculate USD value using entry notional or price lookup
                # For now, use entry_ntl as USD value approximation
                usd_val = entry_ntl_val if entry_ntl_val > 0 else total_val
                
                # Only show spot assets with >$1 value and non-zero total
                if usd_val > 1.0 and total_val > 0:
                    spot_text += f"- {coin}: {total_val:.2f} (${usd_val:,.2f})\n"
                    spot_count += 1
            except (ValueError, TypeError):
                continue
    
    # Build the sections
    sections = []
    
    # Futures section
    if futures_count > 0:
        sections.append(f"📈 **Futures:**\n{futures_text}")
    else:
        sections.append("📈 **Futures:**\n- No open futures positions\n\n")
    
    # Spot section
    if spot_count > 0:
        sections.append(f"💰 **Spot:**\n{spot_text}")
    else:
        sections.append("💰 **Spot:**\n- No spot assets\n\n")
    
    positions_text = "\n".join(sections)
    
    # Format the final message
    message = (
        f"📊 **Positions for {alias_to_find}**\n"
        f"`{wallet_address}`\n\n"
        f"{positions_text}"
        f"📊 **Margin Balance:** {margin_balance}"
    )
    
    await update.message.reply_text(message, parse_mode='Markdown')
    logger.info(f"User {user_id} checked positions for wallet {wallet_address} ({alias_to_find})")

async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE):
    """Job function to monitor wallet positions every 30 seconds."""
    try:
        # Get all unique wallet addresses from database
        all_wallets = set()
        
        # Get from old in-memory storage (legacy support)
        for user_wallets_list in user_wallets.values():
            for wallet in user_wallets_list:
                all_wallets.add((wallet['address'], wallet['alias']))
        
        # Get from database (new storage)
        try:
            async with aiosqlite.connect(DATABASE_FILE) as db:
                cur = await db.execute(
                    "SELECT DISTINCT wallet_address, alias FROM tracked_wallets"
                )
                db_wallets = await cur.fetchall()
                for wallet_address, alias in db_wallets:
                    all_wallets.add((wallet_address, alias))
        except Exception as db_error:
            logger.error(f"Error reading wallets from database: {db_error}")
        
        # Check positions for each wallet with rate limiting
        for i, (wallet_address, alias) in enumerate(all_wallets):
            new_positions = await check_new_positions(wallet_address, alias)
            
            # Send alerts for new positions
            for position in new_positions:
                await send_position_alert(wallet_address, alias, position)
                await asyncio.sleep(1)  # Small delay between messages
            
            # Add delay between API calls to avoid rate limiting (except for last wallet)
            if i < len(all_wallets) - 1:
                await asyncio.sleep(2)  # 2 second delay between wallet checks
        
        logger.info(f"Completed position check for {len(all_wallets)} wallets")
        
    except Exception as e:
        logger.error(f"Error in position monitoring: {e}")

async def generate_wallet() -> tuple[str, str]:
    """Generate a new Ethereum wallet and return (private_key, address)."""
    # Generate a new account
    account = Account.create()
    private_key = account.key.hex()
    address = account.address
    
    return private_key, address

async def register_wallet_with_referral(private_key: str, address: str) -> bool:
    """Register the wallet with Hyperliquid using referral code."""
    try:
        # Create an Account object from the private key
        account = Account.from_key(private_key)
        
        # Try to create exchange instance and set referral
        try:
            # Method 1: Try with Account object
            exchange = Exchange(account, constants.MAINNET_API_URL)
            result = exchange.set_referrer("0XWIJI")
            
            # Check if the result indicates success
            if isinstance(result, dict) and result.get('status') == 'err':
                if 'does not exist' in result.get('response', ''):
                    logger.info(f"Wallet {address} not yet registered with Hyperliquid - referral will be set on first transaction")
                    return False  # Expected for new wallets
                else:
                    logger.warning(f"Referral registration failed for {address}: {result}")
                    return False
            else:
                logger.info(f"Referral registration successful for {address}: {result}")
                return True
                
        except Exception as method1_error:
            logger.debug(f"Method 1 (Account object) failed: {method1_error}")
            
            try:
                # Method 2: Try with clean hex string
                clean_private_key = private_key.replace('0x', '')
                exchange = Exchange(clean_private_key, constants.MAINNET_API_URL)
                result = exchange.set_referrer("0XWIJI")
                
                # Check result
                if isinstance(result, dict) and result.get('status') == 'err':
                    if 'does not exist' in result.get('response', ''):
                        logger.info(f"Wallet {address} not yet registered with Hyperliquid - referral will be set on first transaction")
                        return False
                    else:
                        logger.warning(f"Referral registration failed for {address}: {result}")
                        return False
                else:
                    logger.info(f"Referral registration successful for {address}: {result}")
                    return True
                    
            except Exception as method2_error:
                logger.debug(f"Method 2 (hex string) failed: {method2_error}")
                logger.info(f"Referral registration not possible for new wallet {address} - will be applied on first use")
                return False
            
    except Exception as e:
        logger.error(f"Error in referral registration for {address}: {e}")
        return False

async def retry_referral_registration(address: str, private_key: str) -> bool:
    """Retry setting referral code for a wallet that's now active."""
    try:
        account = Account.from_key(private_key)
        exchange = Exchange(account, constants.MAINNET_API_URL)
        result = exchange.set_referrer("0XWIJI")
        
        if isinstance(result, dict) and result.get('status') == 'err':
            return False
        else:
            logger.info(f"Referral code successfully set for active wallet {address}: {result}")
            return True
    except Exception as e:
        logger.debug(f"Referral retry failed for {address}: {e}")
        return False

async def createwallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for existing wallet and prompt for confirmation if needed."""
    user_id = str(update.effective_user.id)  # Convert to string to match database storage
    
    try:
        # Check if user already has a generated wallet in database
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT wallet_address FROM created_wallets WHERE user_id = ?",
                (user_id,)
            )
            existing_wallet = await cur.fetchone()
            
            if existing_wallet:
                existing_address = existing_wallet[0]
                await update.message.reply_text(
                    f"⚠️ You already have a wallet: {existing_address}\n\n"
                    f"Creating a new one will overwrite the existing wallet and cannot be undone.\n\n"
                    f"If you still want to proceed, use /confirmcreate.",
                    parse_mode='Markdown'
                )
                return
        
        # No existing wallet, create one directly
        await generate_new_wallet_for_user(update, int(user_id))
        
    except Exception as e:
        logger.error(f"Error checking existing wallet for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ **Error checking wallet status.**\n\n"
            "Please try again later.",
            parse_mode='Markdown'
        )

async def confirmcreate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a new wallet after user confirmation."""
    user_id = update.effective_user.id
    
    # Generate new wallet (this will replace any existing one)
    await generate_new_wallet_for_user(update, user_id)

async def generate_new_wallet_for_user(update: Update, user_id: int) -> None:
    """Generate a new Hyperliquid wallet for the user."""
    
    # Send initial message
    status_message = await update.message.reply_text(
        "🔄 **Generating new Hyperliquid wallet...**\n\n"
        "⏳ This may take a few seconds...",
        parse_mode='Markdown'
    )
    
    try:
        # Generate new wallet
        private_key, address = await generate_wallet()
        
        # Update status
        await status_message.edit_text(
            "🔄 **Wallet generated! Registering with Hyperliquid...**\n\n"
            f"Address: `{address}`\n"
            "⏳ Setting up referral link...",
            parse_mode='Markdown'
        )
        
        # Register wallet with referral
        registration_success = await register_wallet_with_referral(private_key, address)
        
        # Store wallet using secure storage
        await store_user_wallet(user_id, address, private_key)
        
        # Create success message
        success_message = (
            f"✅ New wallet created: {address}\n\n"
            f"Make sure to back it up using /exportkey if needed."
        )
        
        await status_message.edit_text(success_message, parse_mode='Markdown')
        
        logger.info(f"User {user_id} created new wallet {address}")
        
    except Exception as e:
        logger.error(f"Error creating wallet for user {user_id}: {e}")
        await status_message.edit_text(
            f"❌ **Wallet Creation Failed**\n\n"
            f"An error occurred while creating your wallet. Please try again later.\n"
            f"If the problem persists, please contact support.",
            parse_mode='Markdown'
        )

async def exportkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export the user's private key securely."""
    user_id = update.effective_user.id
    user_id_str = str(user_id)  # Convert to string for database storage
    
    try:
        # Check if user has a generated wallet in database
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT wallet_address FROM created_wallets WHERE user_id = ?",
                (user_id_str,)
            )
            result = await cur.fetchone()
            
            if not result:
                await update.message.reply_text(
                    "❌ **No Generated Wallet Found**\n\n"
                    "You don't have a wallet generated through this bot.\n"
                    "Use `/createwallet` to generate a new wallet.",
                    parse_mode='Markdown'
                )
                return
            
            address = result[0]
        
        # Get private key using secure storage
        private_key = await get_user_private_key(user_id)
        
        # Send private key in a secure format
        private_key_message = (
            f"🔐 **Private Key Export**\n\n"
            f"**Address:** `{address}`\n"
            f"**Private Key:** `{private_key}`\n\n"
            f"⚠️ **CRITICAL SECURITY WARNINGS:**\n"
            f"• **NEVER share this private key with anyone**\n"
            f"• Store it in a secure password manager\n"
            f"• Anyone with this key can access your funds\n"
            f"• Delete this message after backing up safely\n\n"
            f"🔒 **Recommended Storage:**\n"
            f"• Hardware wallet import\n"
            f"• Encrypted password manager\n"
            f"• Secure offline storage\n\n"
            f"**This message will self-destruct in 5 minutes for security.**"
        )
        
        # Send the private key message
        key_message = await update.message.reply_text(private_key_message, parse_mode='Markdown')
        
        # Schedule message deletion after 5 minutes
        async def delete_key_message():
            await asyncio.sleep(300)  # 5 minutes
            try:
                await key_message.delete()
                logger.info(f"Deleted private key message for user {user_id}")
            except Exception as e:
                logger.warning(f"Could not delete private key message: {e}")
        
        # Start the deletion task
        asyncio.create_task(delete_key_message())
        
        logger.info(f"User {user_id} exported private key for wallet {address}")
        
    except Exception as e:
        logger.error(f"Error exporting key for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ **Export Failed**\n\n"
            "An error occurred while retrieving your private key.\n"
            "Please try again later.",
            parse_mode='Markdown'
        )

async def mywallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the user's HyperMate wallet information and balances."""
    user_id = update.effective_user.id
    user_id_str = str(user_id)  # Convert to string for database storage
    
    try:
        # Check if user has a generated wallet in database
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT wallet_address FROM created_wallets WHERE user_id = ?",
                (user_id_str,)
            )
            result = await cur.fetchone()
            
            if not result:
                await update.message.reply_text(
                    "❌ **No Wallet Found**\n\n"
                    "You haven't created a wallet yet. Use `/createwallet` to get started.",
                    parse_mode='Markdown'
                )
                return
            
            address = result[0]
        
        # Send loading message
        loading_message = await update.message.reply_text(
            "🔄 **Fetching wallet information...**",
            parse_mode='Markdown'
        )
        
        # Query the API for user state
        user_state = await get_user_positions(address)
        
        if not user_state:
            await loading_message.edit_text(
                "❌ **Failed to fetch wallet data.**\n\n"
                "There was an error querying the Hyperliquid API.\n"
                "Please try again later.",
                parse_mode='Markdown'
            )
            return
        
        # Extract margin balance
        margin_balance = "N/A"
        if 'marginSummary' in user_state:
            margin_summary = user_state['marginSummary']
            # Try different possible field names for account value
            for field in ['accountValue', 'usdcValue', 'totalValue']:
                if field in margin_summary:
                    try:
                        balance = float(margin_summary[field])
                        margin_balance = f"${balance:,.2f}"
                        break
                    except (ValueError, TypeError):
                        continue
        
        # Extract futures positions
        futures_text = ""
        futures_count = 0
        
        if 'assetPositions' in user_state:
            for pos in user_state['assetPositions']:
                if 'position' in pos:
                    position = pos['position']
                    coin = position.get('coin', 'Unknown')
                    szi = position.get('szi', '0')
                    entry_px = position.get('entryPx', 'N/A')
                    position_value = position.get('positionValue', 'N/A')
                    
                    # Only show positions with non-zero size
                    if float(szi) != 0:
                        side = "LONG" if float(szi) > 0 else "SHORT"
                        side_emoji = "📈" if side == "LONG" else "📉"
                        
                        # Format position value
                        if position_value != 'N/A':
                            try:
                                pos_val = float(position_value)
                                size_str = f"${pos_val:,.0f}"
                            except (ValueError, TypeError):
                                size_str = f"{abs(float(szi)):.2f}"
                        else:
                            size_str = f"{abs(float(szi)):.2f}"
                        
                        # Format entry price
                        if entry_px != 'N/A':
                            try:
                                entry_price = float(entry_px)
                                entry_str = f"${entry_price:,.4f}".rstrip('0').rstrip('.')
                            except (ValueError, TypeError):
                                entry_str = "N/A"
                        else:
                            entry_str = "N/A"
                        
                        # Format unrealized PnL
                        unrealized_pnl = position.get('unrealizedPnl', 'N/A')
                        if unrealized_pnl != 'N/A':
                            try:
                                pnl_val = float(unrealized_pnl)
                                pnl_emoji = "🟢" if pnl_val >= 0 else "🔴"
                                pnl_str = f"{pnl_emoji} ${pnl_val:,.2f}"
                            except (ValueError, TypeError):
                                pnl_str = "N/A"
                        else:
                            pnl_str = "N/A"
                        
                        futures_text += f"• {side_emoji} **{side}** ${coin} — Size: {size_str} — Entry: {entry_str} — PnL: {pnl_str}\n"
                        futures_count += 1
        
        if futures_count == 0:
            futures_text = "• No open futures positions"
        
        # Extract spot balances
        spot_balances_text = ""
        total_spot_value = 0
        
        if 'spotPositions' in user_state and user_state['spotPositions']:
            spot_balances = []
            for spot_pos in user_state['spotPositions']:
                coin = spot_pos.get('coin', 'Unknown')
                total = spot_pos.get('total', '0')
                entry_ntl = spot_pos.get('entryNtl', '0')
                
                try:
                    total_val = float(total)
                    entry_ntl_val = float(entry_ntl)
                    
                    # Calculate USD value using entry notional
                    usd_val = entry_ntl_val if entry_ntl_val > 0 else 0
                    
                    # Only show spot assets with >$0.01 value and non-zero total
                    if usd_val > 0.01 and total_val > 0:
                        spot_balances.append(f"• {coin}: {total_val:.4f} (${usd_val:,.2f})")
                        total_spot_value += usd_val
                except (ValueError, TypeError):
                    continue
            
            if spot_balances:
                spot_balances_text = "\n".join(spot_balances)
            else:
                spot_balances_text = "• No spot assets"
        else:
            spot_balances_text = "• No spot assets"
        
        # Format wallet address (shortened)
        address_short = f"{address[:6]}...{address[-4:]}"
        
        # Calculate total portfolio value
        portfolio_section = ""
        if margin_balance != "N/A":
            try:
                margin_val = float(margin_balance.replace('$', '').replace(',', ''))
                total_portfolio = margin_val + total_spot_value
                portfolio_section = f"📊 **Total Portfolio Value:** ${total_portfolio:,.2f}"
            except (ValueError, TypeError):
                portfolio_section = f"📊 **Spot Value:** ${total_spot_value:,.2f}"
        else:
            portfolio_section = f"📊 **Spot Value:** ${total_spot_value:,.2f}"
        
        # Build the message
        message = (
            f"💳 **Your HyperMate Wallet**\n\n"
            f"🔐 **Address:** `{address_short}`\n"
            f"🔗 [View on Hypurrscan](https://hypurrscan.io/address/{address})\n\n"
            f"💰 **Margin Balance:** {margin_balance}\n\n"
            f"📈 **Futures Positions:**\n{futures_text}\n\n"
            f"🪙 **Spot Assets:**\n{spot_balances_text}\n\n"
            f"{portfolio_section}\n\n"
            f"🔧 **Wallet Management:**\n"
            f"• Export key: `/exportkey`\n"
            f"• Refresh wallet: `/mywallet`"
        )
        
        await loading_message.edit_text(message, parse_mode='Markdown')
        logger.info(f"User {user_id} viewed wallet information for {address}")
        
    except Exception as e:
        logger.error(f"Error fetching wallet info for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ **Error Loading Wallet**\n\n"
            "An error occurred while fetching your wallet information.\n"
            "Please try again later.",
            parse_mode='Markdown'
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the command /start is issued."""
    welcome_message = """🚀 *Welcome to HyperMate!*

Your ultimate companion for Hyperliquid wallet tracking and monitoring.

*What is HyperMate?*
HyperMate is a powerful Telegram bot designed to help you track and monitor wallets in the Hyperliquid ecosystem, including both HyperCore and HyperEVM.

*🎯 Current Features:*
• 📊 Real-time wallet tracking with custom aliases
• 📈📉 Perpetual position alerts (LONG/SHORT positions)
• 🟢🔴 Spot trading notifications (BUY/SELL orders)
• ↗️↘️ Transfer monitoring (in/out/internal)
• 💰💸 Deposit and withdrawal alerts
• 🔄 TWAP order tracking
• 📋 Position and balance viewing
• 📊 Trading statistics (PnL, volume)

*🤖 Available Commands:*

*Wallet Management:*
• `/add <wallet_address> <alias>` - Add a wallet to track
• `/list` - Show all your tracked wallets  
• `/remove <alias>` - Remove a tracked wallet

*Monitoring:*
• `/positions <alias>` - View current positions & balance
• `/stats <alias>` - Show trading statistics

*📝 Quick Start:*
1. **Track wallets:** `/add 0x1234...5678 WhaleTrader`
2. **Monitor activity:** `/positions WhaleTrader` • `/stats WhaleTrader`

*🔔 Real-Time Alerts:*
Once you add wallets, you'll automatically receive notifications for:
- New perpetual positions (longs/shorts)
- Position size changes
- Spot trading activity
- Token transfers and deposits
- TWAP order updates

Ready to start tracking? Add your first wallet! 🌟"""
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')



def is_valid_wallet_address(address: str) -> bool:
    """
    Validate if the provided string is a valid Hyperliquid wallet address.
    
    Args:
        address: The wallet address string to validate
        
    Returns:
        bool: True if valid, False otherwise
    """
    # Check if it's a 0x-prefixed hex string with exactly 42 characters
    if not address or len(address) != 42:
        return False
    
    if not address.startswith('0x'):
        return False
    
    # Check if the remaining 40 characters are valid hex
    hex_part = address[2:]
    return bool(re.match(r'^[0-9a-fA-F]{40}$', hex_part))

async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a wallet address to the user's tracking list."""
    user_id = str(update.effective_user.id)  # Convert to string to match database storage
    
    # Check if wallet address and alias are provided
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /add <address> <alias>",
            parse_mode='Markdown'
        )
        return
    
    wallet_address = context.args[0].strip()
    
    # Get alias (join remaining args as alias can contain spaces)
    alias = " ".join(context.args[1:]).strip()
    
    # Validate wallet address format
    if not is_valid_wallet_address(wallet_address):
        await update.message.reply_text(
            "Usage: /add <address> <alias>",
            parse_mode='Markdown'
        )
        return
    
    # Convert to lowercase for consistency
    wallet_address = wallet_address.lower()
    
    try:
        # Connect to database and perform checks
        async with aiosqlite.connect(DATABASE_FILE) as db:
            # Check if alias already exists for this user
            cur = await db.execute(
                "SELECT COUNT(*) FROM tracked_wallets WHERE user_id = ? AND alias = ?",
                (user_id, alias)
            )
            alias_count = await cur.fetchone()
            
            if alias_count[0] > 0:
                await update.message.reply_text(
                    "You're already tracking a wallet with this alias.",
                    parse_mode='Markdown'
                )
                return
            
            # Check if address is already being tracked by this user
            cur = await db.execute(
                "SELECT COUNT(*) FROM tracked_wallets WHERE user_id = ? AND wallet_address = ?",
                (user_id, wallet_address)
            )
            address_count = await cur.fetchone()
            
            if address_count[0] > 0:
                await update.message.reply_text(
                    "You've already added this address.",
                    parse_mode='Markdown'
                )
                return
            
            # Insert new wallet
            await db.execute(
                "INSERT INTO tracked_wallets (user_id, wallet_address, alias) VALUES (?, ?, ?)",
                (user_id, wallet_address, alias)
            )
            await db.commit()
        
        # Success response
        await update.message.reply_text(
            f"✅ Wallet added as '{alias}'",
            parse_mode='Markdown'
        )
        
        logger.info(f"User {user_id} added wallet {wallet_address} with alias '{alias}' to tracking list")
        
    except Exception as e:
        logger.error(f"Error adding wallet for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ **Error adding wallet.**\n\n"
            "Please try again later.",
            parse_mode='Markdown'
        )

async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all tracked wallets for the user."""
    user_id = str(update.effective_user.id)  # Convert to string to match database storage
    
    try:
        # Query the database for user's tracked wallets
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT wallet_address, alias, added_at FROM tracked_wallets WHERE user_id = ? ORDER BY alias",
                (user_id,)
            )
            wallets = await cur.fetchall()
        
        # Check if user has any wallets
        if not wallets:
            await update.message.reply_text(
                "You're not tracking any wallets yet. Use /add to start.",
                parse_mode='Markdown'
            )
            return
        
        # Build the list of wallets
        wallet_list = []
        for wallet_address, alias, added_at in wallets:
            # Create clickable link to hypurrscan using the alias as link text
            hypurrscan_link = f"[{alias}](https://hypurrscan.io/address/{wallet_address})"
            # Show full wallet address
            wallet_info = f"• {hypurrscan_link}: {wallet_address}"
            wallet_list.append(wallet_info)
        
        wallets_text = "\n".join(wallet_list)
        
        await update.message.reply_text(
            f"Here are your tracked wallets:\n"
            f"{wallets_text}",
            parse_mode='Markdown'
        )
        
        logger.info(f"User {user_id} listed {len(wallets)} tracked wallets")
        
    except Exception as e:
        logger.error(f"Error listing wallets for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ **Error loading your tracked wallets.**\n\n"
            "Please try again later.",
            parse_mode='Markdown'
        )

async def remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a wallet from the user's tracking list by alias."""
    user_id = str(update.effective_user.id)  # Convert to string to match database storage
    
    # Check if alias is provided
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /remove <alias>",
            parse_mode='Markdown'
        )
        return
    
    # Get alias (join all args as alias can contain spaces)
    alias_to_remove = " ".join(context.args).strip()
    
    try:
        # Connect to database and remove the wallet
        async with aiosqlite.connect(DATABASE_FILE) as db:
            # Delete the wallet with matching user_id and alias
            cur = await db.execute(
                "DELETE FROM tracked_wallets WHERE user_id = ? AND alias = ?",
                (user_id, alias_to_remove)
            )
            await db.commit()
            
            # Check if any rows were deleted
            if cur.rowcount > 0:
                await update.message.reply_text(
                    f"✅ Removed '{alias_to_remove}' from your tracked wallets.",
                    parse_mode='Markdown'
                )
                logger.info(f"User {user_id} removed wallet with alias '{alias_to_remove}' from tracking list")
            else:
                await update.message.reply_text(
                    "Alias not found.",
                    parse_mode='Markdown'
                )
        
    except Exception as e:
        logger.error(f"Error removing wallet for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ **Error removing wallet.**\n\n"
            "Please try again later.",
            parse_mode='Markdown'
        )





async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main() -> None:
    """Start the bot."""
    global app_instance
    
    # Validate configuration
    try:
        Config.validate_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Please set the BOT_TOKEN and WALLET_ENCRYPTION_KEY environment variables")
        return
    
    # Initialize database
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(init_db())
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return
    
    # Load existing wallet data
    load_wallets()
    
    # Migrate tracked wallets from JSON to database
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(migrate_tracked_wallets_to_db())
    except Exception as e:
        logger.error(f"Failed to migrate tracked wallets: {e}")
    
    # Migrate old wallets to secure storage if needed
    migrate_old_wallets()
    
    # Load secure wallets
    load_secure_wallets()
    
    # Create the Application
    application = Application.builder().token(Config.BOT_TOKEN).build()
    app_instance = application

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    # Wallet creation features disabled
    # application.add_handler(CommandHandler("createwallet", createwallet_command))
    # application.add_handler(CommandHandler("confirmcreate", confirmcreate_command))
    # application.add_handler(CommandHandler("exportkey", exportkey_command))
    # application.add_handler(CommandHandler("mywallet", mywallet_command))
    application.add_handler(CommandHandler("add", add_wallet))
    application.add_handler(CommandHandler("list", list_wallets))
    application.add_handler(CommandHandler("remove", remove_wallet))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    # Register error handler
    application.add_error_handler(error_handler)

    # Schedule the position monitoring job to run every 30 seconds
    job_queue = application.job_queue
    job_queue.run_repeating(monitor_positions_job, interval=30, first=10)
    
    # Schedule the transfer monitoring job to run every 30 seconds (offset by 15 seconds)
    job_queue.run_repeating(monitor_transfers_job, interval=30, first=25)

    # Run the bot until the user presses Ctrl-C
    logger.info("Starting HyperMate bot...")
    logger.info("Position monitoring will start in 10 seconds...")
    logger.info("Transfer monitoring will start in 25 seconds...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main() 