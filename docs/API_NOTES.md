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
- **[?] Not verified (still open after the PM live check 2026-10-03): whether `twapStates` includes HIP-3 TWAPs.** `twapHistory` does include them (`xyz:MSTR` in the fixture), but the webData2 fixture's 5 active TWAPs are all main-dex coins. Needs a live check with a wallet running a HIP-3 TWAP (owner). If they are missing, HIP-3 TWAP slices will not be suppressed.

### HIP-3 dexs (Phase 1 PR B)

- `perpDexs` (PM live check 2026-10-03): `[null, {name, fullName, deployer, oracleUpdater, feeRecipient, assetToStreamingOiCap, ...}, ...]`. The first entry `null` is the main dex; the others are objects. The client reads `name` and skips `null` (it still accepts a plain string).
- `clearinghouseState` with `{"dex": "xyz"}` returns that dex's positions only. The bot queries the main dex plus every dex in `venue_accounts.dexs_json`. `position.coin` inside a dex state is `"xyz:MU"` (PM live check 2026-10-03), the same form as in fills.
- Margin balance in `/positions` is the sum of `marginSummary.accountValue` over all queried dexs.
- `webData2` mark prices (`meta.universe` / `assetCtxs`) cover the main dex only, so a HIP-3 TWAP_START shows the coin amount instead of USD.
- Dex discovery: one scan of all `perpDexs` on `/add`, one automatic scan per existing wallet (cursor kind `dex_scan`), `/rescan` on demand, and a new dex prefix seen in fills is added automatically.

### userFillsByTime (Phase 1 PR B)

- One request returns fills of every dex (main, HIP-3) and spot. Spot coins are `"PURR/USDC"` or `"@107"`; HIP-3 coins are `"xyz:MU"`.
- `tid` is not monotonic within a millisecond. Sorting same-ms fills by `tid` breaks the `startPosition` chain, so the bot keeps the API order (stable sort on `time` only).
- `dir == "Spot Dust Conversion"` appears in spot history; it is skipped.
- TWAP slice fills do not appear in `userFillsByTime` (fixture: 8 gaps in the `startPosition` chain, each explained by a TWAP slice in `userTwapSliceFillsByTime`). So a native TWAP never produces position alerts from fills; the START/END messages come from webData2/twapHistory as in PR A.
- Fixture replay (`hl_userFillsByTime.json`, 348 fills): 114 orders, OPEN 38 / INCREASE 8 / DECREASE 21 / CLOSE 47 after the spot split; realized PnL total equals the sum of `closedPnl`; dexs `""` 106, `xyz` 6, `para` 2.
- Fixture `hl_userFillsByTime_sweep.json` (PM recording): 243 fills over 4 oids. One BTC Close Long market order of 160 fills in the same millisecond, then 3 `@107` (HYPE) spot sells within 34 s. Replay: 4 events, 2 messages (the 3 spot orders merge by debounce).
- Fixture `hl_userFillsByTime_algo.json` (PM recording, loracle): 1,068 fills over 834 oids in one hour, `twapId` null on every fill. BTC Open Long and CASHCAT Close Short. `hl_clearinghouseState_algo.json` is the state at the end of that hour; its BTC and CASHCAT sizes equal the fills' last `startPosition + sz`. Replay with 30 s cycles: 2 ALGO_START, 2 ALGO_END, no fill message.
- Polling: fills are fetched every transfers cycle (same cadence as Phase 0). Spec 5.2 says fetch fills only when the snapshot changed, but spot fills never change perp snapshots. The weight optimization is left for PR C (scheduler).

### Rate limit and weights (Phase 1 PR C)

