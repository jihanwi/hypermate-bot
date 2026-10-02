"""Message formatting."""

import logging

logger = logging.getLogger(__name__)

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

