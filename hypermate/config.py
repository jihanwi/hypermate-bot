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
    # Alert hygiene (deploy review 2026-10-05)
    MIN_NOTIONAL_USD: int = int(os.getenv("MIN_NOTIONAL_USD", "1000"))   # spec 9.4 min_notional, global for now
    ALGO_STALE_SEC: int = 2 * 3600       # on /add, algos idle longer than this end silently
    ALGO_REARM_SEC: int = 600            # after an ALGO_END, fills on that coin wait for re-detection this long
    ADMIN_USER_IDS: frozenset = frozenset(
        int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(",", " ").split() if x.strip().isdigit())

    # Lighter (spec 6.2): public REST 60 req/min per IP and L1 address; the bucket uses 50
    LIGHTER_API_URL: str = os.getenv("LIGHTER_API_URL", "https://mainnet.zklighter.elliot.ai/api/v1")
    LIGHTER_WS_URL: str = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
    LIGHTER_REQ_BUDGET: int = int(os.getenv("LIGHTER_REQ_BUDGET", "50"))
    LIGHTER_WS_ENABLED: bool = os.getenv("LIGHTER_WS_ENABLED", "true").lower() == "true"
    LIGHTER_POLL_MIN_SEC: int = 40                   # REST polling floor; raised with the account count
    # RISEx (spec 6.3): public REST 500 req / 10 s / IP, WS 10 req/s
    RISEX_API_URL: str = os.getenv("RISEX_API_URL", "https://api.rise.trade")
    RISEX_WS_URL: str = os.getenv("RISEX_WS_URL", "wss://api.rise.trade/ws/")
    RISEX_REQ_BUDGET: int = int(os.getenv("RISEX_REQ_BUDGET", "2400"))   # per minute, 80% of 3000
    RISEX_WS_ENABLED: bool = os.getenv("RISEX_WS_ENABLED", "true").lower() == "true"
    VENUE_RESCAN_HOUR_KST: int = 4                   # daily rescan of inactive venues at 04:10 KST
    VENUE_RESCAN_MINUTE: int = 10

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
        # multi-algo summary mode (spec 5.2 멀티 알고 요약)
        'multi_algo_min': 5,             # active algos on one account -> summary mode
        'multi_algo_update_sec': 3600,   # summary message edit interval
        'multi_algo_exit': 2,            # leave when active algos stay at or under this...
        'multi_algo_exit_idle_sec': 1800,   # ...for this long
        'multi_algo_big_order_pct': 10,  # alerted in summary mode: one order >= 10% of the position
        'multi_algo_big_order_usd': 100_000,   # or >= $100k
    }

    @classmethod
    def validate_config(cls) -> None:
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")
