"""Legacy wallet generation / JSON storage code (to be removed)."""

import asyncio
import base64
import hashlib
import json
import logging
import os
from typing import Dict, List

import aiosqlite
from cryptography.fernet import Fernet
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
import secrets
from telegram import Update
from telegram.ext import ContextTypes

from hypermate.config import Config
from hypermate.db.repo import DATABASE_FILE
from hypermate.venues.hyperliquid.client import get_user_positions

logger = logging.getLogger(__name__)

# Data file path
DATA_FILE = 'user_wallets.json'

# In-memory storage for tracked wallets per user
# Structure: {user_id: [{"address": "0x...", "alias": "name"}, ...]}
user_wallets: Dict[int, List[Dict[str, str]]] = {}

# Store user-generated wallets (encrypted private keys)
# Structure: {user_id: {"address": "0x...", "encrypted_key": "encrypted_private_key"}}
user_generated_wallets: Dict[int, Dict[str, str]] = {}

# Data file for generated wallets
GENERATED_WALLETS_FILE = 'generated_wallets.json'

# Data file for secure wallet storage
SECURE_WALLETS_FILE = 'wallets_secure.json'

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

