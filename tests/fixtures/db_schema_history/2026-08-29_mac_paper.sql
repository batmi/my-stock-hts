-- 실제 옛 스키마: 맥 가상 paper_trading.db 백업(2026-08-29) 의 DDL(sqlite_master). 데이터는 없다.
-- tests/test_db_schema.py 가 이 스키마로 만든 파일을 지금 코드로 열어 새 DB 와 같은 열이 되는지 본다.
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
                        asset REAL,
                        PRIMARY KEY (date, account)
                    );

CREATE TABLE half_tp_status (
                        code TEXT PRIMARY KEY,
                        update_time TEXT
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
                        is_backlog INTEGER DEFAULT 0
                    );

CREATE TABLE notified_disclosures (
                        rcept_no TEXT PRIMARY KEY,
                        notified_at TEXT
                    );

CREATE TABLE paper_equity (
                       date TEXT PRIMARY KEY, cash REAL, stock_value REAL, total REAL);

CREATE TABLE paper_fills (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, type TEXT,
                       code TEXT, name TEXT, qty INTEGER, price REAL,
                       amount REAL, fee REAL, profit_amt REAL, profit_rate REAL,
                       odno TEXT);

CREATE TABLE paper_positions (
                       code TEXT PRIMARY KEY, name TEXT, qty INTEGER,
                       avg_price REAL, first_buy_at TEXT, last_buy_at TEXT);

CREATE TABLE paper_state (
                       key TEXT PRIMARY KEY, value TEXT);

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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

CREATE TABLE signal_ledger (
                            date TEXT, code TEXT, is_sim INTEGER DEFAULT 0, name TEXT,
                            cycles INTEGER DEFAULT 0, passed INTEGER DEFAULT 0,
                            blocked_vol INTEGER DEFAULT 0, blocked_abr INTEGER DEFAULT 0,
                            blocked_hold INTEGER DEFAULT 0, blocked_corr INTEGER DEFAULT 0,
                            blocked_rs INTEGER DEFAULT 0, blocked_tq INTEGER DEFAULT 0,
                            blocked_reentry INTEGER DEFAULT 0, blocked_other INTEGER DEFAULT 0,
                            max_score REAL DEFAULT 0.0, max_vol REAL, min_abr REAL,
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
                        half_take_profit_use INTEGER
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
                    , profit_amt INTEGER DEFAULT 0, profit_rate REAL DEFAULT 0.0, reason TEXT, strategy_score REAL DEFAULT 0, order_status TEXT DEFAULT '접수', stop_loss_rate REAL DEFAULT 0.0, buy_price REAL DEFAULT 0.0);

CREATE TABLE trailing_stops (
                        code TEXT PRIMARY KEY,
                        highest_price REAL,
                        update_time TEXT
                    , ref_avg_price REAL DEFAULT 0.0, ref_pchs_amt REAL DEFAULT 0.0, pyramid_count INTEGER DEFAULT 0);

CREATE INDEX idx_journal_outbox_queue_v2 ON journal_outbox(synced_at, dead_at, is_backlog, id);

CREATE INDEX idx_trades_code ON trades(code);

