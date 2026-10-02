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

    # SQLite file. On Fly.io the hypermate_data volume is mounted at /data so it survives redeploys.
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "/data/hypermate.db")

    HYPERLIQUID_API_URL: str = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"

    # Polling (v1 values; Phase 1 replaces these with the weight-budget scheduler)
    POSITIONS_POLL_SEC: int = 30
    TRANSFERS_POLL_SEC: int = 30
    SPOT_META_TTL_SEC: int = 3600
    PERP_DEXS_TTL_SEC: int = 3600

    # Spec 9.4 defaults. Detection and debounce run per account, so Phase 1 uses
    # these values for everyone; per-subscription overrides come with /settings.
    DEFAULT_SETTINGS: dict = {
        'debounce_sec': 60,
        'algo_window_sec': 300,
        'algo_min_fills': 8,
        'algo_max_slice_pct': 2,
        'algo_progress_sec': 600,
        'algo_idle_sec': 600,
    }

    @classmethod
    def validate_config(cls) -> None:
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")
