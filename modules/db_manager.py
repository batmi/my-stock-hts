# modules/db_manager.py
import sqlite3
import json
import threading
import os
from datetime import datetime
import config

class DBManager:
    def __init__(self):
        self.db_path = config.DB_FILE_PATH
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
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
                
                # [추가] 트레일링 스탑 추적 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trailing_stops (
                        code TEXT PRIMARY KEY,
                        highest_price REAL,
                        update_time TEXT
                    )
                ''')
                
                # [추가] 컬럼 확장 (마이그레이션)
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
                            if config.DEBUG_LEVEL != "OFF":
                                config.console.print(f"[dim green][DB] 컬럼 추가됨: {col}[/dim green]")
                        except Exception as e:
                            config.console.print(f"[red][DB] 컬럼 추가 실패({col}): {e}[/red]")

                conn.commit()
                conn.close()
            except Exception as e:
                if config.DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Init Error: {e}[/red]")

    def insert_trade(self, type_str, code, name, qty, price, odno, org_odno=None, snapshot=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, order_status="접수", custom_time=None):
        """거래 내역 및 스냅샷 저장"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()
                
                now_str = custom_time if custom_time else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # 계좌 정보 식별
                acc_no = f"{config.CANO}-{config.ACNT_PRDT_CD}"
                if getattr(config.trade_context, 'use_auto_account', False) and config.AUTO_CANO:
                    acc_no = f"{config.AUTO_CANO}-{config.AUTO_ACNT_PRDT_CD}"
                
                is_sim = 1 if config.IS_SIMULATION else 0
                snapshot_json = json.dumps(snapshot, ensure_ascii=False) if snapshot else "{}"
                
                cursor.execute('''
                    INSERT INTO trades (time, type, code, name, qty, price, odno, org_odno, account, is_sim, snapshot, profit_amt, profit_rate, reason, strategy_score, order_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (now_str, type_str, code, name, str(qty), str(price), odno, org_odno, acc_no, is_sim, snapshot_json, profit_amt, profit_rate, reason, score, order_status))
                
                conn.commit()
                conn.close()
                
                if config.DEBUG_LEVEL == "DEBUG":
                    config.console.print(f"[dim green][DB] 거래 내역 저장 완료 (ODNO: {odno})[/dim green]")
                    
            except Exception as e:
                if config.DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Insert Error: {e}[/red]")

    def get_trades(self, limit=None, start_date=None, end_date=None, code=None, is_auto=False, is_sim=None):
        """거래 내역 조회"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
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
                
                # [추가] 모의/실전 필터링
                if is_sim is not None:
                    query += " AND is_sim = ?"
                    params.append(1 if is_sim else 0)
                
                query += " ORDER BY id DESC"
                
                if limit:
                    query += " LIMIT ?"
                    params.append(limit)
                
                cursor.execute(query, params)
                rows = cursor.fetchall()
                conn.close()
                return [dict(row) for row in rows]
            except Exception as e:
                if config.DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Select Error: {e}[/red]")
                return []
    
    def update_trade(self, odno, price=None, qty=None, profit_amt=None, profit_rate=None, order_status=None):
        """주문번호(odno)를 기준으로 거래 내역 업데이트 (체결 단가 등)"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
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
                conn.close()
            except Exception as e:
                if config.DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Update Error: {e}[/red]")
    
    def check_trade_exists(self, odno, order_status):
        """특정 주문번호와 상태를 가진 거래 내역 존재 여부 확인"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) FROM trades WHERE odno = ? AND order_status = ?", (odno, order_status))
                cnt = cursor.fetchone()[0]
                conn.close()
                return cnt > 0
            except: return False
            
    def update_highest_price(self, code, price):
        """트레일링 스탑용 최고가 갱신 (현재가가 더 높을 때만 업데이트)"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # 기존 최고가 조회
                cursor.execute("SELECT highest_price FROM trailing_stops WHERE code = ?", (code,))
                row = cursor.fetchone()
                
                if row:
                    if price > row[0]:
                        cursor.execute("UPDATE trailing_stops SET highest_price = ?, update_time = ? WHERE code = ?", (price, now_str, code))
                else:
                    cursor.execute("INSERT INTO trailing_stops (code, highest_price, update_time) VALUES (?, ?, ?)", (price, now_str, code))
                
                conn.commit()
                conn.close()
            except Exception as e:
                if config.DEBUG_LEVEL != "OFF":
                    config.console.print(f"[red][DB] Trailing Stop Update Error: {e}[/red]")

    def get_highest_price(self, code):
        """종목의 기록된 최고가 조회"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()
                cursor.execute("SELECT highest_price FROM trailing_stops WHERE code = ?", (code,))
                row = cursor.fetchone()
                conn.close()
                return row[0] if row else None
            except: return None

    def delete_trailing_stop(self, code):
        """매도 후 트레일링 스탑 정보 삭제"""
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM trailing_stops WHERE code = ?", (code,))
                conn.commit()
                conn.close()
            except: pass

# 전역 인스턴스
db = DBManager()