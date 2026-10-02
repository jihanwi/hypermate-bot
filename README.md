# HyperMate Telegram Bot

A Telegram bot that tracks Hyperliquid wallets and sends alerts for their positions, spot trades and transfers.

The v2 upgrade plan is in [docs/HYPERMATE_V2_SPEC.md](docs/HYPERMATE_V2_SPEC.md). API facts verified during implementation are in [docs/API_NOTES.md](docs/API_NOTES.md).

## Commands

- `/start` - Short welcome
- `/help` - Commands and alert types
- `/add <wallet_address> <alias>` - Track a wallet
- `/list` - Your tracked wallets with account value
- `/remove <alias>` - Stop tracking a wallet
- `/positions [alias]` - Positions and balances for one wallet (main dex and HIP-3 dexs), or a one-line summary per wallet without an alias
- `/twap [alias]` - Active native TWAPs and detected bot (algo) executions
- `/recent <alias> [n]` - Last n events (default 10, max 30), including ones that were not sent and why
- `/stats <alias>` - PnL and volume
- `/rescan <alias>` - Look for Hyperliquid HIP-3 dex positions again

Aliases are matched case-insensitively. On startup the bot registers these commands as the Telegram `/` command menu for private chats.

### Alerts

- One alert per order: fills of the same order (coin, direction, oid) are summed (size, notional, VWAP, realized PnL from `closedPnl`). Further orders for the same coin and direction within 60 s edit that alert instead of sending a new one.
- Native TWAPs: one alert at start and one at the end; fills in the same direction while it runs are not alerted.
- Bot-driven executions (repeated small orders from an external bot, no native TWAP) are detected and shown as one "algo" alert that is updated every 10 minutes, plus an end alert after 10 idle minutes.
- HIP-3 dex coins are shown as `$MU (xyz)`. Collateral moves between the main account and a HIP-3 dex, spot/perp class transfers and vault deposits/withdrawals are recorded but not sent (off by default, spec 9.4).

## Running locally

Requires Python 3.10+.

```bash
pip install -r requirements.txt
export BOT_TOKEN=your_bot_token_here
export DATABASE_PATH=./hypermate.db   # default is /data/hypermate.db (Fly.io volume)
python -m hypermate.main
```

### Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `BOT_TOKEN` | yes | | Telegram bot token from @BotFather |
| `DATABASE_PATH` | no | `/data/hypermate.db` | SQLite file. The directory is created on startup if missing |
| `LOG_LEVEL` | no | `INFO` | Python log level |
| `HYPERLIQUID_API_URL` | no | `https://api.hyperliquid.xyz` | Hyperliquid API base URL |

`WALLET_ENCRYPTION_KEY` is no longer used (wallet generation was removed).

## Migrating v1 data (manual, one time)

v1 stored tracked wallets in a `tracked_wallets` table. v2 uses the schema in `hypermate/db/schema.sql`. The bot does not migrate on startup; run the script by hand:

```bash
# v1 DB copied somewhere, v2 DB at DATABASE_PATH
python scripts/migrate_v1.py --source /path/to/old/hypermate.db --target /data/hypermate.db

# v1 table in the same file as the v2 DB
python scripts/migrate_v1.py --target /data/hypermate.db
```

- `--target` defaults to `$DATABASE_PATH` (or `/data/hypermate.db`). `--source` defaults to the target.
- The script is idempotent: running it again reports rows as `already_present` and adds nothing.
- Rows v2 cannot represent are printed as `SKIP` lines and left out: invalid addresses, and the same user tracking one address under two aliases (v2 allows one alias per user and wallet).
- The v1 table and the legacy JSON files (`user_wallets.json`, `generated_wallets.json`, `wallets_secure.json`) are not modified. The bot does not read the JSON files; it logs a warning if it finds them.
- Migrated wallets start alerting from the time of migration. Past activity is not replayed.

The v1 DB lives in the old Railway service's container filesystem. Copy it out before that service is shut down if you want to migrate it. On Fly.io, upload it to the volume and run the script as the app user:

```bash
fly ssh sftp shell            # then: put hypermate_v1.db /data/hypermate_v1.db
fly ssh console -C "setpriv --reuid=app --regid=app --init-groups python /app/scripts/migrate_v1.py --source /data/hypermate_v1.db"
```

## Fly.io deployment

The bot runs as a single Fly Machine in `nrt` (Tokyo) with the SQLite DB on the `hypermate_data` volume mounted at `/data`. Config is in `fly.toml`, the image in `Dockerfile`.

```bash
fly launch --no-deploy --copy-config
fly volumes create hypermate_data --region nrt --size 1
fly secrets set BOT_TOKEN=...
fly deploy --ha=false
fly logs
```

- **Fly trial accounts stop the machine every 5 minutes** ("Trial machine stopping. To run for longer than 5m0s, add a credit card"). Add a payment method to the Fly organization to run the bot continuously.
- After `fly deploy` (or `fly secrets set`), check `fly status` that the machine is `started`; if it is `stopped`, run `fly machine start <machine-id>`.
- **Keep exactly one machine: `fly scale count 1`.** A volume attaches to one machine only, and SQLite cannot be shared between machines. Check with `fly status` after deploys and scale back to 1 if Fly created more.
- `fly.toml` has no `[http_service]` / `[[services]]`. The bot is a worker that opens no ports, so Fly keeps the machine running instead of auto-stopping it.
- `kill_signal = "SIGINT"`, `kill_timeout = 30`: python-telegram-bot stops polling and runs `post_shutdown` (closes the DB and HTTP session) on SIGINT.
- `DATABASE_PATH` and `LOG_LEVEL` are set in `fly.toml` `[env]`. `BOT_TOKEN` is a Fly secret.
- The container starts as root only long enough for `docker-entrypoint.sh` to `chown` the volume (Fly mounts it owned by root), then runs the bot as the unprivileged `app` user.

### DB backup

```bash
fly ssh console -C "sqlite3 /data/hypermate.db '.backup /data/backup.db'"
fly ssh sftp get /data/backup.db
```

`.backup` uses SQLite's online backup API, so it is safe while the bot is running. Do not copy `hypermate.db` directly (WAL mode keeps recent writes in `hypermate.db-wal`).

## Layout

```
hypermate/
  config.py               env and constants
  main.py                 Application, handlers, jobs, post_init
  db/schema.sql           SQLite schema (spec section 3.4)
  db/repo.py              all SQL
  core/formatter.py       Telegram HTML messages
  core/links.py           explorer links
  core/numbers.py         Decimal parsing
  core/pipeline.py        polling jobs and alert delivery
  venues/hyperliquid/     info API client and change detection
  bot/commands.py         command handlers
  bot/texts.py            command texts
scripts/migrate_v1.py     v1 to v2 data migration
Dockerfile                image (python:3.12-slim, non-root)
docker-entrypoint.sh      chown /data, then drop to the app user
fly.toml                  Fly.io app config
```
