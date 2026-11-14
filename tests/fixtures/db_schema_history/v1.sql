-- 스키마 세대 1 의 새 DB DDL(직전 tests/fixtures/db_schema.json 에서).
CREATE TABLE corp_action_refs (
                        code TEXT PRIMARY KEY,
                        ref_date TEXT,
                        ref_close REAL DEFAULT 0.0,
                        source TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

CREATE TABLE daily_asset_history (
                        date TEXT,
                        account TEXT,
                        asset REAL, net_transfer REAL DEFAULT 0, principal REAL,
                        PRIMARY KEY (date, account)
                    );

CREATE TABLE fill_latency (
                        date TEXT,
                        odno TEXT,
                        broker TEXT,                 -- 'toss' | 'kis'
                        code TEXT,
                        side TEXT,
                        ws_event_at REAL,            -- 그 주문의 첫 WS 이벤트(epoch). NULL = WS가 안 알림
                        recognized_at REAL NOT NULL, -- 감시기가 체결을 알아챈 순간(epoch)
                        woke_by_ws INTEGER DEFAULT 0,-- 이 주기가 WS 통보로 깨워졌는가
                        poll_interval REAL,          -- 그 순간의 폴링 주기(초) — WS 없을 때의 최대 대기
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (date, odno, broker)
                    );

CREATE TABLE half_tp_status (
                        code TEXT PRIMARY KEY,
                        update_time TEXT
                    );

CREATE TABLE journal_opening (
                        account TEXT PRIMARY KEY,
                        as_of TEXT,
                        sent_at TEXT,
                        count INTEGER DEFAULT 0
                    );

CREATE TABLE journal_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        exec_id TEXT UNIQUE,
                        payload TEXT,
                        created_at TEXT,
                        attempts INTEGER DEFAULT 0,
                        last_attempt_at TEXT,
                        last_error TEXT,
                        synced_at TEXT,
                        remote_id TEXT,
                        dead_at TEXT,
                        reject_count INTEGER DEFAULT 0,
                        is_backlog INTEGER DEFAULT 0,
                        needs_patch INTEGER DEFAULT 0
                    );

CREATE TABLE notified_disclosures (
                        rcept_no TEXT PRIMARY KEY,
                        notified_at TEXT
                    );

CREATE TABLE reserved_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cano TEXT, acnt TEXT, market TEXT,
                        order_type TEXT, code TEXT, name TEXT,
                        qty INTEGER, order_price REAL,
                        condition_type TEXT, target_price REAL, target_time TEXT,
                        status TEXT DEFAULT 'PENDING', odno TEXT,
                        fail_reason TEXT,
                        expire_dt TEXT,
                        lowest_price REAL DEFAULT 0.0,
                        highest_price REAL DEFAULT 0.0,
                        composite_json TEXT,
                        triggered_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

CREATE TABLE score_boundary_ledger (
                        date TEXT,
                        code TEXT,
                        is_sim INTEGER DEFAULT 0,
                        name TEXT,
                        cycles INTEGER DEFAULT 0,          -- 이 표에 기록된(사건이 있던) 주기 수
                        near_cycles INTEGER DEFAULT 0,     -- 비율 경계(0.8배) ±0.1 안
                        flips INTEGER DEFAULT 0,           -- 직전 관측 대비 확산 조건이 뒤집힌 횟수
                        admitted_cycles INTEGER DEFAULT 0, -- 확산 덕에 BUY_SCORE 를 넘긴 주기
                        excluded_cycles INTEGER DEFAULT 0, -- 확산 실패로 BUY_SCORE 미달인 주기
                        cf_gained_buy INTEGER DEFAULT 0,   -- 반사실 상태가 매수(실제는 아님) = 경계가 막음
                        cf_lost_buy INTEGER DEFAULT 0,     -- 반사실 상태가 매수 아님(실제는 매수) = 경계가 들임
                        fail_open_cycles INTEGER DEFAULT 0,-- 가격 모멘텀 단기 게이트 미확인 가점
                        max_unknown INTEGER DEFAULT 0,     -- 확인 불가 항목 수의 최댓값
                        min_ratio REAL,
                        max_ratio REAL,
                        max_score REAL DEFAULT 0.0,
                        last_state TEXT,
                        rules_version TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (date, code, is_sim)
                    );

