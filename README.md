# HyperMate Telegram Bot

A Telegram bot that tracks Hyperliquid wallets and sends alerts for their positions, spot trades and transfers.

The v2 upgrade plan is in [docs/HYPERMATE_V2_SPEC.md](docs/HYPERMATE_V2_SPEC.md). API facts verified during implementation are in [docs/API_NOTES.md](docs/API_NOTES.md).

## Commands

- `/start` - Short welcome
- `/help` - Commands and alert types
- `/add <wallet_address> <alias>` - Track a wallet. `/add risex:<address> <alias>` pins the address to that venue only (hl, lighter, risex, aster): no Hyperliquid row, no dex scan, used when the automatic mapping does not find the address there
- `/list` - Your tracked wallets with account value (summed over venues) and the badges of the venues each is active on
- `/remove <alias>` - Stop tracking a wallet
- `/positions [alias]` - Positions and balances for one wallet (main dex and HIP-3 dexs), or a one-line summary per wallet without an alias
- `/twap [alias]` - Active native TWAPs and detected bot (algo) executions
- `/recent <alias> [n]` - Last n events (default 10, max 30), including ones that were not sent and why
- `/stats <alias>` - PnL and volume
- `/rescan <alias>` - Look for Hyperliquid HIP-3 dex positions again
- `/related <alias> [refresh]` - Linked wallets: subaccounts and master, API wallets, staking link, vault leader, transfer counterparties (likely or weak by the spec 7.2 rules), referrals, followed vaults. Each row has the address with a hypurrscan link, the evidence, the account value and a `Track as <alias>-N` button that adds it like `/add`. Results are cached 24 h; `refresh` forces a new discovery (about 300 HL weight, lowest priority)
- `/settings <alias>` - One message with toggle buttons, per wallet and per user: the venues the wallet is active on, alert types (positions, liquidations, TWAP / algo, spot, transfers, deposits / withdrawals, vaults, perp <-> spot moves) and the minimum order size (Auto = `max($100, account value x 0.5%)`, $100 / $1k / $10k / $100k, or Off; full closes and liquidations always come through). `/settings default` edits your defaults for every wallet; a wallet's own settings override them. Polling and event recording stay per wallet, the filter runs per subscriber at send time (skipped users are logged only)
- `/mute <alias> [1h|6h|1d|7d]` - No alerts for that wallet (no duration = until `/unmute`). `/list` shows 🔇 with the time left. `/mute` alone asks before muting every wallet. `/unmute <alias>` resumes and says how many events were recorded meanwhile (see them with `/recent`, nothing is re-sent)
- `/rename <old> <new>` - 1 to 32 characters, no spaces, unique per user (case-insensitive). Aliases `/related` derived from the old one (`<alias>-N`) are not renamed along
- An unknown alias gets the closest of your aliases as `Did you mean …?` with a button that re-runs the command. Commands without arguments show their usage. Internal errors reply `Something went wrong (id: …)` with the same id in the log
- `/health` - Admins only (`ADMIN_USER_IDS`): last poll time, active accounts, HL weight use and 429s over the last hour, active TWAPs and algos, DB and WAL size, event row counts (total and last 24 h by type), uptime, the startup maintenance outcome (`Maintenance: ok 3m ago (attempt 1, pruned 0, slimmed 0/0, cleaned 0, checkpoint 12 pages)` or `failed … error: …`), and the `algo_active` rows (coin, side, fills, time since the last fill). Not in the `/` menu

Aliases are matched case-insensitively. On startup the bot registers these commands as the Telegram `/` command menu for private chats.

### Venues

