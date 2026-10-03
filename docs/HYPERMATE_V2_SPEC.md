# HyperMate v2 업그레이드 스펙

작성: 2026-10-02 / PM: Claude (Cowork) / 구현: Claude Code / 오너: 위지
대상 리포: https://github.com/jihanwi/hypermate-bot (HEAD `7ea9207`)

이 문서는 Claude Code에 그대로 넘기는 구현 스펙이다. Phase 단위로 하나씩 던지고, 각 Phase는 별도 브랜치/PR로 작업한다. 각 Phase 끝에 "완료 기준"이 있고, 전부 통과해야 다음 Phase로 넘어간다. 확신도 표기: [V] 공식 문서 또는 라이브 호출로 검증됨, [I] 추론, [?] 미확인 (구현 중 확인 필요).

---

## 0. Claude Code에 넘길 때 쓰는 프롬프트 템플릿

```
리포: hypermate-bot. docs/HYPERMATE_V2_SPEC.md 를 먼저 끝까지 읽어라.
이번 작업은 "Phase N"만 구현한다. 다른 Phase 범위의 코드는 건드리지 않는다.
완료 기준(섹션 N.x)을 체크리스트로 만들고, 하나씩 검증한 뒤 PR 설명에 결과를 붙여라.
스펙에 [?]로 표시된 항목은 구현 전에 실제 API를 호출해서 확인하고, 확인 결과를 docs/API_NOTES.md에 기록해라.
스펙과 실제 API가 다르면 코드를 API에 맞추고, 차이를 PR 설명에 적어라. 추측으로 메우지 마라.
```

---

## 1. 현재 코드 진단

### 1.1 실사용 버그

| # | 위치 (bot.py) | 문제 | 영향 |
|---|---|---|---|
| B1 | `positions_command` L1357, `stats_command` L1230 | 레거시 JSON(`user_wallets`)만 조회. `/add`는 DB에만 씀 | 새로 추가한 지갑에 `/positions`, `/stats` 치면 "No wallets tracked yet" |
| B2 | `check_new_positions` L640, `check_twap_orders` L778 | `clearinghouseState` 응답에 `twapOrders` 키가 없음 [V]. TWAP 감지 코드가 실행된 적이 없음. `active_twap_coins` 는 항상 빈 set | TWAP 중 30초마다 슬라이스 체결이 `POSITION_INCREASE` 알림으로 쏟아짐. 이게 "TWAP 노티 폭탄"의 실제 원인 |
| B3 | L739-766 | 청산 판정이 "PnL < 포지션가치의 -15%" 휴리스틱 | 손절 청산을 liquidation으로 오탐, 실제 청산을 close로 미탐 |
| B4 | 전역 `parse_mode='Markdown'` (50여 곳) | Telegram Markdown v1. alias나 코인명에 `_`, `*`, `[` 들어가면 send 실패. escape 없음 | 알림 조용히 유실 (로그에만 남음) |
| B5 | `get_spot_transfers` L392-426 | 존재하지 않는 `GET /spot/user/transfers/{addr}` 를 매 사이클 호출 → 404 → fallback | transfer 체크 3콜 중 1콜 낭비, 불필요한 레이턴시 |
| B6 | 전역 dict 상태 L43-61 | `previous_positions`, `last_transfer_timestamps` 등이 전부 메모리 | 재시작(Railway redeploy)마다 initial scan부터. 그 사이 이벤트 유실, 재시작 직후 포지션 변화 알림 없음 |
| B7 | `main()` L2306, L2317 | `asyncio.get_event_loop()` 를 두 번 호출 (PTB `run_polling` 도 같은 패턴) | Python 3.12+에서 DeprecationWarning, 환경에 따라 RuntimeError 가능성 |
| B8 | L729-735 | 마지막 폴링 시점의 `unrealizedPnl` 을 청산 PnL로 사용 | 실제 realized PnL과 다름. fills의 `closedPnl` 써야 함 |
| B9 | `get_spot_fills` L444, `format_spot_fill_message` L528 | `coin.endswith('USDC')` 로 spot 판정, 표시 시 `.replace('USDC','')` | HL spot 코인 표기는 `PURR/USDC` 또는 `@107` 형태. `@` 인덱스 코인은 전부 누락, `PURR/USDC` 는 `PURR/` 로 표시 |
| B10 | `get_spot_fills` L428-436 | `userFills` 에 `startTime` 을 넘기지만 그 파라미터는 `userFillsByTime` 전용이라 무시됨 | 매 사이클 최근 fills 최대 2000건 전체 수신. weight 20 + 최대 100. S1의 주범 |
| B11 | `format_transfer_message` L453-523 | send 타입 미처리로 이체 알림 전무. 현재 HL 이체는 ledger `delta.type == 'send'` 로 옴 [V], `internalTransfer`/`spotTransfer` 는 관측 안 됨 | 추적 지갑의 입출금 이체 알림이 하나도 안 나감. Phase 0에서 수정 |

### 1.2 구조/운영 리스크

| # | 문제 | 설명 |
|---|---|---|
| S1 | Rate limit | HL REST는 IP당 1200 weight/분 [V]. 현재 지갑당 사이클: clearinghouseState(2) + userFills(20 + 20건당 1, B10 때문에 최대 120) + ledger(20) + 404 호출. 분당 2사이클이면 지갑당 84~284 weight (fills 히스토리 길이에 따라) → 활발한 지갑이면 4개, 조용한 지갑이면 14개쯤에서 429 시작. 폴링 루프가 순차 + `sleep(0.5)` + `sleep(2)` 라 지갑 12개 넘으면 사이클이 30초 초과, APScheduler 기본 max_instances=1로 사이클 스킵 |
| S2 | 영속성 | Fly.io 머신 파일시스템은 deploy마다 이미지로 초기화되므로 Volume 없으면 `hypermate.db` 유실. (오너 결정: Fly.io Volume `hypermate_data` 를 `/data` 에 마운트 + SQLite 유지. Volume은 머신 1대에만 붙으므로 머신 수 1 고정) |
| S3 | 죽은 코드 | 지갑 생성, 개인키 암호화 저장, 레퍼럴 등록, JSON 저장소 3겹 (`user_wallets.json`, `generated_wallets.json`, `wallets_secure.json`), 마이그레이션 코드. 약 700줄. 핸들러는 주석 처리됐지만 코드와 Fernet 키 요구사항은 남아있음 |
| S4 | 단일 파일 | 2,361줄. 베뉴 추가하면 유지 불가 |
| S5 | 테스트 없음 | `test_connection.py` 는 빈 파일 |
| S6 | 멀티유저 알림 | 같은 지갑을 여러 유저가 추적하면 메시지 포맷은 공유, 유저별 설정 불가 |

### 1.3 유지할 것

- python-telegram-bot + JobQueue 기반 구조
- "지갑은 유저 수와 무관하게 한 번만 폴링" 원칙 (이미 그렇게 되어있음)
- hypurrscan 링크 (2026-10 기준 살아있음 [V]. `app.hyperliquid.xyz/explorer/address/{addr}` 를 fallback으로)
- alias 중심 UX

---

## 2. 목표와 비목표

### 목표 (오너 요구사항)

1. HL 외 Lighter, RISEx, Aster, Extended, Variational 포지션 진입 알림 (실현 가능성은 베뉴마다 다름. 섹션 6, 8 참고)
2. 추적 지갑의 연관 지갑 탐색
3. TWAP 알림을 시작/종료 2건으로 압축
4. `/` 입력 시 커맨드 자동완성 메뉴
5. PM 판단으로 추가하는 UX/안정성 개선 (섹션 1.1, 1.2 전부 + 섹션 9, 10)

### 비목표

- 트레이딩 실행, 지갑 생성/키 보관 (전부 제거)
- 웹 대시보드
- 유료 데이터 소스 (Nansen, Bitquery) 의존. 단, Phase 4 스파이크 결과에 따라 재검토
- 50개 초과 지갑 스케일 (오너 결정: 본인 + 지인, 지갑 10~50개)

---

## 3. 타깃 아키텍처

### 3.1 디렉토리 구조

```
hypermate/
  __init__.py
  config.py              # env 로딩, 상수 (폴링 주기, 임계값)
  main.py                # Application 빌드, 핸들러/잡 등록, post_init(set_my_commands)
  db/
    schema.sql           # 섹션 3.4
    repo.py              # 모든 SQL은 여기만. aiosqlite, WAL 모드
  core/
    events.py            # Event dataclass, EventType enum, dedupe_key 규칙
    pipeline.py          # 어댑터 → 이벤트 → 필터/집계 → 포맷 → 전송
    aggregator.py        # TWAP 억제, 윈도우 집계, 임계값 필터
    scheduler.py         # weight 버짓 기반 폴링 스케줄러
    formatter.py         # 이벤트 → Telegram HTML 메시지
    links.py             # 베뉴별 explorer 링크
  venues/
    base.py              # VenueAdapter 추상 클래스
    hyperliquid/
      client.py          # info API 래퍼 + weight 계산
      adapter.py
      twap.py
      related.py         # 연관 지갑 탐색 (Phase 3)
    lighter/
    risex/
    aster/
    extended/            # Phase 4 스파이크
    variational/         # Phase 4 스파이크
  bot/
    commands.py          # 커맨드 핸들러
    callbacks.py         # 인라인 키보드 콜백
    texts.py             # 모든 유저 노출 문자열
tests/
  fixtures/              # 실제 API 응답 JSON (주소 익명화)
  test_*.py
docs/
  HYPERMATE_V2_SPEC.md   # 이 문서
  API_NOTES.md           # 구현 중 확인한 API 사실 기록
```

