"""User-facing command texts (Telegram HTML)."""

WELCOME = (
    "🚀 <b>Welcome to HyperMate!</b>\n\n"
    "Track Hyperliquid wallets and get alerts for their positions, spot trades and transfers.\n\n"
    "Start with <code>/add 0x... alias</code>. Commands: /add /list /remove /positions /stats"
)

ADD_USAGE = "Usage: /add &lt;address&gt; &lt;alias&gt;"
REMOVE_USAGE = "Usage: /remove &lt;alias&gt;"
POSITIONS_USAGE = "Usage: /positions &lt;alias&gt;"
STATS_USAGE = "Usage: /stats &lt;alias&gt;"

ALIAS_EXISTS = "You're already tracking a wallet with this alias."
ADDRESS_EXISTS = "You've already added this address."
WALLET_ADDED = "✅ Wallet added as <b>{alias}</b>"
WALLET_REMOVED = "✅ Removed <b>{alias}</b> from your tracked wallets."
ALIAS_NOT_FOUND = "Alias <b>{alias}</b> not found. Use /list to see your tracked wallets."
NO_WALLETS = "You're not tracking any wallets yet. Use /add to start."
LIST_HEADER = "Here are your tracked wallets:"

HL_API_ERROR = "Hyperliquid API error. Try the command again in a moment."
INTERNAL_ERROR = "Something went wrong (id: {error_id})"
STATS_NOT_AVAILABLE = "Stats not available for this wallet."
