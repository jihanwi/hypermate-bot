# API Notes

Facts about external APIs confirmed during implementation. Spec items marked [?] are resolved here.

Hyperliquid facts below were checked live against `https://api.hyperliquid.xyz/info` by the PM on 2026-10-03. The Claude Code dev environment could not reach the API (egress policy), so the responses were recorded outside it and committed as fixtures in `tests/fixtures/` with addresses anonymized.

## Hyperliquid

### spotMeta (spec 4.1 step 8, was [?])

- Request: `{"type": "spotMeta"}`
- Shape: `{universe: [{tokens: [base_idx, quote_idx], name, index, isCanonical}], tokens: [{name, index, tokenId, ...}]}`
- 331 pairs at recording time.
- Spot fills reference a pair by its `name` (`PURR/USDC`) or by `@{index}` (`@107`).
- `build_spot_names` (hypermate/venues/hyperliquid/client.py) maps `@107` to `HYPE` and `PURR/USDC` to `PURR`. Verified against the live response.
- Token lookup uses the token's `index` field, not its position in the `tokens` array.
- Fixture: `hl_spotMeta.json`

### userFillsByTime

- Request: `{"type": "userFillsByTime", "user": addr, "startTime": ms}`
- `side` values: `B` (buy) and `A` (sell). v1 checked for `s`, which never occurs, so sells showed as "traded". Fixed in Phase 0.
- `dir` values observed: `Open Long`, `Close Long`, `Open Short`, `Close Short`.
- Fixture: `hl_userFillsByTime.json` (348 fills)

### userNonFundingLedgerUpdates

- Request: `{"type": "userNonFundingLedgerUpdates", "user": addr, "startTime": ms}`
- In recently active wallets, every transfer arrives as `delta.type == "send"`, with fields: `type, user, destination, sourceDex, destinationDex, token, amount, usdcValue, fee, nativeTokenFee, nonce, feeToken`.
- `internalTransfer` and `spotTransfer` were not observed. They are probably legacy. Their handlers are kept for older history.
- v1 and Phase 0 before the fix did not handle `send`, so no transfer alerts went out (B11). Phase 0 now treats `send` like `spotTransfer`: `user` == tracked wallet means sent, `destination` == tracked wallet means received.
- `vaultWithdraw` uses `netWithdrawnUsd`, as documented.
- `vaultDeposit` was not observed. The formatter reads `usdc` and falls back to `usd` (the field v1 read).
- Fixture: `hl_userNonFundingLedgerUpdates.json`

### Phase 1 fixtures (recorded, not used in Phase 0)

- `webData2`: `twapStates` (active TWAPs). Fixture: `hl_webData2.json`
- `twapHistory`: statuses observed: `activated`, `finished`, `terminated`, `error`. Fixture: `hl_twapHistory.json`
- `userTwapSliceFillsByTime`. Fixture: `hl_userTwapSliceFillsByTime.json`

## Telegram

- `setMyCommands` is called once in `post_init` with `BotCommandScopeAllPrivateChats`. The request shape was checked against a local fake Bot API server. Whether the menu shows up in the Telegram client needs a check with the real bot token.
