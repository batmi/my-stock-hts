# modules/db_manager.py
import sqlite3
import json
import threading
import os
import time
from datetime import datetime
import config
import atexit

class DBManager:
    def __init__(self):
        self.db_path = config.DB_FILE_PATH
        self.lock = threading.Lock() # 스레드 간 동기화를 위한 락 (필요 시 사용)
        self.local = threading.local() # 스레드별 로컬 저장소
        self._init_db()

    def _get_conn(self):
        """스레드별 DB 연결 객체 반환 (없으면 생성)"""
        if not hasattr(self.local, 'conn'):
            self.local.conn = sqlite3.connect(self.db_path, timeout=60)
            self.local.conn.execute("PRAGMA journal_mode=WAL;") # WAL 모드 설정
            self.local.conn.row_factory = sqlite3.Row
        return self.local.conn

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
                
                # 컬럼 확장 (마이그레이션)
                cursor.execute("PRAGMA table_info(trades)")
                columns = [info[1] for info in cursor.fetchall()]
                
                new_columns = {
                    "profit_amt": "INTEGER DEFAULT 0",
                    "profit_rate": "REAL DEFAULT 0.0",
                    "reason": "TEXT",
                    "strategy_score": "REAL DEFAULT 0",
                    "order_status": "TEXT DEFAULT '접수'"
                }
                
                for col, dtype in new_columns.items():
                    if col not in columns:
                        try:
                            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {dtype}")
                            if config.SCREEN_DEBUG_LEVEL != "OFF":
                                config.console.print(f"[dim green][DB] 컬럼 추가됨: {col}[/dim green]")
                        except Exception as e:
                            config.console.print(f"[red][DB] 컬럼 추가 실패({col}): {e}[/red]")

                conn.commit()
                conn.close()
            except Exception as e:
                if config.SCREEN_DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Init Error: {e}[/red]")

    def insert_trade(self, type_str, code, name, qty, price, odno, org_odno=None, snapshot=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, order_status="접수", custom_time=None):
        """거래 내역 및 스냅샷 저장"""
        # 쓰기 작업은 락으로 보호하여 순차 처리 (SQLite 특성상 안전)
        with self.lock:
            for attempt in range(5):
                try:
                    if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim cyan][DB][{threading.get_ident()}] insert_trade 요청 ({attempt+1}/5): {type_str} {name}({code})[/dim cyan]")

                    conn = self._get_conn()
                    cursor = conn.cursor()
                    
                    now_str = custom_time if custom_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    acc_no = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                    if getattr(config.trade_context, 'use_auto_account', False) and config.session.auto_cano:
                        acc_no = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                    
                    is_sim = 1 if config.session.is_simulation else 0
                    snapshot_json = json.dumps(snapshot, ensure_ascii=False) if snapshot else "{}"
                    
                    cursor.execute('''
                        INSERT INTO trades (time, type, code, name, qty, price, odno, org_odno, account, is_sim, snapshot, profit_amt, profit_rate, reason, strategy_score, order_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (now_str, type_str, code, name, str(qty), str(price), odno, org_odno, acc_no, is_sim, snapshot_json, profit_amt, profit_rate, reason, score, order_status))
                    
                    conn.commit()
                    
                    if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim green][DB][{threading.get_ident()}] 거래 내역 저장 완료 (ODNO: {odno})[/dim green]")
                    break
                    
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 4:
                        time.sleep(0.5)
                        continue
                    if config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Insert Error: {e}[/red]")
                    break
                except Exception as e:
                    if config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Insert Error: {e}[/red]")
                    break

    def get_trades(self, limit=None, start_date=None, end_date=None, code=None, is_auto=False, is_sim=None):
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
            
            query += " ORDER BY id DESC"
            
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] Select Error: {e}[/red]")
            return []
    
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
                    if config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Update Error: {e}[/red]")
                    break
                except Exception as e:
                    if config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Update Error: {e}[/red]")
                    break
    
    def check_trade_exists(self, odno, order_status):
        """특정 주문번호와 상태를 가진 거래 내역 존재 여부 확인"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT count(*) FROM trades WHERE odno = ? AND order_status = ?", (odno, order_status))
            cnt = cursor.fetchone()[0]
            
            if config.SCREEN_DEBUG_LEVEL == "DEBUG" and cnt > 0:
                config.console.print(f"[dim yellow][DB] check_trade_exists: {odno} ({order_status}) -> 존재함[/dim yellow]")
            return cnt > 0
        except: return False
            
    def get_original_order_type(self, odno):
        """주문번호로 원 주문(접수 상태)의 유형 조회"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT type FROM trades WHERE odno = ? AND order_status = '접수' ORDER BY id DESC LIMIT 1", (odno,))
            row = cursor.fetchone()
            return row[0] if row else None
        except: return None
            
    def update_highest_price(self, code, price):
        """트레일링 스탑용 최고가 갱신"""
        with self.lock:
            for attempt in range(5):
                try:
                    if config.SCREEN_DEBUG_LEVEL == "DEBUG":
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
                        if config.SCREEN_DEBUG_LEVEL != "OFF":
                            config.console.print(f"[yellow][DB] Locked during update_highest_price ({attempt+1}/5). Retrying...[/yellow]")
                    if config.SCREEN_DEBUG_LEVEL != "OFF":
                        config.console.print(f"[red][DB] Trailing Stop Update Error: {e}[/red]")
                    break
                except Exception as e:
                    if config.SCREEN_DEBUG_LEVEL != "OFF":
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
            
            if deleted_count > 0 and config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[dim yellow][DB] 오래된 거래 내역 {deleted_count}건을 정리했습니다. ({days_to_keep}일 이전)[/dim yellow]")
        except Exception as e:
            if config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] Cleanup Error: {e}[/red]")

    def run_vacuum(self):
        """DB 최적화 (VACUUM) 실행 - 프로그램 종료 시 호출"""
        try:
            # 별도 연결 생성하여 실행 (스레드 로컬 연결 간섭 방지)
            # [수정] VACUUM은 트랜잭션 내에서 실행할 수 없으므로 isolation_level=None (Auto-commit) 설정
            conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
            if config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print("[dim cyan][DB] 데이터베이스 정리 및 최적화(VACUUM) 수행 중...[/dim cyan]")
            
            # 1. 오래된 데이터 삭제 (설정된 기간 기준)
            retention_days = getattr(config, 'DB_DATA_RETENTION_DAYS', 365)
            if retention_days > 0:
                conn.execute(f"DELETE FROM trades WHERE time < date('now', '-{retention_days} days')")
            
            # 2. VACUUM 실행 (공간 회수)
            conn.execute("VACUUM;")
            conn.close()
            
            if config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print("[dim green][DB] 데이터베이스 최적화 완료[/dim green]")
        except Exception as e:
            if config.SCREEN_DEBUG_LEVEL != "OFF":
                config.console.print(f"[red][DB] VACUUM Error: {e}[/red]")

# 전역 인스턴스
db = DBManager()
atexit.register(db.run_vacuum)
