# API Notes

Facts about external APIs confirmed during implementation. Spec items marked [?] are resolved here.

Hyperliquid facts below were checked live against `https://api.hyperliquid.xyz/info` by the PM on 2026-10-03. The Claude Code dev environment could not reach the API (egress policy), so the responses were recorded outside it and committed as fixtures in `tests/fixtures/` with addresses anonymized.

## Hyperliquid

### spotMeta (spec 4.1 step 8, was [?])

- Request: `{"type": "spotMeta"}`
- Shape: `{universe: [{tokens: [base_idx, quote_idx], name, index, isCanonical}], tokens: [{name, index, tokenId, ...}]}`
- The PM's live check counted 331 pairs. The committed fixture has 330 universe entries and 503 tokens; the count changes as pairs are listed.
- Only `PURR/USDC` has `isCanonical: true`; every other pair is addressed as `@{index}`.
- Quotes are not always USDC (fixture: 313 USDC, 11 USDH, 5 USDT0, 1 USDE). Non-USDC pairs display as `BASE/QUOTE`, e.g. `@207` is `HYPE/USDT0`.
- Spot fills reference a pair by its `name` (`PURR/USDC`) or by `@{index}` (`@107`).
- `build_spot_names` (hypermate/venues/hyperliquid/client.py) maps `@107` to `HYPE` and `PURR/USDC` to `PURR`. Verified against the live response.
- Token lookup uses the token's `index` field, not its position in the `tokens` array.
- Fixture: `hl_spotMeta.json`

### userFillsByTime

- Request: `{"type": "userFillsByTime", "user": addr, "startTime": ms}`
- `side` values: `B` (buy) and `A` (sell). v1 checked for `s`, which never occurs, so sells showed as "traded". Fixed in Phase 0.
- `dir` values observed: `Open Long`, `Close Long`, `Open Short`, `Close Short`.
- `twapId` is null on all 348 fills, matching spec 5.1 (TWAP slices are not in userFills).
- Perp coins include HIP-3 dex coins written `dex:COIN` (`xyz:GOLD`, `para:ANSEM`). `is_spot_coin` treats them as perp, which is correct.
- Fixture: `hl_userFillsByTime.json` (348 fills)

### userNonFundingLedgerUpdates

- Request: `{"type": "userNonFundingLedgerUpdates", "user": addr, "startTime": ms}`
- In recently active wallets, every transfer arrives as `delta.type == "send"`, with fields: `type, user, destination, sourceDex, destinationDex, token, amount, usdcValue, fee, nativeTokenFee, nonce, feeToken`.
- `internalTransfer` and `spotTransfer` were not observed. They are probably legacy. Their handlers are kept for older history.
- v1 and Phase 0 before the fix did not handle `send`, so no transfer alerts went out (B11). Phase 0 now treats `send` like `spotTransfer`: `user` == tracked wallet means sent, `destination` == tracked wallet means received.
- `vaultWithdraw` uses `netWithdrawnUsd`, as documented.
- `vaultDeposit` was not observed. The formatter reads `usdc` and falls back to `usd` (the field v1 read).
- Fixture: `hl_userNonFundingLedgerUpdates.json` (10 entries, all `send`; `sourceDex` is `spot`, `destinationDex` is `spot` or empty)

### clearinghouseState

- Top-level keys: `marginSummary, crossMarginSummary, crossMaintenanceMarginUsed, withdrawable, assetPositions, time`. No `twapOrders` (B2 confirmed).
- `liquidationPx` can be `null`. `leverage.value` and `maxLeverage` are JSON integers; the client parses JSON floats as Decimal.
- `marginSummary.accountValue` is stored as Decimal text in `snapshots.account_value`.
- Fixture: `hl_clearinghouseState.json`

### Phase 1 fixtures (recorded, sanity-tested only in Phase 0)

- `webData2`: `twapStates` is `[[twapId, {coin, user, side, sz, executedSz, executedNtl, minutes, reduceOnly, randomize, timestamp, trigger, stopPx}]]`. Also carries `clearinghouseState`, `openOrders`, `agentAddress`, `spotState` and more. Fixture: `hl_webData2.json`
- `twapHistory`: statuses observed: `activated`, `finished`, `terminated`, `error` (`error` carries `status.description`, e.g. "Insufficient margin to place order."). `time` is in seconds, `state.timestamp` in ms. TWAPs on HIP-3 coins (`xyz:MSTR`) appear too. Fixture: `hl_twapHistory.json`
- `userTwapSliceFillsByTime`: `[{fill: {...same fields as userFills...}, twapId}]`. Every slice twapId in the fixture is in webData2 `twapStates`. Fixture: `hl_userTwapSliceFillsByTime.json`

