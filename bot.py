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
                return f"↗️ **{alias}** sent {amount:.2f} {token} (${usd_value:,.2f}) to `{dest_short}`"
            elif destination.lower() == wallet_address.lower():
                # Incoming transfer
                user_short = f"{user[:6]}...{user[-4:]}" if len(user) > 10 else user
                return f"↘️ **{alias}** received {amount:.2f} {token} (${usd_value:,.2f}) from `{user_short}`"
        
        elif transfer_type == 'accountClassTransfer':
            usdc_amount = float(delta.get('usdc', 0))
            to_perp = delta.get('toPerp', True)
            
            if to_perp:
                return f"🔄 **{alias}** transferred ${usdc_amount:,.2f} from Spot to Perp"
            else:
                return f"🔄 **{alias}** transferred ${usdc_amount:,.2f} from Perp to Spot"
        
        elif transfer_type == 'deposit':
            usdc_amount = float(delta.get('usdc', 0))
            return f"💰 **{alias}** deposited ${usdc_amount:,.2f}"
        
        elif transfer_type == 'withdraw':
            usdc_amount = float(delta.get('usdc', 0))
            return f"💸 **{alias}** withdrew ${usdc_amount:,.2f}"
        
        # For other types, try to detect buy/sell from spot fills
        # This would require additional API calls to get recent fills
        # For now, return a generic message
        return f"📊 **{alias}** - {transfer_type}: {delta}"
        
    except Exception as e:
        logger.error(f"Error formatting transfer message: {e}")
        return f"📊 **{alias}** had a transfer activity"