진입점은 `python -m hypermate.main`. `Dockerfile` 의 CMD, `fly.toml` 에 반영.

### 3.2 VenueAdapter 인터페이스

```python
class VenueAdapter(Protocol):
    venue: str                                   # "hyperliquid" | "lighter" | "risex" | "aster" | ...
    async def resolve(self, evm_address: str) -> list[VenueAccount]
        # EVM 주소 → 이 베뉴의 계정 식별자 목록. 없으면 []. (Lighter는 서브계정 여러 개 가능)
    async def snapshot(self, account: VenueAccount) -> AccountSnapshot
        # 현재 포지션/잔고. 변화 감지용. 비용 최소 엔드포인트 사용.
    async def fetch_events(self, account: VenueAccount, cursor: Cursor) -> tuple[list[Event], Cursor]
        # cursor 이후 발생한 이벤트 (fills, ledger, twap 상태변화). 멱등해야 함.
    def explorer_url(self, account: VenueAccount) -> str
    def cost(self, op: str) -> int                # weight 계산, 스케줄러가 사용
```

원칙:
- 어댑터는 Telegram을 모른다. `Event` 만 반환.
- 어댑터는 상태를 들고 있지 않는다. cursor와 snapshot은 DB에 저장되고 인자로 주입.
- 어댑터는 429/5xx에서 예외를 던지고, 재시도/백오프는 scheduler가 담당.

### 3.3 Event 모델

```python
class EventType(str, Enum):
    POSITION_OPEN = "position_open"
    POSITION_INCREASE = "position_increase"
    POSITION_DECREASE = "position_decrease"
    POSITION_CLOSE = "position_close"
    POSITION_FLIP = "position_flip"          # Long > Short, Short > Long
    LIQUIDATION = "liquidation"
    TWAP_START = "twap_start"
    TWAP_END = "twap_end"                    # finished | terminated | error
    SPOT_BUY = "spot_buy"
    SPOT_SELL = "spot_sell"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    ACCOUNT_CLASS_TRANSFER = "account_class_transfer"   # HL spot <-> perp, 기본 알림 off
    DEX_COLLATERAL_TRANSFER = "dex_collateral_transfer" # HL 메인 <-> HIP-3 덱스 담보 이동, 기본 알림 off
    ALGO_START = "algo_start"                # 합성 TWAP (외부 실행봇의 반복 소액 체결) 감지, 5.2 "합성 TWAP"
    ALGO_END = "algo_end"
    VAULT_DEPOSIT = "vault_deposit"
    VAULT_WITHDRAW = "vault_withdraw"
    PRIVACY_ON = "privacy_on"                # Aster 전용

@dataclass(frozen=True)
class VenueAccount:
    venue_account_id: int | None             # DB id, resolve 직후엔 None
    wallet_id: int
    venue: str
    account_ref: str                         # venue_accounts.account_ref 와 동일 의미
    label: str | None                        # Lighter 서브계정 index 등 표시용

@dataclass
class AccountSnapshot:
    positions: dict[str, PositionState]      # coin -> {szi, entry_px, position_value, mark_px, leverage, liq_px, unrealized_pnl}
    account_value: Decimal | None
    extra: dict                              # Aster accountPrivacy 등 베뉴별
    fetched_at_ms: int

Cursor = dict[str, str]                      # kind -> cursor 문자열. cursors 테이블 행들을 한 계정 단위로 묶은 것

@dataclass
class Event:
    venue: str
    account: VenueAccount
    type: EventType
    ts_ms: int
    coin: str | None
    side: Literal["LONG", "SHORT"] | None
    size: Decimal | None                      # 이번 이벤트에서 변한 수량 (절대값)
    notional_usd: Decimal | None              # 변한 수량의 USD
    price: Decimal | None                     # 체결가 또는 평균가
    position_after: Decimal | None            # 이벤트 후 포지션 수량 (부호 포함)
    realized_pnl: Decimal | None
    meta: dict                                # twap_id, minutes, executed_sz, status, counterparty 등
    dedupe_key: str                           # f"{venue}:{account.id}:{type}:{source_id}"
```

`dedupe_key` 의 `source_id` 는 베뉴가 주는 고유 id (HL fill `tid`, ledger `hash`, twapId+status). 없으면 `coin:ts_ms:size` 해시. events 테이블에 UNIQUE. 재시작/중복 폴링에도 같은 알림이 두 번 안 나가는 유일한 방어선.

### 3.4 DB 스키마 (SQLite, `PRAGMA journal_mode=WAL`)

```sql
CREATE TABLE users (
  user_id INTEGER PRIMARY KEY,               -- Telegram user id
  lang TEXT DEFAULT 'en',
  tz TEXT DEFAULT 'Asia/Seoul',
  created_at INTEGER
);

CREATE TABLE wallets (                        -- EVM 주소 단위, 유저와 무관
  wallet_id INTEGER PRIMARY KEY,
  evm_address TEXT UNIQUE NOT NULL            -- lowercase
);

CREATE TABLE venue_accounts (                 -- 한 지갑이 여러 베뉴/서브계정을 가질 수 있음
  venue_account_id INTEGER PRIMARY KEY,
  wallet_id INTEGER NOT NULL REFERENCES wallets,
  venue TEXT NOT NULL,
  account_ref TEXT NOT NULL,                  -- HL: address, Lighter: account_index, RISEx/Aster: address, Extended: position_id
  active INTEGER DEFAULT 1,                   -- resolve 결과 활동 없으면 0 (폴링 제외)
  last_activity_ms INTEGER,                   -- 티어 폴링용
  dexs_json TEXT NOT NULL DEFAULT '[]',       -- HL 전용: 활동이 확인된 HIP-3 덱스 목록 (예: ["xyz","cash"]). 메인 덱스는 항상 폴링하므로 넣지 않음
  UNIQUE(venue, account_ref)
);

CREATE TABLE subscriptions (                  -- 유저 x 지갑
  user_id INTEGER REFERENCES users,
  wallet_id INTEGER REFERENCES wallets,
  alias TEXT NOT NULL,
  settings_json TEXT DEFAULT '{}',            -- 섹션 9.4
  muted_until_ms INTEGER,
  created_at INTEGER,
  PRIMARY KEY (user_id, wallet_id),
  UNIQUE (user_id, alias COLLATE NOCASE)
);

CREATE TABLE cursors (
  venue_account_id INTEGER REFERENCES venue_accounts,
  kind TEXT NOT NULL,                          -- 'fills' | 'ledger' | 'twap' | 'trades'
  cursor TEXT NOT NULL,                        -- ms timestamp 또는 베뉴별 cursor 문자열
  updated_at INTEGER,
  PRIMARY KEY (venue_account_id, kind)
);

CREATE TABLE snapshots (
  venue_account_id INTEGER PRIMARY KEY REFERENCES venue_accounts,
  positions_json TEXT NOT NULL,                -- HL: {dex: {coin: {szi, entry_px, position_value, ...}}}, 메인 덱스 키는 "" . 다른 베뉴: {coin: {...}}
  account_value TEXT,                          -- Decimal 문자열 (marginSummary.accountValue). float 금지
  updated_at INTEGER
);

CREATE TABLE twap_active (
  venue_account_id INTEGER REFERENCES venue_accounts,
  twap_id TEXT NOT NULL,
  state_json TEXT NOT NULL,
  started_ms INTEGER,
  PRIMARY KEY (venue_account_id, twap_id)
);

CREATE TABLE algo_active (                    -- 합성 TWAP 추적 (5.2 "합성 TWAP")
  venue_account_id INTEGER REFERENCES venue_accounts,
  coin TEXT NOT NULL,
  sign INTEGER NOT NULL,                      -- fill 이 포지션에 주는 변화 부호: +1 (Open Long, Close Short) / -1 (Open Short, Close Long)
  started_ms INTEGER,
  last_fill_ms INTEGER,
  fills_count INTEGER,
  total_sz TEXT,                              -- Decimal 문자열
  total_ntl TEXT,                             -- Decimal 문자열
  PRIMARY KEY (venue_account_id, coin, sign)
);

CREATE TABLE events (
  event_id INTEGER PRIMARY KEY,
  dedupe_key TEXT UNIQUE NOT NULL,
  venue_account_id INTEGER REFERENCES venue_accounts,
  type TEXT NOT NULL,
  ts_ms INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  delivery TEXT NOT NULL DEFAULT 'sent',       -- 'sent' | 'suppressed_twap' | 'filtered_threshold' | 'filtered_settings' | 'muted'
  created_at INTEGER
);
CREATE INDEX idx_events_account_ts ON events(venue_account_id, ts_ms);

CREATE TABLE sent_messages (                  -- TWAP 진행 메시지 edit용, /recent 용
  event_id INTEGER REFERENCES events,
  user_id INTEGER,
  chat_id INTEGER,
  message_id INTEGER,
  PRIMARY KEY (event_id, user_id)
);

CREATE TABLE wallet_links (                   -- Phase 3
  wallet_id INTEGER REFERENCES wallets,
  related_address TEXT NOT NULL,
  related_venue TEXT,
  link_type TEXT NOT NULL,                     -- 섹션 7.2
  confidence TEXT NOT NULL,                    -- 'confirmed' | 'likely' | 'weak'
  evidence_json TEXT NOT NULL,
  discovered_at INTEGER,
  PRIMARY KEY (wallet_id, related_address, link_type)
);

CREATE TABLE api_cache (                      -- userRole(weight 60) 등 비싼 호출 캐시
  cache_key TEXT PRIMARY KEY,
  value_json TEXT,
  expires_at INTEGER
);
```

