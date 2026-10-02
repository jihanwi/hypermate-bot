"""Telegram command handlers."""

import logging
import re

import aiohttp
import aiosqlite
from telegram import Update
from telegram.ext import ContextTypes

from hypermate import legacy
from hypermate.db.repo import DATABASE_FILE
from hypermate.venues.hyperliquid.client import get_user_positions

logger = logging.getLogger(__name__)

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
    if user_id not in legacy.user_wallets or not legacy.user_wallets[user_id]:
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
    for wallet in legacy.user_wallets[user_id]:
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
    if user_id not in legacy.user_wallets or not legacy.user_wallets[user_id]:
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
    for wallet in legacy.user_wallets[user_id]:
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
    user_id_int = update.effective_user.id  # Keep integer version for JSON storage
    
    try:
        all_wallets = {}  # Use dict to avoid duplicates: {alias: wallet_address}
        
        # Query the database for user's tracked wallets
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cur = await db.execute(
                "SELECT wallet_address, alias, added_at FROM tracked_wallets WHERE user_id = ? ORDER BY alias",
                (user_id,)
            )
            db_wallets = await cur.fetchall()
            
            for wallet_address, alias, added_at in db_wallets:
                all_wallets[alias] = wallet_address
        
        # Also check JSON storage (legacy support)
        if user_id_int in legacy.user_wallets:
            for wallet in legacy.user_wallets[user_id_int]:
                alias = wallet.get('alias', '')
                address = wallet.get('address', '')
                if alias and address and alias not in all_wallets:
                    all_wallets[alias] = address.lower()
        
        # Check if user has any wallets
        if not all_wallets:
            await update.message.reply_text(
                "You're not tracking any wallets yet. Use /add to start.",
                parse_mode='Markdown'
            )
            return
        
        # Build the list of wallets
        wallet_list = []
        for alias in sorted(all_wallets.keys()):
            wallet_address = all_wallets[alias]
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
        
        logger.info(f"User {user_id} listed {len(all_wallets)} tracked wallets")
        
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
    user_id_int = update.effective_user.id  # Keep integer version for JSON storage
    
    # Check if alias is provided
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: /remove <alias>",
            parse_mode='Markdown'
        )
        return
    
    # Get alias (join all args as alias can contain spaces)
    alias_to_remove = " ".join(context.args).strip()
    
    removed_from_db = False
    removed_from_json = False
    
    try:
        # Remove from database
        async with aiosqlite.connect(DATABASE_FILE) as db:
            # Delete the wallet with matching user_id and alias
            cur = await db.execute(
                "DELETE FROM tracked_wallets WHERE user_id = ? AND alias = ?",
                (user_id, alias_to_remove)
            )
            await db.commit()
            
            if cur.rowcount > 0:
                removed_from_db = True
                logger.info(f"User {user_id} removed wallet with alias '{alias_to_remove}' from database")
        
        # Also remove from JSON storage (legacy support)
        if user_id_int in legacy.user_wallets:
            original_count = len(legacy.user_wallets[user_id_int])
            legacy.user_wallets[user_id_int] = [
                wallet for wallet in legacy.user_wallets[user_id_int] 
                if wallet.get('alias', '') != alias_to_remove
            ]
            
            if len(legacy.user_wallets[user_id_int]) < original_count:
                removed_from_json = True
                # Save updated JSON
                legacy.save_wallets()
                logger.info(f"User {user_id} removed wallet with alias '{alias_to_remove}' from JSON storage")
        
        # Check if wallet was found in either storage
        if removed_from_db or removed_from_json:
            await update.message.reply_text(
                f"✅ Removed '{alias_to_remove}' from your tracked wallets.",
                parse_mode='Markdown'
            )
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