- Weights used by the budget (spec 3.5 table, from the HL docs): `clearinghouseState` 2, `spotClearinghouseState` 2, `userFillsByTime` 20 + 1 per 20 fills returned, `twapHistory` 20 + 1 per 20 entries, `userNonFundingLedgerUpdates` 20, `webData2` 20 [I], `perpDexs` / `spotMeta` / `portfolio` 20 (not listed explicitly in the docs, the default for info requests).
- The budget charges the per-item part after the response, so a large fills page can push the bucket into debt for a moment; the next request waits it out.
- **[?] Not verified live: whether HL sends a `Retry-After` header on 429.** The client reads it as whole seconds and falls back to a 30 s pause. The response body of a 429 was not recorded either. Owner: capture one 429 response (status, headers) if it ever happens in production; the `/health` 429 counter shows whether it did.
- The 50-address, 30-minute completion criterion is a simulation (`tests/test_poller.py`): a fake HL that counts weight like the documented limit (1200 per rolling minute) and answers 429 above it. Not run against the live API from the dev environment (api.hyperliquid.xyz is blocked there).

### /related discovery (Phase 3)

Shapes from the PM fixtures (master wallet 0x6aca…, 2026-10-03):

- `userRole`: `{"role": "user"}` with no `data` for a plain user. The spec's `data.master` / `data.user` apply to `subAccount` / `agent` roles only (not in the fixtures, handled from the spec [?]). Cached 7 days in `api_cache` (weight 60).
- `subAccounts`: `[{name, subAccountUser, master, clearinghouseState, spotState}]`, one entry for the fixture master. The embedded `clearinghouseState` is ignored (the `/related` row price uses the 2-weight call for shown rows only).
- `extraAgents`: `[]` in the fixture. Entries are read as `{address, name, validUntil}` from the spec [?].
- `userFees.stakingLink`: `null` in the fixture. Read as `{type, stakingUser}` from the spec [?].
- `referral`: `referredBy.referrer` and `referrerState.data.referralStates[].user` as in the spec, both present in the fixture.
- `userVaultEquities`: `[{vaultAddress, equity, lockedUntilTimestamp}]`.
- `vaultDetails`: `leader`, `followers[] {user, vaultEquity, ...}`, `relationship {type: parent, data: {childAddresses}}` (HLP fixture, 20 followers, 7 children). Called only when the tracked address itself has role `vault`.
- `userNonFundingLedgerUpdates(startTime=0)`: 239 entries for the master; types seen: `deposit, withdraw, send, spotTransfer, accountClassTransfer, vaultDeposit, vaultWithdraw, cStakingTransfer, spotGenesis`. `send` with `user == destination` is a spot/perp move of the same account (sourceDex `spot`, destinationDex ``), not a counterparty. The per-item cost (1 per 20 entries) is charged like fills; 239 entries cost 20 + 11.
- Discovery weight on the fixture: 295 (userRole 60 + 7 calls at 20 + ledger 31 + 12 row prices at 2 + counterparty userRole checks while under 300). Second run within 7 days skips the userRole 60.
- System addresses seen as transfer counterparties (PM live review 2026-10-03): `0x2000000000000000000000000000000000000079`, `…0168`, `…010c` and others of the form `0x2` + 32 zeros + 1 to 3 hex digits (spot token / HIP-3 escrow, the token index at the end). `related.is_system_address` treats `0x2` or `0x0` followed by 28 or more zeros as system, plus the explicit list (escrow `0x2000…0000`, zero, assistance fund `0xfefe…fefe`, HLP vault).
- hypurrscan tags (PM live check 2026-10-03): `GET /tags/{address}` returns `{}` for every address tried, including the documented example and the bridge; `POST /tags/addresses` with the documented `{"addresses": [...]}` body returns 404. Not used. `api.hypurrscan.io` is also unreachable from the dev environment, so `openapi.json` was not read here. Fixtures `hypurrscan_tags_*.json` are not in the repo.

## Telegram

- `setMyCommands` is called once in `post_init` with `BotCommandScopeAllPrivateChats`. The request shape was checked against a local fake Bot API server. Whether the menu shows up in the Telegram client needs a check with the real bot token.