마이그레이션: 기존 `tracked_wallets(user_id, wallet_address, alias)` → `users` + `wallets` + `venue_accounts(venue='hyperliquid')` + `subscriptions`. 1회성 스크립트 `scripts/migrate_v1.py`. 레거시 JSON 파일은 읽지 않는다 (있으면 경고 로그만).

### 3.5 폴링 스케줄러와 weight 버짓

HL 기준 (다른 베뉴는 각자 한도, 섹션 6):

| 호출 | weight [V] | 용도 |
|---|---|---|
| `clearinghouseState` | 2 | 변화 감지 (1차) |
| `userFillsByTime` | 20 + 20건당 1 | 변화 있을 때만 상세 (2차) |
| `userNonFundingLedgerUpdates` | 20 | 입출금/이체 |
| `webData2` | 20 [I] | twapStates 포함. TWAP 활성 의심 시만 |
| `twapHistory` | 20 + 20건당 1 | TWAP 종료 상태 확인 |
| `userRole` | 60 | Phase 3. 반드시 캐시 |

2단계 설계 (50계정 기준 분당 weight 추정):
1. 매 `POLL_FAST` (기본 20초) 모든 활성 HL 계정 `clearinghouseState`. 50 × 3회/분 × 2 = 300/분.
2. snapshot 대비 `szi` 변화가 있는 계정만 `userFillsByTime(startTime=cursor)` (20 + 20건당 1) 그리고 같은 사이클에 `webData2` (20, TWAP 시작 감지용). 평시 분당 변화 계정 3~5개 가정 → 120~250/분. `accountValue` 만 변한 경우(펀딩, 가격)는 호출 없음.
3. 매 `POLL_LEDGER` (기본 180초) 모든 계정 ledger. 50 × (1/3) × 20 = 333/분.
4. TWAP 활성 계정(`twap_active` 에 행 있음)은 매 fast 사이클에 `webData2` (2번과 중복이면 1회). 동시 TWAP 3개 가정 → 180/분.

합계 평시 ~950, 최악(변화 계정 10개 + TWAP 5개) ~1,330/분으로 버짓 초과 가능. 그래서 스케줄러는 토큰 버킷(1200/분, 안전 마진 15% → `HL_WEIGHT_BUDGET` 기본 1020)으로 모든 호출을 큐잉하고, 우선순위는 1차 폴링 > TWAP webData2 > fills > ledger. 버짓 부족 시 ledger 주기가 자동으로 늘어나고(최대 600초), 그래도 부족하면 `POLL_FAST` 자동 상향: `P = max(20, ceil(n_accounts * 2 * 60 / (budget * 0.4)))` 초 (1차 폴링에 버짓의 40%까지만 허용). 50계정이면 `50*120/408 = 14.7` → 20초 유지, 100계정이면 30초.

티어 폴링: `last_activity_ms` 가 7일 이상 전인 계정은 `POLL_FAST * 3`. 활동 감지되면 즉시 fast 티어 복귀.

WebSocket 사용 여부: HL WS는 IP당 커넥션 10개, 구독 1000개, 그리고 **유저별 구독에 걸치는 유니크 유저 10개** 제한 [V]. 지갑 10~50개 범위라 WS는 기본 비활성. Phase 1 완료 기준에 "지갑 50개 시뮬레이션에서 429 없음" 을 넣는다. (옵션: 유저가 `/settings` 에서 "priority" 지정한 지갑 최대 10개만 WS `userFills` + `twapStates` 구독. Phase 1 범위 밖, 섹션 12 백로그.)

동시성: 베뉴별로 독립 asyncio task. `aiohttp.ClientSession` 은 프로세스당 1개 재사용. 429 수신 시 해당 베뉴 큐를 `Retry-After` 또는 30초 정지.

---

## 4. Phase 0: 안정화와 정리

목적: 기준선 확보. B1, B4, B5, B6, B7, B9, B10, B11 수정, 죽은 코드 제거, 영속성, 커맨드 메뉴. B2, B3, B8은 이벤트 엔진 교체가 전제라 Phase 1에서 해결. 새 기능은 없음.

### 4.1 작업

1. 섹션 3.1 구조로 파일 분리. 로직은 가능한 한 그대로 옮기되, 아래 항목은 수정.
2. 죽은 코드 제거: `generate_wallet`, `register_wallet_with_referral`, `retry_referral_registration`, `createwallet_command`, `confirmcreate_command`, `generate_new_wallet_for_user`, `exportkey_command`, `mywallet_command`, `store_user_wallet`, `get_user_private_key`, `encrypt_private_key`, `decrypt_private_key`, `load_*_wallets`, `save_*_wallets`, `migrate_old_wallets`, `migrate_tracked_wallets_to_db`, `created_wallets` 테이블, Fernet 초기화 (L77-93), 전역 `user_generated_wallets`, `DATA_FILE` / `GENERATED_WALLETS_FILE` / `SECURE_WALLETS_FILE`, import `eth_account`, `cryptography`, `secrets`, `base64`, `hashlib`, `hyperliquid.*`. B2 때문에 한 번도 실행된 적 없는 `check_twap_orders` (L778-842) 와 `previous_twap_states` 도 삭제 (Phase 1에서 새로 작성). `config.py` 의 `WALLET_ENCRYPTION_KEY`, `DATABASE_URL`, `validate_config` 의 키 검사 삭제. 의존성 `cryptography`, `hyperliquid-python-sdk`, `APScheduler` 제거 (SDK의 `Exchange`/`constants` 는 삭제되는 레퍼럴 코드에서만 사용, `Info` 는 import만. `eth-account` 는 SDK transitive라 같이 빠짐. APScheduler는 아래 12번 extra로 대체).
3. B1: 모든 커맨드가 DB만 조회. `user_wallets` 전역 제거.
4. B4: `parse_mode=ParseMode.HTML` 로 전환. 유저 입력(alias, coin)은 `html.escape`. `formatter.py` 에 `h()` 헬퍼 하나로 통일.
5. B5, B10: `get_spot_transfers` 의 첫 GET 제거. `userNonFundingLedgerUpdates` 만 호출. `userFills` → `userFillsByTime(startTime=cursor)` 로 교체.
6. B6: `previous_positions`, `last_transfer_timestamps`, `initial_*_scan_done` 를 `snapshots`, `cursors` 테이블로. 재시작 후 initial scan 없이 cursor부터 이어서.
7. B7: `asyncio.run()` 또는 PTB `post_init` 으로 DB init. `get_event_loop` 제거.
8. B9: spot 판정을 `coin` 에 `/` 포함 또는 `@` 로 시작으로 변경. `@N` 은 `spotMeta` 로 이름 치환 (1시간 캐시).
9. S2: `fly.toml` 의 `[mounts]` 로 볼륨 마운트 (`hypermate_data` → `/data`), `DATABASE_PATH` 환경변수 (기본 `/data/hypermate.db`). 시작 시 디렉토리 없으면 생성. `Dockerfile` (python:3.12-slim, 비root 실행).
10. 요구사항 4: `post_init` 에서 `bot.set_my_commands(...)` 호출. 커맨드 목록은 섹션 9.1. scope `BotCommandScopeAllPrivateChats`.
11. `/help` 추가 (섹션 9.2). `/start` 는 짧은 환영 + `/help` 안내로 축소.
12. `requirements.txt` 핀 고정. `python-telegram-bot[job-queue]>=21` (JobQueue는 extra 필요 [V 공식 문서]. 현재는 `APScheduler` 를 따로 넣어 우회 중이라 그 줄 삭제).
13. 테스트 스캐폴드: `tests/fixtures/hl_clearinghouseState.json`, `hl_userFillsByTime.json`, `hl_webData2.json`, `hl_ledger.json` 등 실응답 저장 (주소 익명화). Phase 0에서는 formatter와 repo 테스트만, diff 로직 테스트는 Phase 1에서 새 엔진 기준으로 작성 (현재 diff 로직은 Phase 1에서 폐기됨).

### 4.2 완료 기준

- [ ] 새 유저가 `/add` → `/positions <alias>` 가 바로 동작
- [ ] alias `test_wallet_1` 로 추가해도 알림 전송 성공
- [ ] 봇 재시작 후 initial scan 로그 없이 이전 cursor부터 이어짐 (DB에 cursors 행 존재)
- [ ] `WALLET_ENCRYPTION_KEY` 없이 기동
- [ ] Telegram에서 `/` 입력 시 커맨드 메뉴 노출
- [ ] bot.py 삭제, `python -m hypermate.main` 으로 기동
- [ ] `pytest` 통과 (최소 diff 로직 + formatter 테스트)
- [ ] Fly.io Volume 마운트 후 redeploy 해도 지갑 목록 유지 (오너가 수동 확인)

---

## 5. Phase 1: HL 이벤트 엔진 재작성 (TWAP 압축 포함)

목적: 요구사항 3 해결. 상태 diff 기반 → fills + ledger + twapStates 기반으로 교체. 섹션 3.5 스케줄러 도입.

