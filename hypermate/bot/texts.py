"""User-facing command texts (Telegram HTML)."""

# Command menu (spec 9.1 table). Not in the menu: /health (admins only).
MENU_COMMANDS = [
    ("add", "Track a wallet: /add 0x... alias"),
    ("remove", "Stop tracking: /remove alias"),
    ("list", "Your tracked wallets with account value"),
    ("positions", "Open positions: /positions alias (no alias = all)"),
    ("twap", "Active TWAPs: /twap [alias]"),
    ("recent", "Recent events: /recent alias [n]"),
    ("related", "Find linked wallets: /related alias"),
    ("stats", "PnL and volume: /stats alias"),
    ("settings", "Notification settings: /settings alias"),
    ("mute", "Mute alerts: /mute alias [1h/1d]"),
    ("unmute", "Unmute alerts: /unmute alias"),
    ("rename", "Rename alias: /rename old new"),
    ("rescan", "Re-detect venues for a wallet: /rescan alias"),
    ("help", "Commands and examples"),
]

WELCOME = (
    "🚀 <b>Welcome to HyperMate!</b>\n\n"
    "Track Hyperliquid wallets and get alerts when they trade or move funds.\n"
    "Start with /add, or see /help for all commands."
)

HELP = (
    "<b>Wallets</b>: /add 0x... alias · /remove alias · /rename old new · /list · /rescan alias\n"
    "<b>Info</b>: /positions [alias] · /stats alias · /twap [alias] · /recent alias [n] · /related alias\n"
    "<b>Alerts</b>: /settings alias (venues, alert types, minimum size; /settings default for new wallets) · "
    "/mute alias [1h|6h|1d|7d] · /unmute alias\n\n"
    "Aliases are case-insensitive. A command without arguments shows its usage.\n\n"
    "<b>What you get</b>, per tracked wallet on Hyperliquid, Lighter, RISEx and Aster:\n"
    "• Perp positions opened, added to, reduced, closed, flipped or liquidated (incl. HIP-3 dexs). "
    "One alert per order; quick follow-ups edit that alert. Orders under your minimum size are recorded, "
    "not sent (full closes and liquidations always are)\n"
    "• Spot buys and sells\n"
    "• Deposits, withdrawals and transfers\n"
    "• TWAPs and bot-driven (algo) executions: start and end only, the start alert kept up to date\n"
    "• Many algos at once: one summary message instead of a flood"
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
DID_YOU_MEAN = "Alias <b>{alias}</b> not found. Did you mean <code>{suggestion}</code>?"
DYM_BUTTON = "/{command} {alias}"
NO_WALLETS = "You're not tracking any wallets yet. Use /add to start."
LIST_HEADER = "Here are your tracked wallets:"
POSITIONS_SUMMARY_HEADER = "📊 <b>Positions summary</b>"

HL_API_ERROR = "Hyperliquid API error: the venue did not answer."
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

# /rename (spec 9.1)
RENAME_USAGE = "Usage: /rename old new\nExample: <code>/rename whale1 jez</code>"
RENAME_INVALID = "An alias is 1 to 32 characters without spaces."
RENAME_EXISTS = "You already have a wallet named <b>{alias}</b>."
RENAMED = "✅ Renamed <b>{old}</b> → <b>{new}</b>."

# /mute, /unmute (spec 9.5)
MUTE_USAGE = "Usage: /mute alias [1h|6h|1d|7d] (no duration = until /unmute)\nExample: <code>/mute whale1 1d</code>"
UNMUTE_USAGE = "Usage: /unmute alias\nExample: <code>/unmute whale1</code>"
MUTED = "🔇 <b>{alias}</b> muted {until}."
MUTED_UNTIL_FOREVER = "until /unmute"
MUTED_UNTIL_FOR = "for {duration}"
MUTE_BAD_DURATION = "Duration must be one of 1h, 6h, 1d, 7d."
MUTE_ALL_CONFIRM = "Mute alerts for all your wallets until /unmute?"
MUTE_ALL_YES = "Mute all"
MUTE_ALL_NO = "Cancel"
MUTE_ALL_DONE = "🔇 Muted {count} wallets until /unmute."
MUTE_ALL_CANCELLED = "Not muted."
UNMUTED = "🔔 <b>{alias}</b> unmuted. {count} events while muted, see /recent {alias}."
NOT_MUTED = "<b>{alias}</b> is not muted."
NOT_YOURS = "Not your settings."
