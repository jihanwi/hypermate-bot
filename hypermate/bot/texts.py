"""User-facing command texts (Telegram HTML)."""

# Command menu (spec 9.1). Only commands that work today are registered;
# related, settings, mute, unmute, rename come in later phases.
MENU_COMMANDS = [
    ("add", "Track a wallet: /add 0x... alias"),
    ("remove", "Stop tracking: /remove alias"),
    ("list", "Your tracked wallets with account value"),
    ("positions", "Open positions: /positions alias (no alias = all)"),
    ("twap", "Active TWAPs: /twap [alias]"),
    ("recent", "Recent events: /recent alias [n]"),
    ("stats", "PnL and volume: /stats alias"),
    ("rescan", "Re-detect venues for a wallet: /rescan alias"),
    ("related", "Find linked wallets: /related alias"),
    ("help", "Commands and examples"),
]
# not in the menu: /health (admins only)

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
    "/stats alias: all-time PnL and volume\n"
    "/twap [alias]: active TWAPs and algo executions\n"
    "/recent alias [n]: last events, including ones not sent\n"
    "/rescan alias: look for Hyperliquid HIP-3 dex positions again\n"
    "/related alias [refresh]: subaccounts, API wallets, transfer counterparties and more, "
    "with a Track button per wallet\n\n"
    "Aliases are case-insensitive.\n\n"
    "<b>Alerts</b> for every tracked wallet:\n"
    "• Perp positions opened, added to, reduced, closed, flipped or liquidated, incl. HIP-3 dexs. "
    "One alert per order; quick follow-ups edit that alert\n"
    "• Spot buys and sells\n"
    "• Deposits, withdrawals and transfers\n"
    "• TWAPs and bot-driven (algo) executions: a start alert, kept up to date, and an end alert"
)

EXAMPLE_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
ADD_USAGE = (f"Usage: /add 0x... alias\nExample: <code>/add {EXAMPLE_ADDRESS} whale1</code>\n"
             f"One venue only: <code>/add risex:{EXAMPLE_ADDRESS} whale1</code> (hl, lighter, risex, aster)")
REMOVE_USAGE = "Usage: /remove alias\nExample: <code>/remove whale1</code>"
STATS_USAGE = "Usage: /stats alias\nExample: <code>/stats whale1</code>"
ADMIN_ONLY = "This command is for admins (ADMIN_USER_IDS)."
RELATED_USAGE = "Usage: /related alias [refresh]\nExample: <code>/related whale1</code>"
RELATED_SEARCHING = "🔎 Searching related wallets for <b>{alias}</b>... (10 to 20 seconds)"
RELATED_NONE = "🔗 No related wallets found for <b>{alias}</b>."
TRACK_BUTTON = "Track as {alias}"
TRACK_DONE = "✅ Tracking <b>{alias}</b> ({address})"
TRACK_EXPIRED = "This list is stale. Run /related again."
RECENT_USAGE = "Usage: /recent alias [n]\nExample: <code>/recent whale1 20</code>"
RESCAN_USAGE = "Usage: /rescan alias\nExample: <code>/rescan whale1</code>"
RESCAN_RESULT = "🔎 <b>{alias}</b>: Hyperliquid main dex{dexs}"
WALLET_VENUES = " · {venues}"
RESCAN_VENUES = "🔎 <b>{alias}</b>: {venues}"

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

# /settings (spec 9.4)
SETTINGS_USAGE = "Usage: /settings alias (or /settings default)\nExample: <code>/settings whale1</code>"
SETTINGS_HEADER = ("⚙️ <b>Settings for {alias}</b>\nTap to toggle. Venues, alert types and the minimum order size "
                   "(Auto = $100 or 0.5% of the account value, whichever is higher). Full closes and liquidations "
                   "always come through.")
SETTINGS_DEFAULT_HEADER = ("⚙️ <b>Default settings</b> for wallets you add\nA wallet's own /settings override these. "
                           "Auto = $100 or 0.5% of the account value, whichever is higher.")
SETTINGS_STALE = "This keyboard is stale. Run /settings again."
NOT_YOURS = "Not your settings."