`/add` resolves the wallet on every venue in parallel and tracks the ones with activity: Hyperliquid (positions, account value or spot balance), Lighter (sub-accounts of the L1 address), RISEx (open positions or any trade history) and Aster (positions or a wallet balance; the reply shows the privacy state). The reply says which: `✅ Wallet added as cl · HL ✅ · dex: xyz · Lighter ✅ (2 sub-accounts)` (`dex:` lists HIP-3 dexs with activity). Re-adding a wallet ends silently any algo of it that has been idle for 2 hours; after an algo ends, fills on that coin wait 10 minutes for re-detection and go out as one summed alert if none happens. `/rescan` repeats it, and a daily job at 04:10 KST re-checks the venues a wallet is not active on. Alerts carry a venue badge (`[HL]`, `[LTR]`, `[RISE]`, `[ASTER]`) and Lighter sub-accounts show as `alias#index`. Aster is polled every 30 s over JSON-RPC; when a wallet turns on Aster privacy the bot says so once and stops tracking it there until the daily rescan sees it open again. RISEx uses one WebSocket for all tracked wallets (positions and trades channels) with REST as the fallback. `/positions` has one section per venue account and `/list` sums the account values over venues. Lighter polls use the WebSocket stream when it connects and REST otherwise (interval raised with the account count so the public 60 req/min limit holds); `/health` shows the mode.

### Alerts

- One alert per order: fills of the same order (coin, direction, oid) are summed (size, notional, VWAP, realized PnL from `closedPnl`). Further orders for the same coin and direction within 60 s edit that alert instead of sending a new one.
- Native TWAPs: one alert at start and one at the end; fills in the same direction while it runs are not alerted. A TWAP that was already running when the wallet was added is reported as "TWAP in progress (started 2h ago)".
- Bot-driven executions (repeated small orders from an external bot, no native TWAP) are detected after a few cycles. The fill alerts sent before detection stay; then one "algo" alert is sent and updated every 10 minutes, and an end alert follows after 10 idle minutes. The suppressed fills are not stored; `/recent` shows one "🤖 algo $DOGE · 18,423 fills $1.52M (active)" line per running algo instead.
- When one wallet runs 5 or more algos at once (a custom TWAP bot across many coins) the bot switches to one summary message per wallet ("running TWAP-style algos on 17 coins", grouped by direction, edited hourly) instead of per-coin algo alerts. Position opens, closes, flips, liquidations and single orders of $100k or 10% of the position are still alerted. It ends with "algos wound down · 24h total …" after 30 minutes with 2 or fewer algos left.
- Transfer counterparties seen in alerts are stored as weak links for `/related` in the background, without alerts.
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
| `ADMIN_USER_IDS` | no | | Telegram user ids allowed to run `/health`, comma or space separated |
| `MIN_NOTIONAL_FLOOR_USD` | no | `100` | Alert threshold floor. The threshold per account is `max(floor, account value x MIN_NOTIONAL_PCT)`; position and spot alerts under it are recorded but not sent (full closes and liquidations always go out) |
| `MIN_NOTIONAL_PCT` | no | `0.005` | Share of the account value (from the last snapshot) used for the threshold: a $483k account filters under $2,415, a $79k one under $397, a $5k one at the floor |
| `HL_WEIGHT_BUDGET` | no | `1020` | Hyperliquid info API weight per minute the bot allows itself (HL limit 1200) |
| `POLL_FAST_SEC` | no | `20` | Floor for the position poll interval. Raised automatically when polling would need over 40% of the budget |
| `POLL_LEDGER_SEC` | no | `180` | Ledger (deposits, withdrawals, transfers) poll interval, stretched up to 600 s when the budget is short |
| `BACKUP_DIR` | no | `<DATABASE_PATH dir>/backups` | Where the daily DB backup goes |
| `LIGHTER_API_URL` | no | `https://mainnet.zklighter.elliot.ai/api/v1` | Lighter public REST base |
| `LIGHTER_WS_URL` | no | `wss://mainnet.zklighter.elliot.ai/stream` | Lighter WebSocket stream |
| `LIGHTER_WS_ENABLED` | no | `true` | Try the Lighter WS; REST polling is the fallback either way |
| `LIGHTER_REQ_BUDGET` | no | `50` | Lighter requests per minute the bot allows itself (public limit 60) |
| `RISEX_API_URL` | no | `https://api.rise.trade` | RISEx public REST base |
| `RISEX_WS_URL` | no | `wss://api.rise.trade/ws/` | RISEx WebSocket |
| `RISEX_WS_ENABLED` | no | `true` | Use the RISEx WS (positions and trades channels); REST is the fallback |
| `RISEX_REQ_BUDGET` | no | `2400` | RISEx requests per minute (public limit 500 per 10 s) |
| `ASTER_RPC_URL` | no | `https://tapi.asterdex.com/info` | Aster Chain JSON-RPC endpoint |
| `ASTER_REQ_BUDGET` | no | `300` | Aster requests per minute to start with; halved on every 429 |

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