def format_spot_fill_message(fill: dict, wallet_address: str, alias: str) -> str:
    """Format a spot fill into a buy/sell message."""
    try:
        coin = fill.get('coin', 'Unknown').replace('USDC', '')  # Remove USDC suffix for cleaner display
        side = fill.get('side', 'unknown')
        price = float(fill.get('px', 0))
        quantity = float(fill.get('sz', 0))
        usd_value = price * quantity
        
        if side.lower() == 'b':  # Buy
            return f"🟢 **{alias}** bought {quantity:.2f} {coin} for ${usd_value:,.2f} @ ${price:.4f}"
        elif side.lower() == 's':  # Sell
            return f"🔴 **{alias}** sold {quantity:.2f} {coin} for ${usd_value:,.2f} @ ${price:.4f}"
        else:
            return f"📊 **{alias}** traded {quantity:.2f} {coin} for ${usd_value:,.2f} @ ${price:.4f}"
        
    except Exception as e:
        logger.error(f"Error formatting spot fill message: {e}")
        return f"📊 **{alias}** had a spot trading activity"

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
    """Check for new positions and return list of new ones."""
    current_positions = await get_wallet_positions(wallet_address)
    
    if not current_positions or 'assetPositions' not in current_positions:
        logger.debug(f"No positions data for {wallet_address}")
        return []
    
    # Check if this is the first scan for this wallet
    is_initial_scan = wallet_address not in initial_scan_done
    
    # Get current asset positions
    asset_positions = current_positions.get('assetPositions', [])
    logger.debug(f"Found {len(asset_positions)} asset positions for {wallet_address}")
    
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
                    'coin': coin
                }
    
    # Get previous positions for this wallet
    previous_position_map = previous_positions.get(wallet_address, {})
    
    new_position_alerts = []
    
    if is_initial_scan:
        # First scan - record positions but don't alert
        logger.info(f"Initial scan for {wallet_address} ({alias}) - recording {len(current_position_map)} positions")
        initial_scan_done[wallet_address] = True
    else:
        # Subsequent scan - check for new positions or size increases
        for coin, current_pos in current_position_map.items():
            if coin not in previous_position_map:
                # Completely new position
                logger.info(f"New position detected: {coin} for {wallet_address} ({alias})")
                new_position_alerts.append({
                    **current_pos,
                    'alert_type': 'NEW_POSITION'
                })
            else:
                # Position exists - check if size increased
                prev_szi = float(previous_position_map[coin]['szi'])
                curr_szi = float(current_pos['szi'])
                
                # Check if position size increased (same direction)
                if ((prev_szi > 0 and curr_szi > prev_szi) or 
                    (prev_szi < 0 and curr_szi < prev_szi)):
                    size_increase = abs(curr_szi - prev_szi)
                    logger.info(f"Position size increase detected: {coin} for {wallet_address} ({alias}) - added {size_increase}")
                    new_position_alerts.append({
                        **current_pos,
                        'alert_type': 'POSITION_INCREASE',
                        'size_increase': size_increase
                    })
    
    # Check for TWAP orders (if present in the API response)
    if 'twapOrders' in current_positions:
        twap_alerts = await check_twap_orders(wallet_address, alias, current_positions['twapOrders'])
        new_position_alerts.extend(twap_alerts)
    
    # Update stored positions
    previous_positions[wallet_address] = current_position_map
    
    return new_position_alerts

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
    for user_id, wallets in user_wallets.items():
        for wallet in wallets:
            if wallet['address'] == wallet_address:
                users_to_notify.append(user_id)
                break
    
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
    
    # Additional info for position increases
    additional_info = ""
    if alert_type == 'POSITION_INCREASE' and 'size_increase' in position:
        additional_info = f" (+{position['size_increase']:.2f})"
    elif alert_type == 'TWAP_COMPLETED' and position.get('filled'):
        additional_info = f" (filled: {position['filled']})"
    
    # Special handling for TWAP messages
    if alert_type.startswith('TWAP_'):
        message = (
            f"{side_emoji} **{wallet_address[:6]}...{wallet_address[-4:]}** ({alias}) "
            f"just {action_text} **{position['direction']}** on ${position['coin']}"
            f"{additional_info}."
        )
    else:
        message = (
            f"{side_emoji} **{wallet_address[:6]}...{wallet_address[-4:]}** ({alias}) "
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
    for user_id, wallets in user_wallets.items():
        for wallet in wallets:
            if wallet['address'] == wallet_address:
                users_to_notify.append(user_id)
                break
    
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
        # Get all unique wallet addresses
        all_wallets = set()
        for user_wallets_list in user_wallets.values():
            for wallet in user_wallets_list:
                all_wallets.add((wallet['address'], wallet['alias']))
        
        # Check transfers for each wallet
        for wallet_address, alias in all_wallets:
            new_transfer_messages = await check_new_transfers(wallet_address, alias)
            
            # Send alerts for new transfers
            for message in new_transfer_messages:
                await send_transfer_alert(wallet_address, alias, message)
                await asyncio.sleep(1)  # Small delay between messages
        
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
            "• /stats MyWallet\n"
            "• /stats Big Trader\n\n"
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
            "• /add <wallet_address> <alias>",
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
            "• `/positions MyWallet`\n"
            "• `/positions Big Trader`\n\n"
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
            "Add a wallet first with:\n"
            "• `/add <wallet_address> <alias>`",
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
        # Get all unique wallet addresses
        all_wallets = set()
        for user_wallets_list in user_wallets.values():
            for wallet in user_wallets_list:
                all_wallets.add((wallet['address'], wallet['alias']))
        
        # Check positions for each wallet
        for wallet_address, alias in all_wallets:
            new_positions = await check_new_positions(wallet_address, alias)
            
            # Send alerts for new positions
            for position in new_positions:
                await send_position_alert(wallet_address, alias, position)
                await asyncio.sleep(1)  # Small delay between messages
        
        logger.info(f"Completed position check for {len(all_wallets)} wallets")
        
    except Exception as e:
        logger.error(f"Error in position monitoring: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the command /start is issued."""
    welcome_message = """🚀 *Welcome to HyperMate!*

Your ultimate companion for Hyperliquid trading and wallet tracking.

*What is HyperMate?*
HyperMate is a powerful Telegram bot designed to help you navigate the Hyperliquid ecosystem, including both HyperCore and HyperEVM.

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
1. Add a wallet: `/add 0x1234...5678 MyWallet`
2. Check positions: `/positions MyWallet`
3. View stats: `/stats MyWallet`

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
    user_id = update.effective_user.id
    
    # Check if wallet address and alias are provided
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Please provide both a wallet address and an alias.\n\n"
            "**Usage:** `/add <wallet_address> <alias>`\n\n"
            "**Examples:**\n"
            "• `/add 0x1234567890abcdef1234567890abcdef12345678 MyWallet`\n"
            "• `/add 0x1234567890abcdef1234567890abcdef12345678 Big Trader`\n"
            "• `/add 0x1234567890abcdef1234567890abcdef12345678 Degen Master`\n\n"
            "📝 **Note:** Aliases are required to help you identify your wallets!",
            parse_mode='Markdown'
        )
        return
    
    wallet_address = context.args[0].strip()
    
    # Get alias (join remaining args as alias can contain spaces)
    alias = " ".join(context.args[1:]).strip()
    
    # Validate wallet address format
    if not is_valid_wallet_address(wallet_address):
        await update.message.reply_text(
            "❌ **Invalid wallet address.**\n\n"
            "Wallet addresses must be:\n"
            "• 0x-prefixed hex strings\n"
            "• Exactly 42 characters long\n\n"
            "**Example:** 0x1234567890abcdef1234567890abcdef12345678",
            parse_mode='Markdown'
        )
        return
    
    # Convert to lowercase for consistency
    wallet_address = wallet_address.lower()
    
    # Initialize user's wallet list if not exists
    if user_id not in user_wallets:
        user_wallets[user_id] = []
    
    # Check if wallet is already being tracked
    existing_wallet = None
    for wallet in user_wallets[user_id]:
        if wallet["address"] == wallet_address:
            existing_wallet = wallet
            break
    
    if existing_wallet:
        await update.message.reply_text(
            "⚠️ **You're already tracking this wallet.**\n\n"
            f"Wallet: `{wallet_address}`\n"
            f"Current alias: **{existing_wallet['alias']}**",
            parse_mode='Markdown'
        )
        return
    
    # Add wallet to user's tracking list
    wallet_entry = {
        "address": wallet_address,
        "alias": alias
    }
    user_wallets[user_id].append(wallet_entry)
    
    # Save data to file
    save_wallets()
    
    logger.info(f"User {user_id} added wallet {wallet_address} with alias '{alias}' to tracking list")
    
    await update.message.reply_text(
        "✅ **Wallet added successfully!**\n\n"
        f"Wallet: `{wallet_address}`\n"
        f"Alias: **{alias}**\n"
        f"Total tracked wallets: {len(user_wallets[user_id])}\n\n"
        "🔔 *Real-time monitoring is now active!*",
        parse_mode='Markdown'
    )

async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all tracked wallets for the user."""
    user_id = update.effective_user.id
    
    # Check if user has any wallets
    if user_id not in user_wallets or not user_wallets[user_id]:
        await update.message.reply_text(
            "📭 **No wallets tracked yet.**\n\n"
            "Add your first wallet with:\n"
            "• `/add <wallet_address> <alias>`\n\n"
            "**Examples:**\n"
            "• `/add 0x1234567890abcdef1234567890abcdef12345678 MyWallet`\n"
            "• `/add 0x1234567890abcdef1234567890abcdef12345678 Big Trader`\n\n"
            "📝 **Note:** Aliases are required!",
            parse_mode='Markdown'
        )
        return
    
    # Build the list of wallets
    wallet_list = []
    for i, wallet in enumerate(user_wallets[user_id], 1):
        wallet_info = f"{i}. `{wallet['address']}`\n   🏷️ **{wallet['alias']}**"
        wallet_list.append(wallet_info)
    
    wallets_text = "\n\n".join(wallet_list)
    
    await update.message.reply_text(
        f"📊 **Your Tracked Wallets** ({len(user_wallets[user_id])} total)\n\n"
        f"{wallets_text}\n\n"
        "🔔 *Real-time monitoring is active!*",
        parse_mode='Markdown'
    )

async def remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a wallet from the user's tracking list by alias."""
    user_id = update.effective_user.id
    
    # Check if alias is provided
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Please provide an alias to remove.\n\n"
            "**Usage:** `/remove <alias>`\n\n"
            "**Examples:**\n"
            "• `/remove MyWallet`\n"
            "• `/remove Big Trader`\n\n"
            "Use `/list` to see all your tracked wallets and their aliases.",
            parse_mode='Markdown'
        )
        return
    
    # Get alias (join all args as alias can contain spaces)
    alias_to_remove = " ".join(context.args).strip()
    
    # Check if user has any wallets
    if user_id not in user_wallets or not user_wallets[user_id]:
        await update.message.reply_text(
            "📭 **No wallets tracked yet.**\n\n"
            "Add your first wallet with:\n"
            "• `/add <wallet_address> <alias>`",
            parse_mode='Markdown'
        )
        return
    
    # Find and remove the wallet with the matching alias
    wallet_to_remove = None
    for i, wallet in enumerate(user_wallets[user_id]):
        if wallet["alias"] == alias_to_remove:
            wallet_to_remove = user_wallets[user_id].pop(i)
            break
    
    if wallet_to_remove:
        # Save data to file
        save_wallets()
        
        logger.info(f"User {user_id} removed wallet {wallet_to_remove['address']} with alias '{alias_to_remove}' from tracking list")
        
        await update.message.reply_text(
            f"✅ **Removed {alias_to_remove} from your tracked wallets.**\n\n"
            f"Wallet: `{wallet_to_remove['address']}`\n"
            f"Remaining tracked wallets: {len(user_wallets[user_id])}",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "❌ **Alias not found.**\n\n"
            f"'{alias_to_remove}' is not in your tracked wallets.\n\n"
            "Use `/list` to see all your tracked wallets and their aliases.",
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
        logger.error("Please set the BOT_TOKEN environment variable")
        return
    
    # Load existing wallet data
    load_wallets()
    
    # Create the Application
    application = Application.builder().token(Config.BOT_TOKEN).build()
    app_instance = application

    # Register handlers
    application.add_handler(CommandHandler("start", start))
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