### 5.1 HL API 사실 (구현 전제)

- `userFills` / `userFillsByTime` 응답 필드 [V]: `coin, px, sz, side("B"|"A"), time, startPosition, dir, closedPnl, hash, oid, crossed, fee, tid, feeToken, twapId, cloid?, builderFee?, liquidation?`
- `dir` 관측값 [V]: `"Open Long"`, `"Open Short"`, `"Close Long"`, `"Close Short"`, `"Long > Short"`, `"Short > Long"`, `"Buy"`, `"Sell"` (spot), `"Spot Dust Conversion"`. 프론트 표시용 문자열이라 enum 보장 없음. 파싱 실패 시 `startPosition` 과 `sz`, `side` 로 계산해서 fallback.
- 중요 [V]: **본인 TWAP 슬라이스 체결은 `userFills` 에 안 나온다.** `userTwapSliceFills` / `userTwapSliceFillsByTime` 에만 나옴. 그리고 `userFills` 의 `twapId` 는 항상 null로 관측됨. 따라서 TWAP 감지는 fills로 하면 안 됨.
- `webData2` (`{"type":"webData2","user":addr}`) [V]: `twapStates: [[twapId, {coin, user, side, sz, executedSz, executedNtl, minutes, reduceOnly, randomize, timestamp}]]` 가 **현재 활성 TWAP 목록**. `clearinghouseState`, `openOrders`, `agentAddress` 도 같이 옴. 공식 문서에는 없는 프론트엔드용 엔드포인트. weight 20 추정 [I].
- `twapHistory` (`{"type":"twapHistory","user":addr}`) [V]: `[{time(초 단위), state, status:{status, description?}, twapId}]`. status: `activated | terminated | finished | error` (+ `waitingForTrigger`, `stopped` [I]).
- `userTwapSliceFillsByTime` [V]: `[{fill:{...}, twapId}]`.
- TWAP 메커니즘 [V]: 슬라이스 최소 30초 간격, 지속 5분~7일, 최소 $100.
- `clearinghouseState` top-level 키 [V]: `assetPositions, crossMaintenanceMarginUsed, crossMarginSummary, marginSummary, time, withdrawable`. `twapOrders` 없음.
- 청산 [V]: fills의 `liquidation: {liquidatedUser?, markPx, method: "market"|"backstop"}`. ledger의 `{"type":"liquidation", accountValue, leverageType, liquidatedPositions:[{coin, szi}]}`.
- ledger delta 타입 [V]: `deposit{usdc}`, `withdraw{usdc,nonce,fee}`, `internalTransfer{usdc,user,destination,fee}`, `subAccountTransfer{usdc,user,destination}`, `spotTransfer{token,amount,usdcValue,user,destination,fee}`, `accountClassTransfer{usdc,toPerp}`, `liquidation{...}`, `vaultDeposit{vault,usdc}`, `vaultWithdraw{vault,user,requestedUsd,commission,closingCost,basis,netWithdrawnUsd}`, `vaultCreate`, `vaultDistribution`, `vaultLeaderCommission`, `spotGenesis`, `rewardsClaim`, `send{user,destination,sourceDex,destinationDex,token,amount,usdcValue,fee,nativeTokenFee,nonce,feeToken}` (undocumented, 라이브 관측 [V]. 2026-10-03 기준 활발한 지갑의 이체는 전부 `send`, `internalTransfer`/`spotTransfer` 는 미관측. B11 참고). 숫자는 전부 문자열.
- 모든 숫자 필드는 `Decimal` 로 파싱. float 금지.

#### HIP-3 빌더 덱스 (PM 라이브 확인 2026-10-03)

- `{"type":"perpDexs"}` [V] → `[null, "xyz", "flx", "vntl", "hyna", "km", "abcd", "cash", "para", "mkts", "io"]`. `null` 이 메인 덱스. 목록은 1시간 캐시.
- `clearinghouseState` 에 `"dex": "xyz"` 를 주면 그 덱스의 포지션과 마진만 반환 [V]. 덱스별 `accountValue` 가 분리됨. 파라미터 없으면 메인 덱스만.
- `userFillsByTime` 은 모든 덱스의 체결을 한 번에 반환 [V]. HIP-3 코인은 `"xyz:MU"` 처럼 `"<dex>:<coin>"` 형식.
- `allDexsClearinghouseState` 는 REST 에서 422 (WS 전용) [V]. `webData2` 는 `dex` 파라미터를 무시하고 메인 덱스만 반환 [V]. `twapStates` 가 HIP-3 TWAP 을 포함하는지는 [?] 구현 중 확인.
- ledger `send` 의 `destination` (또는 `user`) 이 시스템 주소(`0x2000000000000000000000000000000000000000` 등)이고 `destinationDex` (또는 `sourceDex`) 가 있으면 HIP-3 덱스 담보 이동 [V]. 상대방 이체가 아니므로 `TRANSFER_IN/OUT` 이 아니라 `DEX_COLLATERAL_TRANSFER` 이벤트 (기본 알림 off, settings 로 on).

### 5.2 이벤트 생성 규칙

**HIP-3 덱스 커버리지**
- `venue_accounts.dexs_json` 에 활동이 확인된 HIP-3 덱스 목록을 둔다 (기본 `[]`). `/add` 와 `/rescan` 시 `perpDexs` 전체를 `clearinghouseState(dex=...)` 로 1회 스캔해서 포지션이 있는 덱스를 기록. 이후 fills 에 새 `<dex>:` 접두사가 보이면 자동 추가. Phase 1 의 `/rescan` 은 HL 덱스 스캔만 (다른 베뉴 resolve 는 Phase 2). 기존 추적 지갑은 배포 후 첫 폴링에서 1회 자동 스캔.
- 1차 폴링은 메인 덱스 + `dexs_json` 의 덱스만 `clearinghouseState`. snapshot 은 `{dex: {coin: ...}}` 로 덱스별 저장 (메인 덱스 키는 `""`).
- 알림 메시지에서 HIP-3 코인은 `$MU (xyz)` 로 표시 (접두사 대신 괄호로 덱스). `/positions` 는 덱스별 소제목과 덱스별 account value.

**포지션 (fills 기반)**
- 1차 폴링에서 snapshot 대비 변화 감지된 계정만 `userFillsByTime(startTime=cursor.fills+1)`.
- perp fill(coin에 `/` 없고 `@` 로 시작 안 함)을 `dir` 로 분류: Open → `POSITION_OPEN` (단, `startPosition != 0` 이면 `POSITION_INCREASE`), Close → `startPosition - sz == 0` 이면 `POSITION_CLOSE` 아니면 `POSITION_DECREASE`, `Long > Short` / `Short > Long` → `POSITION_FLIP`. `liquidation` 필드 있으면 `LIQUIDATION` 으로 승격.
- 같은 폴링 윈도우 안의 fills 는 아래 "체결 집계" 규칙으로 묶는다: size 합, notional 합, VWAP, `realized_pnl` 합, `position_after` 는 마지막 fill 기준.
- 집계 윈도우를 넘어서 이어지는 체결(예: 수동으로 1분 간격 분할 매수)은 별도 알림. 단, `settings.debounce_sec` (기본 60) 안에 같은 (coin, 분류) 이벤트가 또 오면 직전 메시지를 edit해서 누적 (sent_messages 참조). 메시지 edit 실패 시 새 메시지.
- cursor.fills = 처리한 마지막 fill의 `time`.
- 1차 폴링에서 변화가 감지됐는데 fills가 비어있으면 (funding, 가격 변동으로 accountValue만 변한 경우) 알림 없이 snapshot만 갱신.

**체결 집계** (PM 라이브 확인 2026-10-03)
- 같은 폴링 윈도우 안의 fills 를 먼저 `(coin, dir, oid)` 로 묶는다. 시장가 주문 1개가 호가 수십 개를 쓸면 fills N건이 되는데, 이를 메시지 1건으로 만든다 (size 합, VWAP, notional 합).
- 실사례: cl 지갑이 30초 동안 oid 4개로 HYPE spot 243건 체결 → v1 은 알림 수십 건. 기대 결과 4건. fixture: `tests/fixtures/hl_userFillsByTime_sweep.json` (243건, oid 4개).
- 그 다음 `(coin, dir)` 기준으로 기존 debounce edit 누적 규칙(위 `settings.debounce_sec`)을 적용한다.

**합성 TWAP (외부 실행봇)** (PM 라이브 확인 2026-10-03)
- 배경: 네이티브 TWAP 없이 외부 봇이 소액 주문을 반복하는 경우. 실사례: loracle 지갑이 `twapStates` 빈 배열, hypurrscan TWAP 없음 상태에서 1시간에 1,068건, 3~10초마다 BTC Open Long 0.02~0.09, CASHCAT Close Short 수백~수천 개. 전부 서명된 개별 주문(hash 정상)이라 네이티브 TWAP 억제가 걸리지 않는다. fixture: `tests/fixtures/hl_userFillsByTime_algo.json` (1,068건), `tests/fixtures/hl_clearinghouseState_algo.json` (같은 시점 포지션).
- 규칙은 settings 로 조정 가능 (9.4 기본값).
- 상태 키: `(venue_account, coin, 방향부호)`. 방향부호는 fill 이 포지션에 주는 변화 부호 (Open Long / Close Short = `+`, Open Short / Close Long = `-`).
- 진입 조건 (모두 충족):
  - 최근 `algo_window_sec` (300) 안에 서로 다른 폴링 사이클 3개 이상에서 같은 키의 fills 가 있음
  - 누적 fills 수 >= `algo_min_fills` (8)
  - 각 fill notional 의 중앙값이 그 coin 현재 포지션 notional 의 `algo_max_slice_pct` (2%) 미만. 포지션이 0 에서 시작하면 이 조건은 보지 않는다 (오너 결정 2026-10-03: 누적 notional 기준이면 같은 크기 fill 이 50건 넘게 쌓여야 2% 미만이 되어 사실상 감지 불가)
  - 카운트 단위는 "체결 집계" 로 묶은 주문(oid) 단위 (오너 결정). 시장가 1건이 호가 수십 개를 쓸어도 1로 센다. 메시지의 "N fills" 도 주문 수
