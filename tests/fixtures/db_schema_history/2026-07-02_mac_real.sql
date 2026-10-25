-- 실제 옛 스키마: 맥 실전 trade_history.db 백업(2026-07-02) 의 DDL(sqlite_master). 데이터는 없다.
-- tests/test_db_schema.py 가 이 스키마로 만든 파일을 지금 코드로 열어 새 DB 와 같은 열이 되는지 본다.
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

CREATE TABLE market_analysis_cache (
                    market_type TEXT PRIMARY KEY,
                    updated_at TEXT,
                    params TEXT,
                    data TEXT
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    , composite_json TEXT);

CREATE TABLE stock_memo_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT,
                    name TEXT,
                    memo TEXT,
                    updated_at TEXT
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

CREATE TABLE theme_analysis_cache (
                        key TEXT PRIMARY KEY,
                        updated_at TEXT,
                        data TEXT
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
                    , profit_amt INTEGER DEFAULT 0, profit_rate REAL DEFAULT 0.0, reason TEXT, strategy_score REAL DEFAULT 0, order_status TEXT DEFAULT '접수', stop_loss_rate REAL DEFAULT 0.0);

CREATE TABLE trailing_stops (
                        code TEXT PRIMARY KEY,
                        highest_price REAL,
                        update_time TEXT
                    );

