# HyperMate Telegram Bot

A Telegram bot that tracks Hyperliquid wallets and sends alerts for their positions, spot trades and transfers.

The v2 upgrade plan is in [docs/HYPERMATE_V2_SPEC.md](docs/HYPERMATE_V2_SPEC.md). API facts verified during implementation are in [docs/API_NOTES.md](docs/API_NOTES.md).

## Commands

- `/start` - Short welcome
- `/help` - Commands and alert types
- `/add <wallet_address> <alias>` - Track a wallet
- `/list` - Your tracked wallets with account value
- `/remove <alias>` - Stop tracking a wallet
- `/positions [alias]` - Positions and balances for one wallet, or a one-line summary per wallet without an alias
- `/stats <alias>` - PnL and volume

Aliases are matched case-insensitively. On startup the bot registers `add`, `remove`, `list`, `positions`, `stats` and `help` as the Telegram `/` command menu for private chats.

## Running locally

Requires Python 3.10+.

```bash
pip install -r requirements.txt
export BOT_TOKEN=your_bot_token_here
export DATABASE_PATH=./hypermate.db   # default is /data/hypermate.db (Railway volume)
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

On Railway the v1 DB lives in the container filesystem, which is replaced on redeploy. Copy it out of the running v1 service before deploying v2 if you want to migrate it.

## Railway deployment

1. Attach a Volume to the service with mount path `/data` (service, Settings, Volumes). Without it the DB is lost on every redeploy.
2. Set `BOT_TOKEN`. `DATABASE_PATH` can stay at its default.
3. Deploy. `railway.toml` and `Procfile` start `python -m hypermate.main`.

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
```