- 진입 시 `ALGO_START` 이벤트 1건 (10 의 algo 예시) 을 새 메시지로 보낸다 (텔레그램 알림이 가야 하므로 edit 승격 아님, 오너 결정 2026-10-03). 진입 판정 전 사이클의 체결 메시지(debounce 누적)는 그대로 둔다. 이후 같은 키의 개별 fill 이벤트는 `delivery='suppressed_algo'` 로 기록만 한다. 따라서 algo 하나당 메시지는 감지 전 체결 메시지 + START + END.
- 메시지 방향 표기: 시작 시점 포지션과 같은 방향으로 늘리면 `accumulating LONG/SHORT`, 반대 방향(Close)이면 `reducing LONG/SHORT` (오너 결정). 예: CASHCAT Close Short (+) → `algo reducing SHORT $CASHCAT`. ALGO_END 도 같은 표기: `algo done accumulating LONG $BTC`, `algo done reducing SHORT $CASHCAT`.
- 진행: START 메시지를 `algo_progress_sec` (600) 마다 edit (누적 fills, 누적 notional, VWAP, 경과시간). 새 메시지 아님. `sent_messages` 테이블 사용.
- 종료: 마지막 fill 이후 `algo_idle_sec` (600) 동안 fill 없음 → `ALGO_END` 1건 (총 size, notional, VWAP, 소요시간) 후 상태 삭제.
- 반대 방향 체결, 청산, 포지션 완전 종료는 즉시 정상 알림. 상태는 유지.
- 메시지 형식은 네이티브 TWAP 과 맞추되 라벨은 "TWAP" 대신 "algo".
- 상태는 `algo_active` 테이블 (3.4, `twap_active` 와 별도). 재시작 후에도 DB 에 있으므로 이어서 추적.
- 테스트 기대값: algo fixture 1시간 리플레이 → `ALGO_START` 2건 (BTC `+`, CASHCAT `+`), 개별 fill 알림 0건, 리플레이 끝에서 idle 경과 시 `ALGO_END` 2건.

**TWAP**
- 1차 폴링에서 포지션 변화 감지 시, 또는 `twap_active` 에 행이 있는 계정은, 그 사이클에 `webData2` 호출.
- `twapStates` 에 있는데 `twap_active` 에 없는 twapId → `TWAP_START`. meta: twap_id, coin, side, sz, minutes, reduceOnly, 예상 종료 시각 (`timestamp + minutes*60*1000`).
- `twap_active` 에 있는데 `twapStates` 에서 사라진 twapId → `twapHistory` 로 최종 status 조회 → `TWAP_END`. meta: status, executedSz, executedNtl, 평균가 `executedNtl / executedSz`, 실제 소요 시간. `twapHistory` 에서 못 찾으면 (호출 실패 등) 마지막 state로 status `unknown`.
- **억제 규칙**: `twap_active` 에 (account, coin) 이 있는 동안, 그 coin의 같은 방향 fills 기반 포지션 이벤트는 알림 생성하지 않음 (events 테이블에는 `delivery='suppressed_twap'` 로 기록, `/recent` 에서는 보임). 반대 방향 체결(TWAP 중 수동 반대매매)은 정상 알림.
- 슬라이스 체결은 `userFills` 에 안 나오므로 억제 규칙은 "같은 방향의 수동 체결" 만 대상이 된다. 슬라이스 자체는 1차 폴링의 snapshot 변화로만 보임. snapshot 변화가 TWAP 활성 coin 때문이면 fills 조회를 **건너뛴다** (weight 절약).
- 선택 (settings `twap_progress`, 기본 off): TWAP 활성 중 25/50/75% 도달 시 START 메시지를 edit 해서 진행률 표시. 새 메시지 아님.
- TWAP 활성 중 봇이 재시작되면 `twap_active` 가 DB에 있으므로 이어서 추적.

**청산**
- fills `liquidation` 필드 또는 ledger `liquidation` 타입. 둘 다 오면 dedupe_key가 다르므로 두 번 나갈 수 있음 → ledger 쪽은 `LIQUIDATION` 이벤트가 같은 coin으로 직전 5분 내 있으면 skip.
- B3의 휴리스틱 삭제.

**ledger**
- `deposit` → `DEPOSIT`, `withdraw` → `WITHDRAW`, `internalTransfer` / `spotTransfer` / `send` / `subAccountTransfer` → `user == 본인` 이면 `TRANSFER_OUT` 아니면 `TRANSFER_IN`, meta.counterparty 기록 (Phase 3가 씀). `accountClassTransfer` → `ACCOUNT_CLASS_TRANSFER` (기본 알림 off, 설정으로 on). 단 `send` 의 상대방이 시스템 주소이고 `sourceDex`/`destinationDex` 가 있으면 `DEX_COLLATERAL_TRANSFER` (기본 알림 off, 설정으로 on, meta 에 source/destination 덱스). `vaultDeposit`/`vaultWithdraw` → `VAULT_*`. `vaultLeaderCommission`, `rewardsClaim`, `spotGenesis`, `vaultDistribution`, `vaultCreate` 는 이벤트 생성 안 함. 모르는 delta type은 경고 로그 1회 + 무시.
- 알림 안 나가는 이벤트도 events에 기록하되 `delivery` 컬럼으로 이유 표시 (`suppressed_twap`, `filtered_threshold`, `filtered_settings`, `muted`). `/recent` 가 이걸 보여준다.
- cursor.ledger = 마지막 `time`.

**spot**
- spot fills (`dir` Buy/Sell 또는 coin 형식으로 판정) → `SPOT_BUY` / `SPOT_SELL`. 같은 윈도우 집계 동일 적용.

### 5.3 완료 기준

- [ ] 1시간짜리 TWAP 추적 시 알림이 정확히 2건 (START, END). END에 평균가와 executedNtl 표시
- [ ] TWAP 중 같은 coin 수동 추가 매수 → 알림 없음, `/recent` 에는 suppressed로 표시
- [ ] 10 slice로 쪼개진 수동 분할 매수 (1분 간격) → 메시지 1건이 edit되며 누적
- [ ] 청산 fixture로 `LIQUIDATION` 1건, 중복 없음
- [ ] fixture 리플레이 테스트: 녹화된 fills 시퀀스 → 기대 이벤트 목록 (최소 open, increase, decrease, close, flip, liquidation, spot buy 각 1개)
- [ ] 50개 주소(활동 많은 공개 지갑 섞어서) 30분 폴링 시 429 0회, 로그에 분당 weight 사용량 출력
- [ ] 재시작 후 진행 중이던 TWAP의 END 알림 정상 발송
- [ ] `float(` 가 venues/hyperliquid 안에 없음

---

## 6. Phase 2: 멀티 베뉴 어댑터 (Lighter, RISEx, Aster)

목적: 요구사항 1 중 공개 API로 가능한 3개 베뉴. Extended, Variational은 Phase 4.

### 6.1 공통

- `/add <evm_address> <alias>` 시 모든 어댑터의 `resolve()` 를 병렬 호출. 활동 있는 베뉴만 `venue_accounts` 에 `active=1` 로 생성. 결과를 유저에게 표시: "HL ✅ / Lighter ✅ (2 sub-accounts) / RISEx ❌ / Aster ✅ (privacy: off)".
- 이미 추적 중인 지갑도 `/rescan <alias>` 로 재탐색. 그리고 매일 1회 비활성 베뉴 재탐색 (새로 쓰기 시작하는 경우).
- 알림 메시지 머리에 베뉴 뱃지 (섹션 10).
- 포지션 이벤트는 HL과 동일 `EventType`. 베뉴가 fills를 공개하지 않으면 snapshot diff 로 OPEN/INCREASE/DECREASE/CLOSE/FLIP 생성 (가격은 snapshot의 entry/mark 사용, realized_pnl은 None).
- 베뉴별 폴링 주기와 한도는 scheduler에 베뉴별 버킷으로 등록.

### 6.2 Lighter [V]