### TWAP tracking (Phase 1 PR A)

- Active TWAPs: `webData2.twapStates` (main dex only, per the PM's 2026-10-03 check). The bot calls webData2 only in cycles where the positions snapshot changed or `twap_active` has rows for the account.
- TWAP_START size in USD uses `sz * markPx`, with `markPx` from webData2: `meta.universe[i].name` and `assetCtxs[i].markPx` are parallel arrays (234 entries in the fixture). Without a mark price the message shows the coin amount.
- TWAP_END status comes from the newest non-`activated` `twapHistory` entry for the twapId. Its `state` carries the final `executedSz` / `executedNtl`; `time` (seconds) is used as the end time.
- If the TWAP left `twapStates` but `twapHistory` has no final entry yet, the bot waits one more polling cycle before reporting status `unknown` (spec says report `unknown` immediately; the extra cycle covers a possible lag between the two endpoints, not verified live).
- webData2 is large (311 KB in the fixture). Its weight is still [I] 20.
- **[?] Not verified: whether `twapStates` includes HIP-3 TWAPs.** `twapHistory` does include them (`xyz:MSTR` in the fixture), but the webData2 fixture's 5 active TWAPs are all main-dex coins. Needs a live check with a wallet running a HIP-3 TWAP (owner). If they are missing, HIP-3 TWAP slices will not be suppressed.

### HIP-3 dexs (Phase 1 PR B)

- `perpDexs` returns a list whose first entry is `null` (the main dex). The other entries were documented as objects with `name`; the client accepts both a plain string and an object with `name`, and skips `null`. Not verified live which shape the API returns today (owner).
- `clearinghouseState` with `{"dex": "xyz"}` returns that dex's positions only. The bot queries the main dex plus every dex in `venue_accounts.dexs_json`. Not verified live: whether `position.coin` inside a dex state is `"xyz:MU"` or `"MU"` (owner). The snapshot is keyed by dex, so either form works for change detection; `/positions` shows what the API returns.
- Margin balance in `/positions` is the sum of `marginSummary.accountValue` over all queried dexs.
- `webData2` mark prices (`meta.universe` / `assetCtxs`) cover the main dex only, so a HIP-3 TWAP_START shows the coin amount instead of USD.
- Dex discovery: one scan of all `perpDexs` on `/add`, one automatic scan per existing wallet (cursor kind `dex_scan`), `/rescan` on demand, and a new dex prefix seen in fills is added automatically.

### userFillsByTime (Phase 1 PR B)

- One request returns fills of every dex (main, HIP-3) and spot. Spot coins are `"PURR/USDC"` or `"@107"`; HIP-3 coins are `"xyz:MU"`.
- `tid` is not monotonic within a millisecond. Sorting same-ms fills by `tid` breaks the `startPosition` chain, so the bot keeps the API order (stable sort on `time` only).
- `dir == "Spot Dust Conversion"` appears in spot history; it is skipped.
- TWAP slice fills do not appear in `userFillsByTime` (fixture: 8 gaps in the `startPosition` chain, each explained by a TWAP slice in `userTwapSliceFillsByTime`). So a native TWAP never produces position alerts from fills; the START/END messages come from webData2/twapHistory as in PR A.
- Fixture replay (`hl_userFillsByTime.json`, 348 fills): 114 orders, OPEN 38 / INCREASE 8 / DECREASE 21 / CLOSE 47 after the spot split; realized PnL total equals the sum of `closedPnl`; dexs `""` 106, `xyz` 6, `para` 2.
- Polling: fills are fetched every transfers cycle (same cadence as Phase 0). Spec 5.2 says fetch fills only when the snapshot changed, but spot fills never change perp snapshots. The weight optimization is left for PR C (scheduler).

## Telegram

- `setMyCommands` is called once in `post_init` with `BotCommandScopeAllPrivateChats`. The request shape was checked against a local fake Bot API server. Whether the menu shows up in the Telegram client needs a check with the real bot token.