Pushes to `main` deploy automatically: `.github/workflows/fly-deploy.yml` runs `flyctl deploy --remote-only --depot=false --ha=false` with the `FLY_API_TOKEN` repository secret (`fly tokens create deploy -x 999999h`, then Settings > Secrets and variables > Actions). `--depot=false` because the Depot builder stalled for 10+ minutes twice. Without the secret the deploy steps are skipped and the job stays green.

- **Fly trial accounts stop the machine every 5 minutes** ("Trial machine stopping. To run for longer than 5m0s, add a credit card"). Add a payment method to the Fly organization to run the bot continuously.
- After `fly deploy` (or `fly secrets set`), check `fly status` that the machine is `started`; if it is `stopped`, run `fly machine start <machine-id>`.
- **Keep exactly one machine: `fly scale count 1`.** A volume attaches to one machine only, and SQLite cannot be shared between machines. Check with `fly status` after deploys and scale back to 1 if Fly created more.
- `fly.toml` has no `[http_service]` / `[[services]]`. The bot is a worker that opens no ports, so Fly keeps the machine running instead of auto-stopping it.
- `kill_signal = "SIGINT"`, `kill_timeout = 30`: python-telegram-bot stops polling and runs `post_shutdown` (closes the DB and HTTP session) on SIGINT.
- `DATABASE_PATH` and `LOG_LEVEL` are set in `fly.toml` `[env]`. `BOT_TOKEN` is a Fly secret.
- The container starts as root only long enough for `docker-entrypoint.sh` to `chown` the volume (Fly mounts it owned by root), then runs the bot as the unprivileged `app` user.

### Polling and API budget

Every `POLL_FAST_SEC` the bot reads each wallet's positions (`clearinghouseState`, main dex plus HIP-3 dexs) and spot balances (`spotClearinghouseState`), 2 weight each. Fills are fetched only when a position size or a spot balance changed, or while a native TWAP or an algo is being tracked. Wallets with no activity for 7 days are polled every third cycle until they move again. All requests go through one token bucket of `HL_WEIGHT_BUDGET` per minute with priority positions > TWAP > fills > ledger; a 429 pauses every request for `Retry-After` seconds (30 s without the header). One INFO log line per minute reports the weight used and the 429 count.

### DB backup

The bot writes a backup every day at 04:00 KST to `<BACKUP_DIR>/hypermate-YYYYMMDD.db` with SQLite's online backup API and keeps the last 7 days. Right after a successful backup it deletes events older than 30 days (`EVENTS_RETENTION_DAYS`) with their `sent_messages` rows and runs `PRAGMA wal_checkpoint(TRUNCATE)`. Event payloads hold aggregates only (fill count, size, notional, VWAP, first and last tid), never the raw fills; an older database is slimmed once on startup. That startup maintenance (prune, slimming, cleanup of old suppressed rows, VACUUM when free pages exceed 30% of the file or 10 MB, WAL checkpoint) runs in `post_init` before the polling jobs start, so VACUUM never competes with the poller; it is a no-op on a migrated database. The connection has a 30 s `busy_timeout`, every read closes its cursor so no statement stays open, the checkpoint retries briefly when a reader is active, and the whole run is retried once after 5 s. One `Maintenance: …` INFO line is logged on every boot and `/health` shows the same (`Maintenance: db 200 MB, freelist 190 MB (95%), vacuum done (189 MB freed), checkpoint 3 pages, … · ok (attempt 1)`). For a manual copy:

```bash
fly ssh console -C "sqlite3 /data/hypermate.db '.backup /data/backup.db'"
fly ssh sftp get /data/backup.db
```

If the machine is killed with OOM (the free tier has 256 MB; `fly logs` shows `Out of memory` with the RSS), raise it with `fly scale memory 512`. The one-time payload slimming after the retention change runs in the background in 1,000-row batches and should not need it.

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