- Base `https://mainnet.zklighter.elliot.ai/api/v1`. 무인증 60 req/분 (IP + L1 주소 기준).
- resolve: `GET /accountsByL1Address?l1_address=0x...` → `sub_accounts[].index`. 각 index가 하나의 `venue_account`.
- snapshot: `GET /account?by=index&value=<index>` → `accounts[0].positions[]` 필드 `market_id, symbol, sign(1|-1), position, avg_entry_price, position_value, unrealized_pnl, realized_pnl, liquidation_price, margin_mode, allocated_margin`. 계정: `collateral, available_balance, total_asset_value`.
- events: `GET /trades?sort_by=timestamp&limit=100&account_index=N` (cursor 페이지네이션, `type=trade|liquidation|deleverage`). cursor.trades = 마지막 trade id/timestamp. `type=liquidation` → `LIQUIDATION`.
- WS `wss://mainnet.zklighter.elliot.ai/stream`: `account_all_positions/{index}`, `account_all_trades/{index}` 가 무인증 구독 가능 [V 문서, 라이브 미검증 [?]]. 한도: 커넥션당 500 구독. **Lighter는 REST 한도(60/분)가 빡빡하므로 WS를 기본으로**, REST는 resolve와 fallback. WS 끊기면 REST 폴링 60초 주기로 degrade.
- TWAP: 주문 타입 `twap` / `twap-sub` 존재 [V SDK] 하지만 부모 TWAP은 인증 엔드포인트에만. 공개로는 `twap-sub` 체결의 등간격 패턴으로 추론만 가능 [I]. **Phase 2 범위: Lighter TWAP 감지 안 함.** 대신 집계/debounce 규칙(5.2)이 적용되어 메시지 edit로 누적됨. 휴리스틱 감지는 백로그.
- explorer: Lighter 공식 explorer URL 형식 [?] 확인 후 `links.py` 에 추가. 없으면 `https://app.lighter.xyz/` 로 대체.
- SDK: 쓰지 않음 (REST/WS 직접). 참고용 `github.com/elliottech/lighter-python`.

### 6.3 RISEx [V]

- RISE Chain(ETH L2, chain id 4153) 위 온체인 CLOB. Base `https://api.rise.trade`. 무인증 500 req/10초/IP. WS 10 req/s.
- resolve: `GET /v1/positions?account=0x...` 가 200이면 활성. 계정 없으면 `/v1/account/cross-margin-balance` 가 500 반환하므로 positions 쪽으로 판정.
- snapshot: `GET /v1/positions?account=&page=&page_size=` → `data.positions[]`: `market_id, size, side(BUY|SELL), quote_amount` + leverage/entry/mark/uPnL [I, 필드명 확인 필요 [?]]. 수량은 18-decimal 고정소수점 → `Decimal(x) / 10**18`.
- events: `GET /v1/trade-history?account=&limit=&market_id=`. cursor = 마지막 trade timestamp/id.
- WS `wss://api.rise.trade/ws/`: `{"method":"subscribe","params":{"channel":"positions","makers":[addr,...],"market_ids":[...]}}` 로 **임의 주소 리스트 무인증 구독** [V SDK 소스]. 50개 주소를 한 커넥션에 넣을 수 있으므로 RISEx는 WS 기본, REST fallback.
- market_id → 심볼 매핑: `/v1/markets` [?] 1시간 캐시.
- TWAP: 없음 [V 부재].
- explorer: RISE 체인 explorer 주소 형식 [?].
- 참고: `developer.rise.trade/reference/general-information`, PyPI `risex`.

### 6.4 Aster [V]

- Pro 모드 포지션은 Aster Chain에 있음. 퍼블릭 JSON-RPC `POST https://tapi.asterdex.com/info` (무인증).
  - `aster_getBalance` params `[address, "latest"]` → `positions[].positions[]`: `id, symbol, collateral, positionAmount, entryPrice, unrealizedProfit, notionalValue, markPrice, leverage, isolated, positionSide, marginValue`. 상위에 `perpAssets[].walletBalance`, `accountPrivacy`.
  - `aster_userFills` `[address, symbol|null, from, to, "latest"]` → `fills[] {symbol, side, price, qty, time}`. 7일 윈도우, 최대 1000.
  - `aster_openOrders` `[address, symbol|"", "latest"]`.
  - 문서: `github.com/asterdex/api-docs/blob/master/RPC/aster-chain-rpc.md`
- resolve: `aster_getBalance` 에 포지션 또는 `walletBalance > 0` 이면 활성. `accountPrivacy` 저장.
- snapshot: `aster_getBalance`. events: `aster_userFills(from=cursor)`. fills에 realized pnl 없음 → `realized_pnl=None`, `/positions` 에는 `unrealizedProfit` 표시.
- **privacy** [V]: `accountPrivacy == "enabled"` 면 포지션/주문이 가려짐. snapshot에서 privacy가 off → on 으로 바뀌면 `PRIVACY_ON` 이벤트 1회 ("이 지갑은 Aster privacy를 켰습니다. 이후 Aster 포지션은 추적 불가"). 그 계정은 `active=0`, 일 1회 재확인.
- 한도: RPC 문서는 weight 1만 명시, 분당 한도 미공개 [?]. 보수적으로 베뉴 버킷 300/분으로 시작, 429 보이면 자동 반감. 폴링 30초.
- WS: 타인 계정용 없음 → 폴링만.
- TWAP: 웹 UI 전용 private endpoint. 타인 TWAP 비공개 [V]. `aster_openOrders` 의 `type` 에 TWAP 노출되는지 [?] 구현 중 확인. 안 되면 Lighter와 동일하게 집계/debounce만.
- explorer: Aster Chain explorer [?].

### 6.5 완료 기준

- [ ] 각 베뉴에서 실제 활동 중인 공개 지갑 1개씩 추가 → `/positions` 에 베뉴별 섹션 표시
- [ ] 각 베뉴에서 OPEN/CLOSE 알림 실제 수신 (오너가 테스트 지갑으로 소액 체결 또는 활발한 공개 지갑 관찰)
- [ ] Lighter/RISEx WS 끊김 시 REST fallback 로그 확인, 복구 시 WS 재구독
- [ ] Aster privacy on 계정 fixture로 `PRIVACY_ON` 1회만
- [ ] 베뉴별 한도 초과 0회 (24시간 로그)
- [ ] `docs/API_NOTES.md` 에 [?] 항목 전부 확인 결과 기록

---

## 7. Phase 3: 연관 지갑 탐색 (`/related`)

목적: 요구사항 2. HL 네이티브 primitive만으로 1차, 온체인(Arbitrum)은 선택.

### 7.1 데이터 소스와 비용 (HL) [V]

| 소스 | 호출 | 주는 것 | weight | 신뢰 |
|---|---|---|---|---|
| `subAccounts` | `{type, user: master}` | `[{name, subAccountUser, master, ...}]`. master일 때만 | 20 | confirmed |
| `userRole` | `{type, user}` | `{role: user|agent|vault|subAccount|missing, data:{master?|user?}}` | **60** (DB 캐시 7일) | confirmed |
| `webData2.agentAddress`, `extraAgents` | | API 지갑 주소 (트레이딩 지갑 아님, 식별용) | 20 | confirmed (type=agent) |
| `userFees.stakingLink` | | `{type:"tradingUser", stakingUser}` 명시 링크 | 20 | confirmed |
| ledger counterparties | Phase 1에서 이미 수집 중 (`events.meta.counterparty`) + 최초 1회 전체 히스토리 `userNonFundingLedgerUpdates(startTime=0)` | `internalTransfer/spotTransfer/send/subAccountTransfer` 의 `user`/`destination` | 20 (페이지당) | likely / weak |
| `referral` | `{type, user}` | `referredBy.referrer`, `referrerState.data.referralStates[].user` | 20 | weak |
| `userVaultEquities` | | 팔로우 중인 vault 주소 | 20 | weak |
| `vaultDetails` | `{type, vaultAddress}` | 본인이 vault면 `leader`, `followers[]`, `relationship.data.childAddresses` | 20 | leader: confirmed, followers: weak |
| 크로스 베뉴 | Phase 2 어댑터 `resolve()` | 같은 EVM 주소의 Lighter 서브계정 index들, RISEx/Aster 활동 여부 | 베뉴별 | confirmed (동일 주소) |
| hypurrscan API | `GET api.hypurrscan.io/tags/{addr}`, `/transfers/{from}/{to}`, `/bridges/{from}/{to}` | 주소 태그(거래소, 알려진 엔티티), 이체 | 1000/분/IP [V] | 태그: confirmed |

Arbitrum 브릿지 (선택, `ARBISCAN_API_KEY` 있을 때만): 레거시 브릿지 `0x2df1c51e09aecf9cacb7bc98cb1742757f163df7` 로 가는 USDC transfer의 `from` = HL 계정 [V]. 같은 EOA가 여러 번 입금한 경우는 자기 자신이라 의미 없음. 의미 있는 건 "A가 Arbitrum에서 B에게 USDC 보냈고 B가 브릿지 입금" 패턴인데, 현재 입금은 CCTP/Across 경유가 많아 커버리지가 낮음 [V 문서가 레거시 브릿지 deprecated 명시]. **Phase 3 범위에서 제외, 백로그.**

### 7.2 link_type 과 confidence

| link_type | confidence | 조건 |
|---|---|---|
| `subaccount` / `master` | confirmed | subAccounts, userRole |
| `agent` | confirmed | agentAddress, extraAgents (표시는 "API wallet"으로 구분) |
| `staking_link` | confirmed | userFees.stakingLink |
| `vault_leader` | confirmed | 추적 주소가 vault이고 leader가 다른 주소, 또는 추적 주소가 어떤 vault의 leader |
| `same_address_other_venue` | confirmed | 어댑터 resolve |
| `transfer_counterparty` | likely | 양방향 이체가 있거나, 단방향 2회 이상, 또는 1회라도 $10k 이상. 거래소 태그(hypurrscan tags) 붙은 주소는 제외 |
| `transfer_counterparty` | weak | 단방향 1회, $10k 미만 |
| `referral` | weak | referredBy 또는 referralStates |
| `vault_follow` | weak | userVaultEquities, vaultDetails.followers |

