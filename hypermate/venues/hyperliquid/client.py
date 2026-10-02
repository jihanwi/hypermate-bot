"""Hyperliquid info API calls."""

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

async def get_spot_transfers(wallet_address: str, start_time: int) -> list:
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
                        "startTime": start_time
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

async def get_spot_fills(wallet_address: str, start_time: int) -> list:
    """Query Hyperliquid API for recent spot fills to detect buy/sell activities."""
    try:
        url = "https://api.hyperliquid.xyz/info"
        payload = {
            "type": "userFills",
            "user": wallet_address,
            "startTime": start_time
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

