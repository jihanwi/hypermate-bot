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

    # Polling (spec 3.5). POLL_FAST_SEC is the floor; the poller raises it when snapshot
    # polling would need more than 40% of HL_WEIGHT_BUDGET.
    POLL_FAST_SEC: int = int(os.getenv("POLL_FAST_SEC", "20"))
    POLL_LEDGER_SEC: int = int(os.getenv("POLL_LEDGER_SEC", "180"))
    POLL_LEDGER_MAX_SEC: int = 600
    HL_WEIGHT_BUDGET: int = int(os.getenv("HL_WEIGHT_BUDGET", "1020"))   # 1200/min with a 15% margin
    DORMANT_AFTER_MS: int = 7 * 24 * 3600 * 1000     # no activity for 7 days -> polled every 3rd cycle
    ADMIN_USER_IDS: frozenset = frozenset(
        int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(",", " ").split() if x.strip().isdigit())

    # Daily DB backup (sqlite backup API), kept for BACKUP_KEEP_DAYS
    BACKUP_DIR: str = os.getenv("BACKUP_DIR", "")       # default: <DATABASE_PATH dir>/backups
    BACKUP_KEEP_DAYS: int = 7
    BACKUP_HOUR_KST: int = 4
    EVENTS_RETENTION_DAYS: int = 30                  # pruned after the daily backup
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
        'dust_notional_usd': 10,     # /positions folds smaller positions into one line
    }

    @classmethod
    def validate_config(cls) -> None:
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")