거래소 입출금 주소(hypurrscan tags 에 exchange 류 태그)는 결과에서 제외하되 "거래소 N곳 사용" 으로 한 줄 요약.

### 7.3 UX

- `/related <alias>`: 최초 호출 시 전체 탐색 (10~20초 소요, "탐색 중..." 메시지 후 edit). 결과는 `wallet_links` 에 저장, 24시간 캐시. `/related <alias> refresh` 로 강제 재탐색.
- 출력: confidence 별 그룹, 각 행에 주소 축약 + explorer 링크 + 근거 요약 (예: "내부이체 3회 $42k, 최근 2026-09-28") + 그 주소의 HL accountValue (clearinghouseState, weight 2) + 인라인 버튼 `[Track as <alias>-2]`. 버튼 누르면 `/add` 와 동일 플로우.
- 깊이: 1-hop만. confirmed 링크(subaccount, master)에 한해 2-hop 자동 확장 (master의 다른 subaccount들).
- 백그라운드: Phase 1 ledger 이벤트에서 새 counterparty가 보이면 `wallet_links` 에 weak로 자동 추가. 유저 알림은 안 함 (설정으로 on 가능: "새 연관 지갑 발견 시 알림").

### 7.4 완료 기준

- [ ] 알려진 master/sub 쌍 (오너가 제공, 또는 공개 vault leader) 으로 confirmed 링크 검출
- [ ] 탐색 1회 총 weight 300 이하 (로그 출력). `userRole` 캐시 적중 확인
- [ ] `/related` 결과의 `[Track]` 버튼으로 추가 → `/list` 반영
- [ ] 거래소 태그 주소가 결과에 안 뜨고 요약 줄에만 나옴

---

## 8. Phase 4: 스파이크, Extended와 Variational

오너 결정은 "5개 전부 시도". PM 의견: 두 베뉴는 공개 per-account API가 없어서 확정 범위로 넣으면 안 되고, **타임박스 스파이크**로 넣는다. 각 스파이크는 2일 한도, 아래 exit 조건 중 하나라도 못 넘으면 백로그로 보낸다.

### 8.1 Extended (Starknet) [V]

- 사실: 모든 `/user/*` 는 `X-Api-Key` 필요, 본인 서브계정만 [V, 401 확인]. 리더보드는 UI에만. 퍼블릭은 마켓 데이터뿐.
- 온체인 경로: Extended 메인넷 Perpetuals 컨트랙트 `0x062da0780fae50d68cecaa5a051606dc21217ba290969b302db4dd99d2e9b470` (`/info/settings` 에서 확인) [V], StarkWare `starknet-perpetual` Cairo 클래스. 스펙상 `Trade` 이벤트에 `order_a_position_id`, `order_b_position_id` (키), `actual_amount_base/quote`; `Liquidate`, `Deleverage`, `Deposit(position_id, depositing_address)` 이벤트; view `get_position_assets(position_id)` [V 스펙, 라이브 미검증 [?]]. 즉 **position_id 를 알면 온체인으로 완전 추적 가능.**
- 막힌 곳: EVM 주소 → Starknet position_id 공개 매핑 없음. 포지션 소유자는 Stark public key.
- 스파이크 exit 조건:
  1. Starknet RPC(Alchemy/Blast 무료 티어)로 Perpetuals 컨트랙트 `Trade` 이벤트를 position_id 필터로 구독해서 체결 1건 디코딩 성공
  2. position_id → 현재 포지션 조회(`get_position_assets`) 성공
  3. **position_id 확보 경로 확정.** 옵션 (a) `/add extended:<position_id> <alias>` 수동 입력 (유저가 UI나 다른 경로로 알아낸 경우), (b) `Deposit` 이벤트의 `depositing_address` 로 역추적 (브릿지 경유면 실패) [?], (c) **오너가 StarkWare BD로서 Extended 팀에 address→position_id 조회 또는 read-only 파트너 API 가능 여부 확인.** (c)가 되면 난이도가 "쉬움"으로 바뀜. 이 확인은 스파이크 전에 오너가 먼저 진행.
- 1~3 통과 시 `venues/extended/` 어댑터를 온체인 이벤트 기반으로 구현. snapshot diff 방식, TWAP은 Extended 주문타입에 있으나 [V] 타인 것은 비공개.

### 8.2 Variational (Omni, Arbitrum) [V]

- 사실: 공개 API는 `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats` 하나. 문서가 "The trading API is still in development, and is not yet available to any users." 명시 [V]. WS/SDK 없음.
- 온체인: Settlement Pool Factory `0x0F820B9afC270d658a9fD7D16B1Bdc45b70f074C`, OLP Vault `0x74bbbb...f2cd` (Arbitrum) [V]. 유저 ↔ OLP 양자 settlement pool 구조이나 포지션 데이터 레이아웃/이벤트 ABI 미공개.
- 스파이크 exit 조건:
  1. Arbiscan에서 Factory가 생성한 settlement pool 컨트랙트가 verified 소스인지 확인. 미verified면 **즉시 중단** (리버싱은 범위 밖)
  2. verified면 pool 이벤트에서 (user, market, size 변화) 를 뽑을 수 있는지 1건 확인
  3. 유저 EVM 주소 → pool 주소 매핑이 Factory 이벤트로 가능한지 확인
- PM 권고: 1에서 막힐 확률 높음. 오너가 Variational 팀 컨택이 있으면 API 출시 일정 먼저 확인.

### 8.3 완료 기준

- [ ] 각 스파이크 결과를 `docs/SPIKE_EXTENDED.md`, `docs/SPIKE_VARIATIONAL.md` 에 기록: 시도한 것, 막힌 곳, go/no-go, go면 공수 추정
- [ ] go 판정 베뉴는 별도 Phase 5로 어댑터 구현

---

## 9. 커맨드와 UX

### 9.1 `set_my_commands` 목록 (description은 256자 이하 [V], 영어 기본)

| command | description |
|---|---|
| `add` | Track a wallet: /add 0x... alias |
| `remove` | Stop tracking: /remove alias |
| `list` | Your tracked wallets with account value |
| `positions` | Open positions: /positions alias (no alias = all) |
| `twap` | Active TWAPs: /twap [alias] |
| `recent` | Recent events: /recent alias [n] |
| `related` | Find linked wallets: /related alias |
| `stats` | PnL and volume: /stats alias |
| `settings` | Notification settings: /settings alias |
| `mute` | Mute alerts: /mute alias [1h/1d] |
| `unmute` | Unmute alerts: /unmute alias |
| `rename` | Rename alias: /rename old new |
| `rescan` | Re-detect venues for a wallet: /rescan alias |
| `help` | Commands and examples |

인자 없이 치면 각 커맨드가 usage 한 줄 + 예시 1개로 응답. alias 매칭은 대소문자 무시. alias 못 찾으면 가장 비슷한 alias 1개 제안 ("Did you mean `whale1`?"). `/health` 는 admin 전용이라 메뉴에 넣지 않음.

### 9.2 `/help`

커맨드 그룹별 한 줄씩. 끝에 알림 종류 설명 (어떤 이벤트가 오는지, TWAP은 시작/종료만). 200단어 이내.

### 9.2a `/twap [alias]`

`twap_active` 기준 활성 TWAP 목록. 행당: 베뉴 뱃지, alias, 방향, coin, 총 사이즈(USD), 진행률 `executedSz/sz`, 시작 시각, 예상 종료 시각. alias 없으면 모든 추적 지갑. 없으면 "No active TWAPs". HL만 해당 (다른 베뉴는 TWAP 상태 비공개).

### 9.3 `/positions`

- `/positions <alias>`: 베뉴별 섹션. 각 포지션: 방향, coin, 사이즈(USD), 평균진입, 마크, uPnL, 레버리지, 청산가 (베뉴가 주면). 활성 TWAP 있으면 포지션 아래 "⏳ TWAP: 45% (ends ~14:32)" 한 줄. 계정 가치 합계.
- `/positions` (인자 없음): 모든 추적 지갑 요약 1줄씩 (alias, 계정가치, 포지션 수, 가장 큰 포지션). 지갑 20개 넘으면 페이지 버튼.
- 응답 1건에 4096자 넘으면 분할.

### 9.4 `/settings <alias>` (인라인 키보드 토글)

`subscriptions.settings_json` 기본값:

```json
{
  "venues": {"hyperliquid": true, "lighter": true, "risex": true, "aster": true},
  "events": {
    "position": true, "liquidation": true, "twap": true,
    "spot": true, "transfer": true, "deposit_withdraw": true,
    "vault": false, "account_class_transfer": false, "dex_collateral": false
  },
  "min_notional_usd": 0,
  "debounce_sec": 60,
  "twap_progress": false,
  "algo_window_sec": 300,
  "algo_min_fills": 8,
  "algo_max_slice_pct": 2,
  "algo_progress_sec": 600,
  "algo_idle_sec": 600
}
```

`min_notional_usd` 는 포지션/spot 이벤트의 `notional_usd` 가 그 미만이면 알림 skip (events에는 기록). 버튼으로 0 / 1k / 10k / 100k 선택. 전역 기본값 변경은 `/settings default`.

### 9.5 `/mute <alias> [duration]`

