# modules/db_manager.py
import sqlite3
import json
import threading
import os
import time
from datetime import datetime
import config
import context
import atexit

class DBManager:
    def __init__(self):
        self.db_path = config.DB_FILE_PATH
        self.lock = threading.RLock() # 스레드 간 동기화를 위한 락 (RLock으로 변경)
        self.local = threading.local() # 스레드별 로컬 저장소
        self._init_db()

    def __del__(self):
        """객체 소멸 시 연결 종료"""
        try:
            if hasattr(self.local, 'conn') and self.local.conn:
                self.local.conn.close()
        except: pass

    def close_connection(self):
        """현재 스레드의 DB 연결을 명시적으로 종료"""
        try:
            if hasattr(self.local, 'conn') and self.local.conn:
                self.local.conn.close()
                self.local.conn = None
        except: pass

    def _get_conn(self):
        """스레드별 DB 연결 객체 반환 (없으면 생성)"""
        if not hasattr(self.local, 'conn') or self.local.conn is None:
            self.local.conn = sqlite3.connect(self.db_path, timeout=60)
            self.local.conn.execute("PRAGMA journal_mode=WAL;") # WAL 모드 설정
            self.local.conn.row_factory = sqlite3.Row
        return self.local.conn

    def _is_screen_output_allowed(self):
        return threading.current_thread().name != "TelegramBot"

    def _init_db(self):
        """DB 초기화 (테이블 생성 등) - 메인 스레드에서 한 번만 실행"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, timeout=60)
                conn.execute("PRAGMA journal_mode=WAL;")
                cursor = conn.cursor()
                
                # 거래 내역 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
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
                    )
                ''')
                
                # 트레일링 스탑 추적 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trailing_stops (
                        code TEXT PRIMARY KEY,
                        highest_price REAL,
                        update_time TEXT
                    )
                ''')
                
                # [추가] 종목별 개별 트레이딩 룰 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS stock_strategies (
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
                    )
                ''')
                
                # [Fix: Point 2] 반익절(Half TP) 상태 추적 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS half_tp_status (
                        code TEXT PRIMARY KEY,
                        update_time TEXT
                    )
                ''')
                
                # [추가] 일별 자산 스냅샷 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS daily_asset_history (
                        date TEXT,
                        account TEXT,
                        asset REAL,
                        PRIMARY KEY (date, account)
                    )
                ''')

                # [추가] 공시 알림 중복방지 테이블 (접수번호 기준)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS notified_disclosures (
                        rcept_no TEXT PRIMARY KEY,
                        notified_at TEXT
                    )
                ''')
                
                # [추가] 예약 주문 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS reserved_orders (
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
                    )
                ''')
                
                # 컬럼 확장 (마이그레이션)
                cursor.execute("PRAGMA table_info(trades)")
                columns = [info[1] for info in cursor.fetchall()]
                
                new_columns = {
                    "profit_amt": "INTEGER DEFAULT 0",
                    "profit_rate": "REAL DEFAULT 0.0",
                    "reason": "TEXT",
                    "strategy_score": "REAL DEFAULT 0",
                    "order_status": "TEXT DEFAULT '접수'",
                    "stop_loss_rate": "REAL DEFAULT 0.0"
                }
                
                for col, dtype in new_columns.items():
                    if col not in columns:
                        try:
                            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
                            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                                config.console.print(f"[dim green][DB] 컬럼 추가됨: {col}[/dim green]")
                        except Exception as e:
                            config.console.print(f"[red][DB] 컬럼 추가 실패({col}): {e}[/red]")

                # stock_strategies 테이블 컬럼 확장 (memo 추가)
                cursor.execute("PRAGMA table_info(stock_strategies)")
                strat_columns = [info[1] for info in cursor.fetchall()]
                
                new_strat_columns = {
                    "buy_vol_strength": "REAL", "memo": "TEXT", "weights": "TEXT",
                    "invest_ratio": "REAL", "time_stop_days": "INTEGER",
                    "use_atr_stop": "INTEGER", "atr_stop_multiplier": "REAL",
                    "half_take_profit_use": "INTEGER"
                }
                for col, dtype in new_strat_columns.items():
                    if col not in strat_columns:
                        try:
                            cursor.execute(f"ALTER TABLE stock_strategies ADD COLUMN {col} {dtype}")
                            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                                config.console.print(f"[dim green][DB] stock_strategies 테이블에 {col} 컬럼 추가 완료[/dim green]")
                        except Exception as e:
                            config.console.print(f"[red][DB] stock_strategies 컬럼 추가 실패({col}): {e}[/red]")

                # reserved_orders 테이블 컬럼 확장 (fail_reason 추가)
                cursor.execute("PRAGMA table_info(reserved_orders)")
                ro_columns = [info[1] for info in cursor.fetchall()]
                if "fail_reason" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN fail_reason TEXT")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 fail_reason 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(fail_reason): {e}[/red]")

                if "expire_dt" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN expire_dt TEXT")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 expire_dt 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(expire_dt): {e}[/red]")

                if "lowest_price" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN lowest_price REAL DEFAULT 0.0")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 lowest_price 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(lowest_price): {e}[/red]")

                if "highest_price" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN highest_price REAL DEFAULT 0.0")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 highest_price 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(highest_price): {e}[/red]")

                # [추가] 복합(AND) 조건 예약 주문용 컬럼 (서브 조건 리스트를 JSON으로 저장)
                if "composite_json" not in ro_columns:
                    try:
                        cursor.execute("ALTER TABLE reserved_orders ADD COLUMN composite_json TEXT")
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print("[dim green][DB] reserved_orders 테이블에 composite_json 컬럼 추가 완료[/dim green]")
                    except Exception as e:
                        config.console.print(f"[red][DB] reserved_orders 컬럼 추가 실패(composite_json): {e}[/red]")

                conn.commit()
                conn.close()
            except Exception as e:
                if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Init Error: {e}[/red]")

    def insert_trade(self, type_str, code, name, qty, price, odno, org_odno=None, snapshot=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, order_status="접수", custom_time=None, stop_loss_rate=0.0):
        """거래 내역 및 스냅샷 저장"""
        # 쓰기 작업은 락으로 보호하여 순차 처리 (SQLite 특성상 안전)
        with self.lock:
            for attempt in range(5):
                try:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim cyan][DB][{threading.get_ident()}] insert_trade 요청 ({attempt+1}/5): {type_str} {name}({code})[/dim cyan]")

                    conn = self._get_conn()
                    cursor = conn.cursor()
                    
                    now_str = custom_time if custom_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    acc_no = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                    if getattr(context.trade_context, 'use_auto_account', False) and config.session.auto_cano:
                        acc_no = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                    
                    is_sim = 1 if config.session.is_simulation else 0
                    snapshot_json = json.dumps(snapshot, ensure_ascii=False) if snapshot else "{}"
                    
                    cursor.execute('''
                        INSERT INTO trades (time, type, code, name, qty, price, odno, org_odno, account, is_sim, snapshot, profit_amt, profit_rate, reason, strategy_score, order_status, stop_loss_rate)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (now_str, type_str, code, name, str(qty), str(price), odno, org_odno, acc_no, is_sim, snapshot_json, profit_amt, profit_rate, reason, score, order_status, stop_loss_rate))
                    
                    conn.commit()
                    
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim green][DB][{threading.get_ident()}] 거래 내역 저장 완료 (ODNO: {odno})[/dim green]")
                    break
                    
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Insert Error: {e}[/red]")
                    break
                except Exception as e:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Insert Error: {e}[/red]")
                    break

    def get_trades(self, limit=None, start_date=None, end_date=None, code=None, is_auto=False, is_sim=None, order_status=None, account=None):
        """거래 내역 조회"""
        # 읽기 작업은 락 없이 수행 가능 (WAL 모드 덕분)
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            query = "SELECT * FROM trades WHERE 1=1"
            params = []
            
            if start_date:
                query += " AND time >= ?"
                params.append(f"{start_date} 00:00:00")
            if end_date:
                query += " AND time <= ?"
                params.append(f"{end_date} 23:59:59")
            if code:
                query += " AND (code LIKE ? OR name LIKE ?)"
                params.append(f"%{code}%")
                params.append(f"%{code}%")
            
            if is_auto:
                query += " AND type LIKE '%(AUTO)%'"
            
            if is_sim is not None:
                query += " AND is_sim = ?"
                params.append(1 if is_sim else 0)
            
            # [추가] 계좌 번호 필터링
            if account:
                query += " AND account = ?"
                params.append(account)
            
            # [추가] 주문 상태 필터링
            if order_status:
                query += " AND order_status = ?"
                params.append(order_status)
            
            query += " ORDER BY id DESC"
            
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] Select Error: {e}[/red]")
            return []
            
    def delete_trade_by_id(self, trade_id):
        """특정 ID의 거래 내역을 삭제"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
                    deleted = cursor.rowcount
                    conn.commit()
                    return deleted > 0
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    return False
                except Exception:
                    return False
    
    def update_trade(self, odno, price=None, qty=None, profit_amt=None, profit_rate=None, order_status=None):
        """주문번호(odno)를 기준으로 거래 내역 업데이트"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    
                    updates = []
                    params = []
                    
                    if price is not None: updates.append("price = ?"); params.append(str(price))
                    if qty is not None: updates.append("qty = ?"); params.append(str(qty))
                    if profit_amt is not None: updates.append("profit_amt = ?"); params.append(profit_amt)
                    if profit_rate is not None: updates.append("profit_rate = ?"); params.append(profit_rate)
                    if order_status is not None: updates.append("order_status = ?"); params.append(order_status)
                    
                    if updates:
                        params.append(odno)
                        cursor.execute(f"UPDATE trades SET {', '.join(updates)} WHERE odno = ?", params)
                        conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Update Error: {e}[/red]")
                    break
                except Exception as e:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Update Error: {e}[/red]")
                    break
    
    def check_trade_exists(self, odno, order_status):
        """특정 주문번호와 상태를 가진 거래 내역 존재 여부 확인"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM trades WHERE odno = ? AND order_status = ?", (odno, order_status))
            cnt = cursor.fetchone()[0]
            
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG" and cnt > 0:
                config.console.print(f"[dim yellow][DB] check_trade_exists: {odno} ({order_status}) -> 존재함[/dim yellow]")
            return cnt > 0
        except: return False
            
    def get_original_order_type(self, odno):
        """주문번호로 원 주문(접수 상태)의 유형 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # [수정] 원본 주문 조회 시 '접수'뿐만 아니라 '정정', '취소' 상태도 조회 허용
            cursor.execute("SELECT type FROM trades WHERE odno = ? AND order_status IN ('접수', '정정', '취소') ORDER BY id DESC LIMIT 1", (odno,))
            row = cursor.fetchone()
            return row[0] if row else None
        except: return None

    def get_trade_by_odno(self, odno):
        """주문번호로 원 주문(접수) 내역 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # [수정] 정정 주문에 의해 새로 생성된 odno도 찾을 수 있도록 상태 범위 확장
            cursor.execute("SELECT * FROM trades WHERE odno = ? AND order_status IN ('접수', '정정', '취소') ORDER BY id DESC LIMIT 1", (odno,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except: return None

    def get_cancel_record_by_org_odno(self, odno):
        """원주문번호(org_odno)로 가장 최근 취소 이력 1건 조회 (외부/사후 취소 중복 판별용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, reason FROM trades WHERE org_odno = ? AND order_status IN ('취소', '취소(추정)') ORDER BY id DESC LIMIT 1",
                (odno,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        except: return None

    def get_reserved_order_by_odno(self, odno):
        """주문번호(odno)로 발동된 예약 주문 1건 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reserved_orders WHERE odno = ?", (str(odno),))
            row = cursor.fetchone()
            return dict(row) if row else None
        except: return None

    def get_buy_trades_for_current_holding(self, code):
        """
        현재 보유 수량에 해당하는 매수 거래 내역들을 조회합니다.
        (마지막 매도 이후의 모든 매수 내역)
        """
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            # 1. 해당 종목의 마지막 매도 시점 조회
            cursor.execute("SELECT time FROM trades WHERE code = ? AND (type LIKE '%sell%' OR type LIKE '%매도%') ORDER BY id DESC LIMIT 1", (code,))
            last_sell_time = cursor.fetchone()
            
            # 2. 마지막 매도 이후의 모든 매수 내역 조회
            query = "SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%')"
            params = [code]
            
            if last_sell_time:
                query += " AND time > ?"
                params.append(last_sell_time['time'])
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except:
            return []

    def get_latest_buy_trade(self, code):
        """특정 종목의 가장 최근 매수 내역 조회 (ATR 손절률 확인용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # [수정] ATR 손절률이 누락되지 않은 원본 매수 내역을 우선 조회합니다. (체결 확인용 더미 레코드 제외)
            cursor.execute("SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%') AND stop_loss_rate != 0.0 AND (reason IS NULL OR reason NOT LIKE '체결 확인%') ORDER BY id DESC LIMIT 1", (code,))
            row = cursor.fetchone()
            
            # 만약 0이 아닌 내역이 없다면(고정 손절 등), 기본 최신 매수 내역을 조회합니다.
            if not row:
                cursor.execute("SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%') AND (reason IS NULL OR reason NOT LIKE '체결 확인%') ORDER BY id DESC LIMIT 1", (code,))
                row = cursor.fetchone()

            # [추가] 그래도 없으면, '접수' 원본 없이 '체결 확인' 레코드만 존재하는 경우
            #        (수동/외부 매수 등)이므로 체결 확인 더미라도 조회하여
            #        매수 시각(holding_days)·사유를 확보합니다. (시간청산 등 정상 동작 보장)
            if not row:
                cursor.execute("SELECT * FROM trades WHERE code = ? AND (type LIKE '%buy%' OR type LIKE '%매수%') ORDER BY id DESC LIMIT 1", (code,))
                row = cursor.fetchone()

            return dict(row) if row else None
        except: return None
            
    def update_highest_price(self, code, price):
        """트레일링 스탑용 최고가 갱신"""
        with self.lock:
            for attempt in range(5):
                try:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim cyan][DB][{threading.get_ident()}] update_highest_price 요청 ({attempt+1}/5): {code} {price}[/dim cyan]")

                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    # UPSERT 구문 사용 (SQLite 3.24+)
                    cursor.execute('''
                        INSERT INTO trailing_stops (code, highest_price, update_time)
                        VALUES (?, ?, ?)
                        ON CONFLICT(code) DO UPDATE SET
                        highest_price = excluded.highest_price,
                        update_time = excluded.update_time
                        WHERE excluded.highest_price > trailing_stops.highest_price
                    ''', (code, price, now_str))
                    
                    conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    if "locked" in str(e):
                        if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print(f"[yellow][DB] Locked during update_highest_price ({attempt+1}/5). Retrying...[/yellow]")
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Trailing Stop Update Error: {e}[/red]")
                    break
                except Exception as e:
                    if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Trailing Stop Update Error: {e}[/red]")
                    break

    def get_highest_price(self, code):
        """종목의 기록된 최고가 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT highest_price FROM trailing_stops WHERE code = ?", (code,))
            row = cursor.fetchone()
            return row[0] if row else None
        except: return None

    def get_all_trailing_stops(self):
        """모든 종목의 트레일링 스탑 기준가 조회 (시스템 시작 시 캐시 로드용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT code, highest_price FROM trailing_stops")
            return {row[0]: row[1] for row in cursor.fetchall()}
        except: return {}

    def delete_trailing_stop(self, code):
        """매도 후 트레일링 스탑 정보 삭제"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM trailing_stops WHERE code = ?", (code,))
                    conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except: break

    def insert_half_tp(self, code):
        """반익절 상태 저장"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute('''
                        INSERT OR REPLACE INTO half_tp_status (code, update_time)
                        VALUES (?, ?)
                    ''', (code, now_str))
                    conn.commit()
                    break
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except: break

    def delete_half_tp(self, code):
        """반익절 상태 삭제"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM half_tp_status WHERE code = ?", (code,))
                    conn.commit()
                    break
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except: break

    def get_all_half_tp(self):
        """모든 반익절 상태 종목 조회 (시스템 시작 시 캐시 로드용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT code FROM half_tp_status")
            return set(row[0] for row in cursor.fetchall())
        except: return set()

    def is_disclosure_notified(self, rcept_no):
        """공시 접수번호가 이미 알림 발송됐는지 확인"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM notified_disclosures WHERE rcept_no = ?", (rcept_no,))
            return cursor.fetchone() is not None
        except: return False

    def mark_disclosure_notified(self, rcept_no):
        """공시 접수번호를 알림 발송됨으로 기록"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute("INSERT OR REPLACE INTO notified_disclosures (rcept_no, notified_at) VALUES (?, ?)", (rcept_no, now_str))
                    conn.commit()
                    break
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except: break

    def save_stock_strategy(self, code, name, strategy):
        """종목별 매매 전략 저장"""
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            memo = strategy.get('memo', '')
            weights = strategy.get('weights')
            weights_json = json.dumps(weights) if weights else None
            buy_vol_strength = strategy.get('buy_vol_strength', 100.0)
            
            invest_ratio = strategy.get('invest_ratio')
            time_stop_days = strategy.get('time_stop_days')
            use_atr_stop = strategy.get('use_atr_stop')
            atr_stop_multiplier = strategy.get('atr_stop_multiplier')
            half_take_profit_use = strategy.get('half_take_profit_use', 1)
            
            cursor.execute('''
                INSERT OR REPLACE INTO stock_strategies 
                (code, name, buy_score, buy_rsi, buy_vol_strength, sell_score, stop_loss, take_profit, take_profit_rsi, ts_activation, ts_callback, updated_at, memo, weights, invest_ratio, time_stop_days, use_atr_stop, atr_stop_multiplier, half_take_profit_use)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (code, name, 
                  strategy['buy_score'], strategy['buy_rsi'], buy_vol_strength,
                  strategy['sell_score'], strategy['stop_loss'], 
                  strategy['take_profit'], strategy['take_profit_rsi'], 
                  strategy['ts_activation'], strategy['ts_callback'], 
                  now, memo, weights_json, invest_ratio, time_stop_days, use_atr_stop, atr_stop_multiplier, half_take_profit_use))
            conn.commit()

    def get_stock_strategy(self, code):
        """특정 종목의 매매 전략 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM stock_strategies WHERE code = ?", (code,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except: return None

    def get_all_stock_strategies(self):
        """모든 종목별 매매 전략 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM stock_strategies ORDER BY updated_at DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except: return []

    def delete_stock_strategy(self, code):
        """종목별 매매 전략 삭제"""
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM stock_strategies WHERE code = ?", (code,))
            conn.commit()

    def cleanup_old_data(self, days_to_keep):
        """보존 기간이 지난 오래된 거래 내역 삭제"""
        if days_to_keep <= 0: return
        
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            # SQLite의 date 함수를 사용하여 기준일 계산
            cursor.execute(f"DELETE FROM trades WHERE time < date('now', '-{days_to_keep} days')")
            deleted_count = cursor.rowcount
            conn.commit()
            
            if self._is_screen_output_allowed() and deleted_count > 0 and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[dim yellow][DB] 오래된 거래 내역 {deleted_count}건을 정리했습니다. ({days_to_keep}일 이전)[/dim yellow]")
        except Exception as e:
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] Cleanup Error: {e}[/red]")

    def run_vacuum(self):
        """DB 최적화 (VACUUM) 실행 - 프로그램 종료 시 호출"""
        try:
            # 별도 연결 생성하여 실행 (스레드 로컬 연결 간섭 방지)
            # [수정] VACUUM은 트랜잭션 내에서 실행할 수 없으므로 isolation_level=None (Auto-commit) 설정
            conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print("[dim cyan][DB] 데이터베이스 정리 및 최적화(VACUUM) 수행 중...[/dim cyan]")
            
            # 1. 오래된 데이터 삭제 (설정된 기간 기준)
            retention_days = getattr(config, 'DB_DATA_RETENTION_DAYS', 365)
            if retention_days > 0:
                conn.execute(f"DELETE FROM trades WHERE time < date('now', '-{retention_days} days')")
            
            # 2. VACUUM 실행 (공간 회수)
            conn.execute("VACUUM;")
            conn.close()
            
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print("[dim green][DB] 데이터베이스 최적화 완료[/dim green]")
        except Exception as e:
            if self._is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] VACUUM Error: {e}[/red]")

    def save_daily_asset(self, date_str, account, asset_value):
        """일일 총 자산 스냅샷 저장"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT OR REPLACE INTO daily_asset_history (date, account, asset)
                        VALUES (?, ?, ?)
                    ''', (date_str, account, asset_value))
                    conn.commit()
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break

    def get_daily_asset(self, start_date, account):
        """특정 날짜 이후의 가장 오래된 자산 스냅샷 조회 (기초 자산용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT asset FROM daily_asset_history WHERE account = ? AND date >= ? ORDER BY date ASC LIMIT 1", (account, start_date))
            row = cursor.fetchone()
            return row[0] if row else None
        except:
            return None
            
    def insert_reserved_order(self, cano, acnt, market, order_type, code, name, qty, order_price, condition_type, target_price, target_time, expire_dt=None, composite_json=None):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO reserved_orders (cano, acnt, market, order_type, code, name, qty, order_price, condition_type, target_price, target_time, expire_dt, composite_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (cano, acnt, market, order_type, code, name, qty, order_price, condition_type, target_price, target_time, expire_dt, composite_json))
            conn.commit()

    def get_pending_reserved_orders(self):
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reserved_orders WHERE status = 'PENDING'")
            return [dict(row) for row in cursor.fetchall()]
        except: return []

    def get_completed_reserved_orders(self, start_date=None, keyword=None):
        """발동 완료되거나 취소된 예약 주문 내역 조회 (히스토리용)"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            q = "SELECT * FROM reserved_orders WHERE status != 'PENDING'"
            params = []
            
            if start_date:
                q += " AND created_at >= ?"
                params.append(start_date + " 00:00:00")
            if keyword:
                q += " AND (code LIKE ? OR name LIKE ?)"
                params.append(f"%{keyword}%")
                params.append(f"%{keyword}%")
                
            cursor.execute(q, params)
            return [dict(row) for row in cursor.fetchall()]
        except: return []

    def update_reserved_order_status(self, order_id, status, odno=None, fail_reason=None):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            if odno: cursor.execute("UPDATE reserved_orders SET status=?, odno=? WHERE id=?", (status, odno, order_id))
            elif fail_reason: cursor.execute("UPDATE reserved_orders SET status=?, fail_reason=? WHERE id=?", (status, fail_reason, order_id))
            else: cursor.execute("UPDATE reserved_orders SET status=? WHERE id=?", (status, order_id))
            conn.commit()
            
    def update_reserved_order_lowest(self, order_id, price):
        """트레일링 매수용 최저점 추적 업데이트"""
        with self.lock:
            conn = self._get_conn()
            conn.cursor().execute("UPDATE reserved_orders SET lowest_price=? WHERE id=?", (price, order_id))
            conn.commit()
            
    def update_reserved_order_highest(self, order_id, price):
        """트레일링 매도용 최고점 추적 업데이트"""
        with self.lock:
            conn = self._get_conn()
            conn.cursor().execute("UPDATE reserved_orders SET highest_price=? WHERE id=?", (price, order_id))
            conn.commit()
            
    def cancel_reserved_sell_orders(self, cano, acnt, code):
        """특정 계좌/종목의 대기 중인 예약 매도 주문을 일괄 취소 처리 (전량 매도 시)"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE reserved_orders 
                        SET status='CANCELED', fail_reason='수동/자동 전량 매도로 인한 예약 자동 취소' 
                        WHERE cano=? AND acnt=? AND code=? AND order_type='sell' AND status='PENDING'
                    ''', (cano, acnt, code))
                    updated = cursor.rowcount
                    conn.commit()
                    return updated
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break
            return 0
            
    def cancel_reserved_buy_orders(self, cano, acnt, code):
        """특정 계좌/종목의 대기 중인 예약 매수 주문을 일괄 취소 처리 (신규 매수 시 중복 방지)"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE reserved_orders 
                        SET status='CANCELED', fail_reason='수동/자동 매수로 인한 예약 매수 자동 취소' 
                        WHERE cano=? AND acnt=? AND code=? AND order_type='buy' AND status='PENDING'
                    ''', (cano, acnt, code))
                    updated = cursor.rowcount
                    conn.commit()
                    return updated
                except sqlite3.OperationalError: time.sleep(0.5); continue
                except: break
            return 0

    def cancel_other_reserved_orders(self, triggered_id, cano, acnt, code):
        """특정 예약 주문이 발동되었을 때, 동일 계좌/종목의 나머지 대기 중인 예약 주문을 일괄 취소"""
        with self.lock:
            for attempt in range(5):
                try:
                    conn = self._get_conn()
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT * FROM reserved_orders 
                        WHERE cano=? AND acnt=? AND code=? AND id != ? AND status='PENDING'
                    ''', (cano, acnt, code, triggered_id))
                    targets = [dict(row) for row in cursor.fetchall()]
                    
                    if targets:
                        cursor.execute('''
                            UPDATE reserved_orders 
                            SET status='CANCELED', fail_reason='동일 종목의 다른 예약 매매 발동으로 인한 자동 취소' 
                            WHERE cano=? AND acnt=? AND code=? AND id != ? AND status='PENDING'
                        ''', (cano, acnt, code, triggered_id))
                        conn.commit()
                    return targets
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    break
                except Exception:
                    break
            return []

# 전역 인스턴스
db = DBManager()
atexit.register(db.run_vacuum)
