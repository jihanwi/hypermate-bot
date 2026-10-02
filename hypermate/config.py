"""
Configuration module for HyperMate bot
"""

import os

class Config:
    """Configuration class for bot settings"""
    
    # Bot Configuration
    BOT_TOKEN: str = os.getenv("BOT_TOKEN")
    
    # Hyperliquid API Configuration (for future use)
    HYPERLIQUID_API_URL: str = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")
    HYPERLIQUID_TESTNET_URL: str = os.getenv("HYPERLIQUID_TESTNET_URL", "https://api.hyperliquid-testnet.xyz")
    
    # Logging Configuration
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    # Development settings
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"
    
    @classmethod
    def load_env(cls) -> None:
        """Load environment variables from .env file if available"""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass  # dotenv not available, skip loading 
            
    @classmethod
    def validate_config(cls) -> None:
        """Validate that required configuration is present"""
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")