CREATE TABLE signal_ledger (
                        date TEXT,
                        code TEXT,
                        -- 실전/모의는 같은 DB 파일을 쓰고 trades는 is_sim으로 가른다.
                        --  원장도 같이 갈라야 한다 — 재진입 차단·상관 차단은 그 계좌의
                        --  보유 상태에 따라 달라지므로, 섞으면 차단율이 무엇을 뜻하는지
                        --  알 수 없게 된다(관찰모드는 DB 파일 자체가 분리된다).
                        is_sim INTEGER DEFAULT 0,
                        name TEXT,
                        cycles INTEGER DEFAULT 0,
                        passed INTEGER DEFAULT 0,
                        blocked_vol INTEGER DEFAULT 0,
                        blocked_abr INTEGER DEFAULT 0,
                        blocked_hold INTEGER DEFAULT 0,
                        blocked_corr INTEGER DEFAULT 0,
                        blocked_rs INTEGER DEFAULT 0,
                        blocked_tq INTEGER DEFAULT 0,
                        blocked_reentry INTEGER DEFAULT 0,
                        blocked_other INTEGER DEFAULT 0,
                        -- [계좌 상태] 게이트는 통과했는데 계좌가 못 사게 한 주기.
                        --  passed 와 함께 오른다(신호는 섰고, 막은 것은 계좌다).
                        --  이 둘이 없던 동안 원장은 '왜 안 샀나'를 답하지 못했다 — 실제로
                        --  2026-08-27~28 가상투자에서 대한항공이 287주기 내내 passed 인데
                        --  매수는 0건이었고(슬롯 4/4 만석), 그 사유는 로그에만 있었다.
                        --  로그 파싱은 원장을 만든 이유 자체이므로 되돌아갈 수 없다.
                        blocked_slot INTEGER DEFAULT 0,
                        blocked_cash INTEGER DEFAULT 0,
                        -- [시장 필터] 종목 판정 **자체를 못 한** 주기. 위 게이트들과 성격이
                        --  다르다 — 나머지는 '분석해 보니 미달'이지만 이것은 시장 필터가
                        --  분석 전에 잘라, 그 종목이 신호였는지 알 수 없다는 뜻이다.
                        --  그래서 별도 칸에 둔다(차단율의 분자로 섞으면 안 된다).
                        --  이 칸이 없던 동안 필터를 켠 계좌의 원장은 통째로 비어 있었다
                        --  (실전 trade_history.db: 2026-08-19 신설 후 2026-09-08까지 0행).
                        blocked_market INTEGER DEFAULT 0,
                        max_score REAL DEFAULT 0.0,
                        max_vol REAL,
                        min_abr REAL,
                        last_state TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (date, code, is_sim)
                    );

CREATE TABLE stock_strategies (
                        code TEXT PRIMARY KEY,
                        name TEXT,
                        buy_score REAL,
                        buy_rsi REAL,
                        buy_vol_strength REAL,
                        sell_score REAL,
                        stop_loss REAL,
                        take_profit REAL,
                        take_profit_rsi REAL,
                        ts_activation REAL,
                        ts_callback REAL,
                        updated_at TEXT,
                        memo TEXT,
                        weights TEXT,
                        invest_ratio REAL,
                        time_stop_days INTEGER,
                        use_atr_stop INTEGER,
                        atr_stop_multiplier REAL,
                        half_take_profit_use INTEGER,
                        buy_ask_bid_ratio REAL,
                        auto_adjust_ask_bid_ratio INTEGER
                    );

CREATE TABLE trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        time TEXT,
                        type TEXT,
                        code TEXT,
                        name TEXT,
                        qty TEXT,
                        price TEXT,
                        odno TEXT,
                        org_odno TEXT,
                        account TEXT,
                        is_sim INTEGER,
                        snapshot TEXT
                    , profit_amt INTEGER DEFAULT 0, profit_rate REAL DEFAULT 0.0, reason TEXT, strategy_score REAL DEFAULT 0, order_status TEXT DEFAULT '접수', stop_loss_rate REAL DEFAULT 0.0, buy_price REAL DEFAULT 0.0, cost_amt REAL DEFAULT 0.0);

CREATE TABLE trailing_stops (
                        code TEXT PRIMARY KEY,
                        highest_price REAL,
                        update_time TEXT,
                        ref_avg_price REAL DEFAULT 0.0,
                        ref_pchs_amt REAL DEFAULT 0.0,
                        pyramid_count INTEGER DEFAULT 0
                    );

CREATE INDEX idx_journal_outbox_queue_v2 ON journal_outbox(synced_at, dead_at, is_backlog, id);

CREATE INDEX idx_trades_code ON trades(code);

