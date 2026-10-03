-- HyperMate schema, spec section 3.4. Applied on every startup, so statements are idempotent.
-- Columns added after a table was first deployed are also added by Repo.connect() (ALTER TABLE),
-- because CREATE TABLE IF NOT EXISTS does not touch existing tables.

CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,               -- Telegram user id
  lang TEXT DEFAULT 'en',
  tz TEXT DEFAULT 'Asia/Seoul',
  created_at INTEGER
);

CREATE TABLE IF NOT EXISTS wallets (                        -- EVM 주소 단위, 유저와 무관
  wallet_id INTEGER PRIMARY KEY,
  evm_address TEXT UNIQUE NOT NULL            -- lowercase
);

CREATE TABLE IF NOT EXISTS venue_accounts (                 -- 한 지갑이 여러 베뉴/서브계정을 가질 수 있음
  venue_account_id INTEGER PRIMARY KEY,
  wallet_id INTEGER NOT NULL REFERENCES wallets,
  venue TEXT NOT NULL,
  account_ref TEXT NOT NULL,                  -- HL: address, Lighter: account_index, RISEx/Aster: address, Extended: position_id
  active INTEGER DEFAULT 1,                   -- resolve 결과 활동 없으면 0 (폴링 제외)
  last_activity_ms INTEGER,                   -- 티어 폴링용
  dexs_json TEXT NOT NULL DEFAULT '[]',       -- HL: HIP-3 dexs with activity (main dex is always polled)
  UNIQUE(venue, account_ref)
);

CREATE TABLE IF NOT EXISTS subscriptions (                  -- 유저 x 지갑
  user_id INTEGER REFERENCES users,
  wallet_id INTEGER REFERENCES wallets,
  alias TEXT NOT NULL,
  settings_json TEXT DEFAULT '{}',            -- 섹션 9.4
  muted_until_ms INTEGER,
  created_at INTEGER,
  PRIMARY KEY (user_id, wallet_id),
  UNIQUE (user_id, alias COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS cursors (
  venue_account_id INTEGER REFERENCES venue_accounts,
  kind TEXT NOT NULL,                          -- 'fills' | 'ledger' | 'twap' | 'trades'
  cursor TEXT NOT NULL,                        -- ms timestamp 또는 베뉴별 cursor 문자열
  updated_at INTEGER,
  PRIMARY KEY (venue_account_id, kind)
);

CREATE TABLE IF NOT EXISTS snapshots (
  venue_account_id INTEGER PRIMARY KEY REFERENCES venue_accounts,
  positions_json TEXT NOT NULL,                -- HL: {dex: {coin: {szi, entry_px, position_value, ...}}}, main dex key ""
  account_value TEXT,                          -- Decimal string (marginSummary.accountValue)
  spot_json TEXT,                              -- HL: {coin: total} spot balances, for change detection (spec 3.5)
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS twap_active (
  venue_account_id INTEGER REFERENCES venue_accounts,
  twap_id TEXT NOT NULL,
  state_json TEXT NOT NULL,
  started_ms INTEGER,
  PRIMARY KEY (venue_account_id, twap_id)
);

CREATE TABLE IF NOT EXISTS algo_active (     -- synthetic TWAP (external execution bot) tracking, spec 5.2
  venue_account_id INTEGER REFERENCES venue_accounts,
  coin TEXT NOT NULL,
  sign INTEGER NOT NULL,                      -- +1: Open Long / Close Short, -1: Open Short / Close Long
  started_ms INTEGER,
  last_fill_ms INTEGER,
  fills_count INTEGER,
  total_sz TEXT,                              -- Decimal string
  total_ntl TEXT,                             -- Decimal string
  PRIMARY KEY (venue_account_id, coin, sign)
);

CREATE TABLE IF NOT EXISTS multi_algo_mode (   -- summary mode when many algos run on one account (spec 5.2)
  venue_account_id INTEGER PRIMARY KEY REFERENCES venue_accounts,
  entered_ms INTEGER NOT NULL,
  event_id INTEGER,                           -- the MULTI_ALGO_ENTER event (its sent_messages are edited)
  message_ids_json TEXT NOT NULL DEFAULT '{}', -- {user_id: [chat_id, message_id]} mirror of sent_messages
  last_update_ms INTEGER,
  below_since_ms INTEGER                      -- active algos <= multi_algo_exit since (NULL: above)
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY,
  dedupe_key TEXT UNIQUE NOT NULL,
  venue_account_id INTEGER REFERENCES venue_accounts,
  type TEXT NOT NULL,
  ts_ms INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  delivery TEXT NOT NULL DEFAULT 'sent',       -- 'sent' | 'summarized' | 'filtered_threshold' | 'filtered_settings' | 'muted'
  created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_account_ts ON events(venue_account_id, ts_ms);

CREATE TABLE IF NOT EXISTS sent_messages (                  -- TWAP 진행 메시지 edit용, /recent 용
  event_id INTEGER REFERENCES events,
  user_id INTEGER,
  chat_id INTEGER,
  message_id INTEGER,
  PRIMARY KEY (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS wallet_links (                   -- Phase 3
  wallet_id INTEGER REFERENCES wallets,
  related_address TEXT NOT NULL,
  related_venue TEXT,
  link_type TEXT NOT NULL,                     -- 섹션 7.2
  confidence TEXT NOT NULL,                    -- 'confirmed' | 'likely' | 'weak'
  evidence_json TEXT NOT NULL,
  discovered_at INTEGER,
  PRIMARY KEY (wallet_id, related_address, link_type)
);

CREATE TABLE IF NOT EXISTS api_cache (                      -- userRole(weight 60) 등 비싼 호출 캐시
  cache_key TEXT PRIMARY KEY,
  value_json TEXT,
  expires_at INTEGER
);
