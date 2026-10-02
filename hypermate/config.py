"""Configuration: environment variables and constants."""

import os


def _load_dotenv() -> None:
    """Load a local .env if python-dotenv is installed (optional, for local development)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


# Must run before the Config class body reads os.environ
_load_dotenv()


class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN")

    # SQLite file. On Railway a Volume is mounted at /data so it survives redeploys.
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "/data/hypermate.db")

    HYPERLIQUID_API_URL: str = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"

    # Polling (v1 values; Phase 1 replaces these with the weight-budget scheduler)
    POSITIONS_POLL_SEC: int = 30
    TRANSFERS_POLL_SEC: int = 30
    SPOT_META_TTL_SEC: int = 3600

    @classmethod
    def validate_config(cls) -> None:
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")
