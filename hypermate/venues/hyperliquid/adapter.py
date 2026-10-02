"""Hyperliquid position / transfer change detection."""

import logging
import time
from typing import Dict

from hypermate.core.formatter import format_spot_fill_message, format_transfer_message
from hypermate.venues.hyperliquid.client import get_spot_fills, get_spot_transfers, get_wallet_positions

logger = logging.getLogger(__name__)

# Track previously seen positions per wallet to detect new ones
# Structure: {wallet_address: {position_id: position_data}}
previous_positions: Dict[str, Dict[str, dict]] = {}

# Track if we've done the initial scan for each wallet (to avoid alerting on existing positions)
initial_scan_done: Dict[str, bool] = {}

# Track last seen transfer timestamp per wallet
# Structure: {wallet_address: last_timestamp}
last_transfer_timestamps: Dict[str, int] = {}

# Track if we've done the initial transfer scan for each wallet (to avoid alerting on existing transfers)
initial_transfer_scan_done: Dict[str, bool] = {}


async def check_new_transfers(wallet_address: str, alias: str) -> list:
    """Check for new transfers and spot fills, return formatted messages."""
    # Get both transfers and fills
    transfers = await get_spot_transfers(wallet_address, last_transfer_timestamps.get(wallet_address, 0))
    fills = await get_spot_fills(wallet_address, last_transfer_timestamps.get(wallet_address, 0))
    
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
    
    # Update stored positions
    previous_positions[wallet_address] = current_position_map
    
    return position_alerts
