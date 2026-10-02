"""User-facing command texts (Telegram HTML)."""

# Command menu (spec 9.1). Phase 0 registers only the commands that work today;
# twap, recent, related, settings, mute, unmute, rename, rescan come in later phases.
MENU_COMMANDS = [
    ("add", "Track a wallet: /add 0x... alias"),
    ("remove", "Stop tracking: /remove alias"),
    ("list", "Your tracked wallets with account value"),
    ("positions", "Open positions: /positions alias (no alias = all)"),
    ("stats", "PnL and volume: /stats alias"),
    ("help", "Commands and examples"),
]

WELCOME = (
    "🚀 <b>Welcome to HyperMate!</b>\n\n"
    "Track Hyperliquid wallets and get alerts when they trade or move funds.\n"
    "Start with /add, or see /help for all commands."
)

HELP = (
    "<b>Wallets</b>\n"
    "/add 0x... alias: track a wallet\n"
    "/remove alias: stop tracking\n"
    "/list: tracked wallets with account value\n\n"
    "<b>Info</b>\n"
    "/positions alias: open positions and balances (no alias: one line per wallet)\n"
    "/stats alias: all-time PnL and volume\n\n"
    "Aliases are case-insensitive.\n\n"
    "<b>Alerts</b> for every tracked wallet:\n"
    "• Perp positions opened, increased, reduced, closed or liquidated\n"
    "• Spot buys and sells\n"
    "• Deposits, withdrawals, transfers and vault deposits/withdrawals\n"
    "• TWAP orders: one alert when a TWAP starts and one when it ends. "
    "Position changes from a running TWAP are not alerted."
)

EXAMPLE_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
ADD_USAGE = f"Usage: /add 0x... alias\nExample: <code>/add {EXAMPLE_ADDRESS} whale1</code>"
REMOVE_USAGE = "Usage: /remove alias\nExample: <code>/remove whale1</code>"
STATS_USAGE = "Usage: /stats alias\nExample: <code>/stats whale1</code>"

ALIAS_EXISTS = "You're already tracking a wallet with this alias."
ADDRESS_EXISTS = "You've already added this address."
WALLET_ADDED = "✅ Wallet added as <b>{alias}</b>"
WALLET_REMOVED = "✅ Removed <b>{alias}</b> from your tracked wallets."
ALIAS_NOT_FOUND = "Alias <b>{alias}</b> not found. Use /list to see your tracked wallets."
NO_WALLETS = "You're not tracking any wallets yet. Use /add to start."
LIST_HEADER = "Here are your tracked wallets:"
POSITIONS_SUMMARY_HEADER = "📊 <b>Positions summary</b>"

HL_API_ERROR = "Hyperliquid API error. Try the command again in a moment."
INTERNAL_ERROR = "Something went wrong (id: {error_id})"
STATS_NOT_AVAILABLE = "Stats not available for this wallet."
