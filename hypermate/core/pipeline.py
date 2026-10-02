"""Polling jobs and alert delivery."""

import asyncio
import logging

import aiosqlite
from telegram.ext import ContextTypes

from hypermate import legacy
from hypermate.db.repo import DATABASE_FILE
from hypermate.venues.hyperliquid.adapter import check_new_positions, check_new_transfers

logger = logging.getLogger(__name__)

# Global application instance for sending messages
app_instance = None

async def send_position_alert(wallet_address: str, alias: str, position: dict):
    """Send a position alert to all users tracking this wallet."""
    if not app_instance:
        return
    
    # Find all users tracking this wallet
    users_to_notify = []
    
    # Check old in-memory storage (legacy support)
    for user_id, wallets in legacy.user_wallets.items():
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
    for user_id, wallets in legacy.user_wallets.items():
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
        for user_wallets_list in legacy.user_wallets.values():
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

async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE):
    """Job function to monitor wallet positions every 30 seconds."""
    try:
        # Get all unique wallet addresses from database
        all_wallets = set()
        
        # Get from old in-memory storage (legacy support)
        for user_wallets_list in legacy.user_wallets.values():
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