`1h`, `6h`, `1d`, `7d`, 없으면 무기한. `/list` 에 🔇 표시.

### 9.6 `/recent <alias> [n]`

최근 n건(기본 10, 최대 30) 이벤트를 시간순. suppressed(TWAP 억제)와 필터된 것도 흐리게 표시. 유저가 "왜 알림 안 왔지" 를 스스로 확인하는 용도.

### 9.7 에러 메시지

API 실패는 "Hyperliquid API error, retrying" 류로 베뉴 이름 포함. 내부 예외는 유저에게 "Something went wrong (id: abc123)" + 로그에 같은 id. 매번 "Please try again later" 금지.

---

## 10. 알림 메시지 포맷 (HTML)

원칙: 한 줄 헤더에 모든 핵심 숫자. 두 번째 줄부터 부가 정보. 베뉴 뱃지 접두사. 주소는 explorer 링크, alias는 굵게.

```
[HL] 📈 <b>whale1</b> opened LONG $BTC
$1.25M (14.5 BTC) @ 86,281 · 10x · liq 78,400
```

```
[HL] ➕ <b>whale1</b> added to LONG $ETH
+$320k (102 ETH) @ 3,140 · now $1.1M · avg 3,085
```

```
[HL] 🔒 <b>whale1</b> closed SHORT $SOL
$540k · realized 🟢 +$18,420 · held 2d 4h
```

```
[HL] 🔥 <b>whale1</b> LIQUIDATED LONG $DOGE
$210k · account value after $12,300
```

```
[HL] ⏳ <b>whale1</b> started TWAP BUY $HYPE
$2.0M over 120 min · ends ~14:32 KST
```

```
[HL] ✅ <b>whale1</b> TWAP done BUY $HYPE
filled $1.98M (41,200 HYPE) avg 48.06 · 118 min · status finished
```

```
[LTR] 📉 <b>whale1</b>#2 opened SHORT $ETH
$450k @ 3,120 · 5x
```

```
[HL] ↘️ <b>whale1</b> received $500k USDC from 0x1a2b…9f3e
```

```
[HL] 🤖 <b>loracle</b> algo accumulating LONG $BTC
12 fills +$41k in 5m · pos $35.9M avg 86,188
```

```
[HL] ✅ <b>loracle</b> algo done accumulating LONG $BTC
+$1.2M (14.2 BTC) avg 86,040 · 412 fills · 58m
```

규칙:
- 베뉴 뱃지: `[HL]`, `[LTR]`, `[RISE]`, `[ASTER]`, `[EXT]`, `[VAR]`. Lighter 서브계정은 alias 뒤 `#index`.
- 금액: $1.2k / $45k / $1.25M 식 축약. 수량은 유효숫자 3~4자리. 가격은 코인별 tick에 맞춰 (HL `szDecimals`, `meta` 캐시).
- 시간: KST 기본 (`users.lang` 과 별도로 `tz` 설정, 기본 Asia/Seoul).
- 기간: 사람이 읽는 단위로 축약. 10080 min → `7d`, 8302 min → `5d 18h`, 90 min → `1h 30m`, 5 min → `5m` (큰 단위 2개까지, 0 인 단위는 생략).
- held 시간: `POSITION_CLOSE` 시 그 coin의 마지막 `POSITION_OPEN` 이벤트 ts 와 차이. events 테이블에서 조회.
- 메시지 edit 누적 시 헤더의 금액을 갱신하고 끝에 "(3 fills)" 추가.
- 같은 유저에게 1초에 1건 이상 보내지 않음 (Telegram 30msg/s 전역, 유저당 1msg/s). 큐잉.

---

## 11. 운영

- 로깅: 구조화 (JSON 한 줄). 분당 베뉴별 weight 사용량, 429 횟수, 이벤트 수, 전송 실패 수.
- `/health` (ADMIN_USER_IDS 환경변수에 있는 유저만): 베뉴별 마지막 폴링 시각, 활성 계정 수, 큐 길이, 최근 1시간 429, WS 연결 상태, DB 크기.
- 전송 실패 (유저가 봇 차단 등 `Forbidden`): 그 유저 subscriptions 전부 `muted_until = 무기한` 처리 + 로그. 재시도 안 함.
- 환경변수: `BOT_TOKEN`, `DATABASE_PATH`, `ADMIN_USER_IDS`, `LOG_LEVEL`, `POLL_FAST_SEC`, `POLL_LEDGER_SEC`, `HL_WEIGHT_BUDGET` (기본 1020), `ARBISCAN_API_KEY` (선택), `STARKNET_RPC_URL` (Phase 4).
- Fly.io: 리전 `nrt`. Volume `hypermate_data` 를 `/data` 에 마운트. `Dockerfile` 의 `CMD ["python", "-m", "hypermate.main"]`. `[http_service]`/`[[services]]` 없음 (worker, 포트 안 엶, 머신 상시 가동). `kill_signal = "SIGINT"`, `kill_timeout = 30` (PTB graceful shutdown). 머신은 반드시 1대 (`fly scale count 1`, SQLite 볼륨은 머신 간 공유 불가). `BOT_TOKEN` 은 `fly secrets`.
- 백업: 매일 1회 `hypermate.db` 를 `/data/backups/` 에 복사, 7일 보관 (`sqlite3 .backup` API 사용, 파일 복사 금지).

---

## 12. 백로그 (이번 범위 밖)

- HL WS 하이브리드: priority 지갑 ≤10개 WS 구독 (userFills, twapStates, userNonFundingLedgerUpdates), 나머지 REST
- Lighter / Aster TWAP 휴리스틱 감지 (등간격, 등사이즈 체결 패턴)
- Arbitrum 브릿지 / CCTP 입금 소스 분석 (`ARBISCAN_API_KEY`)
- hypurrscan `/twap/{address}` 로 TWAP 교차 검증
- 그룹 채팅 지원 (현재 private chat만)
- 한국어 메시지 (`users.lang`)
- 일간 요약 (추적 지갑들의 24h 순변화)
- 포지션 크기 상위 N 지갑 리더보드

---

## 13. 오너 확인 필요 (구현 전)

1. Extended: StarkWare 쪽에서 Extended 팀에 address→position_id 조회나 read-only 파트너 API를 요청할 수 있는지. 되면 Phase 4가 쉬워짐.
2. Variational: 팀 컨택 유무, API 출시 일정.
3. 알림 언어: 영어 유지 (지인 포함 유저 구성 때문) vs 한국어. 기본값 결정.
4. `min_notional_usd` 전역 기본값: 0 (전부) vs 1k. 지갑이 50개면 0은 시끄러울 수 있음.
5. spot / transfer 알림 기본 on 유지 여부.
6. Fly.io Volume (`hypermate_data`, `/data`) 생성/마운트 상태. 안 돼있으면 Phase 0에서 오너가 직접 생성 (`fly volumes create`, 코드로 못 함).
7. 테스트용 지갑: 각 베뉴에서 소액으로 체결 테스트 가능한 본인 지갑이 있는지. 없으면 활발한 공개 지갑 관찰로 대체 (시간 더 걸림).
8. 알림 받는 유저 수 (지인 몇 명). Telegram 전송 큐 설계에는 영향 없지만 `/settings default` 를 유저별로 둘지 전역으로 둘지 결정.

---

## 부록 A. Phase별 작업 순서와 예상 세션

| Phase | 내용 | Claude Code 세션 예상 | 의존 |
|---|---|---|---|
| 0 | 안정화, 구조 분리, 커맨드 메뉴 | 1 | 없음 |
| 1 | HL 이벤트 엔진, TWAP, 스케줄러 | 2 | 0 |
| 2 | Lighter, RISEx, Aster 어댑터 | 2 (베뉴당 ~0.7) | 1 |
| 3 | `/related` | 1 | 1 (ledger counterparty), 2 (크로스 베뉴) |
| 4 | Extended / Variational 스파이크 | 각 1 (타임박스) | 섹션 13-1, 13-2 오너 확인. 코드 의존은 1 (어댑터 인터페이스) |

Phase 0 → 1 은 순서 고정. 2와 3은 병렬 가능 (3의 크로스 베뉴 부분만 2 이후). 4는 온체인 독립이라 1 이후 아무 때나.

## 부록 B. 참고 URL

- HL info: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint (`.md` 붙이면 raw)
- HL rate limits: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- HL WS subscriptions: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- HL order types (TWAP): https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types
- HL python SDK info.py: https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/info.py
- 커뮤니티 타입 정의 (가장 완전): npm `@nktkas/hyperliquid`
- hypurrscan API: https://api.hypurrscan.io/openapi.json
- Lighter: https://apidocs.lighter.xyz/docs/rate-limits , https://apidocs.lighter.xyz/docs/websocket-reference , https://github.com/elliottech/lighter-python
- RISEx: https://developer.rise.trade/reference/general-information , https://docs.risechain.com/docs/risex/api
- Aster RPC: https://github.com/asterdex/api-docs/blob/master/RPC/aster-chain-rpc.md
- Extended: https://api.docs.extended.exchange/ , https://github.com/starkware-libs/starknet-perpetual/blob/main/docs/spec.md
- Variational: https://docs.variational.io/technical-documentation/api.md
- Telegram setMyCommands: https://core.telegram.org/bots/api#setmycommands
- PTB post_init: https://docs.python-telegram-bot.org/en/stable/telegram.ext.applicationbuilder.html
