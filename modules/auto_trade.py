import threading
import concurrent.futures
import logging
import time
import requests
import json
import os
import sqlite3 # [추가] DB 직접 접근용
from datetime import datetime, timedelta
from collections import Counter
from rich.prompt import Prompt
from rich.markup import escape
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
import config
import context # [추가]
import api
import utils
import indicators
from modules import analysis, account # [수정] account 모듈 재사용
import math # [추가] math 모듈
from modules import db_manager # [추가] DB 매니저
from modules import chart # [추가] 차트 모듈
import re # [추가] 정규식 모듈
import pandas as pd

console = config.console

logger = logging.getLogger(__name__)

def get_mystock_log_tail(lines=20):
    """에러 발생 시 전송할 mystock.log의 꼬리 부분을 반환합니다."""
    log_path = os.path.join(getattr(config, 'LOG_DIR', 'logs'), 'mystock.log')
    if not os.path.exists(log_path):
        return "로그 파일이 존재하지 않습니다."
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.readlines()
            tail = "".join(content[-lines:])
            if len(tail) > 2000: # 텔레그램 메시지 길이 제한 방어
                tail = tail[-2000:]
            return tail
    except Exception as e:
        return f"로그 읽기 실패: {e}"

# [추가] 거래 제한 종목 파일 경로 및 관리 함수
RESTRICTED_FILE = os.path.join(config.JSON_DIR, "restricted_stocks.json")

def load_restricted_stocks():
    if os.path.exists(RESTRICTED_FILE):
        try:
            with open(RESTRICTED_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return {}
    return {}

def save_restricted_stocks(data):
    try:
        with open(RESTRICTED_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        console.print(f"[red]저장 실패: {e}[/red]")

# [추가] 일일 자산 상태 파일 경로 및 관리 함수 (재시작 시 손실 제한 기준 유지용)
DAILY_STATE_FILE = os.path.join(config.JSON_DIR, "daily_asset_state.json")

def load_daily_initial_asset(account_key):
    """계좌별 일일 시작 자산을 로드합니다."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(DAILY_STATE_FILE):
        try:
            with open(DAILY_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    accounts = data.get("accounts", {})
                    if account_key in accounts and accounts[account_key] > 0:
                        return accounts[account_key]
        except: pass
    return 0

def save_daily_initial_asset(account_key, asset_value):
    """계좌별 일일 시작 자산을 저장합니다."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    data = {"date": today_str, "accounts": {}}
    if os.path.exists(DAILY_STATE_FILE):
        try:
            with open(DAILY_STATE_FILE, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                if old_data.get("date") == today_str:
                    data["accounts"] = old_data.get("accounts", {})
        except: pass
    
    data["accounts"][account_key] = asset_value
    try:
        with open(DAILY_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"일일 자산 상태 저장 실패: {e}")

# [추가] 주문 상태 상수 정의 (Order State Machine)
class OrderStatus:
    IDLE = "IDLE"
    ORDER_SENT = "ORDER_SENT"       # 주문 전송 (접수 대기)
    ACCEPTED = "ACCEPTED"           # 접수 완료 (미체결)
    PARTIAL_FILLED = "PARTIAL_FILLED" # 부분 체결
    FILLED = "FILLED"               # 전량 체결
    CANCELED = "CANCELED"           # 취소
    REJECTED = "REJECTED"           # 거부/에러

# [추가] DB 스키마 보정 및 가중치 관리 헬퍼 함수
def _ensure_db_weights_column_logic():
    """stock_strategies 테이블에 weights 컬럼이 없으면 추가"""
    conn = None
    try:
        conn = sqlite3.connect(config.DB_FILE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_strategies'")
        if not cursor.fetchone(): return

        cursor.execute("PRAGMA table_info(stock_strategies)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'weights' not in columns:
            cursor.execute("ALTER TABLE stock_strategies ADD COLUMN weights TEXT")
            conn.commit()
    except Exception as e:
        logger.error(f"DB 스키마 업데이트 실패: {e}")
    finally:
        if conn:
            conn.close()

def _save_rule_weights_logic(code, weights):
    """가중치 정보를 DB에 직접 저장 (JSON 직렬화)"""
    conn = None
    try:
        _ensure_db_weights_column()
        weights_json = json.dumps(weights) if weights else None
        conn = sqlite3.connect(config.DB_FILE_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE stock_strategies SET weights = ? WHERE code = ?", (weights_json, code))
        conn.commit()
    except Exception as e:
        logger.error(f"가중치 저장 실패: {e}")
    finally:
        if conn:
            conn.close()

def _enrich_rules_with_weights_logic(rules):
    """DB에서 weights 컬럼을 조회하여 룰 리스트에 병합"""
    if not rules: return rules
    conn = None
    try:
        _ensure_db_weights_column()
        conn = sqlite3.connect(config.DB_FILE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT code, weights FROM stock_strategies")
        rows = cursor.fetchall()
        
        weights_map = {}
        for row in rows:
            if row['weights']:
                try:
                    weights_map[row['code']] = json.loads(row['weights'])
                except: pass
        
        # Row 객체일 수 있으므로 dict로 변환하며 병합
        new_rules = []
        for r in rules:
            r_dict = dict(r)
            if r_dict['code'] in weights_map:
                r_dict['weights'] = weights_map[r_dict['code']]
            elif 'weights' not in r_dict:
                r_dict['weights'] = None
            
            # None 값 초기화
            if r_dict.get('use_atr_stop') is None:
                r_dict['use_atr_stop'] = 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0
            new_rules.append(r_dict)
        return new_rules
    except Exception as e:
        logger.error(f"가중치 로드 실패: {e}")
        return rules
    finally:
        if conn:
            conn.close()

# [수정] 큐 시스템을 통한 실행 래퍼 함수들
def _ensure_db_weights_column():
    # 내부 로직이므로 별도 래핑 없이 호출되는 함수 내에서 처리되거나,
    # 필요 시 execute_custom을 사용. 여기서는 _save/_enrich 내부에서 호출되므로 로직만 분리.
    _ensure_db_weights_column_logic()

def _save_rule_weights(code, weights):
    if hasattr(db_manager.db, 'execute_custom'):
        db_manager.db.execute_custom(_save_rule_weights_logic, code, weights)
    else:
        _save_rule_weights_logic(code, weights)

def _enrich_rules_with_weights(rules):
    if hasattr(db_manager.db, 'execute_custom'):
        return db_manager.db.execute_custom(_enrich_rules_with_weights_logic, rules)
    else:
        return _enrich_rules_with_weights_logic(rules)


class ConclusionMonitor:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConclusionMonitor, cls).__new__(cls)
            cls._instance._lock = threading.RLock() # [추가] 스레드 동기화 락
            cls._instance.is_running = False
            cls._instance.thread = None
            cls._instance.order_status = {} # 주문별 체결 수량 추적 {계좌-주문번호: qty}
            cls._instance.processed_sim_fills = set() # [추가] 모의투자 중복 알림 방지 캐시
            
            # [수정] 적응형 폴링 설정 로드
            cls._instance.active_interval = getattr(config, 'CONCLUSION_CHECK_INTERVAL', 2)
            cls._instance.idle_interval = getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 180)
            cls._instance.active_duration = getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60)
            cls._instance.active_until = 0 # 집중 감시 유지 만료 시간
            
            cls._instance.event = threading.Event() # 즉시 실행 트리거용
            cls._instance.initialized = False # [추가] 초기화 여부
            cls._instance.consecutive_errors = 0 # [추가] 연속 에러 카운트 (Kill Switch용)
        return cls._instance

    def start(self):
        if self.is_running: return
        
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="ConclusionMonitor")
        self.thread.start()

    def stop(self):
        self.is_running = False
        self.event.set() # 대기 해제
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def check_now(self):
        """즉시 체결 확인 요청"""
        # [추가] 주문 발생 시 집중 감시 모드 시간 연장
        self.active_until = time.time() + self.active_duration
        self.event.set()

    def is_healthy(self):
        """체결 감시 시스템 상태 확인 (Kill Switch 연동)"""
        max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
        return self.consecutive_errors < max_err

    def _is_market_open(self):
        """국내 정규장 운영 시간 확인"""
        if api.is_holiday_today(): return False # 주말 및 공휴일(휴장일) 처리

        now = datetime.now()
        current_time = now.strftime("%H%M")
        start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0915")
        end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "1515")
        return start_time <= current_time <= end_time

    def _run_loop(self):
        my_thread = threading.current_thread()
        self.was_active_mode = False
        
        # [수정] 프로그램 시작 직후 API 요청 집중 방지를 위한 초기 지연 (설정값의 3배)
        initial_delay = getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5) * 3
        time.sleep(initial_delay)
        
        while self.is_running and self.thread is my_thread:
            # [추가] 대기 중인 미체결 주문이 있는지 확인 (해외주식 장외 시간 대응)
            has_pending_orders = False
            try:
                trader_cls = globals().get('AutoTrader')
                if trader_cls:
                    trader = trader_cls()
                    if trader.order_manager.pending_orders:
                        has_pending_orders = True
            except: pass

            # [수정] 장 운영 시간 외이더라도 미체결 주문이 있으면 모니터링 지속
            # 단, 시스템 초기 1회 실행(초기화)은 장 마감 상태여도 무조건 수행해야 하므로 조건 추가
            if self.initialized and not self._is_market_open() and not has_pending_orders:
                self.event.wait(60)
                if not self.event.is_set():
                    continue

            # [추가] 현재 모드(Active/Idle)에 따른 주기 결정
            is_active_mode = time.time() < self.active_until
            
            if is_active_mode != self.was_active_mode:
                if is_active_mode:
                    logger.debug(f"[Monitor] 집중 감시 모드 진입 (주기: {self.active_interval}초)")
                else:
                    logger.debug(f"[Monitor] 대기 모드 복귀 (주기: {self.idle_interval}초)")
                self.was_active_mode = is_active_mode
            
            if is_active_mode:
                wait_time = self.active_interval
            else:
                wait_time = self.idle_interval
            
            # Idle 주기가 0이면(비활성), 이벤트가 올 때까지 무한 대기 (트래픽 0)
            if not is_active_mode and wait_time <= 0:
                self.event.wait() 
                self.event.clear()
                continue

            is_rate_limited = False
            has_error = False # [추가] 에러 발생 여부 플래그
            
            # [수정] 초기화 상태에 따라 모드 결정 (초기화 실패 시 재시도 보장)
            is_initial_run = not self.initialized

            try:
                # 초기화가 안 되었다면 initial=True로 호출하여 알림 없이 상태만 동기화
                is_rate_limited, has_error = self._check_conclusions(initial=is_initial_run)
                
                if is_initial_run: logger.debug(f"[ORDER_DEBUG] 체결 모니터 초기화 실행 (결과: RateLimit={is_rate_limited}, Error={has_error})")
                # 에러 없이 수행되었다면 초기화 완료 처리
                if is_initial_run and not has_error:
                    self.initialized = True
                    logger.info("[ConclusionMonitor] 체결 내역 초기화 완료 (알림 모드 전환)")
                
                # [추가] 에러 카운트 관리
                if has_error:
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5):
                        logger.error(f"[Monitor] 체결 감시 시스템 불안정 (연속 에러 {self.consecutive_errors}회)")
                else:
                    self.consecutive_errors = 0

            except Exception as e:
                self.consecutive_errors += 1
                logger.error(f"체결 감시 중 오류: {e}")
                has_error = True
            
            # [추가] Rate Limit 감지 시 호출 간격 자동 조절
            if is_rate_limited:
                wait_time = min(wait_time * 2.0, 60.0) # 최대 60초까지 증가
                logger.warning(f"[Monitor] API 호출 제한(Rate Limit) 감지. 대기 시간을 {wait_time:.1f}초로 조정합니다.")
            elif has_error:
                # [추가] 서버 에러(OPSQ2000 등) 발생 시 대기 시간을 늘려 로그 도배 방지
                wait_time = max(wait_time, 20.0)
                logger.debug(f"[Monitor] 체결 확인 중 에러 발생. 대기 시간을 {wait_time:.1f}초로 조정합니다.")
            
            # interval 만큼 대기하되, event가 설정되면 즉시 깨어남
            self.event.wait(wait_time)
            self.event.clear()

    def _check_conclusions(self, initial=False):
        """금일 체결 내역을 확인하고 로그에 기록 (모든 활성 계좌 대상)"""
        rate_limit_hit = False
        has_error = False # [추가]
        
        # [추가] 개별 룰 로드 (체결 알림 시 정보 표시용)
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
        rules_map = {r['code']: r for r in custom_rules}
        
        try:
            # 모니터링 대상 계좌 목록 구성
            accounts_to_check = []
            
            # 1. 메인 계좌 (수동 매매용)
            if config.session.cano and config.session.acnt_prdt_cd:
                accounts_to_check.append({
                    "cano": config.session.cano,
                    "acnt": config.session.acnt_prdt_cd,
                    "type": "MAIN"
                })
            
            # 2. 자동매매 계좌 (실전 모드이고 별도 설정된 경우)
            if not config.session.is_simulation and config.session.auto_cano and config.session.auto_acnt_prdt_cd:
                if config.session.auto_cano != config.session.cano or config.session.auto_acnt_prdt_cd != config.session.acnt_prdt_cd:
                    accounts_to_check.append({
                        "cano": config.session.auto_cano,
                        "acnt": config.session.auto_acnt_prdt_cd,
                        "type": "AUTO"
                    })
            
            # 중복 제거
            unique_accounts = []
            seen = set()
            for acc in accounts_to_check:
                key = (acc['cano'], acc['acnt'])
                if key not in seen:
                    seen.add(key)
                    unique_accounts.append(acc)
            
            for acc in unique_accounts:
                cano = acc['cano']
                acnt = acc['acnt']
                
                try:
                    # [최적화] retries=0 설정: 모니터링 루프가 주기적으로 돌기 때문에
                    # API 내부에서 blocking 재시도를 하지 않고 즉시 실패 처리(Fail Fast)하여 다음 주기로 넘김
                    data = api.get_today_history(cano, acnt, retries=0)
                    ovrs_data = api.get_overseas_today_history(cano, acnt, retries=0)
                    
                    if data.get('msg_cd') == 'EGW00201' or ovrs_data.get('msg_cd') == 'EGW00201':
                        rate_limit_hit = True
                    
                    # [추가] API 호출 실패(RT_CD != 0) 감지
                    if data.get('rt_cd') != '0' and ovrs_data.get('rt_cd') != '0':
                        has_error = True
                    
                    # [추가] 모의투자 전용: API 체결내역 누락 대비 잔고 기반 체결 확인
                    if config.session.is_simulation:
                        self._check_simulation_conclusions_by_balance(cano, acnt)

                    trades = []
                    if data.get('rt_cd') == '0':
                        trades.extend(data.get('output1', []))
                    if ovrs_data.get('rt_cd') == '0':
                        trades.extend(ovrs_data.get('output', []))

                    if trades:
                        
                        # [추가] 모의투자 API 데이터 불일치(output1 Empty, output2 Not Empty) 감지 로그
                        if not trades and config.session.is_simulation:
                            out2 = data.get('output2', {})
                            try:
                                tot_qty = int(out2.get('tot_ccld_qty', 0))
                                if tot_qty > 0 and config.FILE_DEBUG_LEVEL == "DEBUG":
                                    logger.debug(f"[Monitor] API 데이터 불일치: 체결내역 리스트는 비어있으나 요약 수량은 {tot_qty}입니다. (Pagination 또는 필터링 문제 가능성)")
                            except: pass

                        for item in trades:
                            odno = item.get('odno')
                            if not odno: continue
                            
                            is_overseas_trade = 'ft_ord_qty' in item or 'ft_ccld_qty' in item
                            
                            # [추가] 주문 상태 파악 및 업데이트 (State Machine)
                            # API 필드: ord_qty(주문), tot_ccld_qty(체결), cncl_cfrm_qty(취소), rmn_qty(잔량)
                            if is_overseas_trade:
                                ord_qty = api.safe_int(item.get('ft_ord_qty'))
                                ccld_qty = api.safe_int(item.get('ft_ccld_qty'))
                                cncl_qty = api.safe_int(item.get('cncl_cfrm_qty', 0))
                                rmn_qty = api.safe_int(item.get('nccs_qty'))
                                avg_price = float(item.get('ft_ccld_unpr3', 0))
                                type_name = item.get('sll_buy_dvsn_cd_name')
                                if not type_name:
                                    sll_buy_cd = item.get('sll_buy_dvsn_cd', '')
                                    type_name = "매수" if sll_buy_cd == "02" else ("매도" if sll_buy_cd == "01" else "")
                            else:
                                ord_qty = api.safe_int(item.get('ord_qty'))
                                ccld_qty = api.safe_int(item.get('tot_ccld_qty'))
                                cncl_qty = api.safe_int(item.get('cncl_cfrm_qty'))
                                rmn_qty = api.safe_int(item.get('rmn_qty'))
                                avg_price = float(item.get('avg_prvs', 0))
                                type_name = item.get('sll_buy_dvsn_cd_name')

                            code_chk = item.get('pdno')
                            
                            new_status = OrderStatus.ACCEPTED
                            if ord_qty > 0:
                                if ccld_qty == ord_qty: new_status = OrderStatus.FILLED
                                elif cncl_qty == ord_qty or (cncl_qty > 0 and rmn_qty == 0): new_status = OrderStatus.CANCELED
                                elif ccld_qty > 0 and rmn_qty > 0: new_status = OrderStatus.PARTIAL_FILLED
                            
                            # AutoTrader 상태 업데이트 (싱글톤 인스턴스 접근)
                            if code_chk and odno:
                                if new_status == OrderStatus.FILLED: logger.debug(f"[ORDER_DEBUG] API 체결 확인: {code_chk} (No.{odno})")
                                AutoTrader().update_order_status(code_chk, odno, new_status)
                            
                            tot_ccld_qty = ccld_qty
                            if tot_ccld_qty <= 0: continue
                            
                            order_key = f"{cano}-{odno}"
                            prev_qty = self.order_status.get(order_key, 0)
                            
                            if tot_ccld_qty > prev_qty: logger.debug(f"[ORDER_DEBUG] 신규 체결 감지: {odno} (기존:{prev_qty} -> 신규:{tot_ccld_qty}) Initial={initial}")
                            if tot_ccld_qty > prev_qty:
                                new_qty = tot_ccld_qty - prev_qty
                                name = item.get('prdt_name') or item.get('ovrs_item_name') or item.get('item_nm')
                                code = item.get('pdno')
                                
                                # [추가] 매매일시 정보 추출 (DB 저장용)
                                ord_dt = item.get('ord_dt', '')
                                ord_tmd = item.get('ord_tmd', '')
                                trade_time_str = None
                                if len(ord_dt) == 8 and len(ord_tmd) == 6:
                                    trade_time_str = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:]} {ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:]}"
                                
                                # 원 주문 유형 조회 (수동/자동 태그 반영)
                                try:
                                    origin_trade = db_manager.db.get_trade_by_odno(odno)
                                    db_type_name = type_name
                                    profit_amt = 0
                                    profit_rate = 0.0
                                    score = 0
                                    stop_loss_rate = 0.0
                                    reason_to_save = "체결 확인"
                                    actual_reason = ""
                                    if origin_trade:
                                        db_type_name = origin_trade['type']
                                        profit_amt = origin_trade.get('profit_amt', 0)
                                        profit_rate = origin_trade.get('profit_rate', 0.0)
                                        score = origin_trade.get('strategy_score', 0)
                                        stop_loss_rate = float(origin_trade.get('stop_loss_rate', 0.0))
                                        orig_reason = origin_trade.get('reason', '')
                                        if orig_reason and "체결 확인" not in orig_reason:
                                            reason_to_save = f"체결 확인 ({orig_reason})"
                                            actual_reason = orig_reason
                                            
                                        # [추가] 시간 역전 방지: KIS 거래소 서버 시간이 로컬 접수 시간보다 과거인 경우 동기화 보정
                                        if trade_time_str and origin_trade.get('time') and trade_time_str < origin_trade['time']:
                                            trade_time_str = origin_trade['time']
                                    else:
                                        # [추가] trades 테이블에 없으면 reserved_orders 테이블에서 예약 발동 주문인지 조회
                                        try:
                                            conn = db_manager.db._get_conn()
                                            cursor = conn.cursor()
                                            cursor.execute("SELECT * FROM reserved_orders WHERE odno = ?", (str(odno),))
                                            r_row = cursor.fetchone()
                                            if r_row:
                                                t_type = "매수" if r_row['order_type'] == 'buy' else "매도"
                                                db_type_name = f"{t_type}(예약)"
                                                c_type = r_row['condition_type']
                                                tp_val = r_row['target_price']
                                                res_reason = f"조건: {c_type}"
                                                if c_type == 'TIME': res_reason += f" ({r_row['target_time']})"
                                                elif 'SCORE' in c_type: res_reason += f" (목표: {tp_val}점)"
                                                elif 'RSI' in c_type: res_reason += f" (목표: {tp_val})"
                                                elif 'EMA' in c_type: res_reason += f" (EMA {int(tp_val)} {'돌파' if 'UP' in c_type else '이탈'})"
                                                elif c_type == 'TRAILING_BUY': res_reason += f" (바닥반등 {tp_val}%)"
                                                elif c_type == 'TRAILING_SELL': res_reason += f" (고점하락 {tp_val}%)"
                                                else: res_reason += f" (목표가 {tp_val})"
                                                reason_to_save = f"체결 확인 ({res_reason})"
                                                actual_reason = res_reason
                                        except Exception as e:
                                            logger.debug(f"[Monitor] 예약 주문 조회 실패: {e}")
                                except Exception:
                                    db_type_name = type_name
                                    stop_loss_rate = 0.0
                                    reason_to_save = "체결 확인"
                                    actual_reason = ""
                                
                                # [추가] 매도 체결 시 실현 손익 및 사유 조회
                                profit_msg = ""
                                reason_msg = ""
                                if "매도" in type_name:
                                    try:
                                        found_record = None
                                        # 1. AutoTrader 메모리 검색 (클래스가 정의된 경우)
                                        trader_cls = globals().get('AutoTrader')
                                        if trader_cls:
                                            trader = trader_cls()
                                            for record in reversed(trader.trade_records):
                                                if str(record.get('odno')) == str(odno):
                                                    found_record = record
                                                    break
                                        # 2. DB 검색 (메모리에 없을 경우)
                                        if not found_record:
                                            trades = db_manager.db.get_trades(limit=30)
                                            for t in trades:
                                                if str(t.get('odno')) == str(odno):
                                                    found_record = t
                                                    break
                                        if found_record:
                                            if found_record.get('profit_amt') is not None:
                                                profit_msg = f"\n손익: {int(found_record['profit_amt']):+,}원 ({float(found_record.get('profit_rate', 0)):+.2f}%)"
                                            if found_record.get('reason'):
                                                reason_msg = f"\n사유: {found_record['reason']}"
                                    except: pass
                                
                                # [추가] 매도 사유 조회가 안 되었거나 매수인 경우 actual_reason 활용
                                if not reason_msg and actual_reason:
                                    reason_msg = f"\n사유: {actual_reason}"

                                # [수정] 초기화 단계가 아닐 때만 알림 및 로그 수행
                                if not initial:
                                    # [추가] 현재가 및 등락률 조회 (알림용)
                                    cur_info = ""
                                    try:
                                        # 체결 확인은 주로 국내 주식 대상
                                        cp_data = api.get_current_price_data(code, is_overseas=False)
                                        cp_data = api.get_current_price_data(code, is_overseas=is_overseas_trade)
                                        if cp_data.get('rt_cd') == '0':
                                            if is_overseas_trade:
                                                curr = float(cp_data['output'].get('last', 0))
                                                rate = float(cp_data['output'].get('rate', 0))
                                                icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                                cur_info = f"\n현재가: ${curr:,.2f} ({icon} {rate:+.2f}%)"
                                            else:
                                                curr = float(cp_data['output']['stck_prpr'])
                                                rate = float(cp_data['output']['prdy_ctrt'])
                                                icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                                cur_info = f"\n현재가: {int(curr):,}원 ({icon} {rate:+.2f}%)"
                                    except: pass
                                    
                                    # [추가] 개별 룰 조회
                                    rule = rules_map.get(code)
                                    
                                    # [추가] 매수 체결 시 전략 점수 및 지표 추가
                                    strategy_info = ""
                                    if type_name and "매수" in type_name:
                                        try:
                                            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
                                            df = api.get_chart_data(code, is_overseas=is_overseas_stock)
                                            if df is not None and not df.empty:
                                                ind = indicators.calculate_indicators(df)
                                                current_price = float(df.iloc[-1]['close'])
                                                
                                                # [추가] prev_rsi 계산 (상태 분류용)
                                                delta = df['close'].diff()
                                                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                                                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                                                prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None
                                                
                                                sm_flag, _ = analysis.check_smart_money_turnaround(code, is_overseas_stock)

                                                thresholds = None
                                                rule_tag = ""
                                                if rule:
                                                    thresholds = {
                                                        "BUY_SCORE": rule['buy_score'],
                                                        "BUY_RSI_MAX": rule['buy_rsi'],
                                                        "BUY_VOL_STRENGTH": rule.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)),
                                                        "BUY_ASK_BID_RATIO": rule.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)),
                                                        "AUTO_ADJUST_ASK_BID_RATIO": bool(rule.get('auto_adjust_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True))),
                                                        "WEIGHTS": rule.get('weights')
                                                    }
                                                    rule_tag = " [개별 룰 적용]"
                                                    
                                                w52_pos = 0.0
                                                if len(df) > 0:
                                                    recent_df = df.tail(250)
                                                    h52 = recent_df['high'].max()
                                                    l52 = recent_df['low'].min()
                                                    if h52 > l52:
                                                        w52_pos = (current_price - l52) / (h52 - l52) * 100

                                                # [수정] 상태 및 사유 조회 (thresholds 적용)
                                                state, _, state_reason = analysis.classify_stock_state(
                                                    df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
                                                )

                                                score, _ = analysis.calculate_score(
                                                    df=df, ind=ind, weights=thresholds.get('WEIGHTS') if thresholds else None, smart_money=sm_flag
                                                )
                                                score = round(score, 1)
                                                
                                                rsi_str = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
                                                adx_str = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
                                                cci_str = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
                                                
                                                strategy_info = f"\n\n📊 [전략 지표]{rule_tag}\n점수: {score}점 ({state})\n상태: {state_reason}\nRSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                        except Exception as e:
                                            logger.error(f"체결 지표 계산 중 오류: {e}")
                                            
                                    if strategy_info:
                                        strategy_info += cur_info
                                        cur_info = ""
                                    elif cur_info:
                                        strategy_info = f"\n\n📊 [현재 시장 데이터]{cur_info}"
                                        cur_info = ""

                                    # 알림 발송
                                    title_tag = f"[{type_name} 체결]" if type_name else "[체결 알림]"
                                    rule_info = ""
                                    if rule:
                                        title_tag += " [개별]"
                                        rule_info = f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                                        if rule.get('ts_activation'):
                                            rule_info += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                                    
                                    exec_amt = avg_price * new_qty
                                    if is_overseas_trade:
                                        price_str = f"${avg_price:,.2f}"
                                        amt_str = f"${exec_amt:,.2f}"
                                    else:
                                        price_str = f"{avg_price:,.0f}원"
                                        amt_str = f"{int(exec_amt):,}원"

                                    msg = f"✅ {title_tag} {name}({code})\n수량: {new_qty}주\n단가: {price_str}\n금액: {amt_str}\n주문번호: {odno}{profit_msg}{reason_msg}{cur_info}{strategy_info}{rule_info}"
                                    with utils.AccountContext(cano):
                                        api.send_telegram_message(msg)
                                    
                                    # 로그 기록 (시스템 로거 활용)
                                    if context.SYSTEM_LOGGER:
                                        context.SYSTEM_LOGGER(f"[체결 확인] {type_name} {name}({code}) {new_qty}주 (단가: {price_str})")
                                    
                                    # [추가] 매도 체결 시 AI 매매 복기 실행
                                    if type_name == "매도" and found_record:
                                        threading.Thread(target=self._send_trading_autopsy, args=(code, name, found_record), daemon=True).start()
                                        
                                else:
                                    logger.debug(f"[Init] 체결 내역 동기화: {name} {tot_ccld_qty}주 (ODNO: {odno})")
                                    if config.FILE_DEBUG_LEVEL == "DEBUG": logger.debug(f"[ORDER_DEBUG] 초기화 중이라 알림 스킵됨: {odno}")
                                
                                # 상태 업데이트
                                with self._lock:
                                    self.order_status[order_key] = tot_ccld_qty
                                
                                # DB 저장
                                if not db_manager.db.check_trade_exists(odno, "체결"):
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] DB 저장 시도: {odno}")
                                        logger.debug(f"[AutoTrade] 신규 체결 DB 저장 시도: {odno} ({name})")
                                    
                                    db_manager.db.insert_trade(db_type_name, code, name, tot_ccld_qty, avg_price, odno, order_status="체결", reason=reason_to_save, custom_time=trade_time_str, profit_amt=profit_amt, profit_rate=profit_rate, score=score, stop_loss_rate=stop_loss_rate)
                                    
                                    # [추가] 시장가 주문 등의 경우를 위해 원 주문(접수)의 단가도 체결가로 업데이트
                                    # 원본 '접수' 기록을 보존하기 위해 order_status는 덮어쓰지 않음
                                    db_manager.db.update_trade(odno, price=avg_price)
                                else:
                                    logger.debug(f"[ORDER_DEBUG] DB 저장 스킵 (이미 존재): {odno}")
                                    logger.debug(f"[AutoTrade] 이미 존재하는 체결 내역(체결)입니다. 저장 스킵 (ODNO: {odno})")
                except Exception as e:
                    logger.error(f"계좌({cano}) 체결 확인 중 오류: {e}")
                    has_error = True
        except Exception as e:
            logger.error(f"체결 확인 중 오류 발생: {e}")
            has_error = True
        return rate_limit_hit, has_error

    def _check_simulation_conclusions_by_balance(self, cano, acnt):
        """모의투자: 잔고 변동을 확인하여 체결 처리 (API 누락 대응)"""
        trader = AutoTrader()
        # 대기 중인 주문이 없으면 스킵
        if not trader.order_manager.pending_orders:
            return

        try:
            # 현재 잔고 조회 (API 호출)
            holdings, _ = api.get_domestic_balance(cano, acnt)
            holdings_map = {h['pdno']: int(h['hldg_qty']) for h in holdings} if holdings else {}
            
            # 해외 잔고 조회
            ovrs_holdings = api.get_overseas_balance(cano, acnt)
            if ovrs_holdings:
                for h in ovrs_holdings:
                    holdings_map[h['ovrs_pdno']] = int(float(h.get('ovrs_cblc_qty', 0) or h.get('ord_psbl_qty', 0)))

            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[Monitor] 모의투자 잔고 기반 체결 확인 중... (보유종목: {len(holdings_map)}개)")
            
            # 대기 중인 주문 확인
            with trader.order_manager._lock:
                pending_codes = list(trader.order_manager.pending_orders.keys())
                
                for code in pending_codes:
                    orders = trader.order_manager.pending_orders.get(code, {})
                    odnos = list(orders.keys())
                    
                    for odno in odnos:
                        status = orders[odno]
                        # '주문 전송' 상태인 주문만 대상
                        if status != OrderStatus.ORDER_SENT: continue
                        
                        # DB에서 주문 정보 조회
                        trade = db_manager.db.get_trade_by_odno(odno)
                        if not trade: continue
                        
                        type_str = trade.get('type', '')
                        qty = int(trade.get('qty', 0))
                        
                        is_filled = False
                        reason = ""
                        
                        # 매수 주문: 잔고 수량이 주문 수량 이상이면 체결로 간주
                        if "buy" in type_str.lower() or "매수" in type_str:
                            current_qty = holdings_map.get(code, 0)
                            if current_qty >= qty:
                                is_filled = True
                                reason = "잔고 입고 확인"
                        
                        # 매도 주문: 잔고가 0이면 체결로 간주 (전량 매도 가정)
                        elif "sell" in type_str.lower() or "매도" in type_str:
                            current_qty = holdings_map.get(code, 0)
                            if current_qty == 0:
                                is_filled = True
                                reason = "잔고 0 확인"
                        
                        if is_filled:
                            logger.debug(f"[ORDER_DEBUG] 모의투자 잔고 기반 체결 감지: {code} (No.{odno})")
                            self._handle_simulation_fill(trader, trade, odno, code, qty, reason)
                            
        except Exception as e:
            logger.error(f"[Monitor] 모의투자 잔고 기반 체결 확인 중 오류: {e}")

    def _handle_simulation_fill(self, trader, trade, odno, code, qty, reason):
        """모의투자 체결 처리 핸들러"""
        # [추가] Race Condition 방지용 메모리 락 검증
        with self._lock:
            if odno in self.processed_sim_fills:
                return
            self.processed_sim_fills.add(odno)
            
        try:
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] _handle_simulation_fill 진입: {odno} / Code: {code} / Qty: {qty}")
                logger.debug(f"[ORDER_DEBUG] Trade Info: {trade}")

            name = trade.get('name', code)
            price = float(trade.get('price', 0))
            is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum()) if code else False
            
            # [추가] 시장가(0)인 경우 현재가 조회하여 대체
            if price <= 0:
                try:
                    cp = api.get_current_price(code, is_overseas=is_overseas)
                    if cp > 0: price = float(cp)
                    else:
                        # [추가] get_current_price 실패 시 상세 데이터에서 추출
                        cp_data = api.get_current_price_data(code, is_overseas=is_overseas)
                        if cp_data and cp_data.get('rt_cd') == '0':
                            if is_overseas:
                                price = float(cp_data['output'].get('last', 0))
                            else:
                                price = float(cp_data['output'].get('stck_prpr', 0))
                except: pass

            type_str = trade.get('type', '') # [수정] KeyError 방지
            
            # [추가] None 값 안전 처리 (DB 저장 실패 방지)
            try: profit_amt = int(float(trade.get('profit_amt') or 0))
            except: profit_amt = 0
            try: profit_rate = float(trade.get('profit_rate') or 0.0)
            except: profit_rate = 0.0
            
            # [추가] snapshot 데이터 타입 안전 처리
            snapshot_data = trade.get('snapshot')
            if isinstance(snapshot_data, dict):
                snapshot_data = json.dumps(snapshot_data, ensure_ascii=False)
            
            # [추가] 예약 매매 사유 보존 로직
            reason_to_save = f"체결 확인 ({reason})"
            if "예약" in type_str:
                orig_reason = trade.get('reason', '')
                if orig_reason and "체결 확인" not in orig_reason:
                    reason_to_save = f"체결 확인 ({orig_reason})"

            # 1. 원본 '접수' 기록 보존을 위해 상태 덮어쓰기 로직 제거
            
            # 2. 체결 히스토리 생성 (중복 방지 및 재시도 로직)
            success_db = False
            
            # [수정] '체결' 또는 '체결(추정)' 상태가 이미 존재하는지 확인
            exists_check = False
            try:
                # 큐를 통해 순차 처리되므로 별도의 락이나 재시도 불필요
                exists_check = db_manager.db.check_trade_exists(odno, "체결") or db_manager.db.check_trade_exists(odno, "체결(추정)")
                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    logger.debug(f"[ORDER_DEBUG] 체결 내역 존재 여부: {exists_check}")
            except Exception as e:
                logger.error(f"[ORDER_DEBUG] check_trade_exists 오류: {e}", exc_info=True)

            if not exists_check:
                # [수정] 큐 시스템 적용으로 단순 호출로 변경
                db_manager.db.insert_trade(
                    type_str, code, name, qty, price, odno, 
                    order_status="체결(추정)", 
                    reason=reason_to_save, 
                    custom_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    snapshot=snapshot_data,
                    score=trade.get('strategy_score', 0),
                    profit_amt=profit_amt,
                    profit_rate=profit_rate,
                    stop_loss_rate=float(trade.get('stop_loss_rate', 0.0))
                )
                success_db = True
                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    logger.debug(f"[ORDER_DEBUG] insert_trade 성공")

                # 3. 알림 발송 (상세 정보 포함)
                try:
                    type_name = "매수" if "buy" in type_str.lower() or "매수" in type_str else "매도"
                    
                    # 개별 룰 조회
                    custom_rules = db_manager.db.get_all_stock_strategies()
                    rules_map = {r['code']: r for r in custom_rules}
                    rule = rules_map.get(code)
                    
                    title_tag = f"[{type_name} 체결(추정)]" if type_name else "[체결 알림(추정)]"
                    rule_info = ""
                    if rule:
                        title_tag += " [개별]"
                        rule_info = f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                        if rule.get('ts_activation'):
                            rule_info += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                    
                    # 현재가 정보
                    cur_info = ""
                    try:
                        cp_data = api.get_current_price_data(code, is_overseas=False)
                        if cp_data.get('rt_cd') == '0':
                            curr = float(cp_data['output']['stck_prpr'])
                            rate = float(cp_data['output']['prdy_ctrt'])
                            icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                            cur_info = f"\n현재가: {int(curr):,}원 ({icon} {rate:+.2f}%)"
                    except: pass

                    # 전략 지표 (스냅샷 활용)
                    strategy_info = ""
                    if trade.get('snapshot'):
                        try:
                            snap = json.loads(trade['snapshot'])
                            if 'indicators' in snap:
                                ind = snap['indicators']
                                score = trade.get('strategy_score', 0)
                                rsi_str = f"{ind.get('rsi', 0):.1f}"
                                adx_str = f"{ind.get('adx', 0):.1f}"
                                cci_str = f"{ind.get('cci', 0):.1f}"
                                strategy_info = f"\n\n📊 [전략 지표(진입시점)]\n점수: {score}점\nRSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                        except: pass
                                
                    if strategy_info:
                        strategy_info += cur_info
                        cur_info = ""
                    elif cur_info:
                        strategy_info = f"\n\n📊 [현재 시장 데이터]{cur_info}"
                        cur_info = ""

                    exec_amt = int(price * qty)
                    price_fmt = f"{price:,.0f}원" if price > 0 else "시장가"
                    amt_fmt = f"{exec_amt:,}원" if exec_amt > 0 else "-"
                    
                    original_reason = trade.get('reason', reason)
                    profit_msg = ""
                    if type_name == "매도":
                        p_amt = trade.get('profit_amt')
                        p_rate = trade.get('profit_rate')
                        if p_amt is not None and p_rate is not None:
                            profit_msg = f"\n손익: {int(p_amt):+,}원 ({float(p_rate):+.2f}%)"
                            
                    msg = f"✅ {title_tag} {name}({code})\n수량: {qty}주\n단가: {price_fmt}(추정체결가)\n금액: {amt_fmt}\n주문번호: {odno}{profit_msg}\n사유: {original_reason}{cur_info}{strategy_info}{rule_info}"
                    api.send_telegram_message(msg)
                    logger.info(f"[Monitor] 모의투자 체결 확인: {name} {qty}주 ({reason})")
                except Exception as e:
                    logger.error(f"알림 전송 실패: {e}")
            else:
                logger.debug(f"[ORDER_DEBUG] 모의투자 체결 DB 저장 스킵 (이미 체결/체결추정 존재): {odno}")
                success_db = True # 이미 존재하면 성공으로 간주

            # 4. 상태 업데이트 (메모리) - DB 저장 성공 시에만 수행하여 재시도 보장
            if success_db:
                logger.debug(f"[ORDER_DEBUG] 메모리 상태 업데이트(FILLED): {odno}")
                trader.update_order_status(code, odno, OrderStatus.FILLED)
        except Exception as e:
            logger.error(f"[Monitor] 체결 처리 핸들러 오류: {e}", exc_info=True)

    def _send_trading_autopsy(self, code, name, sell_trade):
        """AI 매매 복기 알림 생성 및 전송"""
        try:
            last_buy = db_manager.db.get_latest_buy_trade(code)
            buy_time = last_buy.get('time') if last_buy else "알 수 없음"
            buy_score = last_buy.get('score', 0) if last_buy else 0
            
            sell_reason = sell_trade.get('reason', '알 수 없음')
            profit_rate = sell_trade.get('profit_rate', 0.0)
            holding_days = 0
            if buy_time != "알 수 없음":
                try:
                    buy_dt = datetime.strptime(buy_time, "%Y-%m-%d %H:%M:%S")
                    holding_days = (datetime.now() - buy_dt).days
                except: pass
            
            from modules import theme_analysis
            autopsy = theme_analysis.generate_trading_autopsy(code, name, buy_time, buy_score, sell_reason, profit_rate, holding_days)
            if autopsy:
                if autopsy.startswith("⚠️"):
                    api.send_telegram_message(f"📝 [AI 매매 복기 리포트] {name}({code})\n{autopsy}")
                else:
                    api.send_telegram_message(f"📝 [AI 매매 복기 리포트] {name}({code})\n\n{autopsy}")
        except Exception as e:
            logger.error(f"Trading autopsy error: {e}")

class DefaultStrategy:
    """기본 매매 전략 클래스 (매수/매도 판단 로직 분리)"""
    def __init__(self):
        self.trailing_stop_cache = {}

    def analyze_buy(self, code, name, df, current_price, vol_strength=None, thresholds=None, ask_bid_ratio=None):
        """매수 진입 여부 판단"""
        if df is None or df.empty:
            return None

        ind = indicators.calculate_indicators(df)
        # 전일 RSI 계산 (상태 분류용)
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
        loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
        prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None
        
        # [추가] 52주 위치 계산 (슈퍼 모멘텀 판정용)
        w52_pos = 0.0
        if df is not None and not df.empty:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100
                
        is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
        sm_flag, sm_reason = analysis.check_smart_money_turnaround(code, is_overseas)

        state, _, state_reason = analysis.classify_stock_state(
            df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
        )
        
        score, _ = analysis.calculate_score(
            df=df, ind=ind, weights=thresholds.get('WEIGHTS') if thresholds else None, smart_money=sm_flag
        )
        score = round(score, 1)
        
        # [수정] 역매수 상태에 따른 체결강도 분기
        if state == "역매수":
            min_vol = thresholds.get("MR_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
        else:
            min_vol = thresholds.get("BUY_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        
        # [수정] 체결강도 미달 및 가짜 체결강도(호가창 비대칭성) 필터링
        is_vol_ok = True
        vol_reject_reason = ""
        min_ask_bid_ratio = thresholds.get("BUY_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)

        # [추가] 체결강도 100% 기준으로 매도잔량비 자동 비례 계산
        auto_adjust = thresholds.get("AUTO_ADJUST_ASK_BID_RATIO", config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True)) if thresholds else config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True)
        
        if auto_adjust and min_ask_bid_ratio > 0 and min_vol > 0:
            ratio_multiplier = min_vol / 100.0
            min_ask_bid_ratio = round(min_ask_bid_ratio * ratio_multiplier, 2)
        
        if vol_strength is not None:
            if vol_strength < min_vol:
                is_vol_ok = False
                vol_reject_reason = f"체결:{vol_strength:.1f}%<{min_vol}%"
            elif ask_bid_ratio is not None and min_ask_bid_ratio > 0:
                # [핵심] 가짜 체결강도 방어 (호가창 매도잔량 비대칭성 확인)
                # 매도 잔량이 매수 잔량보다 최소 기준치 이상 많아야 진짜 상승 에너지로 판단
                if ask_bid_ratio < min_ask_bid_ratio:
                    is_vol_ok = False
                    vol_reject_reason = f"매도잔량비:{ask_bid_ratio:.2f}<{min_ask_bid_ratio}"

        return {
            'action': 'buy' if (state in ["매수", "강매수", "역매수"] and is_vol_ok) else 'wait',
            'state': state,
            'state_reason': state_reason,
            'score': score,
            'rsi': ind['rsi'],
            'adx': ind['adx'],
            'cci': ind['cci'],
            'atr': ind.get('atr', 0), # [추가] ATR
            'psar': ind['psar'],
            'macd': ind.get('macd'),
            'macd_signal': ind.get('macd_signal'),
            'obv_trend': ind.get('obv_trend'),
            'vol_strength': vol_strength,
            'ask_bid_ratio': ask_bid_ratio,
            'vol_reject_reason': vol_reject_reason,
            'smart_money': sm_flag
        }

    def analyze_sell(self, code, name, df, current_price, buy_price, profit_rate, thresholds=None, already_half_sold=False, holding_days=0, is_mr_holding=False, highest_price=0.0):
        """매도 청산 여부 판단"""
        reason = ""
        ind = {}
        score = 0
        state = ""
        sell_ratio = 1.0 # 기본값 전량 매도
        
        # 설정값 로드 (thresholds가 있으면 우선 사용)
        tp_rate = thresholds.get("TAKE_PROFIT_RATE", config.SELL_STRATEGY["TAKE_PROFIT_RATE"]) if thresholds else config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        sl_rate = thresholds.get("STOP_LOSS_RATE", config.SELL_STRATEGY["STOP_LOSS_RATE"]) if thresholds else config.SELL_STRATEGY["STOP_LOSS_RATE"]
        tp_rsi = thresholds.get("TAKE_PROFIT_RSI", config.SELL_STRATEGY["TAKE_PROFIT_RSI"]) if thresholds else config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        sell_score_limit = thresholds.get("SELL_SCORE", config.SELL_STRATEGY["SELL_SCORE"]) if thresholds else config.SELL_STRATEGY["SELL_SCORE"]
        
        # [추가] 반익절 설정 및 계산 (익절 설정의 절반)
        use_half_tp = thresholds.get("HALF_TAKE_PROFIT_USE", config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)) if thresholds else config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
        half_tp_rate = tp_rate / 2.0
        
        # [추가] 시간 청산 설정 로드
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = thresholds.get("TIME_STOP_DAYS", config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)) if thresholds else config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
        time_stop_min_profit = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)
        
        if time_stop_days <= 0:
            use_time_stop = False
        
        mr_grace_loss_rate = thresholds.get("MR_GRACE_LOSS_RATE", config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -7.0)) if thresholds else config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -7.0)
        
        # [추가] 본전 청산(BEP) 및 ATR 기반 트레일링 설정 로드
        use_atr_stop = thresholds.get("USE_ATR_STOP", config.SELL_STRATEGY.get("USE_ATR_STOP", True)) if thresholds else config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        atr_mult = thresholds.get("ATR_STOP_MULTIPLIER", config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)) if thresholds else config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        ts_activation = thresholds.get("ts_activation", config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0)
        ts_callback = thresholds.get("ts_callback", config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0)
        
        bep_activation = thresholds.get("BREAK_EVEN_PROFIT_RATE", config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 7.0)) if thresholds else config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 7.0)
        bep_stop = thresholds.get("BREAK_EVEN_STOP_RATE", config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5)) if thresholds else config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5)
        
        defensive_half_tp = config.SELL_STRATEGY.get("DEFENSIVE_HALF_SELL_USE", True)

        # [추가] 52주 위치 계산 (슈퍼 모멘텀 판정용)
        w52_pos = 0.0

        # 1. 기술적 지표 분석 (시간 청산 시 매수 상태 확인을 위해 우선 수행)
        if df is not None and not df.empty:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100
                
            ind = indicators.calculate_indicators(df)
            # 전일 RSI 계산 (상태 분류용)
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None
            
            is_overseas = not (code.isdigit() and len(code) == 6)
            sm_flag, sm_reason = analysis.check_smart_money_turnaround(code, is_overseas)

            state, _, state_reason = analysis.classify_stock_state(
                df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
            )
            
            score, _ = analysis.calculate_score(
                df=df, ind=ind, weights=thresholds.get('WEIGHTS') if thresholds else None, smart_money=sm_flag
            )
            score = round(score, 1)

        # [추가] 본전 청산(BEP) 임계값 재설정 로직
        is_bep_applied = False
        max_profit_rate = 0.0
        if highest_price > 0 and buy_price > 0:
            max_profit_rate = ((highest_price - buy_price) / buy_price) * 100
            if max_profit_rate >= bep_activation:
                if sl_rate < bep_stop:
                    sl_rate = bep_stop
                    is_bep_applied = True
                    
        # [추가] 트레일링 스탑 동적 콜백 계산 및 판별
        ts_msg = ""
        if highest_price > 0 and buy_price > 0:
            if max_profit_rate >= ts_activation:
                drop_rate = ((highest_price - current_price) / highest_price) * 100
                actual_ts_callback = ts_callback
                
                atr_val = ind.get('atr', 0) if ind else 0
                if use_atr_stop and atr_val > 0:
                    dynamic_callback = (atr_val * atr_mult / highest_price) * 100
                    
                    # [리스크 관리 방어 로직 추가]
                    # 1. 하한선: 너무 작은 변동성으로 인한 조기 털림 방지 (기본 ts_callback 보장)
                    # 2. 상한선: ATR이 너무 커서 도달한 최대 수익의 50% 이상을 반납하는 것 방지
                    max_allowed_callback = max(ts_callback, max_profit_rate * 0.5)
                    actual_ts_callback = min(max(ts_callback, dynamic_callback), max_allowed_callback)
                    
                if drop_rate >= actual_ts_callback:
                    ts_msg = f"트레일링스탑 (최고가:{int(highest_price):,}원, 하락률:-{drop_rate:.1f}%, 기준:-{actual_ts_callback:.1f}%)"

        # 2. 고정 익절/손절 및 시간 청산
        if tp_rate > 0 and profit_rate >= tp_rate:
            reason = f"익절({profit_rate}%)"
        elif tp_rate > 0 and use_half_tp and not already_half_sold and profit_rate >= half_tp_rate:
            reason = f"반익절({profit_rate:.1f}%)"
            sell_ratio = 0.5
        elif sl_rate != 0 and profit_rate <= sl_rate:
            if is_bep_applied:
                reason = f"본전청산({profit_rate:.1f}%)"
            else:
                reason = f"손절({profit_rate:.1f}%)"
        elif use_time_stop and holding_days >= time_stop_days and profit_rate < time_stop_min_profit:
            time_stop_triggered = True
            # [수정] 매도 최적화 4번: 시간 청산 유예 조건을 가격 상방 모멘텀 유무로 엄격하게 변경
            if df is not None and not df.empty and len(df) >= 10:
                if state in ["매수", "강매수", "역매수", "상승"]:
                    recent_5d_high = df['high'].tail(5).max()
                    recent_10d_high = df['high'].tail(10).max()
                    # 최근 5일의 고점이 최근 10일 고점과 같거나 크면 상방 모멘텀이 살아있는 것으로 간주
                    if recent_5d_high >= recent_10d_high:
                        time_stop_triggered = False # 유예
            
            if time_stop_triggered:
                reason = f"시간청산({holding_days}일경과, 상방모멘텀 상실)"
        # 3. 트레일링 스탑 (외부에서 계산된 메시지 반영)
        elif ts_msg:
            reason = ts_msg
        
        if df is not None and not df.empty:
            # [추가] 슈퍼 모멘텀 동적 매도 평가 로직
            use_super = thresholds.get("SUPER_MOMENTUM_USE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)) if thresholds else config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)
            super_score = thresholds.get("SUPER_MOMENTUM_SCORE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5)) if thresholds else config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5)
            super_w52 = thresholds.get("SUPER_MOMENTUM_W52_POS", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)
            super_tp_rsi = thresholds.get("SUPER_TAKE_PROFIT_RSI", config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 85.0)) if thresholds else config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 85.0)
            
            actual_tp_rsi = tp_rsi
            is_super = False
            if use_super and score >= super_score and w52_pos >= super_w52:
                actual_tp_rsi = super_tp_rsi
                is_super = True
                
            # 4. RSI 과열 익절
            if not reason and ind.get('rsi') is not None and ind['rsi'] > actual_tp_rsi:
                if is_super:
                    reason = f"RSI과열(슈퍼모멘텀, 기준:{actual_tp_rsi})"
                else:
                    reason = f"RSI과열(기준:{actual_tp_rsi})"
            
            # [추가] 매도 최적화 3번: 방어적 반매도 (하락 반전 신호 발생 시 절반 덜어내기)
            if not reason and defensive_half_tp and not already_half_sold:
                if ind.get('psar') is not None and ind.get('ema_5') is not None:
                    # [엣지 케이스 방어] 손실 구간에서의 조기 손절(반손절)을 방지하고 '수익 보전' 목적에 맞게,
                    # 최소한의 의미 있는 수익(time_stop_min_profit, 기본 3.0%) 이상일 때만 발동하도록 안전장치 추가
                    if profit_rate >= time_stop_min_profit and current_price < ind['psar'] and current_price < ind['ema_5']:
                        reason = f"하락반전(방어적 반매도, 수익률:+{profit_rate:.1f}%)"
                        sell_ratio = 0.5

            # 5. 추세 이탈
            if not reason and (state == "매도" or score < sell_score_limit):
                rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
                adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
                cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
                
                # [수정] 역추세 매수 종목은 점수 하락뿐만 아니라 '매도' 상태일지라도 지정된 유예 기간 내에는 손절을 보류함
                if is_mr_holding and holding_days <= time_stop_days and profit_rate > mr_grace_loss_rate:
                    pass # 유예 기간 적용
                else:
                    if state == "매도":
                        reason = f"매도진입({state_reason}) [점수:{score}, RSI:{rsi_val}]"
                    else:
                        reason = f"추세이탈({state}/점수하락) [점수:{score}, RSI:{rsi_val}, ADX:{adx_val}, CCI:{cci_val}]"
            
        return {
            'action': 'sell' if reason else 'hold',
            'reason': reason,
            'sell_ratio': sell_ratio,
            'ind': ind,
            'score': score,
            'state': state
        }

class OrderManager:
    """주문 관리 및 상태 추적 전담 클래스"""
    def __init__(self, trader):
        self.trader = trader
        self.pending_orders = {}
        self._lock = threading.RLock()

    def is_pending(self, code):
        """특정 종목의 진행 중인 주문 존재 여부 확인"""
        with self._lock:
            return code in self.pending_orders

    def update_order_status(self, code, odno, status):
        """주문 상태 업데이트"""
        with self._lock:
            if code in self.pending_orders and odno in self.pending_orders[code]:
                current_status = self.pending_orders[code][odno]
                if current_status != status:
                    self.pending_orders[code][odno] = status
                    if status in [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED]:
                        del self.pending_orders[code][odno]
                        if not self.pending_orders[code]:
                            del self.pending_orders[code]
                        self.trader.log(f"[OrderState] 주문 종결({status}): {code} (No.{odno})")
                        
                        # 체결 완료 시 지연 후 보유 종목 리스트 갱신 출력
                        if status == OrderStatus.FILLED:
                            def _delayed_log_holdings():
                                time.sleep(1.5) # KIS API 잔고 갱신 대기
                                self.trader.log_current_holdings()
                            threading.Thread(target=_delayed_log_holdings, daemon=True).start()
                    else:
                        self.trader.log(f"[OrderState] 상태 변경: {code} (No.{odno}) {current_status} -> {status}")

    def register_manual_order(self, code, odno):
        """수동 주문 발생 시 상태 추적 등록 (외부 호출용)"""
        with self._lock:
            if code not in self.pending_orders:
                self.pending_orders[code] = {}
            self.pending_orders[code][odno] = OrderStatus.ORDER_SENT

    def send_order(self, code, qty, type_str, name=None, profit_amt=0, profit_rate=0.0, reason=None, score=0, price=0, rule=None, stop_loss_rate=0.0):
        """주문 전송 및 상태 등록"""
        ord_dvsn = "00" if price > 0 else "01"
        
        self.trader.log(f"━━━━━━━━ [주문 실행] {type_str.upper()} ━━━━━━━━")
        price_log = f"{price:,}원(지정가)" if price > 0 else "시장가(0)"
        
        target_display = f"{name}({code})" if name else code
        amount_log = f"{int(price * qty):,}원" if price > 0 else "-"
        log_detail = f"대상: {target_display}, 수량: {qty}, 단가: {price_log}, 금액: {amount_log}"
        
        if type_str.lower() == 'sell':
            log_detail += f", 손익: {int(profit_amt):+,}원 ({float(profit_rate):+.2f}%)"
            
        self.trader.log(log_detail)

        # [Fix: Point 3] API 지연 중 중복 주문 방지를 위한 임시 ID 선점 (Pre-registration)
        temp_id = f"PRE_{time.time()}"
        with self._lock:
            if code not in self.pending_orders:
                self.pending_orders[code] = {}
            self.pending_orders[code][temp_id] = OrderStatus.ORDER_SENT

        try:
            res_json = api.place_order("domestic", type_str, code, qty, price, ord_dvsn)
            
            if res_json['rt_cd'] == '0':
                odno = res_json['output']['ODNO']
                success_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {type_str.upper()} 성공 | {code} | {qty}주 | No.{odno}"
                
                with self._lock:
                    # 임시 ID 삭제 및 실제 ODNO로 교체
                    if temp_id in self.pending_orders[code]:
                        del self.pending_orders[code][temp_id]
                    self.pending_orders[code][odno] = OrderStatus.ORDER_SENT

                self.trader.trade_history.append(success_msg)
                self.trader.log(f"결과: 성공 (주문번호: {odno})")
                stock_display = f"{name}({code})" if name else code
                
                t_type = "매수" if type_str == 'buy' else "매도"
                title_tag = f"[{t_type} 접수]"
                if rule:
                    title_tag += " [개별]"
                
                msg = f"🚀 {title_tag} {stock_display}\n수량: {qty}주\n단가: {price_log}"
                if price > 0:
                    msg += f"\n금액: {int(price * qty):,}원"
                    
                if type_str.lower() == 'sell':
                    msg += f"\n손익: {int(profit_amt):+,}원 ({float(profit_rate):+.2f}%)"
                    
                msg += f"\n주문번호: {odno}"
                if reason:
                    msg += f"\n사유: {reason}"
                
                if rule:
                    msg += f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                    if rule.get('ts_activation'):
                        msg += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                
                api.send_telegram_message(msg)
                
                snapshot = analysis.get_snapshot(code, is_overseas=False)
                
                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    logger.debug(f"[AutoTrade] 주문 접수 DB 저장 시도: {odno}")
                db_manager.db.insert_trade(f"{type_str}(AUTO)", code, name, qty, str(price), odno, snapshot=snapshot, profit_amt=profit_amt, profit_rate=profit_rate, reason=reason, score=score, stop_loss_rate=stop_loss_rate)
                
                # [추가] DB 큐 처리 시간을 확보하여 체결 감시 모니터가 원주문을 정상 조회할 수 있도록 대기
                time.sleep(0.5)
                ConclusionMonitor().check_now()
                
                if type_str == "buy":
                    init_price = float(price)
                    if init_price <= 0:
                        init_price = api.get_current_price(code, is_overseas=False)
                    
                    if init_price > 0:
                        db_manager.db.update_highest_price(code, init_price)
                        with self.trader._lock:
                            self.trader.trailing_stop_cache[code] = init_price
                        self.trader.log(f"[TrailingStop] 감시 시작가 설정: {name} {init_price:,.0f}원")
                
                return odno
            else:
                with self._lock:
                    if temp_id in self.pending_orders.get(code, {}):
                        del self.pending_orders[code][temp_id]
                        if not self.pending_orders[code]: del self.pending_orders[code]

                err_msg = res_json.get('msg1', 'Unknown Error')
                msg_cd = res_json.get('msg_cd')
                self.trader.log(f"결과: 실패 ({err_msg}) [Code: {msg_cd}]")
                
                stock_display = f"{name}({code})" if name else code
                t_type = "매수" if type_str == 'buy' else "매도"
                fail_msg = f"🚫 [{t_type} 실패] {stock_display}\n수량: {qty}주 / 단가: {price_log}\n원인: {err_msg} (Code: {msg_cd})"
                api.send_telegram_message(fail_msg)
                
                if res_json.get('rt_cd') == '9999' or msg_cd in ['OPSQ2000', 'EGW00201']:
                    raise Exception(f"주문 시스템 치명적 오류: {err_msg}")

        except Exception as e:
            with self._lock:
                if temp_id in self.pending_orders.get(code, {}):
                    del self.pending_orders[code][temp_id]
                    if not self.pending_orders[code]: del self.pending_orders[code]

            self.trader.log(f"결과: 에러 발생 ({str(e)})")
            stock_display = f"{name}({code})" if name else code
            t_type = "매수" if type_str == 'buy' else "매도"
            fail_msg = f"🚫 [{t_type} 에러] {stock_display}\n수량: {qty}주 / 단가: {price_log}\n에러: {str(e)}"
            api.send_telegram_message(fail_msg)
            raise e
        finally:
            self.trader.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return None

    def manage_unfilled_orders(self):
        """오래된 미체결 주문 확인 및 취소"""
        
        # [추가] 장 마감 상태이며 로컬에 진행 중인 주문이 없을 경우 API 호출 생략 (트래픽 낭비 원천 차단)
        if not self.trader.is_market_open():
            with self._lock:
                if not self.pending_orders:
                    return
                    
        try:
            # 1. API를 통한 미체결 내역 조회
            unfilled_list = api.get_unfilled_orders()
            
            api_checked_odnos = set()
            cancel_seconds = getattr(config, 'UNFILLED_ORDER_CANCEL_SECONDS', 120)
            now = datetime.now()
            
            # API 조회 결과 처리
            if unfilled_list:
                for item in unfilled_list:
                    odno = item.get('odno')
                    code = item.get('pdno')
                    name = item.get('prdt_name')
                    qty = int(item.get('rmn_qty', 0))
                    ord_time_str = item.get('ord_tmd')
                    
                    if not odno or qty <= 0 or not ord_time_str: continue
                    api_checked_odnos.add(odno)
                    
                    try:
                        ord_dt = datetime.strptime(f"{now.strftime('%Y%m%d')}{ord_time_str}", "%Y%m%d%H%M%S")
                        elapsed = (now - ord_dt).total_seconds()
                        
                        if elapsed >= cancel_seconds:
                            self.trader.log(f"[미체결 관리] {name}({code}) 주문({odno})이 {int(elapsed)}초 동안 체결되지 않아 취소합니다.")
                            
                            res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
                            
                            if res.get('rt_cd') == '0':
                                trade = db_manager.db.get_trade_by_odno(odno)
                                t_type = ""
                                if trade:
                                    t_str = trade.get('type', '')
                                    t_type = "매수" if "buy" in t_str.lower() or "매수" in t_str else ("매도" if "sell" in t_str.lower() or "매도" in t_str else "")
                                type_label = f"{t_type}취소" if t_type else "주문 취소"
                                api.send_telegram_message(f"🗑 [{type_label}] {name} {qty}주\n사유: 미체결 시간 초과 ({int(elapsed)}초)")
                            else:
                                self.trader.log(f"취소 실패: {res.get('msg1')}")
                    except Exception: pass

            # 2. [추가] API에는 없지만 로컬에는 남아있는 주문 처리 (API 누락 대응)
            # 모의투자 등에서 API가 미체결 내역을 반환하지 않는 경우, 로컬 상태를 믿고 강제 확인
            if config.session.is_simulation:
                with self._lock:
                    pending_codes = list(self.pending_orders.keys())
                    
                    for code in pending_codes:
                        if code not in self.pending_orders: continue
                        orders = self.pending_orders[code]
                        odnos = list(orders.keys())
                        
                        for odno in odnos:
                            if odno in api_checked_odnos: continue
                            
                            status = orders[odno]
                            if status == OrderStatus.ORDER_SENT:
                                trade = db_manager.db.get_trade_by_odno(odno)
                                if trade and trade.get('time'):
                                    try:
                                        ord_time = datetime.strptime(trade['time'], "%Y-%m-%d %H:%M:%S")
                                        elapsed = (now - ord_time).total_seconds()
                                        
                                        if elapsed >= cancel_seconds:
                                            self.trader.log(f"[미체결 관리] 로컬 주문({odno}) 타임아웃({int(elapsed)}초). 강제 취소 시도 (API 누락 대응)")
                                            qty = int(trade['qty'])
                                            res = api.revise_cancel_order("domestic", "cancel", odno, code, qty, "0", "02", "00")
                                            
                                            if res.get('rt_cd') == '0':
                                                self.trader.log(f"-> 강제 취소 성공. (미체결 상태였음)")
                                                
                                                t_str = trade.get('type', '')
                                                t_type = "매수" if "buy" in t_str.lower() or "매수" in t_str else ("매도" if "sell" in t_str.lower() or "매도" in t_str else "")
                                                type_label = f"{t_type}취소" if t_type else "주문 취소"
                                                api.send_telegram_message(f"🗑 [{type_label}] {trade['name']} {qty}주\n사유: 미체결 시간 초과 (API 누락 보정)")
                                                # 원본 접수 기록 보존을 위해 상태 덮어쓰기 로직 제거
                                                
                                                # 취소 주문 번호는 API 응답(res)에서 파싱해야 하나, revise_cancel_order는 현재 json을 반환함
                                                cancel_odno = res.get('output', {}).get('ODNO') or res.get('output', {}).get('KRX_FWDG_ORD_ORGNO') or f"CANCEL_{odno}"
                                                
                                                db_manager.db.insert_trade("취소(자동)", code, trade['name'], qty, 0, cancel_odno, org_odno=odno, reason="미체결 시간 초과 (자동 취소)", order_status="취소")
                                                
                                                # 로컬 상태 정리
                                                if code in self.pending_orders and odno in self.pending_orders[code]:
                                                    del self.pending_orders[code][odno]
                                                    if not self.pending_orders[code]: del self.pending_orders[code]
                                            else:
                                                # 취소 실패 시 (이미 체결되었거나 거부된 주문)
                                                msg_cd = res.get('msg_cd')
                                                # 40330000: 정정/취소할 수량이 없습니다 (이미 체결됨 or 취소됨)
                                                if msg_cd == '40330000':
                                                    self.trader.log(f"-> 이미 체결/취소된 주문입니다. 잔고 확인 후 상태를 동기화합니다.")
                                                    
                                                    # [추가] 잔고 확인을 통해 체결 여부 추정
                                                    is_filled = False
                                                    # 매수 주문이었던 경우 잔고에 해당 종목이 있는지 확인
                                                    if "buy" in trade.get('type', '').lower() or "매수" in trade.get('type', ''):
                                                        try:
                                                            holdings, _ = api.get_domestic_balance(config.session.cano, config.session.acnt_prdt_cd)
                                                            if holdings:
                                                                for h in holdings:
                                                                    if h['pdno'] == code and int(h['hldg_qty']) > 0:
                                                                        is_filled = True
                                                                        break
                                                        except: pass
                                                    elif "sell" in trade.get('type', '').lower() or "매도" in trade.get('type', ''):
                                                        # [추가] 매도 주문인 경우 40330000 에러는 대부분 체결 완료를 의미함
                                                        is_filled = True
                                                    
                                                    if is_filled:
                                                        self.trader.log(f"-> 체결/잔고 확인됨. '체결(추정)'으로 기록합니다.")
                                                        
                                                        fill_price = float(trade['price'])
                                                        is_overseas = not (code.isdigit() and len(code) == 6) if code else False
                                                        if fill_price <= 0:
                                                            try:
                                                                cp = api.get_current_price(code, is_overseas=is_overseas)
                                                                if cp > 0: fill_price = float(cp)
                                                            except: pass

                                                        # 원본 접수 기록 보존을 위해 상태 덮어쓰기 로직 제거
                                                        # 체결 내역 강제 생성 (히스토리 보정)
                                                        db_manager.db.insert_trade(trade['type'], code, trade['name'], qty, fill_price, odno, order_status="체결(추정)", reason="체결 확인(잔고 확인)", custom_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                                                        
                                                        # [수정] 텔레그램 알림 발송 (실전 포맷 적용)
                                                        try:
                                                            type_str = trade.get('type', '')
                                                            type_name = "매수" if "buy" in type_str.lower() or "매수" in type_str else "매도"
                                                            
                                                            # 개별 룰 조회
                                                            custom_rules = db_manager.db.get_all_stock_strategies()
                                                            rules_map = {r['code']: r for r in custom_rules}
                                                            rule = rules_map.get(code)
                                                            
                                                            title_tag = "[체결 알림(추정)]"
                                                            rule_info = ""
                                                            if rule:
                                                                title_tag += " [개별]"
                                                                rule_info = f"\n🔧 [개별 룰] 익절 +{rule['take_profit']}% / 손절 {rule['stop_loss']}%"
                                                                if rule.get('ts_activation'):
                                                                    rule_info += f" / TS +{rule['ts_activation']}%(-{rule['ts_callback']}%)"
                                                            
                                                            # 현재가 정보
                                                            cur_info = ""
                                                            try:
                                                                cp_data = api.get_current_price_data(code, is_overseas=is_overseas)
                                                                if cp_data.get('rt_cd') == '0':
                                                                    if is_overseas:
                                                                        curr = float(cp_data['output'].get('last', 0))
                                                                        rate = float(cp_data['output'].get('rate', 0))
                                                                        icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                                                        cur_info = f"\n현재가: ${curr:,.2f} ({icon} {rate:+.2f}%)"
                                                                    else:
                                                                        curr = float(cp_data['output']['stck_prpr'])
                                                                        rate = float(cp_data['output']['prdy_ctrt'])
                                                                        icon = "🔺" if rate > 0 else ("🔻" if rate < 0 else "➖")
                                                                        cur_info = f"\n현재가: {int(curr):,}원 ({icon} {rate:+.2f}%)"
                                                            except: pass

                                                            # 전략 지표 (스냅샷 활용)
                                                            strategy_info = ""
                                                            if trade.get('snapshot'):
                                                                try:
                                                                    snap = json.loads(trade['snapshot'])
                                                                    if 'indicators' in snap:
                                                                        ind = snap['indicators']
                                                                        score = trade.get('strategy_score', 0)
                                                                        rsi_str = f"{ind.get('rsi', 0):.1f}"
                                                                        adx_str = f"{ind.get('adx', 0):.1f}"
                                                                        cci_str = f"{ind.get('cci', 0):.1f}"
                                                                        strategy_info = f"\n\n📊 [전략 지표(진입시점)]\n• 점수: {score}점\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                                                except: pass
                                                            
                                                            exec_amt = fill_price * qty
                                                            price_fmt = f"${fill_price:,.2f}" if is_overseas else f"{fill_price:,.0f}원"
                                                            amt_fmt = f"${exec_amt:,.2f}" if is_overseas else f"{int(exec_amt):,}원"
                                                            
                                                            profit_msg = ""
                                                            if type_name == "매도":
                                                                p_amt = trade.get('profit_amt')
                                                                p_rate = trade.get('profit_rate')
                                                                if p_amt is not None and p_rate is not None:
                                                                    profit_msg = f"\n손익: {int(p_amt):+,}원 ({float(p_rate):+.2f}%)"
                                                                    
                                                            original_reason = trade.get('reason', '잔고 확인')
                                                            msg = f"✅ {title_tag} {type_name} {trade['name']}({code})\n수량: {qty}주\n단가: {price_fmt}(추정체결가)\n금액: {amt_fmt}\n주문번호: {odno}{profit_msg}\n사유: {original_reason}{cur_info}{strategy_info}{rule_info}"
                                                            api.send_telegram_message(msg)
                                                            
                                                            # [추가] 매도 체결(추정) 시 AI 매매 복기 실행 (모의투자용)
                                                            if type_name == "매도":
                                                                threading.Thread(target=self._send_trading_autopsy, args=(code, trade['name'], trade), daemon=True).start()
                                                        except Exception as e:
                                                            self.trader.log(f"알림 전송 실패: {e}")
                                                    else:
                                                        self.trader.log(f"-> 잔고/체결 확인 안됨. '취소'로 상태를 변경합니다.")
                                                        # 원본 접수 기록 보존 및 취소 더미 이력 생성
                                                        db_manager.db.insert_trade(trade['type'], code, trade['name'], qty, float(trade['price']), odno, order_status="취소(추정)", reason="잔고/체결 확인 안됨 (취소 간주)", custom_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                                                    if code in self.pending_orders and odno in self.pending_orders[code]:
                                                        del self.pending_orders[code][odno]
                                                        if not self.pending_orders[code]: del self.pending_orders[code]
                                                else:
                                                    self.trader.log(f"-> 취소 실패: {res.get('msg1')}")
                                    except Exception as e:
                                        self.trader.log(f"로컬 미체결 처리 중 오류: {e}")
        except Exception as e:
            self.trader.log(f"미체결 관리 중 오류: {e}")

class RiskManager:
    """리스크 관리 및 자산 배분 전담 클래스"""
    def __init__(self, trader):
        self.trader = trader

    def allocate_budget(self, avail_cash, invest_ratio, stop_loss_rate=None, atr=None, current_price=None):
        """자산 배분 계산"""
        if self.trader.initial_asset > 0:
            target_invest_amt = int(self.trader.initial_asset * invest_ratio)
        else:
            target_invest_amt = int(avail_cash * invest_ratio)
        
        base_amt = target_invest_amt
        
        risk_per_trade = getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0)
        risk_based_amt = 0
        
        if risk_per_trade > 0 and stop_loss_rate and abs(stop_loss_rate) > 0:
            total_equity = self.trader.initial_asset if self.trader.initial_asset > 0 else avail_cash
            max_loss_amt = total_equity * (risk_per_trade / 100.0)
            sl_ratio = abs(stop_loss_rate) / 100.0
            risk_based_amt = int(max_loss_amt / sl_ratio)
            target_invest_amt = min(target_invest_amt, risk_based_amt)
        
        scale = 1.0
        if getattr(config, 'USE_VOLATILITY_TARGETING', True) and atr and current_price and current_price > 0:
            daily_vol = atr / current_price
            annual_vol = daily_vol * math.sqrt(252)
            
            target_vol = getattr(config, 'TARGET_VOLATILITY', 0.20)
            scale_max = getattr(config, 'VOLATILITY_SCALING_MAX', 2.0)
            scale_min = getattr(config, 'VOLATILITY_SCALING_MIN', 0.5)
            
            if annual_vol > 0:
                scale = target_vol / annual_vol
                scale = max(scale_min, min(scale_max, scale))
                target_invest_amt = int(target_invest_amt * scale)

        invest_amt = min(target_invest_amt, avail_cash)
        
        log_msg = f"[자산배분] 기초:{base_amt:,}원"
        if risk_based_amt > 0:
            log_msg += f" -> 리스크조정:{risk_based_amt:,}원(손절{abs(stop_loss_rate):.1f}%)"
        if scale != 1.0:
            log_msg += f" -> 변동성조정(x{scale:.2f}):{target_invest_amt:,}원"
        log_msg += f" -> 최종:{invest_amt:,}원"
        
        self.trader.log(log_msg)
            
        return invest_amt

    def check_loss_limit(self, current_total):
        """일일 손실 한도 체크"""
        loss_limit_pct = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
        
        if loss_limit_pct <= 0 or self.trader.initial_asset <= 0: return
        if current_total <= 0: return

        loss_rate = (current_total - self.trader.initial_asset) / self.trader.initial_asset * 100
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[LossCheck] 시작자산:{self.trader.initial_asset:,} -> 현재자산:{current_total:,} | 변동률:{loss_rate:+.2f}% (한도:-{loss_limit_pct}%)")
        
        if loss_rate <= -loss_limit_pct:
            self.trader.log(f"[비상 정지] 일일 손실 한도 초과! (현재: {loss_rate:.2f}% / 제한: -{loss_limit_pct}%)")
            self.trader.log(f"시작 자산: {self.trader.initial_asset:,}원 -> 현재 자산: {current_total:,}원")
            
            # [추가] 화면에 붉은색 경고 출력
            console.print(f"\n[bold red]🛑 [비상 정지] 일일 손실 한도 초과 (수익률: {loss_rate:.2f}% / 제한: -{loss_limit_pct}%)[/bold red]\n[dim]자산 보호를 위해 시스템을 정지했습니다.[/dim]\n")
            
            msg = f"🛑 [비상 정지] 일일 손실 한도 초과\n\n수익률: {loss_rate:.2f}% (제한: -{loss_limit_pct}%)\n현재 자산: {current_total:,}원\n\n자산 보호를 위해 시스템을 정지합니다."
            
            # [추가] 에러 로그 꼬리 첨부 (1시간 쿨타임)
            now = time.time()
            if now - getattr(self.trader, 'last_emergency_alert_time', 0) > 3600:
                log_tail = get_mystock_log_tail(20)
                msg += f"\n\n📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```"
                self.trader.last_emergency_alert_time = now
            
            api.send_telegram_message(msg)
            self.trader.stop(use_status=False)

class AutoTrader:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AutoTrader, cls).__new__(cls)
            cls._instance._lock = threading.RLock() # [추가] 스레드 동기화 락
            cls._instance.is_running = False
            cls._instance.thread = None
            cls._instance.logs = []
            cls._instance.trade_history = []
            cls._instance.trade_records = []
            cls._instance.start_time = None
            cls._instance.consecutive_errors = 0
            cls._instance.initial_asset = 0
            cls._instance.was_market_open = None
            cls._instance.trailing_stop_cache = {} # [추가] 트레일링 스탑 메모리 캐시 (DB 부하 감소용)
            cls._instance.market_status_notified = {} # [수정] 시장 상태 알림 플래그 (시장별 관리)
            cls._instance.market_index_status = {}    # [추가] 지수 상태 캐시
            cls._instance.stock_market_map = {}       # [추가] 종목별 시장 구분 캐시
            cls._instance.stock_state_cache = {}      # [추가] 분석된 종목 상태 캐시 (텔레그램 연동용)
            cls._instance.skipped_by_market_filter_count = {"KOSPI": 0, "KOSDAQ": 0} # [추가] 시장 필터링 보류 종목 수
            cls._instance.strategy = DefaultStrategy() # [추가] 전략 인스턴스
            cls._instance.last_log_date = datetime.now().date() # [추가] 로그 파일 날짜 추적용
            cls._instance.initial_holdings = None # [추가] 초기 조회 잔고 캐시
            cls._instance.initial_summary = None  # [추가] 초기 조회 요약 캐시
            cls._instance.file_logger = config.get_autotrade_logger() # [추가] 파일 로거 초기화
            cls._instance.restricted_notified = {} # [추가] 거래 제한 알림 스로틀링 (종목별 타임스탬프)
            cls._instance.order_manager = OrderManager(cls._instance) # [추가] 주문 매니저
            cls._instance.risk_manager = RiskManager(cls._instance)   # [추가] 리스크 매니저
            cls._instance.half_tp_cache = set()       # [추가] 반익절 실행 여부 추적 캐시
            cls._instance.last_emergency_alert_time = 0 # [추가] 긴급 알림 쿨타임용 타임스탬프
            
            cls._instance.initialized = False # [추가] 초기화 상태 플래그
            # [추가] 로그 디렉토리 확인 및 생성
            log_dir = getattr(config, 'SYSTEM_TRADING_LOG_DIR', 'logs')
            if not os.path.exists(log_dir):
                try:
                    os.makedirs(log_dir)
                except Exception as e:
                    console.print(f"[red]로그 디렉토리 생성 실패: {e}[/red]")
        return cls._instance

    def __init__(self):
        # [추가] 로거 초기화 보장 (인스턴스 호출 시마다 확인)
        # 로거 객체가 없거나 핸들러가 연결되지 않은 경우 재설정
        if not getattr(self, 'file_logger', None) or not self.file_logger.handlers:
            self.file_logger = config.get_autotrade_logger()

    def set_stock_state(self, code, state):
        """종목의 기술적 상태 캐시 업데이트 (텔레그램 /stocks 연동용)"""
        with self._lock:
            if state:
                self.stock_state_cache[code] = state
            else:
                self.stock_state_cache.pop(code, None)

    def _refine_trade_records(self, records):
        """거래 내역 중복 제거 및 우선순위 적용 (전략 사유 > 체결 확인)"""
        unique_records = {}
        
        for r in records:
            odno = r.get('odno')
            # odno가 없으면 고유 키 생성하여 포함
            if not odno:
                key = f"NO_ODNO_{r.get('time', '')}_{r.get('code', '')}_{r.get('type', '')}_{len(unique_records)}"
                unique_records[key] = r
                continue
                
            if odno not in unique_records:
                unique_records[odno] = dict(r) # 복사본 저장
            else:
                existing = unique_records[odno]
                
                # 새 레코드(r)가 더 최신 정보(체결 등)를 담고 있을 때 병합
                if float(r.get('price', 0)) > 0 and float(existing.get('price', 0)) <= 0:
                    existing['price'] = r['price']
                    
                if r.get('profit_amt'):
                    existing['profit_amt'] = r['profit_amt']
                if r.get('profit_rate'):
                    existing['profit_rate'] = r['profit_rate']
                    
                old_reason = str(existing.get('reason', ''))
                new_reason = str(r.get('reason', ''))
                
                if "체결 확인" in old_reason and "체결 확인" not in new_reason:
                    existing['reason'] = new_reason
                elif "체결 확인" not in old_reason and "체결 확인" in new_reason:
                    pass # 기존 구체적 사유 유지
                else:
                    existing['reason'] = new_reason # 최신 사유로 덮어씀

                existing['time'] = r.get('time', existing.get('time'))
                if r.get('order_status'):
                    existing['order_status'] = r['order_status']
        
        return list(unique_records.values())

    def update_order_status(self, code, odno, status):
        """체결 모니터에서 호출하여 주문 상태 업데이트"""
        self.order_manager.update_order_status(code, odno, status)

    def initialize(self):
        """
        자동매매 시작에 필요한 모든 초기화 작업을 병렬로 수행합니다.
        (자산 조회, DB 캐시 로드, 초기 자산 설정 등)
        """
        if self.initialized:
            return True

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
            transient=True,
            disable=not api._is_screen_output_allowed() # [추가] 텔레그램 스레드 등 백그라운드에서는 상태바 숨김
        ) as progress:
            task = progress.add_task("[cyan]자동매매 세션 초기화 중...[/cyan]", total=3)
            
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                # 병렬 실행을 위한 변수
                results = {}

                def _fetch_balance():
                    progress.update(task, description="[cyan]잔고/평가금 조회...[/cyan]")
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    progress.advance(task)
                    return "balance", (holdings, summary)
                
                def _fetch_deposit():
                    progress.update(task, description="[cyan]예수금 상세 조회...[/cyan]")
                    deposit_res = api.get_deposit_balance(target_cano, acnt)
                    progress.advance(task)
                    return "deposit", deposit_res
                
                def _load_db_caches():
                    progress.update(task, description="[cyan]DB 캐시 로드...[/cyan]")
                    ts_cache = db_manager.db.get_all_trailing_stops()
                    half_cache = db_manager.db.get_all_half_tp()
                    progress.advance(task)
                    return "caches", (ts_cache, half_cache)

                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(_fetch_balance), executor.submit(_fetch_deposit), executor.submit(_load_db_caches)]
                    for future in concurrent.futures.as_completed(futures):
                        key, value = future.result()
                        results[key] = value

                # 결과 처리
                holdings, summary = results.get("balance", (None, None))
                deposit_res = results.get("deposit")
                ts_cache, half_cache = results.get("caches", ({}, set()))

                if holdings is None or deposit_res is None:
                    raise Exception("자산/예수금 조회 실패 (API 응답 없음)")

                self.trailing_stop_cache = ts_cache
                self.half_tp_cache = half_cache
                self.initial_holdings = holdings
                self.initial_summary = summary
                
                # [수정] 해외 자산 누락 방지를 위해 account 모듈의 통합 자산 조회 활용
                asset_data = account.get_asset_status_data(target_cano, acnt)
                if asset_data and asset_data.get('tot_asset', 0) > 0:
                    account_key = f"{target_cano}-{acnt}"
                    saved_initial = load_daily_initial_asset(account_key)
                    self.initial_asset = saved_initial if saved_initial > 0 else asset_data['tot_asset']
                    if saved_initial <= 0:
                        save_daily_initial_asset(account_key, self.initial_asset)
                        db_manager.db.save_daily_asset(datetime.now().strftime("%Y-%m-%d"), account_key, self.initial_asset)
                else:
                    self.initial_asset = 0

                self.initialized = True
                return True
        return False

    def start(self, interactive=True):
        if self.is_running:
            if interactive:
                console.print("\n[yellow]이미 자동매매가 실행 중입니다.[/yellow]")
            return
        
        self.log("━━━ 자동매매 시스템 시작 프로세스 진입 ━━━")
        
        if not config.session.is_simulation:
            if not config.session.auto_app_key or not config.session.auto_cano:
                if api._is_screen_output_allowed():
                    console.print("[bold red]오류: 실전 투자 모드에서 시스템 트레이딩을 실행하려면 별도의 자동매매 계좌 설정이 필요합니다.[/bold red]")
                    console.print("[dim]환경 변수 AUTO_APP_KEY, AUTO_APP_SECRET, AUTO_ACC_NUM을 설정해주세요.[/dim]")
                return
            
            if interactive:
                console.print("\n[bold red]!!! 경고: 실전 투자 모드에서 시스템 트레이딩을 시작합니다 !!![/bold red]")
                console.print(f"운용 계좌: [bold yellow]{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}[/bold yellow] (시스템 트레이딩 전용)")
                utils.print_breadcrumb()
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                if api._is_screen_output_allowed():
                    console.print("[bold cyan][시스템 명령] 실전 투자 자동매매를 시작합니다.[/bold cyan]")

        try:
            # [수정] 초기화 로직 분리
            if not self.initialized:
                if not self.initialize():
                    self.log("초기화 실패로 자동매매를 시작할 수 없습니다.")
                    if api._is_screen_output_allowed():
                        console.print("[bold red]시스템 초기화에 실패하여 자동매매를 시작할 수 없습니다.[/bold red]")
                    return

            self.is_running = True
            self.start_time = datetime.now()
            self.consecutive_errors = 0
            self.was_market_open = self.is_market_open()
            self._first_loop_flag = True
            self.market_status_notified = {}
            context.SYSTEM_LOGGER = self.log
            
            self.thread = threading.Thread(target=self._run_loop, daemon=True, name="AutoTrader")
            self.thread.start()

            if api._is_screen_output_allowed():
                console.print("\n[green]자동매매 시스템이 시작되었습니다. (백그라운드)[/green]")
            self.log("시스템 시작")
            
            # [추가] 장 마감 상태에서 시작했을 경우 명확한 안내 메시지 출력
            if not self.was_market_open:
                self.log("━" * 85)
                self.log("💤 [장 마감 대기] 현재는 거래 시간이 아닙니다. 장 시작 시 자동으로 매매가 개시됩니다.")
                self.log("━" * 85)
            
            # [수정] 시작 메시지 생성 로직은 초기화 시 저장된 데이터 활용
            holdings = self.initial_holdings
            summary = self.initial_summary
            deposit = 0
            if self.initial_asset > 0:
                if summary:
                    deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                    if deposit == 0:
                        deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))
            
            msg = f"🟢 [시스템 시작] 자동매매가 시작되었습니다.\n"
            msg += f"초기 자산: {self.initial_asset:,}원"
            msg += f"\n현재 예수금: {deposit:,}원"
            
            # [복원] 상세 자산 현황 추가
            stock_eval_amt = 0
            if summary and len(summary) > 0:
                s_data = summary[0]
                stock_eval_amt = api.safe_int(s_data.get('scts_evlu_amt'))
                total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                
                tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
                if tot_pchs == 0 and holdings:
                    tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                
                rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                msg += f"\n증권 평가 자산: {stock_eval_amt:,}원"
                msg += f"\n증권 평가 손익: {total_profit:+,}원 ({rate:+.2f}%)"

            # [복원] 전략 설정 요약 정보 추가
            buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
            buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]
            sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
            ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
            ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
            sell_score = config.SELL_STRATEGY["SELL_SCORE"]
            tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
            invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2)
            
            use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", False)
            time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)

            msg += "\n\n⚙️ [적용 전략]"
            msg += f"\n• 매수: {buy_score}점↑ & RSI {buy_rsi}↓ & 체결강도 {buy_vol}%↑"
            msg += f"\n• 매도: {sell_score}점 미만 / RSI {tp_rsi} 초과"
            
            tp_str = f"+{tp}%"
            if use_half_tp:
                half_tp_rate = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_RATE", tp / 2.0)
                tp_str += f" (반익절 +{half_tp_rate:.1f}%)"
            
            if use_atr_stop:
                sl_str = f"ATR 동적손절 (x{atr_mult})"
            else:
                sl_str = f"고정 {sl}%"
            
            msg += f"\n• 익절: {tp_str}"
            msg += f"\n• 손절: {sl_str}"
            msg += f"\n• 트레일링: +{ts_act}% 도달 후 -{ts_call}%"
            if use_time_stop:
                msg += f"\n• 시간청산: {time_stop_days}일 경과"
            msg += f"\n• 비중: 종목당 {invest_ratio*100:.0f}%"
                
            # [복원] 보유 종목 현황 추가
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

            if valid_holdings:
                msg += "\n\n📋 [보유 종목 현황]"
                for item in valid_holdings:
                    name = item['prdt_name']
                    qty = int(item['hldg_qty'])
                    cur_price = int(item['prpr'])
                    eval_amt = int(item['evlu_amt'])
                    rate = float(item['evlu_pfls_rt'])
                    profit = int(item['evlu_pfls_amt'])
                    msg += f"\n• {name} ({qty}주)\n  현재가: {cur_price:,}원 | 평가: {eval_amt:,}원\n  손익: {profit:+,}원 ({rate:+.2f}%)"
            else:
                msg += "\n\n📋 [보유 종목] 없음"
                if stock_eval_amt > 0:
                    msg += " (⚠️ 평가금액 존재 - API 데이터 불일치)"

            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                from modules.telegram_bot import TelegramCommander
                reply_markup = TelegramCommander()._get_default_keyboard()
                api.send_telegram_message(msg, reply_markup=reply_markup)

            # 초기화에 사용된 데이터는 비워줌
            self.initial_holdings = None
            self.initial_summary = None

        except Exception as e:
            logger.error(f"자동매매 시작 실패: {e}")
            if api._is_screen_output_allowed():
                console.print(f"[bold red]자동매매 시작 실패: {e}[/bold red]")

            if self.initial_asset > 0:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                account_key = f"{target_cano}-{acnt}"
                saved_msg = " (당일 기준 복원)" if load_daily_initial_asset(account_key) > 0 else " (당일 기준 저장)"
                self.log(f"시스템 시작 자산: {self.initial_asset:,}원{saved_msg}")

    def stop(self, use_status=True):
        if not self.is_running:
            if use_status:
                console.print("\n[yellow]실행 중인 자동매매가 없습니다.[/yellow]")
            return
            
        def _stop_logic():
            self.is_running = False
            ConclusionMonitor().stop() # [추가] 체결 감시 모니터 종료
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(timeout=15) # [수정] 타임아웃 연장 (종목 분석 등 백그라운드 스레드 정상 종료 대기)

        if use_status:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=console,
                transient=True
            ) as progress:
                progress.add_task("[cyan]시스템 중단 요청 처리 중...[/cyan]", total=None)
                _stop_logic()
        else:
            _stop_logic()

        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            if use_status:
                console.print("\n[bold red]경고: 시스템 트레이딩 스레드가 응답하지 않습니다. (DB/API 작업 지연)[/bold red]")
                console.print("[dim]강제로 중단 절차를 진행합니다. 일부 데이터가 누락될 수 있습니다.[/dim]")

        if use_status:
            console.print("\n[red]자동매매 시스템이 중단되었습니다.[/red]")
            
        self.log("시스템 중단")
        
        # [수정] 텔레그램 전송 시 AUTO 계좌 정보가 포함되도록 컨텍스트 설정
        msg = f"⚪️ [시스템 종료] 자동매매가 종료되었습니다.\n시작 자산: {self.initial_asset:,}원"
        
        # [수정] 스레드가 종료된 경우에만 자산 및 보유 종목 조회 (락 충돌 방지)
        if not self.thread or not self.thread.is_alive():
            # [추가] 종료 시 최종 자산 현황 요약 전송
            deposit = 0
            stock_eval = 0
            final_asset = 0
            is_data_valid = False # [추가] 데이터 유효성 플래그

            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                try:
                    # 1. 예수금 조회
                    acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                    res = api.get_deposit_balance(target_cano, acnt)
                    if res:
                        # [수정] 자산 계산 시 D+2 예수금(가수도금) 사용 (매도 대금 포함) - start()와 통일
                        # [Fix] 주문가능금액(d2_deposit)이 아닌 실제 D+2 가수도금(d2_real)을 사용하여 50원 오차 등 왜곡 방지
                        d2_val = res.get('d2_real', 0)
                        if d2_val == 0:
                            d2_val = res.get('d2_deposit', 0)
                        deposit = d2_val + res.get('foreign_deposit', 0)
                        is_data_valid = True
                    else:
                        deposit = 0

                    # 2. 잔고 및 평가금 조회
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    tot_profit = 0
                    tot_pchs = 0

                    if holdings:
                        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                        stock_eval = sum(int(h['evlu_amt']) for h in valid_holdings)
                        tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                    elif summary and len(summary) > 0:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt', 0))
                        tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl', 0))

                    if is_data_valid:
                        final_asset = deposit + stock_eval
                        profit = final_asset - self.initial_asset
                        profit_rate = 0.0 if self.initial_asset <= 0 else (profit / self.initial_asset) * 100
                        stock_rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0

                        msg += f"\n최종 예수금: {deposit:,}원"
                        msg += f"\n증권 평가 자산: {stock_eval:,}원"
                        msg += f"\n증권 평가 손익: {tot_profit:+,}원 ({stock_rate:+.2f}%)"
                        msg += f"\n금일 최종 손익: {profit:+,}원 ({profit_rate:+.2f}%)"
                    else:
                        msg += "\n(⚠️ 종료 시 자산 정보 조회 실패 - 서버 응답 없음)"

                    # [추가] 금일 매매 요약 집계
                    buy_cnt = 0
                    sell_cnt = 0
                    best_stock = None
                    worst_stock = None
                    max_p = 0
                    min_p = 0
                    try:
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}" if config.session.is_simulation else (f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}" if config.session.auto_cano else None)
                        today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=config.session.is_simulation, account=target_account)
                        
                        today_trades_parsed = []
                        for r in today_trades:
                            simple_type = "buy" if "매수" in r['type'] or "buy" in r['type'].lower() else "sell"
                            parsed_r = dict(r)
                            parsed_r['type'] = simple_type
                            today_trades_parsed.append(parsed_r)
                            
                        today_trades_refined = self._refine_trade_records(today_trades_parsed)
                        buy_cnt = len([x for x in today_trades_refined if x['type'] == 'buy'])
                        sell_cnt = len([x for x in today_trades_refined if x['type'] == 'sell'])
                        
                        stock_profits = {}
                        for t in today_trades_refined:
                            if t['type'] == 'sell':
                                code = t.get('code', 'unknown')
                                name = t.get('name', 'Unknown')
                                p_amt = int(float(t.get('profit_amt') or 0))
                                if code not in stock_profits:
                                    stock_profits[code] = {'name': name, 'profit': 0}
                                stock_profits[code]['profit'] += p_amt
                                
                        for code, info in stock_profits.items():
                            if info['profit'] > max_p:
                                best_stock = info
                                max_p = info['profit']
                            if info['profit'] < min_p:
                                worst_stock = info
                                min_p = info['profit']
                    except Exception as e:
                        self.log(f"종료 시 매매 요약 조회 실패: {e}")
                        
                    msg += f"\n오늘 매매 요약: 매수 {buy_cnt}건 / 매도 {sell_cnt}건"
                    if best_stock:
                        msg += f"\n최고 수익: {best_stock['name']} (+{max_p:,}원)"
                    if worst_stock:
                        msg += f"\n최대 손실: {worst_stock['name']} ({min_p:,}원)"

                    # [수정] 보유수량 0 초과인 종목만 필터링
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

                    if valid_holdings:
                        msg += "\n\n📋 [최종 보유 종목 현황]"
                        for item in valid_holdings:
                            name = item['prdt_name']
                            qty = int(item['hldg_qty'])
                            cur_price = int(item['prpr'])
                            eval_amt = int(item['evlu_amt'])
                            rate = float(item['evlu_pfls_rt'])
                            profit_amt = int(item['evlu_pfls_amt'])
                            msg += f"\n• {name} ({qty}주)\n  현재가: {cur_price:,}원 | 평가: {eval_amt:,}원\n  손익: {profit_amt:+,}원 ({rate:+.2f}%)"
                    else:
                        msg += "\n\n📋 [최종 보유 종목] 없음"
                        if not is_data_valid:
                            msg += " (조회 실패 가능성 있음)"
                except Exception as e:
                    self.log(f"종료 시 자산/잔고 조회 실패: {e}")
                    msg += "\n(자산 조회 실패)"
        else:
            msg += "\n(시스템 응답 지연으로 최종 자산 정보 생략)"
            self.log("스레드 종료 지연으로 최종 자산/잔고 조회 생략")
            
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            from modules.telegram_bot import TelegramCommander
            reply_markup = TelegramCommander()._get_default_keyboard()
            api.send_telegram_message(msg, reply_markup=reply_markup)
        
        # [추가] 로거 연결 해제 (메시지 전송 후 해제)
        context.SYSTEM_LOGGER = None

    def log_current_holdings(self):
        """현재 보유 종목 현황을 조회하여 로그에 출력합니다 (체결 후 호출용)"""
        try:
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
            with utils.AccountContext(target_cano):
                holdings, _ = api.get_domestic_balance(target_cano, acnt)
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                
                def pad(s, width, align='>'):
                    k = sum(1 for c in s if ord(c) > 127)
                    real_len = len(s) + k
                    pad_len = width - real_len
                    if pad_len < 0: pad_len = 0
                    if align == '<': return s + ' ' * pad_len
                    else: return ' ' * pad_len + s

                header = (
                    f"{pad('종목명', 30, '<')} "
                    f"{pad('보유수량', 10, '>')} "
                    f"{pad('매입단가', 12, '>')} "
                    f"{pad('현재가', 12, '>')} "
                    f"{pad('매입금액', 15, '>')} "
                    f"{pad('평가금액', 15, '>')} "
                    f"{pad('평가손익', 14, '>')} "
                    f"{pad('수익률', 10, '>')}"
                )
                
                self.log("")
                self.log("─" * 125)
                self.log(header)
                self.log("─" * 125)
                
                if not valid_holdings:
                    self.log(f"{pad('보유 종목 없음', 30, '<')} ")
                else:
                    for item in valid_holdings:
                        name = f"{item['prdt_name']} ({item['pdno']})"
                        qty = int(item['hldg_qty'])
                        buy_price = float(item['pchs_avg_pric'])
                        cur_price = int(item['prpr'])
                        pchs_amt = int(item.get('pchs_amt', 0))
                        eval_amt = int(item.get('evlu_amt', 0))
                        profit = int(item['evlu_pfls_amt'])
                        rate = float(item['evlu_pfls_rt'])
                        
                        row_str = f"{pad(name, 30, '<')} {pad(f'{qty:,}주', 10, '>')} {pad(f'{buy_price:,.0f}원', 12, '>')} {pad(f'{cur_price:,.0f}원', 12, '>')} {pad(f'{pchs_amt:,}원', 15, '>')} {pad(f'{eval_amt:,}원', 15, '>')} {pad(f'{profit:+,}원', 14, '>')} {pad(f'{rate:.2f}%', 10, '>')}"
                        self.log(row_str)
                self.log("─" * 125)
                self.log("")
        except Exception as e:
            self.log(f"보유 종목 로깅 실패: {e}")

    def get_status_message(self):
        """텔레그램 전송용 상태 요약 메시지 생성"""
        status_text = "STOPPED"
        status_icon = "🔴"
        if self.is_running:
            if self.is_market_open():
                status_text = "RUNNING"
                status_icon = "🟢"
            else:
                status_text = "WAITING"
                status_icon = "🟡"
        
        msg = f"{status_icon} [시스템 상태: {status_text}]\n"
        
        # 자산 정보 조회
        current_asset = None
        deposit = 0
        holdings = []

        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            try:
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                
                # 1. 잔고 조회 (평가금 포함)
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                # 2. 예수금 및 총 자산 계산
                if config.session.is_simulation:
                    if summary:
                        # [수정] D+2 예수금(가수도금) 사용 (매도 대금 포함, /balance와 통일)
                        deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                else:
                    res = api.get_deposit_balance(target_cano, acnt)
                    if res:
                        d2_val = res.get('d2_real', 0)
                        if d2_val == 0: d2_val = res.get('d2_deposit', 0)
                        deposit = d2_val
                
                # [수정] 보유 종목 개별 합산으로 평가금액 직접 계산 (데이터 정합성 보장)
                tot_evlu = 0
                if holdings:
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                    if valid_holdings:
                        tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                elif summary:
                    tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                
                current_asset = deposit + tot_evlu
            except: pass

        if current_asset is not None:
            tot_profit = 0
            tot_pchs = 0
            
            # [수정] API 요약 데이터 대신 보유 종목 합산 (데이터 불일치 방지)
            if holdings:
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                if valid_holdings:
                    tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                    tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
            
            rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                account_key = f"{target_cano}-{acnt}"
                saved_initial = load_daily_initial_asset(account_key)
                if saved_initial > 0:
                    self.initial_asset = saved_initial
            
            if self.initial_asset > 0:
                msg += f"오늘 시작 자산: {self.initial_asset:,}원\n"
            else:
                msg += f"오늘 시작 자산: - (미설정)\n"
                
            msg += f"오늘 현재 자산: {current_asset:,}원\n"
            
            if self.initial_asset > 0:
                daily_profit = current_asset - self.initial_asset
                daily_profit_rate = (daily_profit / self.initial_asset) * 100
                msg += f"오늘 현재 손익: {daily_profit:+,}원 ({daily_profit_rate:+.2f}%)\n"
                
            realized_profit = 0
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                
                target_account = None
                if config.session.is_simulation:
                    target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                elif config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                    
                today_trades = db_manager.db.get_trades(
                    start_date=today_str, end_date=today_str, 
                    is_sim=config.session.is_simulation, account=target_account
                )
                
                today_trades_parsed = []
                for r in today_trades:
                    type_str = r['type']
                    simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                    parsed_r = dict(r)
                    parsed_r['type'] = simple_type
                    today_trades_parsed.append(parsed_r)
                
                today_trades_refined = self._refine_trade_records(today_trades_parsed)
                sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
                realized_profit = sum(int(t.get('profit_amt') or 0) for t in sell_trades)
            except: pass
            
            realized_rate = (realized_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
            msg += f"오늘 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)\n"
            msg += f"증권 평가 자산: {tot_evlu:,}원\n"
            msg += f"증권 평가 손익: {tot_profit:+,}원 ({rate:+.2f}%)\n"
            msg += f"주문 가능 금액: {deposit:,}원\n"
        else:
            msg += "자산 정보 조회 실패\n"
            
        # [수정] 현재 시장 상황 정보
        msg += "\n[시장 상황]\n"
        regime_map = {"Bull": "🔴 강세장", "Bear": "🔵 약세장", "Sideways": "🟡 횡보장"}
        regime_ma = config.MARKET_REGIME_PARAMS.get('REGIME_MA_PERIOD', 20)

        for m_type, label in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
            try:
                regime, _ = analysis.get_market_regime(m_type)
                regime_str = regime_map.get(regime, regime)
                msg += f"• {label}: {regime_str} (EMA {regime_ma}일 기준)\n"
            except Exception:
                msg += f"• {label}: 확인 불가\n"

        # [추가] 시장 지수 요약 정보 및 필터링 상태 (시장 상황 아래 배치)
        use_filter = getattr(config, 'USE_MARKET_FILTER', True)
        filter_str = "ON" if use_filter else "OFF"
        filter_ma = getattr(config, 'MARKET_FILTER_MA', 50)
        msg += f"\n[시장 지수 및 필터링 (필터: {filter_str}, SMA {filter_ma}일 기준)]\n"
        
        is_healthy_k = True
        is_healthy_q = True

        try:
            for name, m_type in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
                df = analysis.get_domestic_index_data(m_type)
                if df is not None and not df.empty:
                    curr = df.iloc[-1]['close']
                    prev = df.iloc[-2]['close'] if len(df) > 1 else curr
                    rate = ((curr - prev) / prev) * 100
                    
                    filter_msg = ""
                    if use_filter:
                        # 시스템 루프의 상태 캐시(market_index_status)를 우선 적용하여 보류 카운트와 상태 불일치 방지
                        cached_stat = self.market_index_status.get(m_type)
                        
                        if cached_stat and isinstance(cached_stat, dict) and cached_stat.get('current', 0) > 0:
                            is_healthy = cached_stat.get('is_healthy', True)
                            filter_msg = " [🟢허용]" if is_healthy else " [🚫보류]"
                        else:
                            # 대기 상태(WAITING) 등 캐시가 없을 때만 실시간 계산
                            ma_period = getattr(config, 'MARKET_FILTER_MA', 50)
                            if len(df) >= ma_period:
                                ma_val = df['close'].rolling(window=ma_period).mean().iloc[-1]
                                is_healthy = curr >= ma_val
                                filter_msg = " [🟢허용]" if is_healthy else " [🚫보류]"
                            else:
                                is_healthy = True
                                filter_msg = " [데이터부족]"
                                
                        if m_type == "KOSPI":
                            is_healthy_k = is_healthy
                        elif m_type == "KOSDAQ":
                            is_healthy_q = is_healthy
                            
                    msg += f"• {name}: {curr:,.2f} ({rate:+.2f}%){filter_msg}\n"
        except: pass
        
        if use_filter:
            skip_k = self.skipped_by_market_filter_count.get("KOSPI", 0)
            skip_q = self.skipped_by_market_filter_count.get("KOSDAQ", 0)
            
            # [추가] 분석 루프가 돌지 않았을 경우(0건) stock.json 기준으로 실제 보류 대상 개수 산출
            if (not is_healthy_k and skip_k == 0) or (not is_healthy_q and skip_q == 0):
                calc_k, calc_q = self._get_skipped_stocks_count(holdings)
                if not is_healthy_k and skip_k == 0: skip_k = calc_k
                if not is_healthy_q and skip_q == 0: skip_q = calc_q
            
            skip_msg = []
            if not is_healthy_k or skip_k > 0:
                skip_msg.append(f"KOSPI {skip_k}종목")
            if not is_healthy_q or skip_q > 0:
                skip_msg.append(f"KOSDAQ {skip_q}종목")
                
            if skip_msg:
                msg += f"⚠️ 하락장 방어 중 (현재 {', '.join(skip_msg)} 신규 매수 보류)\n"

        # [수정] 보유수량 0 초과인 종목만 필터링
        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

        if valid_holdings:
            msg += "\n📋 [보유 종목 현황]"
            for item in valid_holdings:
                name = item['prdt_name']
                qty = int(item['hldg_qty'])
                cur_price = int(item['prpr'])
                eval_amt = int(item['evlu_amt'])
                rate = float(item['evlu_pfls_rt'])
                profit = int(item['evlu_pfls_amt'])
                msg += f"\n• {name} ({qty}주)\n  현재가: {cur_price:,}원 | 평가: {eval_amt:,}원\n  손익: {profit:+,}원 ({rate:+.2f}%)"
        else:
            msg += "\n📋 [보유 종목] 없음"
            
        return msg

    def _get_skipped_stocks_count(self, holdings):
        """현재 관심 종목 중 미보유 종목을 대상으로 시장별 대기 종목 수를 계산합니다."""
        targets = config.session.stock_data.get("stocks_kr", [])
        if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
            targets += config.session.stock_data.get("etfs_kr", [])
        holding_codes = {h['pdno'] for h in holdings if int(h.get('hldg_qty', 0)) > 0} if holdings else set()
        
        count_k = 0
        count_q = 0
        for item in targets:
            code = item['code']
            if code in holding_codes:
                continue
            m_type = self._get_stock_market_type(code)
            if m_type == "KOSDAQ": count_q += 1
            else: count_k += 1
            
        return count_k, count_q

    def log(self, msg):
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_msg = f"[{timestamp}] {msg}"
        self.logs.append(log_msg)
        if len(self.logs) > 300: self.logs.pop(0)
        
        # [추가] 로거가 없으면 재할당 시도 (안전장치)
        if not getattr(self, 'file_logger', None) or not self.file_logger.handlers:
            self.file_logger = config.get_autotrade_logger()

        # [수정] 로거를 통해 파일 기록 (자동 로테이션)
        if self.file_logger:
            try:
                self.file_logger.info(log_msg)
            except Exception as e:
                # 파일 쓰기 실패 시 콘솔에만 출력하고 중단하지 않음
                if threading.current_thread().name != "TelegramBot":
                    console.print(f"[dim red]로그 파일 기록 실패: {e}[/dim red]")

    def get_recent_logs(self):
        """최근 로그 반환 (텔레그램용)"""
        if not self.logs:
            return "📭 로그가 없습니다."
        
        final_logs = []
        current_len = 0
        max_len = 3800 # 텔레그램 제한(4096자) 고려하여 여유 있게 설정
        
        header = "📜 [최근 시스템 로그]\n"
        current_len += len(header)

        for log in reversed(self.logs):
            if current_len + len(log) + 1 > max_len:
                break
            final_logs.append(log)
            current_len += len(log) + 1
        
        final_logs.reverse()
        return header + "\n".join(final_logs)

    def print_status(self):
        utils.clear_screen()
        utils.print_breadcrumb()
        
        if not self.is_running:
            status_text = "STOPPED"
            status_color = "red"
        elif self.is_market_open():
            status_text = "RUNNING"
            status_color = "green"
        else:
            status_text = "WAITING"
            status_color = "yellow"
        
        kospi_regime, kospi_adj = "확인 불가", 0.0
        kosdaq_regime, kosdaq_adj = "확인 불가", 0.0

        # 3. 자산 및 손익 현황 (안전성 핵심)
        current_asset = None
        deposit = 0
        holdings = []
        
        # [추가] 상태 조회 시에도 시스템 트레이딩 컨텍스트 사용
        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[cyan]보유 종목 및 자산 정보 조회 중...[/cyan]", total=None)
                progress.update(task, description="[cyan]보유 종목 및 잔고 조회 중...[/cyan]")
                # [추가] 보유 종목 확인
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                try:
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                except: 
                    holdings = []
                    summary = []
                
                progress.update(task, description="[cyan]예수금 정보 확인 중...[/cyan]")
                # 예수금 별도 조회 (매수 여력 확인용)
                try:
                    if summary and len(summary) > 0:
                        deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                        if config.session.is_simulation:
                            deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                    
                    # [수정] 실전 투자는 항상 상세 조회 시도 (정확도 우선)
                    if deposit == 0 or not config.session.is_simulation:
                        res = api.get_deposit_balance(target_cano, acnt)
                        if res:
                            deposit = res.get('d2_real', 0)
                            if deposit == 0: deposit = res.get('d2_deposit', 0)
                except: pass

                # [수정] 중복 API 호출 방지 및 동일 스냅샷 기반 현재 자산 일괄 계산
                tot_evlu = 0
                if holdings:
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                    tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                elif summary and len(summary) > 0:
                    tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt', 0))

                current_asset = deposit + tot_evlu
                
                progress.update(task, description="[cyan]시장 국면(KOSPI/KOSDAQ) 분석 중...[/cyan]")
                try:
                    kospi_regime, kospi_adj = analysis.get_market_regime("KOSPI")
                    kosdaq_regime, kosdaq_adj = analysis.get_market_regime("KOSDAQ")
                except:
                    pass

                # [추가] 지수 상태 정보가 없으면 업데이트 시도 (시장 필터링 사용 시)
                # 시스템이 정지 상태이거나 장 시작 전이라도 상태 조회 시에는 최신 정보를 보여주기 위함
                if getattr(config, 'USE_MARKET_FILTER', True):
                    need_update = False
                    if "KOSPI" not in self.market_index_status or "KOSDAQ" not in self.market_index_status:
                        need_update = True
                    elif self.market_index_status.get("KOSPI", {}).get("current", 0) == 0 or \
                         self.market_index_status.get("KOSDAQ", {}).get("current", 0) == 0:
                        need_update = True
                    
                    if need_update:
                        progress.update(task, description="[cyan]시장 지수(KOSPI/KOSDAQ) 상태 업데이트 중...[/cyan]")
                        self._update_market_indices_status(notify=False)

        console.print()
        table = Table(title=f"시스템 트레이딩 상태 ({status_text})", title_justify="center", title_style="", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("구분", justify="left", style="cyan", width=15)
        table.add_column("상세 내용", justify="left")

        # 1. 실행 정보
        if self.is_running and self.start_time:
            start_str = self.start_time.strftime("%Y-%m-%d %H:%M:%S")
            elapsed = datetime.now() - self.start_time
            elapsed_str = str(elapsed).split('.')[0]
            table.add_row("실행 시간", f"{start_str} (경과: {elapsed_str})")
        
        # 2. 마켓 상태
        market_status = "장 운영 중 (거래 가능)" if self.is_market_open() else "장 마감/휴장 (대기 중)"
        if datetime.now().weekday() > 4: market_status = "주말 휴장 (대기 중)"
        table.add_row("마켓 상태", market_status)
        
        # [추가] 시장 국면 상태 표시
        regime_map = {"Bull": "[red]강세장[/]", "Bear": "[blue]약세장[/]", "Sideways": "[yellow]횡보장[/]"}
        k_regime_str = regime_map.get(kospi_regime, kospi_regime)
        q_regime_str = regime_map.get(kosdaq_regime, kosdaq_regime)
        regime_ma = config.MARKET_REGIME_PARAMS.get('REGIME_MA_PERIOD', 20)
        table.add_row("시장 국면", f"KOSPI: {k_regime_str} (보정: {kospi_adj:+.1f}점) / KOSDAQ: {q_regime_str} (보정: {kosdaq_adj:+.1f}점) [dim](EMA {regime_ma}일 기준)[/]")

        # [추가] 지수 추세 상태 표시 (시장 필터링 사용 시)
        if getattr(config, 'USE_MARKET_FILTER', True):
            # ... existing code ...
            kospi_stat = self.market_index_status.get("KOSPI")
            kosdaq_stat = self.market_index_status.get("KOSDAQ")
            
            def get_stat_msg(stat):
                if not stat or not isinstance(stat, dict) or stat.get('current', 0) == 0:
                    return "[dim]확인 중[/]"
                
                is_healthy = stat.get('is_healthy', True)
                current = stat.get('current', 0)
                trend_icon = "(상승)" if is_healthy else "(하락)"
                color = "red" if is_healthy else "blue"
                return f"[{color}]{current:,.0f} {trend_icon}[/]"
            
            table.add_row("지수 추세", f"KOSPI: {get_stat_msg(kospi_stat)} / KOSDAQ: {get_stat_msg(kosdaq_stat)}")
            
            # [추가] 필터링 보류 개수 표시
            skip_k = self.skipped_by_market_filter_count.get("KOSPI", 0)
            skip_q = self.skipped_by_market_filter_count.get("KOSDAQ", 0)
            
            is_healthy_k = kospi_stat.get('is_healthy', True) if isinstance(kospi_stat, dict) else True
            is_healthy_q = kosdaq_stat.get('is_healthy', True) if isinstance(kosdaq_stat, dict) else True
            
            # [추가] 분석 루프가 돌지 않았을 경우(0건) stock.json 기준으로 실제 보류 대상 개수 산출
            if (not is_healthy_k and skip_k == 0) or (not is_healthy_q and skip_q == 0):
                calc_k, calc_q = self._get_skipped_stocks_count(holdings)
                if not is_healthy_k and skip_k == 0: skip_k = calc_k
                if not is_healthy_q and skip_q == 0: skip_q = calc_q
            
            skip_msg = []
            if not is_healthy_k or skip_k > 0: skip_msg.append(f"KOSPI {skip_k}종목")
            if not is_healthy_q or skip_q > 0: skip_msg.append(f"KOSDAQ {skip_q}종목")
            
            if skip_msg:
                filter_ma = getattr(config, 'MARKET_FILTER_MA', 50)
                table.add_row("시장 필터링", f"[bold blue]{', '.join(skip_msg)} 매수 보류[/] [dim](SMA {filter_ma}일 이탈)[/]")

        table.add_section()
        
        # [추가] 개별 종목 룰 설정 현황
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = _enrich_rules_with_weights(custom_rules) # [Fix] 가중치 JSON 파싱
        rule_table = None
        rule_summary = None
        
        # 보유 종목 코드 집합 생성 (강조 표시용)
        held_codes = set()
        if holdings:
            for h in holdings:
                if int(h.get('hldg_qty', 0)) > 0:
                    held_codes.add(h.get('pdno'))

        if custom_rules:
            rule_summary = f"총 {len(custom_rules)}개 종목 개별 설정됨"
            
            # 별도 테이블로 상세 표시
            rule_table = Table(title="종목별 개별 트레이딩 룰 목록", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            rule_table.add_column("종목명(코드)", justify="left")
            rule_table.add_column("매수(점수/RSI/체결/비대칭)", justify="center")
            rule_table.add_column("청산(익절/TS/RSI/기한)", justify="center")
            rule_table.add_column("리스크(비중/손절)", justify="center")
            rule_table.add_column("가중치", justify="center") # [추가]
            rule_table.add_column("수정일", justify="center", style="dim")
            
            for i, r in enumerate(custom_rules):
                # 보유 중인 종목이면 종목명 강조 (bold cyan)
                name_disp = f"{r['name']}({r['code']})"
                if r['code'] in held_codes:
                    name_disp = f"[bold cyan]{name_disp}[/]"
                
                w_str = "기본"
                if r.get('weights'):
                    w = r['weights']
                    w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

                sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
                ratio_str = f"{r.get('invest_ratio', getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2)) * 100:.0f}%"

                rule_table.add_row(
                    name_disp,
                    f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get('BUY_VOL_STRENGTH', 100.0))}% / {r.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0))}배",
                    f"+{r['take_profit']}% / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
                    f"{ratio_str} / {sl_str}",
                    w_str,
                    r.get('updated_at', '-')
                )
                if (i + 1) % 5 == 0 and (i + 1) < len(custom_rules):
                    rule_table.add_section()

        # 금일 매매 & 실현 손익 계산 (상단 이동)
        today_profit = 0
        buy_cnt = 0
        sell_cnt = 0

        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            
            target_account = None
            if config.session.is_simulation:
                target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
            elif config.session.auto_cano:
                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            today_trades = db_manager.db.get_trades(
                start_date=today_str, end_date=today_str, 
                is_sim=config.session.is_simulation, account=target_account
            )
            
            today_trades_parsed = []
            for r in today_trades:
                type_str = r['type']
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                parsed_r = dict(r)
                parsed_r['type'] = simple_type
                today_trades_parsed.append(parsed_r)
            
            # 중복 제거 및 정제
            today_trades_refined = self._refine_trade_records(today_trades_parsed)
            
            buy_trades = [x for x in today_trades_refined if x['type'] == 'buy']
            sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
            
            buy_cnt = len(buy_trades)
            sell_cnt = len(sell_trades)
            
            # 금일 실현 손익 합산
            for t in sell_trades:
                today_profit += int(t.get('profit_amt') or 0)
                
        except Exception:
            # DB 조회 실패 시 메모리 값 사용 (Fallback)
            buy_cnt = len([x for x in self.trade_records if x['type'] == 'buy'])
            sell_cnt = len([x for x in self.trade_records if x['type'] == 'sell'])
            for r in self.trade_records:
                if r['type'] == 'sell':
                    today_profit += int(r.get('profit_amt') or 0)

        # 3. 자산 현황
        if current_asset is not None:
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                account_key = f"{target_cano}-{acnt}"
                saved_initial = load_daily_initial_asset(account_key)
                if saved_initial > 0:
                    self.initial_asset = saved_initial
                    
            tot_profit = 0
            tot_pchs = 0
            tot_evlu = 0
            
            # [수정] API 요약 데이터 대신 보유 종목 합산 (데이터 불일치 방지)
            if holdings:
                valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                if valid_holdings:
                    tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                    tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                    tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
            
            rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            color = "[red]" if tot_profit > 0 else ("[blue]" if tot_profit < 0 else "[white]")
            
            table.add_row("증권 매입 금액", f"{tot_pchs:,}원")
            table.add_row("증권 평가 금액", f"{tot_evlu:,}원")
            table.add_row("증권 평가 손익", f"{color}{tot_profit:+,}원 ({rate:+.2f}%)[/]")
            table.add_row("주문 가능 금액", f"{deposit:,}원")
            
            table.add_section()

            if self.initial_asset > 0:
                table.add_row("오늘 시작 자산", f"{self.initial_asset:,}원")
                table.add_row("오늘 현재 자산", f"{current_asset:,}원")
                
                daily_profit = current_asset - self.initial_asset
                daily_profit_rate = (daily_profit / self.initial_asset) * 100
                dp_color = "[red]" if daily_profit > 0 else ("[blue]" if daily_profit < 0 else "[white]")
                table.add_row("오늘 현재 손익", f"{dp_color}{daily_profit:+,}원 ({daily_profit_rate:+.2f}%)[/]")
                
                realized_rate = (today_profit / self.initial_asset) * 100
                rp_color = "[red]" if today_profit > 0 else ("[blue]" if today_profit < 0 else "[white]")
                table.add_row("오늘 실현 손익", f"{rp_color}{today_profit:+,}원 ({realized_rate:+.2f}%)[/]")
            else:
                table.add_row("오늘 시작 자산", "- (미설정)")
                table.add_row("오늘 현재 자산", f"{current_asset:,}원")
                table.add_row("오늘 현재 손익", "-")
                table.add_row("오늘 실현 손익", "-")
        else:
            if self.initial_asset > 0:
                table.add_row("오늘 시작 자산", f"{self.initial_asset:,}원")

        table.add_section()

        # 4. 설정 및 상태 정보 (재구성)
        # 매수 조건
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        buy_ask_ratio = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
        auto_adj = "ON" if config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True) else "OFF"
        table.add_row("매수 조건", f"{buy_score}점↑ / RSI {buy_rsi}↓ / 체결강도 {buy_vol}%↑ / 비대칭 {buy_ask_ratio}배↑ (자동연동: {auto_adj})")

        # [추가] 역추세 매수 표시
        use_mr = config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)
        mr_status = "[green]ON[/]" if use_mr else "[red]OFF[/]"
        mr_rsi = config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
        mr_disp = config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
        mr_vol = config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
        table.add_row("", f"역매수 (RSI {mr_rsi}↓ / 20일선 이격도 {mr_disp}%↓ / 체결 {mr_vol}%↑) {mr_status}")

        # 매도 조건
        sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
        
        use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True)
        use_atr = config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
        time_stop_min = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)

        table.add_row("매도 조건", f"추세이탈 ({sell_score}점 미만) / 과열 매도 (RSI {tp_rsi} 초과)")
        
        # 익절 / 반익절
        tp_str = f"익절 (+{tp}%)"
        half_tp_status = "[green]ON[/]" if use_half_tp else "[red]OFF[/]"
        tp_str += f" / 반익절 (+{tp/2:.1f}%, 50%) {half_tp_status}"
        table.add_row("", tp_str)
        
        # ATR손절 / 고정손절
        atr_status = "[green]ON[/]" if use_atr else "[red]OFF[/]"
        sl_str = f"ATR손절 (x{atr_mult}) {atr_status}"
        fixed_sl_status = "[red]OFF[/]" if use_atr else "[green]ON[/]"
        sl_str += f" / 고정손절 ({sl}%) {fixed_sl_status}"
        table.add_row("", sl_str)

        time_stop_status = "[green]ON[/]" if use_time_stop else "[red]OFF[/]"
        table.add_row("", f"시간청산 ({time_stop_days}일 경과 & 수익률 +{time_stop_min}% 미만) {time_stop_status}")
        
        table.add_row("", f"트레일링스탑 (+{ts_act}%/-{ts_call}%)")

        # 투자 설정
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2)
        if invest_ratio <= 0: invest_ratio = 0.2
        max_holdings = getattr(config, 'SYSTEM_MAX_HOLDINGS', 10)
        include_etf = getattr(config, 'SYSTEM_INCLUDE_ETF', False)
        etf_str = "포함" if include_etf else "제외"
        table.add_row("투자 설정", f"비중 {invest_ratio*100:.0f}% (최대 {max_holdings}종목, ETF {etf_str})")

        # 손실 제한
        loss_limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
        if loss_limit > 0:
            safety_msg = "[green]안전[/green]"
            if current_asset is not None and self.initial_asset > 0:
                profit = current_asset - self.initial_asset
                rate = (profit / self.initial_asset) * 100
                
                if rate <= -loss_limit: safety_msg = "[bold red]위험 (한도 초과)[/bold red]"
                elif rate <= -(loss_limit * 0.8): safety_msg = "[bold orange3]주의 (한도 임박)[/bold orange3]"
            table.add_row("손실 제한", f"-{loss_limit}% (상태: {safety_msg})")
        else:
            table.add_row("손실 제한", "미사용")

        # 연속 에러
        err_cnt = self.consecutive_errors
        max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
        if err_cnt == 0:
            err_display = f"[dim green]{err_cnt} / {max_err}회[/]"
        else:
            err_color = "[red]" if err_cnt >= max_err else "[yellow]"
            err_display = f"{err_color}{err_cnt} / {max_err}회[/]"
        table.add_row("연속 에러", err_display)
        
        table.add_row("오늘 매매", f"[red]매수 {buy_cnt}건[/] / [blue]매도 {sell_cnt}건[/]")
        
        if rule_summary:
            table.add_section()
            table.add_row("개별 룰 설정", rule_summary)

        console.print(table)
        
        if rule_table:
            console.print()
            console.print(rule_table)
            console.print()
        
        # [추가] 보유 종목 리스트 출력
        # [수정] 보유수량 0 초과인 종목만 필터링
        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

        if valid_holdings:
            holding_rows = []
            # [추가] 시장 구분 등 추가 정보를 가져오는 지연 시간에 대응하기 위한 프로그레스 바 적용
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[cyan]보유 종목 세부 정보 조회 중...[/cyan]", total=len(valid_holdings))
                for item in valid_holdings:
                    name = item['prdt_name']
                    code = item['pdno']
                    market_type = self._get_stock_market_type(code)
                    qty = int(item['hldg_qty'])
                    buy_price = float(item['pchs_avg_pric'])
                    cur_price = int(item['prpr'])
                    profit = int(item['evlu_pfls_amt'])
                    rate = float(item['evlu_pfls_rt'])
                    holding_rows.append((name, code, market_type, qty, buy_price, cur_price, profit, rate))
                    progress.advance(task)

            console.print()
            h_table = Table(title="보유 종목 리스트", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            h_table.add_column("종목명(코드)", justify="left")
            h_table.add_column("시장", justify="center")
            h_table.add_column("수량", justify="right")
            h_table.add_column("매입가", justify="right")
            h_table.add_column("현재가", justify="right")
            h_table.add_column("평가손익", justify="right")
            h_table.add_column("수익률", justify="right")
            
            for name, code, market_type, qty, buy_price, cur_price, profit, rate in holding_rows:
                p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                h_table.add_row(
                    f"{name}({code})", 
                    market_type,
                    f"{qty:,}주", 
                    f"{buy_price:,.0f}원",
                    f"{cur_price:,}원", 
                    f"{p_color}{profit:+,}원[/]", 
                    f"{p_color}{rate:+.2f}%[/]"
                )
            console.print(h_table)
        else:
            console.print("\n[dim]현재 보유 중인 종목이 없습니다.[/dim]")
            
        console.print()

    def print_report(self, target_account=None):
        menu_items = [
            ("1", "일간 (오늘)", "Daily"),
            ("2", "주간 (최근 7일)", "Weekly"),
            ("3", "월간 (최근 30일)", "Monthly"),
            ("4", "기간 직접 입력", "Custom Days")
        ]
        choice = utils.show_menu("시스템 트레이딩 평가 리포트 (Trading Report)", menu_items, default_choice="4")
        if choice.lower() == 'q': return False
        
        menu_map = {"1": "일간", "2": "주간", "3": "월간", "4": "직접 입력"}
        if choice in menu_map:
            context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
            
        days = None
        if choice == "1": days = 0
        elif choice == "2": days = 7
        elif choice == "3": days = 30
        elif choice == "4":
            utils.print_breadcrumb()
            val = Prompt.ask("조회할 기간(일) 입력 [dim](Enter: 전체 내역, 이전: b, 메인: q)[/dim]", default="")
            console.print()
            if val.lower() in ['b', 'q']: return False
            
            if val.strip() and val.isdigit():
                days = int(val)
                context.USER_ACTION_BREADCRUMB.append(f"[{days}일]")
            else:
                days = None # 전체 내역
                context.USER_ACTION_BREADCRUMB.append("[전체]")

        self._load_trade_records(days=days, target_account=target_account)
        
        if not self.trade_records:
            console.print("\n[yellow]선택한 기간에 해당하는 매매 기록이 없습니다.[/yellow]")
            return
            
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]리포트 통계 분석 및 시장 데이터 수집 중...[/cyan]", total=None)
            
            stats = self._calculate_statistics()
            
            # [추가] 자산 증감 및 시장 성과 통계 추가
            now = datetime.now()
            end_dt = now.strftime("%Y-%m-%d")
            
            if days is not None:
                start_dt = (now - timedelta(days=days)).strftime("%Y-%m-%d")
            else:
                start_dt = self.trade_records[0]['time'][:10] if self.trade_records else end_dt
                
            if target_account:
                target_cano, acnt = target_account.split('-')
            else:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                if not config.session.is_simulation and not target_cano:
                    target_cano = config.session.cano
                    acnt = config.session.acnt_prdt_cd
                    
                target_account = f"{target_cano}-{acnt}"
            
            current_asset = 0
            try:
                with utils.AccountContext(target_cano):
                    asset_data = account.get_asset_status_data(target_cano, acnt)
                    if asset_data:
                        current_asset = asset_data.get('tot_asset', 0)
            except Exception: pass
            
            initial_asset = db_manager.db.get_daily_asset(start_dt, target_account)
            stats['current_asset'] = current_asset
            stats['initial_asset'] = initial_asset
            
            kospi_rate = 0.0
            try:
                kospi_df = analysis.get_domestic_index_data("KOSPI")
                if kospi_df is not None and not kospi_df.empty:
                    s_dt = start_dt.replace('-', '')
                    e_dt = end_dt.replace('-', '')
                    
                    if 'date' in kospi_df.columns:
                        def to_yyyymmdd(x):
                            if hasattr(x, 'strftime'): return x.strftime('%Y%m%d')
                            return str(x).replace('-', '')[:8]
                        
                        dates = kospi_df['date'].apply(to_yyyymmdd)
                        mask = (dates >= s_dt) & (dates <= e_dt)
                        period_df = kospi_df[mask]
                        if not period_df.empty:
                            first_idx = kospi_df.index.get_loc(period_df.index[0])
                            last_idx = kospi_df.index.get_loc(period_df.index[-1])
                            
                            if first_idx > 0:
                                start_val = kospi_df.iloc[first_idx - 1]['close']
                            else:
                                start_val = kospi_df.iloc[first_idx]['close']
                                
                            end_val = kospi_df.iloc[last_idx]['close']
                            if start_val > 0:
                                kospi_rate = ((end_val - start_val) / start_val) * 100
            except Exception: pass
            stats['kospi_rate'] = kospi_rate

            # [추가] 현재 보유 종목에 대한 총 매입금액, 평가손익, 수익률 계산
            holdings_summary = None
            try:
                with utils.AccountContext(target_cano):
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    tot_pchs = 0
                    tot_profit = 0
                    tot_evlu = 0
                    
                    if summary and len(summary) > 0:
                        tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt'))
                        tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl'))
                        tot_evlu = api.safe_int(summary[0].get('scts_evlu_amt'))
                    
                    if tot_pchs == 0 and holdings:
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                        tot_profit = sum(int(h['evlu_pfls_amt']) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                        tot_evlu = sum(int(h['evlu_amt']) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                    
                    if tot_pchs > 0 or tot_profit != 0:
                        rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        holdings_summary = {'tot_pchs': tot_pchs, 'tot_evlu': tot_evlu, 'tot_profit': tot_profit, 'rate': rate}
            except Exception: pass

        self._print_summary_table(stats, holdings_summary)
        self._print_current_holdings(target_cano, acnt)
        self._print_stock_details()

    def _load_trade_records(self, days=None, target_account=None):
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
                BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]DB에서 매매 내역 조회 및 분석 중...[/cyan]", total=None)
            
            # [수정] DB에서 매매 내역 조회 (수동 매매 포함을 위해 is_auto 필터 제거)
            # 시스템 매매와 수동 매매를 모두 포함하여 평가
            limit = 500
            start_date = None
            
            if days is not None:
                limit = None
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            
            # [수정] 자동매매 계좌 번호로 필터링 (시스템 트레이딩 내역만 조회)
            if not target_account:
                if config.session.is_simulation:
                    target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                elif config.session.auto_cano:
                    target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
            # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비 (메모리 필터링)
            try:
                db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date, account=target_account)
            except TypeError:
                db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date)
                if target_account:
                    db_records = [r for r in db_records if r.get('account') == target_account]
            
            # DB 레코드를 내부 포맷으로 변환
            self.trade_records = []
            for r in reversed(db_records): # DB는 최신순이므로 시간순(과거->최신)으로 뒤집음
                # type 파싱: "buy(AUTO)" -> "buy"
                type_str = r['type']
                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                
                self.trade_records.append({
                    "type": simple_type,
                    "code": r['code'],
                    "name": r['name'],
                    "qty": int(r['qty']),
                    "price": float(r['price']),
                    "profit_rate": float(r['profit_rate'] or 0),
                    "profit_amt": int(r['profit_amt'] or 0),
                    "reason": r['reason'],
                    "time": r['time'],
                    "odno": r['odno']
                })
            
            # [추가] 중복 제거 및 정제 (시스템 주문과 체결 확인 병합)
            self.trade_records = self._refine_trade_records(self.trade_records)
            
            # [추가] 시간순 정렬 (통계 계산 및 기간 표시 정확성 확보)
            if self.trade_records:
                self.trade_records.sort(key=lambda x: x['time'])

    def get_performance_report(self, days=None):
        """텔레그램용 성과 리포트 생성"""
        # DB에서 조회 (로그 출력 없이)
        # [수정] 수동 매매 포함을 위해 is_auto 필터 제거
        limit = 500
        start_date = None
        period_msg = "전체 (최근 500건)"
        
        if days is not None:
            limit = None
            start_dt = datetime.now() - timedelta(days=days)
            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            period_msg = f"{start_date} ~ {end_date}"
        
        # [수정] 자동매매 계좌 번호로 필터링
        target_account = None
        if config.session.is_simulation:
            target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
        elif config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        # [Fix] DBManager.get_trades가 account 인자를 지원하지 않는 경우 대비
        try:
            db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date, account=target_account)
        except TypeError:
            db_records = db_manager.db.get_trades(is_sim=config.session.is_simulation, limit=limit, start_date=start_date)
            if target_account:
                db_records = [r for r in db_records if r.get('account') == target_account]
        
        temp_records = []
        for r in reversed(db_records):
            type_str = r['type']
            simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
            
            temp_records.append({
                "type": simple_type,
                "code": r['code'],
                "name": r['name'],
                "qty": int(r['qty']),
                "price": float(r['price']),
                "profit_rate": float(r['profit_rate'] or 0),
                "profit_amt": int(r['profit_amt'] or 0),
                "reason": r['reason'],
                "time": r['time'],
                "odno": r['odno']
            })
            
        # [추가] 중복 제거 및 정제
        refined_records = self._refine_trade_records(temp_records)
        
        # [추가] 시간순 정렬 (통계 계산 및 기간 표시 정확성 확보)
        if refined_records:
            refined_records.sort(key=lambda x: x['time'])
            
        msg = "📊 [시스템 트레이딩 성과 리포트]\n"

        if not refined_records:
            msg += f"기간: {period_msg}\n\n"
            msg += "매매 기록이 없습니다."
            return msg
            
        stats = self._calculate_statistics(refined_records)
        
        # [추가] 기간 정보
        if refined_records:
            start_date = refined_records[0]['time'][:10]
            end_date = refined_records[-1]['time'][:10]
            msg += f"기간: {start_date} ~ {end_date}\n\n"
        
        # [추가] 현재 보유 종목에 대한 총 매입금액, 평가손익, 수익률 계산
        holdings_summary = None
        try:
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                tot_pchs = 0
                tot_profit = 0
                tot_evlu = 0
                
                # [수정] API 요약 데이터 대신 보유 종목 합산
                if holdings:
                    valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
                    if valid_holdings:
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                        tot_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                        tot_evlu = sum(int(h['evlu_amt']) for h in valid_holdings)
                
                if tot_pchs > 0 or tot_profit != 0:
                    rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                    holdings_summary = {'tot_pchs': tot_pchs, 'tot_evlu': tot_evlu, 'tot_profit': tot_profit, 'rate': rate}
        except Exception: pass
        
        if stats['sell_trades_exist']:
            win_rate = stats['win_rate']
            total_profit = stats['total_profit']
            avg_profit_rate = stats['avg_profit_rate']
            
            msg += f"[매매 현황 요약]\n"
            msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
            msg += f"승률: {win_rate:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)\n"
            msg += f"건당 평균 수익률: {avg_profit_rate:+.2f}%\n"
            msg += f"건당 평균 보유: {stats['avg_holding_str']}\n"
            total_realized_rate = stats.get('total_realized_rate', 0.0)
            msg += f"총 실현 손익: {total_profit:+,}원 (매매원금 대비 {total_realized_rate:+.2f}%)\n"
            
            msg += f"\n[최고 / 최다 손익]\n"
            if stats.get('best_trade'):
                b = stats['best_trade']
                msg += f"최고 수익: {b['name']} ({b['profit_amt']:+,}원 / {b['profit_rate']:+.2f}%)\n"
            if stats.get('worst_trade'):
                w = stats['worst_trade']
                msg += f"최다 손실: {w['name']} ({w['profit_amt']:+,}원 / {w['profit_rate']:+.2f}%)\n"
            
            msg += f"\n[매수 사유 분포]\n"
            buy_reasons = stats.get('buy_reasons', {})
            total_buys = stats['buy_count']
            if total_buys > 0:
                for r, count in buy_reasons.most_common():
                    msg += f"• {r}: {count}건 ({count/total_buys*100:.1f}%)\n"
            else:
                msg += "• 매수 내역 없음\n"

            msg += f"\n[매도 사유 분포]\n"
            reasons = stats.get('sell_reasons', {})
            total_sells = stats['sell_count']
            if total_sells > 0:
                for r, count in reasons.most_common():
                    msg += f"• {r}: {count}건 ({count/total_sells*100:.1f}%)\n"
            else:
                msg += "• 매도 내역 없음\n"
                    
            if holdings_summary:
                msg += f"\n[현재 보유 현황]\n"
                msg += f"총 매입금액: {holdings_summary['tot_pchs']:,}원\n"
                msg += f"총 평가금액: {holdings_summary['tot_evlu']:,}원\n"
                msg += f"총 평가손익: {holdings_summary['tot_profit']:+,}원 ({holdings_summary['rate']:+.2f}%)\n"
        else:
            msg += f"[매매 현황 요약]\n"
            msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
            msg += "(청산된 내역이 없어 수익률 산출 불가)\n"
            
            if holdings_summary:
                msg += f"\n[현재 보유 현황]\n"
                msg += f"총 매입금액: {holdings_summary['tot_pchs']:,}원\n"
                msg += f"총 평가금액: {holdings_summary['tot_evlu']:,}원\n"
                msg += f"총 평가손익: {holdings_summary['tot_profit']:+,}원 ({holdings_summary['rate']:+.2f}%)\n"
            
        return msg.strip()

    def _calculate_statistics(self, records=None):
        if records is None: records = self.trade_records
        
        # [수정] 이미 정제된 레코드를 사용하므로 필터링 제거
        # 수동 매매('체결 확인')도 통계에 포함
        
        total_trades = len(records)
        buy_trades = [r for r in records if r['type'] == 'buy']
        sell_trades = [r for r in records if r['type'] == 'sell']
        
        win_trades = 0
        loss_trades = 0
        total_profit = 0
        total_profit_rate = 0.0
        total_buy_amt_for_sell = 0
        
        # [추가] Best/Worst 및 사유 분석 변수
        best_trade = None
        worst_trade = None
        sell_reasons = Counter()
        buy_reasons = Counter()
        
        # [추가] 보유 기간 계산
        total_holding_seconds = 0
        holding_count = 0
        buy_times = {} # code -> list of datetime

        # 시간순 처리를 위해 전체 기록 순회
        for r in records:
            code = r['code']
            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except: continue

            if r['type'] == 'buy':
                if code not in buy_times: buy_times[code] = []
                buy_times[code].append(dt)
                
                reason_raw = r.get('reason', '')
                reason_key = "기타"
                if "조건 만족" in reason_raw:
                    if "슈퍼모멘텀" in reason_raw:
                        reason_key = "추격 매수 (돌파)"
                    else:
                        reason_key = "스코어 진입 (조건 만족)"
                elif "역매수" in reason_raw or "역추세" in reason_raw:
                    reason_key = "역추세 매수 (낙폭과대)"
                elif "수동" in reason_raw or "사용자 수동 주문" in reason_raw:
                    reason_key = "수동 매수"
                elif "예약" in reason_raw:
                    reason_key = "예약 매수"
                buy_reasons[reason_key] += 1
            elif r['type'] == 'sell':
                # 매도 시 매수 기록과 매칭 (FIFO: 먼저 산 것을 먼저 판다고 가정)
                if code in buy_times and buy_times[code]:
                    buy_dt = buy_times[code].pop(0)
                    diff = (dt - buy_dt).total_seconds()
                    total_holding_seconds += diff
                    holding_count += 1
                else:
                    # [추가] 기간 검색으로 인해 주어진 레코드에 매수 기록이 없는 경우 DB에서 과거 내역 조회
                    try:
                        from modules import db_manager
                        past_trades = db_manager.db.get_trades(code=code, limit=100)
                        for pt in past_trades:
                            pt_type = pt.get('type', '').lower()
                            if "buy" in pt_type or "매수" in pt_type:
                                pt_dt = datetime.strptime(pt['time'], "%Y-%m-%d %H:%M:%S")
                                if pt_dt < dt:
                                    diff = (dt - pt_dt).total_seconds()
                                    total_holding_seconds += diff
                                    holding_count += 1
                                    break
                    except Exception:
                        pass
        
        for t in sell_trades:
            profit = t.get('profit_amt', 0)
            rate = t.get('profit_rate', 0.0)
            total_profit += profit
            total_profit_rate += rate
            
            qty = int(float(t.get('qty', 0)))
            price = float(t.get('price', 0))
            sell_amt = qty * price
            buy_amt = sell_amt - profit
            total_buy_amt_for_sell += buy_amt
            
            if profit > 0: win_trades += 1
            else: loss_trades += 1
            
            # [추가] Best/Worst 갱신
            if best_trade is None or profit > best_trade.get('profit_amt', 0):
                best_trade = t
            if worst_trade is None or profit < worst_trade.get('profit_amt', 0):
                worst_trade = t
            
            # [추가] 매도 사유 분석
            reason = t.get('reason', '기타')
            reason_key = "기타"
            if "반익절" in reason: reason_key = "반익절"
            elif "과열" in reason: reason_key = "과열매도"
            elif "익절" in reason: reason_key = "익절"
            elif "ATR손절" in reason: reason_key = "ATR손절"
            elif "손절" in reason: reason_key = "손절"
            elif "트레일링" in reason: reason_key = "트레일링스탑"
            elif "시간청산" in reason: reason_key = "시간청산"
            elif "추세" in reason or "점수하락" in reason or "매도진입" in reason: reason_key = "추세이탈"
            elif "수동" in reason: reason_key = "수동매도"
            sell_reasons[reason_key] += 1
            
        avg_profit_rate = (total_profit_rate / len(sell_trades)) if sell_trades else 0.0
        win_rate = (win_trades / len(sell_trades) * 100) if sell_trades else 0.0
        total_realized_rate = (total_profit / total_buy_amt_for_sell * 100) if total_buy_amt_for_sell > 0 else 0.0

        # [추가] 평균 보유 기간 포맷팅
        avg_holding_str = "-"
        if holding_count > 0:
            avg_sec = total_holding_seconds / holding_count
            if avg_sec < 60: avg_holding_str = f"{int(avg_sec)}초"
            elif avg_sec < 3600: avg_holding_str = f"{int(avg_sec//60)}분 {int(avg_sec%60)}초"
            else: avg_holding_str = f"{int(avg_sec//3600)}시간 {int((avg_sec%3600)//60)}분"

        return {
            "total_trades": total_trades,
            "buy_count": len(buy_trades),
            "sell_count": len(sell_trades),
            "win_trades": win_trades,
            "loss_trades": loss_trades,
            "total_profit": total_profit,
            "total_realized_rate": total_realized_rate,
            "total_buy_amt_for_sell": total_buy_amt_for_sell, # [추가] 투자 원금 기준 알파 계산용
            "avg_profit_rate": avg_profit_rate,
            "win_rate": win_rate,
            "avg_holding_str": avg_holding_str,
            "sell_trades_exist": len(sell_trades) > 0,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "sell_reasons": sell_reasons,
            "buy_reasons": buy_reasons
        }

    def _print_summary_table(self, stats, holdings_summary=None):
        summary_table = Table(title="트레이딩 성과 요약", title_justify="center", title_style="", box=box.HORIZONTALS, show_header=False, border_style="dim")
        summary_table.add_column("항목", style="cyan", justify="left")
        summary_table.add_column("값", justify="left")
        
        # [추가] 조회 기간 표시
        period_str = "전체"
        if getattr(self, 'trade_records', None) and len(self.trade_records) > 0:
            start_date = self.trade_records[0]['time'][:10]
            end_date = self.trade_records[-1]['time'][:10]
            period_str = f"{start_date} ~ {end_date}"
            
        summary_table.add_row("조회 기간", period_str)
        summary_table.add_row("총 매매 실행", f"{stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})")
        
        if stats['sell_trades_exist']:
            summary_table.add_row("승률 (Win Rate)", f"{stats['win_rate']:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)")
            
            # [추가] 시작 자산 및 현재 자산 비교 표시
            initial_asset = stats.get('initial_asset', 0)
            current_asset = stats.get('current_asset', 0)
            if initial_asset and current_asset > 0:
                asset_profit = current_asset - initial_asset
                asset_roi = (asset_profit / initial_asset) * 100
                summary_table.add_row("총 계좌 시작 자산", f"{int(initial_asset):,}원")
                summary_table.add_row("총 계좌 현재 자산", f"{current_asset:,}원")
                ap_color = "[red]" if asset_profit > 0 else ("[blue]" if asset_profit < 0 else "[white]")
                summary_table.add_row("총 계좌 자산 증감", f"{ap_color}{int(asset_profit):+,}원 ({asset_roi:+.2f}%)[/]")
                
            tp = stats['total_profit']
            tr_rate = stats.get('total_realized_rate', 0.0)
            summary_table.add_row("총 실현 손익", f"[red]{tp:+,}원 (매매원금 대비 {tr_rate:+.2f}%)[/]" if tp > 0 else f"[blue]{tp:+,}원 (매매원금 대비 {tr_rate:+.2f}%)[/]")
            
            total_buy = stats.get('total_buy_amt_for_sell', 0)
            sec_pl = holdings_summary['tot_profit'] if holdings_summary else 0
            sec_buy = holdings_summary['tot_pchs'] if holdings_summary else 0
            total_invested = total_buy + sec_buy
            total_net_profit = tp + sec_pl
            
            strategy_roi = 0.0
            if total_invested > 0:
                strategy_roi = (total_net_profit / total_invested) * 100
            
            sp_color = "[red]" if total_net_profit > 0 else ("[blue]" if total_net_profit < 0 else "[white]")
            summary_table.add_row("현재 전략 손익", f"{sp_color}{total_net_profit:+,}원 (실현+평가 손익 {strategy_roi:+.2f}%)[/]")

            apr = stats['avg_profit_rate']
            summary_table.add_row("건당 평균 수익률", f"[red]{apr:+.2f}%[/]" if apr > 0 else f"[blue]{apr:+.2f}%[/]")
            summary_table.add_row("건당 평균 보유", stats['avg_holding_str'])
            
            # [추가] 시장 대비 성과 표시
            kospi_rate = stats.get('kospi_rate', 0.0)
            k_color = "[red]" if kospi_rate > 0 else ("[blue]" if kospi_rate < 0 else "[white]")
            market_perf_str = f"코스피 지수: {k_color}{kospi_rate:+.2f}%[/]"

            if total_invested > 0:
                alpha = strategy_roi - kospi_rate
                a_color = "[red]" if alpha > 0 else ("[blue]" if alpha < 0 else "[white]")
                # [수정] 가독성 향상을 위해 초과/부진 여부 명시
                if alpha > 0:
                    alpha_label = "시장 대비 초과 수익 (Outperform)"
                else:
                    alpha_label = "시장 대비 성과 (Underperform)"
                market_perf_str += f" / {alpha_label}: {a_color}{alpha:+.2f}%[/]"
            summary_table.add_row("시장 대비 성과", market_perf_str)
        
        if holdings_summary:
            summary_table.add_section()
            summary_table.add_row("총 매입금액", f"{holdings_summary['tot_pchs']:,}원")
            summary_table.add_row("총 평가금액", f"{holdings_summary['tot_evlu']:,}원")
            hp = holdings_summary['tot_profit']
            hr = holdings_summary['rate']
            hc = "[red]" if hp > 0 else ("[blue]" if hp < 0 else "[white]")
            summary_table.add_row("총 평가손익", f"{hc}{hp:+,}원 ({hr:+.2f}%)[/]")
            
        console.print(summary_table)

    def _print_current_holdings(self, target_cano=None, target_acnt=None):
        try:
            # 컨텍스트 설정 (시스템 트레이딩 계좌 조회)
            if not target_cano:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                target_acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            with utils.AccountContext(target_cano):
                holdings, _ = api.get_domestic_balance(target_cano, target_acnt)
                
                if holdings:
                    console.print()
                    h_table = Table(title="현재 보유 종목 현황", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
                    h_table.add_column("종목명(코드)", justify="left")
                    h_table.add_column("보유수량", justify="right")
                    h_table.add_column("매입단가", justify="right")
                    h_table.add_column("현재가", justify="right")
                    h_table.add_column("평가손익", justify="right")
                    h_table.add_column("수익률", justify="right")
                    
                    for item in holdings:
                        name = item['prdt_name']
                        code = item['pdno']
                        qty = int(item['hldg_qty'])
                        buy_price = float(item['pchs_avg_pric'])
                        cur_price = int(item['prpr'])
                        profit = int(item['evlu_pfls_amt'])
                        rate = float(item['evlu_pfls_rt'])
                        
                        p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                        
                        h_table.add_row(f"{name}({code})", f"{qty:,}주", f"{buy_price:,.0f}원", f"{cur_price:,}원", f"{p_color}{profit:+,}원[/]", f"{p_color}{rate:+.2f}%[/]")
                    console.print(h_table)
        except Exception: pass

    def _print_stock_details(self):
        stock_stats = {}
        buy_times_per_stock = {} # 종목별 매수 시간 추적 (FIFO)

        # [수정] 이미 정제된 레코드를 사용하므로 필터링 제거
        filtered_records = self.trade_records

        for r in filtered_records:
            code = r['code']
            if code not in stock_stats:
                stock_stats[code] = {
                    'name': r['name'], 
                    'buy': 0, 'sell': 0, 
                    'profit': 0, 'rates': [], 'wins': 0,
                    'reasons': [], # 매도 사유 리스트
                    'holding_secs': [], # 보유 기간 리스트
                    'max_rate': -999.0, 'min_rate': 999.0,
                    'total_buy_amt': 0 # [추가] 총 매수 금액
                }
            if code not in buy_times_per_stock:
                buy_times_per_stock[code] = []
            
            try:
                dt = datetime.strptime(r['time'], "%Y-%m-%d %H:%M:%S")
            except: dt = datetime.now()
            
            if r['type'] == 'buy':
                stock_stats[code]['buy'] += 1
                stock_stats[code]['total_buy_amt'] += int(r['price'] * r['qty']) # [추가] 매수 금액 누적
                buy_times_per_stock[code].append(dt)
            elif r['type'] == 'sell':
                stock_stats[code]['sell'] += 1
                p = r.get('profit_amt', 0)
                rate = r.get('profit_rate', 0.0)
                
                stock_stats[code]['profit'] += p
                stock_stats[code]['rates'].append(rate)
                if p > 0: stock_stats[code]['wins'] += 1
                
                # 사유 분석 (익절/손절/추세 등 키워드 추출)
                reason_raw = r.get('reason', '')
                reason_simple = "기타"
                if "익절" in reason_raw: reason_simple = "익절"
                elif "손절" in reason_raw: reason_simple = "손절"
                elif "추세" in reason_raw: reason_simple = "추세이탈"
                stock_stats[code]['reasons'].append(reason_simple)
                
                # 최대/최소 수익률 갱신
                if rate > stock_stats[code]['max_rate']: stock_stats[code]['max_rate'] = rate
                if rate < stock_stats[code]['min_rate']: stock_stats[code]['min_rate'] = rate
                
                # 보유 기간 계산 (FIFO)
                if buy_times_per_stock[code]:
                    buy_dt = buy_times_per_stock[code].pop(0)
                    hold_sec = (dt - buy_dt).total_seconds()
                    stock_stats[code]['holding_secs'].append(hold_sec)

        if stock_stats:
            console.print()
            s_table = Table(title="종목별 성과 분석", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            s_table.add_column("종목명(코드)", justify="left")
            s_table.add_column("매매(매수/매도)", justify="center")
            s_table.add_column("승률", justify="right")
            s_table.add_column("총 손익", justify="right")
            s_table.add_column("평균 수익률", justify="right")
            # [추가] 상세 정보 컬럼
            s_table.add_column("최대/최소", justify="right")
            s_table.add_column("주요 사유", justify="center")
            s_table.add_column("평균 보유", justify="right")

            for i, (code, stat) in enumerate(stock_stats.items()):
                s_cnt = stat['sell']
                win_rate = (stat['wins'] / s_cnt * 100) if s_cnt > 0 else 0.0
                avg_rate = (sum(stat['rates']) / s_cnt) if s_cnt > 0 else 0.0
                
                p_color = "[red]" if stat['profit'] > 0 else ("[blue]" if stat['profit'] < 0 else "[white]")
                r_color = "[red]" if avg_rate > 0 else ("[blue]" if avg_rate < 0 else "[white]")
                
                # 최대/최소 수익률 포맷팅
                max_r = stat['max_rate'] if stat['max_rate'] != -999.0 else 0.0
                min_r = stat['min_rate'] if stat['min_rate'] != 999.0 else 0.0
                range_str = f"[red]{max_r:+.1f}%[/] / [blue]{min_r:+.1f}%[/]" if s_cnt > 0 else "-"
                
                # 주요 매도 사유 (최빈값)
                reason_str = "-"
                if stat['reasons']:
                    c = Counter(stat['reasons'])
                    most_common = c.most_common(1)[0] # (사유, 횟수)
                    reason_str = f"{most_common[0]}({most_common[1]}회)"
                
                # 평균 보유 시간 포맷팅
                hold_str = "-"
                if stat['holding_secs']:
                    avg_sec = sum(stat['holding_secs']) / len(stat['holding_secs'])
                    if avg_sec < 60: hold_str = f"{int(avg_sec)}초"
                    elif avg_sec < 3600: hold_str = f"{int(avg_sec//60)}분"
                    else: hold_str = f"{int(avg_sec//3600)}시간"
                
                s_table.add_row(
                    f"{stat['name']} ({code})",
                    f"{stat['buy']} / {stat['sell']}",
                    f"{win_rate:.1f}%",
                    f"{p_color}{stat['profit']:+,}원[/]",
                    f"{r_color}{avg_rate:+.2f}%[/]",
                    range_str,
                    reason_str,
                    hold_str
                )
                
                # [추가] 5개마다 실선 추가
                if (i + 1) % 5 == 0 and (i + 1) < len(stock_stats):
                    s_table.add_section()
            console.print(s_table)
        
        # 상세 내역 테이블
        console.print()
        detail_table = Table(title="상세 매매 내역 (최신순)", title_justify="center", title_style="", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        detail_table.add_column("시간", justify="center")
        detail_table.add_column("구분", justify="center")
        detail_table.add_column("종목명", justify="left")
        detail_table.add_column("수량", justify="right")
        detail_table.add_column("단가", justify="right")
        detail_table.add_column("매매금액", justify="right") # [추가]
        detail_table.add_column("손익(수익률)", justify="right")
        detail_table.add_column("사유", justify="left")
        
        # [수정] 필터링된 레코드 사용 (최신순 정렬)
        records = list(reversed(filtered_records))
        
        for i, r in enumerate(records):
            type_str = "[red]매수[/]" if r['type'] == 'buy' else "[blue]매도[/]"
            
            # [수정] 단가 포맷팅 (시장가 0원 처리)
            price_val = float(r['price'])
            if price_val <= 0:
                price_str = "시장가"
                amt_str = "-"
            else:
                if price_val.is_integer():
                    price_str = f"{int(price_val):,}"
                else:
                    price_str = f"{price_val:,.2f}"
                
                trade_amt = int(price_val * r['qty'])
                amt_str = f"{trade_amt:,}"
            
            profit_display = "-"
            reason_display = r.get('reason', '-')

            if r['type'] == 'sell':
                p_amt = r.get('profit_amt', 0)
                p_rate = r.get('profit_rate', 0.0)
                color = "[red]" if p_amt > 0 else "[blue]"
                profit_display = f"{color}{p_amt:+,}원 ({p_rate:+.2f}%)[/]"
            
            detail_table.add_row(
                r['time'][5:], # MM-DD HH:MM:SS
                type_str,
                f"{r['name']}",
                f"{r['qty']}",
                price_str,
                amt_str, # [수정]
                profit_display,
                reason_display
            )
            
            # [추가] 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(records):
                detail_table.add_section()
            
        console.print(detail_table)

    def view_log_file(self):
        """현재 날짜의 시스템 트레이딩 로그 파일을 실시간으로 출력합니다."""
        utils.clear_screen()
        utils.print_breadcrumb()
        
        log_dir = getattr(config, 'SYSTEM_TRADING_LOG_DIR', 'logs')
        filename = "autotrade.log" # [수정] 고정 파일명 사용
        filepath = os.path.join(log_dir, filename)

        # [추가] 파일이 생성될 때까지 잠시 대기 (최대 10초) - 부팅 직후 실행 시 필요
        for _ in range(10):
            if os.path.exists(filepath): break
            time.sleep(1)

        if not os.path.exists(filepath):
            console.print(f"\n[yellow]로그 파일({filename})이 없습니다.[/yellow]")
            return

        console.print(f"\n[bold cyan]━━━ 실시간 로그 모니터링 ({filename}) ━━━[/bold cyan]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.[/dim]\n")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]로그 파일 로딩 중...[/cyan]", total=None)

        f = None
        try:
            f = open(filepath, 'r', encoding='utf-8')
            # 초기 출력: 최근 50줄
            lines = f.readlines()
            for line in lines[-50:]:
                console.print(escape(line.strip()))
            
            # 현재 파일의 inode 저장 (파일 교체 감지용)
            current_inode = os.fstat(f.fileno()).st_ino
            
            # 실시간 모니터링
            while True:
                # [추가] 로그 뷰어 실행 중에도 토큰 만료 체크 및 갱신 수행
                api.check_and_refresh_token_if_expired()

                line = f.readline()
                if line:
                    console.print(escape(line.strip()))
                else:
                    time.sleep(0.1)
                    # 파일 교체(로테이션) 감지
                    try:
                        if os.path.exists(filepath):
                            new_inode = os.stat(filepath).st_ino
                            if new_inode != current_inode:
                                # 파일이 교체됨 (자정 로테이션 등)
                                f.close()
                                f = open(filepath, 'r', encoding='utf-8')
                                current_inode = new_inode
                                console.print("\n[dim yellow]>>> 로그 파일이 교체되었습니다 (Log Rotation) <<<[/dim yellow]\n")
                    except Exception:
                        pass
        except KeyboardInterrupt:
            console.print("\n[yellow]로그 모니터링을 종료합니다.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]로그 파일 읽기 오류: {e}[/red]")
        finally:
            if f and not f.closed:
                f.close()

    def is_market_open(self):
        """국내 정규장 운영 시간 확인 (config 설정 시간 따름)"""
        if api.is_holiday_today(): return False # 주말 및 공휴일(휴장일) 처리

        now = datetime.now()
        current_time = now.strftime("%H%M")
        
        start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0915")
        end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "1515")
        return start_time <= current_time <= end_time

    def _get_holdings_message(self, target_cano):
        """보유 종목 현황 메시지 생성 (장 시작/마감 알림용)"""
        msg = ""
        try:
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            holdings, _ = api.get_domestic_balance(target_cano, acnt)
            
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

            if valid_holdings:
                msg += "\n\n📋 [보유 종목 현황]"
                for item in valid_holdings:
                    name = item['prdt_name']
                    qty = int(item['hldg_qty'])
                    cur_price = int(item['prpr'])
                    eval_amt = int(item['evlu_amt'])
                    rate = float(item['evlu_pfls_rt'])
                    profit = int(item['evlu_pfls_amt'])
                    msg += f"\n• {name} ({qty}주)\n  현재가: {cur_price:,}원 | 평가: {eval_amt:,}원\n  손익: {profit:+,}원 ({rate:+.2f}%)"
            else:
                msg += "\n\n📋 [보유 종목] 없음"
        except Exception as e:
            logger.error(f"보유 종목 조회 실패: {e}")
            msg += "\n\n(보유 종목 조회 실패)"
            
        return msg

    def _run_loop(self):
        my_thread = threading.current_thread()
        while self.is_running and self.thread is my_thread:
            try:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                with utils.AccountContext(target_cano):
                    current_market_status = self.is_market_open()
                    is_log_needed = current_market_status or getattr(self, '_first_loop_flag', True) or (self.was_market_open != current_market_status)
                    self._first_loop_flag = False
                    
                    if is_log_needed:
                        self.log("모니터링 주기 시작...")
                    
                    # [추가] Kill Switch: 체결 감시 시스템 상태 점검
                    # 체결 확인이 불가능한 상태에서는 신규 주문도 위험하므로 중단
                    if not ConclusionMonitor().is_healthy():
                        raise Exception(f"체결 감시 시스템 불안정 (연속 에러 {ConclusionMonitor().consecutive_errors}회)")
                    
                    # [수정] 매 사이클 시작 시점에 수행하던 일일 손실 한도 강제 체크 로직 제거
                    # API Rate Limit 발생 시 잔고가 누락되어 가짜 비상 정지를 유발할 수 있으므로,
                    # API 호출 성공이 보장된 루프 후반부(_monitor_account_status)에서만 안전하게 손실 한도를 체크함
                    
                    # [추가] 현재 운용 계좌 정보 로깅
                    if target_cano and is_log_needed:
                        acc_type = "모의투자" if config.session.is_simulation else "실전투자(자동)"
                        self.log(f"운용 계좌: {target_cano} [{acc_type}]")
                    
                    
                    # [추가] 날짜 변경 감지 및 당일 기준 자산 재설정 (무중단 24시간 운용 지원)
                    current_date = datetime.now().date()
                    if current_date > self.last_log_date:
                        self.log("━" * 80)
                        self.log(f"📅 날짜 변경 감지: {self.last_log_date} -> {current_date}")
                        self.log("당일 기준 자산(initial_asset)을 새로 측정하여 갱신합니다.")
                        self.log("━" * 80)
                        self.last_log_date = current_date
                        self.initial_asset = 0
                        
                        try:
                            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                            
                            # [수정] 해외 자산 누락 방지
                            asset_data = account.get_asset_status_data(target_cano, acnt)
                            if asset_data and asset_data.get('tot_asset', 0) > 0:
                                self.initial_asset = asset_data['tot_asset']
                                save_daily_initial_asset(f"{target_cano}-{acnt}", self.initial_asset)
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                acc_str = f"{target_cano}-{acnt}"
                                db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                                self.log(f"[초기화 완료] 새로운 당일 시작 자산 갱신: {self.initial_asset:,}원")
                        except Exception as e:
                            self.log(f"당일 시작 자산 갱신 실패: {e}")

                    # [추가] 장 시작/마감 상태 변경 감지 및 로그
                    if self.was_market_open is not None:
                        if not self.was_market_open and current_market_status:
                            self.log("━" * 80)
                            self.log(f"📢 [거래 시작] 시스템 트레이딩 거래가 시작되었습니다. ({datetime.now().strftime('%H:%M')})")
                            self.log("━" * 80)
                            
                            msg = "🔔 [장 시작] 거래 가능 시간이 되었습니다."
                            msg += self._get_holdings_message(target_cano)
                            api.send_telegram_message(msg)
                        elif self.was_market_open and not current_market_status:
                            self.log("━" * 80)
                            self.log(f"💤 [거래 종료] 시스템 트레이딩 거래가 종료되었습니다. ({datetime.now().strftime('%H:%M')})")
                            self.log("━" * 80)
                            
                            msg = "🌙 [장 마감] 거래 시간이 종료되었습니다."
                            msg += self._get_holdings_message(target_cano)
                            api.send_telegram_message(msg)
                            
                            # [추가] 장 마감 후 AI 마감 브리핑 자동 실행 (기존 포트폴리오 진단 대체)
                            try:
                                from modules.scheduler import SystemScheduler
                                threading.Thread(target=SystemScheduler().execute_daily_closing_report, daemon=True, name="DailyClosingReport").start()
                                self.log("장 마감 종합 브리핑(AI) 작성을 백그라운드에서 시작합니다.")
                            except Exception as e:
                                self.log(f"장 마감 브리핑 스케줄러 호출 실패: {e}")
                    
                    # [변경] 장 마감 시 분석 중단 (트래픽 감소)
                    if not current_market_status:
                        if is_log_needed:
                            self.log("시스템 상태: WAITING (장 마감 - 분석 중지)")
                        self.was_market_open = current_market_status
                    else:
                        status_msg = "RUNNING"
                        self.log(f"시스템 상태: {status_msg}")
                        
                        # [추가] 시장 지수 상태 업데이트 (KOSPI/KOSDAQ)
                        if getattr(config, 'USE_MARKET_FILTER', True):
                            self._update_market_indices_status()
                            
                        # [최적화] 계좌 정보(잔고, 예수금)를 루프 시작 시 1회만 조회하여 공유
                        # 2 TPS 환경에서 중복 조회를 방지하여 성능 확보
                        acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                        
                        # 1. 잔고 조회
                        # [수정] 초기 구동 시 메인 스레드에서 조회한 데이터 재사용 (API 호출 절약)
                        if self.initial_holdings is not None:
                            holdings = self.initial_holdings
                            summary = self.initial_summary
                            self.initial_holdings = None
                            self.initial_summary = None
                        else:
                            holdings, summary = api.get_domestic_balance(target_cano, acnt)
                        
                        # [수정] 잔고 조회 실패 시 예외 발생 (Kill Switch 연동)
                        # 계좌 상태를 모르는 상태에서 매매를 진행하는 것은 위험함
                        if holdings is None:
                            raise Exception("잔고 조회 실패 (API 응답 없음)")
                        
                        # 2. 예수금 조회
                        # [최적화] 모의투자는 잔고 조회 결과(summary)에 예수금이 포함되어 있어 별도 호출 불필요
                        deposit_res = None
                        # [수정] 실전/모의 모두 summary 정보 우선 활용
                        if summary:
                            dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                            d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                            deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
                        
                        # 예수금이 0이거나 실전투자에서 정밀 조회가 필요한 경우 Fallback
                        if (not deposit_res or deposit_res['deposit'] == 0) and not config.session.is_simulation:
                            deposit_res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        
                        # API 호출 간격 조절 (Rate Limit 방지)
                        time.sleep(0.2)

                        # [수정] 락 범위 축소: 전체 로직을 감싸던 락 제거 (api.call_api 내부 락 활용)
                        # 1. 매도 조건 점검 (리스크 관리)
                        self._check_sell_conditions(holdings, current_market_status)
                        # 2. 매수 조건 점검
                        self._check_buy_conditions(holdings, deposit_res, current_market_status)
                        # 3. 미체결 주문 관리 (오래된 주문 취소) - 장 중에만 수행
                        self.order_manager.manage_unfilled_orders()
                        
                        # [추가] 루프 동안 매수/매도가 발생했을 수 있으므로, 
                        # 최종 로깅 전 잔고와 예수금을 최신 상태로 갱신합니다.
                        time.sleep(0.5)
                        try:
                            upd_holdings, upd_summary = api.get_domestic_balance(target_cano, acnt)
                            if upd_holdings is not None:
                                holdings = upd_holdings
                                summary = upd_summary
                                
                            if not config.session.is_simulation:
                                upd_dep = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                                if upd_dep: deposit_res = upd_dep
                            else:
                                if summary:
                                    dnca = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                                    d2_dep = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                                    deposit_res = {'deposit': dnca, 'foreign_deposit': 0, 'd2_deposit': d2_dep}
                        except Exception as e:
                            logger.debug(f"최종 상태 로깅을 위한 잔고 갱신 실패: {e}")

                        # [추가] 보유 종목 상태 로깅 및 자산 안전장치 체크
                        self._monitor_account_status(holdings, summary, deposit_res)
                        
                        # [추가] 관심 종목 변경 및 분석 제외 종목 메모리 캐시 정리
                        try:
                            valid_codes = {h['pdno'] for h in holdings}
                            for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
                                for item in config.session.stock_data.get(key, []):
                                    valid_codes.add(item['code'])
                                    
                            with self._lock:
                                keys_to_delete = [k for k in self.stock_state_cache if k not in valid_codes]
                                for k in keys_to_delete:
                                    del self.stock_state_cache[k]
                        except Exception as e:
                            logger.debug(f"상태 캐시 정리 중 오류: {e}")
                    
                    self.was_market_open = current_market_status
                    
                    if is_log_needed:
                        self.log("모니터링 완료. 대기 중...")
                
                interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
                
                # [수정] 미체결 주문 확인 시 발생하는 API 호출 지연(Delay)이 누적되어
                # 모니터링 주기가 설정값(180초)을 크게 초과하는 문제를 해결하기 위해 절대 시간 기반 대기 적용
                wait_start_time = time.time()
                last_unfilled_check = wait_start_time
                last_idle_unfilled_check = wait_start_time
                
                while self.is_running:
                    now = time.time()
                    if now - wait_start_time >= interval:
                        break
                        
                    # 5초 주기 도달 시
                    if now - last_unfilled_check >= 5:
                        last_unfilled_check = time.time()
                        
                        with self.order_manager._lock:
                            has_pending = bool(self.order_manager.pending_orders)
                            
                        # 시스템 내부에 대기 중인 미체결 주문이 있다면 5초 주기로 즉각 확인
                        # 없다면 외부 HTS 등 타 매체 주문 감지를 위해 60초 간격으로만 최소한의 API 확인 수행
                        if has_pending or (now - last_idle_unfilled_check >= 60):
                            self.order_manager.manage_unfilled_orders()
                            last_idle_unfilled_check = time.time()
                            
                    time.sleep(1)
                
                # 정상 루프 완료 시 에러 카운트 초기화
                self.consecutive_errors = 0
                    
            except Exception as e:
                self.consecutive_errors += 1
                max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
                self.log(f"에러 발생({self.consecutive_errors}/{max_err}): {str(e)}")
                console.print(f"[dim red]⚠️ 에러 발생: {str(e)}[/dim red]")
                
                if self.consecutive_errors >= max_err:
                    # [수정] 중단 대신 대기 모드로 전환
                    self.log(f"[장애 감지] 연속 에러 {max_err}회 발생. 서버 장애로 판단하여 대기 모드로 전환합니다.")
                    
                    # [개선] 상세 알림 메시지 구성
                    err_reason = str(e)
                    msg = f"🚨 [시스템 긴급 대기] 연속 에러 {max_err}회 발생\n매매를 일시 중단하고 서버 복구를 대기합니다.\n\n원인: {err_reason}\n\n서버 복구 확인 시 자동으로 재개됩니다."
                    
                    # [추가] 에러 로그 꼬리 첨부 (1시간 쿨타임)
                    now = time.time()
                    if now - self.last_emergency_alert_time > 3600:
                        log_tail = get_mystock_log_tail(20)
                        msg += f"\n\n📜 [최근 시스템 로그 (mystock.log)]\n```\n{log_tail}```"
                        self.last_emergency_alert_time = now
                    
                    api.send_telegram_message(msg)
                    
                    self._wait_for_server_recovery()
                    
                    # 복구되어 리턴되면 에러 카운트 초기화 후 루프 재개
                    self.consecutive_errors = 0
                    continue
                
                time.sleep(10)

    def _wait_for_server_recovery(self):
        """서버가 정상화될 때까지 대기"""
        check_interval = 60 # 1분마다 확인
        
        while self.is_running:
            time.sleep(check_interval)
            
            self.log("[장애 대기] 서버 상태 점검 중...")
            
            try:
                # 삼성전자 현재가 조회로 서버 상태 확인
                if api.check_server_health():
                    self.log("[서버 복구] 서버 정상화 확인. 매매를 재개합니다.")
                    api.send_telegram_message("✅ [서버 복구] KIS 서버가 정상화되었습니다.\n자동매매를 재개합니다.")
                    return
                else:
                    self.log("[장애 대기] 서버 여전히 응답 없음.")
            except Exception as e:
                self.log(f"[장애 대기] 점검 중 오류: {e}")


    def _monitor_account_status(self, holdings, summary, deposit_res):
        """현재 보유 종목 상태 로깅 및 자산 손실 제한(Loss Cut) 체크"""
        try:
            # [수정] 보유수량 0 초과인 종목만 필터링
            valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0]
            
            if not valid_holdings:
                self.log("보유 종목: 없음")
            else:
                # 한글 정렬 보정 헬퍼 함수
                def pad(s, width, align='>'):
                    k = sum(1 for c in s if ord(c) > 127)
                    real_len = len(s) + k
                    pad_len = width - real_len
                    if pad_len < 0: pad_len = 0
                    if align == '<': return s + ' ' * pad_len
                    else: return ' ' * pad_len + s

                # 헤더 출력
                header = (
                    f"{pad('종목명', 30, '<')} "
                    f"{pad('보유수량', 10, '>')} "
                    f"{pad('매입단가', 12, '>')} "
                    f"{pad('현재가', 12, '>')} "
                    f"{pad('매입금액', 15, '>')} "
                    f"{pad('평가금액', 15, '>')} "
                    f"{pad('평가손익', 14, '>')} "
                    f"{pad('수익률', 10, '>')}"
                )
                
                self.log("─" * 125)
                self.log(header)
                self.log("─" * 125)
                
                for item in valid_holdings:
                    name = f"{item['prdt_name']} ({item['pdno']})"
                    qty = int(item['hldg_qty'])
                    buy_price = float(item['pchs_avg_pric'])
                    cur_price = int(item['prpr'])
                    pchs_amt = int(item.get('pchs_amt', 0))
                    eval_amt = int(item.get('evlu_amt', 0))
                    profit = int(item['evlu_pfls_amt'])
                    rate = float(item['evlu_pfls_rt'])
                    
                    row_str = (
                        f"{pad(name, 30, '<')} "
                        f"{pad(f'{qty:,}주', 10, '>')} "
                        f"{pad(f'{buy_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{cur_price:,.0f}원', 12, '>')} "
                        f"{pad(f'{pchs_amt:,}원', 15, '>')} "
                        f"{pad(f'{eval_amt:,}원', 15, '>')} "
                        f"{pad(f'{profit:+,}원', 14, '>')} "
                        f"{pad(f'{rate:.2f}%', 10, '>')}"
                    )
                    self.log(row_str)
                
                self.log("─" * 125)
                if summary and len(summary) > 0:
                    s_data = summary[0]
                    
                    # [수정] API 지연에 따른 왜곡 방지를 위해 보유 종목 개별 합산 값 사용
                    tot_pchs = 0
                    total_profit = 0
                    total_eval = 0
                    if valid_holdings:
                        tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                        total_profit = sum(int(h['evlu_pfls_amt']) for h in valid_holdings)
                        total_eval = sum(int(h['evlu_amt']) for h in valid_holdings)
                    
                    # 총 자산 계산 (예수금 + 평가금)
                    current_total = 0
                    deposit_d2 = 0
                    if deposit_res:
                        deposit_d2 = deposit_res.get('d2_real', 0)
                        if deposit_d2 == 0:
                            deposit_d2 = deposit_res.get('d2_deposit', 0)
                    
                    # [수정] account 모듈을 활용하여 해외 자산까지 완벽하게 포함된 총 자산 획득
                    target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                    acnt_cd = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                    
                    asset_data = account.get_asset_status_data(target_cano, acnt_cd)
                    if asset_data:
                        current_total = asset_data.get('tot_asset', 0)
                        order_possible = asset_data.get('order_possible', deposit_d2)
                    else:
                        # Fallback (API 실패 시 기존 로직으로 대안 계산)
                        cash = deposit_d2 + deposit_res.get('foreign_deposit', 0)
                        current_total = cash + total_eval
                        order_possible = deposit_res.get('order_possible', deposit_d2)
                    
                    # [추가] 일일 손실 제한 체크
                    if current_total > 0:
                        # [Fix] 초기 자산 로드 실패(0원) 시, 첫 유효 조회 값으로 보정
                        if self.initial_asset == 0:
                            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                            acc_str = f"{target_cano}-{acnt}"
                            saved_initial = load_daily_initial_asset(acc_str)
                            if saved_initial > 0:
                                self.initial_asset = saved_initial
                                self.log(f"[시스템 보정] 기존 초기 자산 기록 로드: {self.initial_asset:,}원")
                            else:
                                self.initial_asset = current_total
                                save_daily_initial_asset(acc_str, self.initial_asset)
                                self.log(f"[시스템 보정] 초기 자산 정보 갱신 및 저장: {self.initial_asset:,}원")

                            # [추가] DB에 기록
                            try:
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                db_manager.db.save_daily_asset(today_str, acc_str, self.initial_asset)
                            except: pass

                        profit_rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        
                        realized_profit = 0
                        try:
                            today_str = datetime.now().strftime("%Y-%m-%d")
                            
                            target_account = None
                            if config.session.is_simulation:
                                target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
                            elif config.session.auto_cano:
                                target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
                                
                            today_trades = db_manager.db.get_trades(
                                start_date=today_str, end_date=today_str, 
                                is_sim=config.session.is_simulation, account=target_account
                            )
                            
                            today_trades_parsed = []
                            for r in today_trades:
                                type_str = r['type']
                                simple_type = "buy" if "매수" in type_str or "buy" in type_str.lower() else "sell"
                                parsed_r = dict(r)
                                parsed_r['type'] = simple_type
                                today_trades_parsed.append(parsed_r)
                            
                            today_trades_refined = self._refine_trade_records(today_trades_parsed)
                            sell_trades = [x for x in today_trades_refined if x['type'] == 'sell']
                            realized_profit = sum(int(t.get('profit_amt') or 0) for t in sell_trades)
                        except Exception:
                            pass
                            
                        realized_rate = (realized_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
                        daily_profit = current_total - self.initial_asset
                        daily_profit_rate = (daily_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
                        order_possible = deposit_res.get('order_possible', deposit_d2) if deposit_res else 0
                        
                        self.log(f"[증권 자산 현황] 증권 매입 금액: {tot_pchs:,}원 | 증권 평가 금액: {total_eval:,}원 | 증권 평가 손익: {total_profit:+,}원 ({profit_rate:+.2f}%) | 주문 가능 금액: {order_possible:,}원")
                        self.log(f"[오늘 자산 현황] 오늘 시작 자산: {self.initial_asset:,}원 | 오늘 현재 자산: {current_total:,}원 | 오늘 현재 손익: {daily_profit:+,}원 ({daily_profit_rate:+.2f}%) | 오늘 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)")

                        self.risk_manager.check_loss_limit(current_total)
                    else:
                        self.log(f"   총 평가금액: {total_eval:,}원  |  총 평가손익: {total_profit:+,}원")
                    
        except Exception: pass

    def _get_total_estimated_asset(self):
        """현재 총 추정 자산(예수금 + 주식평가금) 계산"""
        try:
            cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
            # [수정] 해외 자산 누락 방지를 위해 완벽하게 계산된 통합 자산 데이터 사용
            asset_data = account.get_asset_status_data(cano, acnt)
            if asset_data:
                return asset_data.get('tot_asset', 0)
        except Exception as e:
            logger.debug(f"자산 조회 중 예외 발생: {str(e)}")
        
        self.log(f"⚠️ 자산 조회 최종 실패. (KIS 서버 응답 지연)")
        return None


    def _get_prev_rsi(self, df):
        """전일 RSI 계산 (주의 조건 판단용)"""
        if df is not None and not df.empty and len(df) >= 16:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            try: return (100 - (100 / (1 + gain/loss))).iloc[-2]
            except: pass
        return None

    def _check_sell_conditions(self, holdings, is_market_open=True):
        # [최적화] 인자로 전달받은 holdings 사용
        if not holdings: return

        # [추가] 개별 룰 로드
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
        rules_map = {r['code']: r for r in custom_rules}
        
        # [추가] 트레이딩 제한 종목 로드
        restricted_stocks = load_restricted_stocks()

        # [최적화] 보유 종목 실시간 데이터 일괄 수집 (Micro-Cache 사전 예열)
        codes_to_prefetch = []
        for item in holdings:
            code = item['pdno']
            qty = api.safe_int(item.get('ord_psbl_qty'))
            if not self.order_manager.is_pending(code) and qty > 0:
                codes_to_prefetch.append(code)
                
        if codes_to_prefetch:
            api.prefetch_multiple_current_prices(codes_to_prefetch, is_overseas=False, include_investor=False)

        # [수정] 일괄 예열 캐시를 활용하므로 워커별 딜레이를 대폭 단축 (Rate Limit 안전장치 유지)
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 0.1  
        
        # [추가] 시장 국면 판단 (적응형 임계값용) - 매도 분석 시에도 상태 분류를 위해 필요
        market_regime_adj = {}
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj

        # [Fix: Point 1] 매도 분석 루프 병렬화 (ThreadPoolExecutor 활용)
        def _sell_worker(item):
            if not self.is_running: return
            
            code = item['pdno']; name = item['prdt_name']
            
            # [추가] 트레이딩 제한 종목은 매도 분석에서 완전히 제외 (수동 매수/홀딩용)
            if code in restricted_stocks:
                self.set_stock_state(code, None)
                self.log(f"[분석스킵] {name}: 트레이딩 제한 종목 (수동 홀딩)")
                return
            
            if self.order_manager.is_pending(code):
                self.set_stock_state(code, None)
                if config.FILE_DEBUG_LEVEL == "DEBUG": self.log(f"[분석스킵] {name}: 진행 중인 주문 존재")
                return
            
            qty = api.safe_int(item.get('ord_psbl_qty'))
            profit_rate = float(item['evlu_pfls_rt'])
            current_price = float(item['prpr'])
            buy_price = float(item['pchs_avg_pric'])
            
            time.sleep(safe_delay)
            
            if not self.is_running: return # 대기 후 재확인
            
            if qty <= 0: 
                self.set_stock_state(code, None)
                if config.FILE_DEBUG_LEVEL == "DEBUG": self.log(f"[분석스킵] {name}: 주문 가능 수량 0")
                return
            
            rule = rules_map.get(code)
            market_type = self._get_stock_market_type(code)
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            score_adj = market_regime_adj.get(market_type, 0.0)
            
            ts_activation = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0)
            ts_callback = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0)
            
            thresholds = None
            if rule:
                ts_activation = rule['ts_activation']
                ts_callback = rule['ts_callback']
                thresholds = {
                    "TAKE_PROFIT_RATE": rule['take_profit'],
                    "STOP_LOSS_RATE": rule['stop_loss'],
                    "TAKE_PROFIT_RSI": rule['take_profit_rsi'],
                    "SELL_SCORE": rule['sell_score'],
                    "WEIGHTS": rule.get('weights'),
                    "BUY_SCORE": rule['buy_score'],
                    "TIME_STOP_DAYS": rule.get('time_stop_days', config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)),
                    "HALF_TAKE_PROFIT_USE": bool(rule.get('half_take_profit_use', config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True)))
                }
            else:
                thresholds = {
                    "WEIGHTS": config.SCORING_WEIGHTS,
                    "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj,
                    "TIME_STOP_DAYS": config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
                }

            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
            if rule and rule.get('use_atr_stop') is not None:
                use_atr_stop = bool(rule['use_atr_stop'])

            applied_sl_rate = None
            if use_atr_stop:
                # [Fix: Point 4] 분할 매수를 고려하여, 현재 보유량에 해당하는 모든 매수 기록의
                # ATR 손절률을 수량 가중 평균하여 적용합니다.
                buy_trades = db_manager.db.get_buy_trades_for_current_holding(code)
                if buy_trades:
                    total_qty_trade = 0
                    weighted_sl_sum = 0
                    for trade in buy_trades:
                        qty_trade = api.safe_int(trade.get('qty', 0))
                        sl_rate_trade = float(trade.get('stop_loss_rate', 0.0))
                        if qty_trade > 0 and sl_rate_trade != 0.0:
                            total_qty_trade += qty_trade
                            weighted_sl_sum += qty_trade * sl_rate_trade
                    
                    if total_qty_trade > 0:
                        avg_sl_rate = weighted_sl_sum / total_qty_trade
                        if avg_sl_rate != 0.0: applied_sl_rate = avg_sl_rate
            
            if applied_sl_rate is not None:
                if thresholds is None: thresholds = {}
                thresholds["STOP_LOSS_RATE"] = applied_sl_rate
                
                # [수정] 실제 매도 평가에 사용될 손절선 변수(sl_rate) 갱신
                sl_rate = applied_sl_rate
                
                # [추가] ATR 동적 손절 사용 시, 본전 청산 발동 기준을 손절폭(절대값)과 1:1로 자동 동기화
                if applied_sl_rate < 0:
                    bep_activation = abs(applied_sl_rate)
                
            holding_days = 0
            is_mr_holding = False
            last_buy = db_manager.db.get_latest_buy_trade(code)
            if last_buy and last_buy.get('time'):
                if '역매수' in str(last_buy.get('reason', '')) or '역추세' in str(last_buy.get('reason', '')):
                    is_mr_holding = True
                try:
                    buy_dt = datetime.strptime(last_buy['time'], "%Y-%m-%d %H:%M:%S")
                    holding_days = (datetime.now() - buy_dt).days
                except: pass

            with self._lock:
                cached_highest = self.trailing_stop_cache.get(code)
                if cached_highest is None:
                    val = db_manager.db.get_highest_price(code)
                    cached_highest = val if val is not None else 0.0
                    self.trailing_stop_cache[code] = cached_highest
            
            highest_price = cached_highest
            
            if current_price > buy_price:
                if highest_price == 0.0 or current_price > highest_price:
                    db_manager.db.update_highest_price(code, current_price)
                    with self._lock:
                        self.trailing_stop_cache[code] = current_price
                    highest_price = current_price

            df = api.get_chart_data(code, is_overseas=is_overseas_stock)
            
            # [추가] 차트 데이터 당일 종가/고가/저가 실시간 갱신 (지표 불일치 완벽 방지)
            if df is not None and not df.empty and current_price > 0:
                df.iloc[-1, df.columns.get_loc('close')] = float(current_price)
                if current_price > df.iloc[-1]['high']: df.iloc[-1, df.columns.get_loc('high')] = float(current_price)
                if current_price < df.iloc[-1]['low']: df.iloc[-1, df.columns.get_loc('low')] = float(current_price)

            already_half_sold = code in self.half_tp_cache
            result = self.strategy.analyze_sell(code, name, df, current_price, buy_price, profit_rate, thresholds=thresholds, already_half_sold=already_half_sold, holding_days=holding_days, is_mr_holding=is_mr_holding, highest_price=highest_price)
            
            # [추가] 분석 성공 시 상태 업데이트
            self.set_stock_state(code, result['state'])
            
            ind = result['ind']
            rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
            adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
            cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
            action_str = "매도" if result['action'] == 'sell' else "보유"
            rule_msg = " [개별 룰 적용]" if rule else ""
            extra_info = ""
            if thresholds:
                bs = thresholds.get('BUY_SCORE')
                if bs is not None: extra_info += f", 기준={bs:.1f}"
                w = thresholds.get('WEIGHTS')
                if w: extra_info += f", 가중치={w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

            self.log(f"[보유분석] {name}({code}): 수익률={profit_rate:.2f}%, 점수={result['score']}, 상태={result['state']}, 판단={action_str}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}{extra_info}{rule_msg}")

            if result['action'] == 'sell':
                reason = result['reason']
                score = result['score']
                sell_ratio = result.get('sell_ratio', 1.0)
                
                if sell_ratio < 1.0:
                    target_sell_qty = int(qty * sell_ratio)
                    if target_sell_qty < 1:
                        self.log(f"매도 보류: {name} - 보유 수량({qty}주) 부족으로 분할 매도({reason}) 스킵 (최종 목표 대기)")
                        return
                else:
                    target_sell_qty = qty
                
                if rule: reason += " [개별 룰 적용]"
                if applied_sl_rate is not None and "손절" in reason: reason = reason.replace("손절", "ATR손절")
                
                raw_order_price = current_price * (1 - config.SLIPPAGE_RATE)
                order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False))
                if order_price <= 0: order_price = int(current_price)

                if not is_market_open:
                    self.log(f"[장마감] 매도 신호 감지 (주문 미전송): {name} - {reason}")
                    return

                real_qty = api.fetch_sellable_quantity(code)
                if real_qty < target_sell_qty:
                    if real_qty > 0:
                        self.log(f"매도 수량 조정: {name} {target_sell_qty}주 -> {real_qty}주")
                        target_sell_qty = real_qty
                    else:
                        self.log(f"매도 중단: {name} 주문 가능 수량 부족 (미체결 존재 가능성)")
                        return

                self.log(f"매도 실행: {name} - {reason}")
                odno = self.order_manager.send_order(code, target_sell_qty, "sell", name=name, profit_amt=int(item['evlu_pfls_amt']), profit_rate=profit_rate, reason=reason, score=score, price=order_price, rule=rule)
                if odno:
                    record = {
                        "type": "sell", "code": code, "name": name, "qty": target_sell_qty,
                        "price": float(order_price), "profit_rate": profit_rate,
                        "profit_amt": int(item['evlu_pfls_amt']), "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "odno": odno
                    }
                    self.trade_records.append(record)
                    
                    # [Fix: Point 2] 반익절 캐시 DB 동기화
                    if sell_ratio < 1.0: 
                        self.half_tp_cache.add(code)
                        db_manager.db.insert_half_tp(code)
                    else:
                        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                        target_acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                        canceled_cnt = db_manager.db.cancel_reserved_sell_orders(target_cano, target_acnt, code)
                        if canceled_cnt > 0:
                            self.log(f"[예약취소] 전량 매도로 인해 대기 중이던 {name} 매도 예약 주문 {canceled_cnt}건 자동 취소")
                            api.send_telegram_message(f"🗑 [예약 취소] {name}({code}) 전량 매도로 인해 대기 중이던 매도 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.")
                            
                        self.half_tp_cache.discard(code)
                        db_manager.db.delete_half_tp(code)
                        db_manager.db.delete_trailing_stop(code)
                        with self._lock:
                            if code in self.trailing_stop_cache: del self.trailing_stop_cache[code]
                            
                    # [추가] 매수 로직(상관관계 분석 등)에서 이미 매도한 종목을 보유 중인 것으로 오인하지 않도록 메모리 잔고 즉시 차감
                    try:
                        item['hldg_qty'] = str(max(0, int(item.get('hldg_qty', 0)) - target_sell_qty))
                    except: pass

        # 병렬 처리 실행
        max_workers = 5 if not config.session.is_simulation else 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_sell_worker, item) for item in holdings]
            concurrent.futures.wait(futures)

    def _check_buy_conditions(self, holdings, deposit_res, is_market_open=True):
        # [수정] 매수 대상 확장을 위해 국내 주식 및 국내 ETF 리스트 병합 (그룹 정보 추가)
        targets = []
        for item in config.session.stock_data.get("stocks_kr", []):
            item_copy = dict(item)
            item_copy['group'] = 'stocks_kr'
            targets.append(item_copy)
            
        # [수정] ETF 포함 여부 설정에 따라 대상에 추가
        if getattr(config, 'SYSTEM_INCLUDE_ETF', False):
            for item in config.session.stock_data.get("etfs_kr", []):
                item_copy = dict(item)
                item_copy['group'] = 'etfs_kr'
                targets.append(item_copy)
            
        if not targets: return
        
        # [추가] 필터링 카운트 초기화 (매 주기마다 갱신)
        self.skipped_by_market_filter_count = {"KOSPI": 0, "KOSDAQ": 0}
        skipped_stocks = [] # [추가] 시장 필터링으로 보류된 종목 리스트
        
        # [추가] 보유 종목 조회 (중복 매수 방지 및 그룹 정보 매핑)
        holding_codes = set()
        holding_names_map = {}
        holding_groups_map = {}
        
        code_to_group = {}
        for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
            for item in config.session.stock_data.get(key, []):
                code_to_group[item['code']] = key
                
        if holdings:
            for h in holdings:
                # [추가] 이번 루프의 매도 로직에서 수량이 0이 된 종목은 제외
                if int(h.get('hldg_qty', 0)) <= 0:
                    continue
                code = h['pdno']
                holding_codes.add(code)
                holding_names_map[code] = h['prdt_name']
                holding_groups_map[code] = code_to_group.get(code, 'stocks_kr')
        
        # [수정] 최대 보유 종목 수 체크 (투자 비중에 따라 자동 계산)
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2)
        if invest_ratio <= 0: invest_ratio = 0.2 # 0 이하일 경우 기본값 20%

        max_holdings = getattr(config, 'SYSTEM_MAX_HOLDINGS', 10)
        
        if len(holding_codes) >= max_holdings:
            if self.consecutive_errors == 0: # 로그 도배 방지
                self.log(f"매수 스킵: 최대 보유 종목 수({max_holdings}개) 도달 (투자비중 {invest_ratio*100:.0f}% 기준)")
            return

        # 예수금 확인 (API 직접 호출)
        avail_cash = 0
        if deposit_res:
            avail_cash = deposit_res['d2_deposit'] # 주문 가능 금액은 D+2 예수금 기준
        else:
            return # 조회 실패 시 매수 중단

        # [수정] 최소 주문 가능 금액 하향 조정 (50,000 -> 1,000) 및 로그 추가
        min_cash = 1000
        if avail_cash < min_cash:
            if self.consecutive_errors == 0: # 로그 도배 방지
                 self.log(f"매수 스킵: 예수금 부족 ({avail_cash:,}원 < {min_cash:,}원)")
            return 
            
        # [추가] 개별 룰 로드
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
        rules_map = {r['code']: r for r in custom_rules}

        # [추가] 당일 매도 이력 확인 및 재진입 허들(체결강도) 설정
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_account = None
        if config.session.is_simulation:
            target_account = f"{config.session.cano}-{config.session.acnt_prdt_cd}"
        elif config.session.auto_cano:
            target_account = f"{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}"
            
        try:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=config.session.is_simulation, account=target_account)
        except TypeError:
            today_trades = db_manager.db.get_trades(start_date=today_str, end_date=today_str, is_sim=config.session.is_simulation)
            if target_account:
                today_trades = [t for t in today_trades if t.get('account') == target_account]
                
        sold_today = set(t['code'] for t in today_trades if "sell" in t.get('type', '').lower() or "매도" in t.get('type', ''))
        
        reentry_hurdles = {}
        for scode in sold_today:
            last_buy = db_manager.db.get_latest_buy_trade(scode)
            if last_buy:
                reason = last_buy.get('reason', '')
                match = re.search(r'체결강도:\s*([0-9.]+)%', reason)
                if match:
                    reentry_hurdles[scode] = float(match.group(1))
                else:
                    reentry_hurdles[scode] = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)

        # 1. 후보 분석
        candidates = self._analyze_candidates(targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map)
        
        # 2. 매수 집행
        if candidates:
            if not is_market_open:
                self.log(f"[장마감] 매수 후보 감지 (주문 미전송): {len(candidates)}종목")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                return

            self._execute_buy_orders(candidates, avail_cash, invest_ratio, len(holding_codes), max_holdings)

    def _analyze_candidate_worker(self, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map):
        """(내부함수) 매수 후보 분석용 단일 워커"""
        if not self.is_running: return None # [추가] 중지 요청 시 즉시 종료
        
        try:
            # [추가] 시스템 트레이딩 스레드임을 마킹 (API 우선순위 획득용)
            context.trade_context.is_system_trading = True
            
            # [추가] API 호출 전 대기 (Rate Limit 방지 - 스레드별 분산 효과)
            time.sleep(safe_delay)
            
            if not self.is_running: return None # 대기 후 재확인
            
            code = item['code']; name = item['name']
            
            # 1. 트레이딩 제한 종목 체크
            if code in restricted_stocks:
                self.set_stock_state(code, None)
                return {'type': 'restricted_skip', 'name': name}
            
            # 2. 진행 중인 주문 체크
            if self.order_manager.is_pending(code):
                self.set_stock_state(code, None)
                return None

            # 3. 보유 종목 체크
            if code in holding_codes: return None
            
            # 4. 시장 지수 필터링 (종목별 적용)
            if getattr(config, 'USE_MARKET_FILTER', True):
                market_type = self._get_stock_market_type(code)
                market_stat = self.market_index_status.get(market_type)
                if market_stat and isinstance(market_stat, dict):
                    if not market_stat.get('is_healthy', True):
                        self.set_stock_state(code, None)
                        return {'type': 'market_skip', 'name': name, 'market_type': market_type}
            
            if not self.is_running: return None # API 호출 전 최종 확인
            
            is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
            
            # 5. [최적화] 차트, 체결강도, 호가창 데이터 병렬(동시) 조회
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                fut_chart = ex.submit(api.get_chart_data, code, is_overseas=is_overseas_stock)
                fut_vol = ex.submit(api.get_realtime_vol_strength, code) if not is_overseas_stock else None
                fut_ob = ex.submit(api.get_order_book, code, is_overseas_stock)
                
                df = fut_chart.result()
                try: vol_strength = fut_vol.result() if fut_vol else None
                except: vol_strength = None
                try: ob_data = fut_ob.result() if fut_ob else None
                except: ob_data = None
                
            if df is None or df.empty:
                self.set_stock_state(code, None)
                return None
            
            # [수정] 캐시된 차트 데이터의 당일 미확정 종가를 실시간 최신 현재가로 업데이트
            # (종목 분석 메뉴와 시스템 트레이딩 간의 지표 및 점수 불일치 원천 차단)
            try:
                realtime_price = api.get_current_price(code, is_overseas=is_overseas_stock)
                if realtime_price and realtime_price > 0:
                    df.iloc[-1, df.columns.get_loc('close')] = float(realtime_price)
                    if realtime_price > df.iloc[-1]['high']: df.iloc[-1, df.columns.get_loc('high')] = float(realtime_price)
                    if realtime_price < df.iloc[-1]['low']: df.iloc[-1, df.columns.get_loc('low')] = float(realtime_price)
            except: pass
            
            current_price = float(df.iloc[-1]['close'])
            
            # [추가] 호가창 매도/매수 잔량 비율(비대칭성) 계산
            ask_bid_ratio = None
            if ob_data and ob_data.get('rt_cd') == '0':
                out1 = ob_data.get('output1', {})
                total_ask = api.safe_int(out1.get('total_askp_rsqn'))
                total_bid = api.safe_int(out1.get('total_bidp_rsqn'))
                if total_bid > 0:
                    ask_bid_ratio = total_ask / total_bid
                elif total_ask > 0:
                    ask_bid_ratio = 99.9 # 매수 잔량은 없고 매도만 있는 상태
            
            # [추가] 상관계수 필터링
            if getattr(config, 'USE_CORRELATION_FILTER', True) and holdings_dfs:
                corr_threshold = getattr(config, 'CORRELATION_THRESHOLD', 0.7)
                cand_ret = df.set_index('date')['close'].astype(float).pct_change().dropna()
                cand_group = item.get('group', 'stocks_kr') # 후보 종목의 그룹
                
                for hold_code, hold_info in holdings_dfs.items():
                    hold_group = holding_groups_map.get(hold_code, 'stocks_kr')
                    
                    # [추가] 같은 그룹(국내주식-국내주식, 국내ETF-국내ETF)끼리만 상관계수 비교
                    if cand_group != hold_group:
                        continue
                        
                    hold_df = hold_info['df']
                    hold_name = hold_info['name']
                    if hold_df is None or hold_df.empty: continue
                    hold_ret = hold_df.set_index('date')['close'].astype(float).pct_change().dropna()
                    
                    combined = pd.concat([cand_ret, hold_ret], axis=1, join='inner').dropna()
                    if len(combined) > 30:
                        corr = combined.iloc[:, 0].corr(combined.iloc[:, 1])
                        if corr >= corr_threshold:
                            log_msg = f"[상관관계 보류] {name}({code}): 보유 종목 '{hold_name}'과 높은 상관관계 (상관계수: {corr:.2f} >= {corr_threshold})"
                            self.set_stock_state(code, None)
                            return {'type': 'correlation_skip', 'name': name, 'log': log_msg}

            # 룰 및 임계값 설정
            rule = rules_map.get(code)
            market_type = self._get_stock_market_type(code)
            score_adj = market_regime_adj.get(market_type, 0.0)
            base_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            
            thresholds = None
            if rule:
                thresholds = {
                    "BUY_SCORE": rule['buy_score'], # [수정] 개별 룰은 시장 보정 무시 (절대값)
                    "BUY_RSI_MAX": rule['buy_rsi'],
                    "BUY_VOL_STRENGTH": rule.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)),
                    "BUY_ASK_BID_RATIO": rule.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)),
                    "AUTO_ADJUST_ASK_BID_RATIO": bool(rule.get('auto_adjust_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True))),
                    "WEIGHTS": rule.get('weights')
                }
            else:
                thresholds = {
                    "BUY_SCORE": base_buy_score + score_adj,
                    "WEIGHTS": config.SCORING_WEIGHTS
                }
            
            # 전략 실행
            result = self.strategy.analyze_buy(code, name, df, current_price, vol_strength=vol_strength, thresholds=thresholds, ask_bid_ratio=ask_bid_ratio)
            if not result:
                self.set_stock_state(code, None)
                return None
                
            # [추가] 분석 성공 시 상태 업데이트
            self.set_stock_state(code, result['state'])
            
            # 로그 출력을 위한 문자열 구성
            rsi_val = f"{result['rsi']:.1f}" if result['rsi'] is not None else "-"
            adx_val = f"{result['adx']:.1f}" if result['adx'] is not None else "-"
            cci_val = f"{result['cci']:.1f}" if result['cci'] is not None else "-"
            sar_val = result.get('psar')
            if sar_val is not None:
                sar_str = "상승" if current_price > sar_val else "하락"
            else:
                sar_str = "-"
            macd_val = result.get('macd'); sig_val = result.get('macd_signal')
            macd_str = "골든" if macd_val is not None and sig_val is not None and macd_val > sig_val else "데드"
            obv_trend = result.get('obv_trend')
            obv_str = "상승" if obv_trend is True else ("하락" if obv_trend is False else "-")
            sm_str = "O" if result.get('smart_money') else "X"
            vol_val = f"{result['vol_strength']:.1f}%" if result.get('vol_strength') else "-"
            rule_msg = " [개별 룰 적용]" if rule else ""
            
            # [추가] 가짜 체결강도로 걸러진 경우 사유 표시
            vol_reject_msg = ""
            if result.get('vol_reject_reason'):
                vol_reject_msg = f" [{result['vol_reject_reason']}]"
            elif result.get('ask_bid_ratio') is not None:
                vol_reject_msg = f" [매도잔량비:{result['ask_bid_ratio']:.2f}]"
            
            log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}"
            
            if result['action'] == "buy":
                reentry_msg = ""
                if code in reentry_hurdles:
                    req_vol = reentry_hurdles[code]
                    vol_strength_val = result.get('vol_strength')
                    if vol_strength_val is None or vol_strength_val <= req_vol:
                        log_msg = f"[분석스킵] {name}({code}): 당일 재진입 불가 (체결강도 {vol_strength_val if vol_strength_val else 0:.1f}% <= 기존매수 {req_vol:.1f}%)"
                        return {'type': 'log_only', 'log': log_msg}
                    else:
                        reentry_msg = f"당일 재진입(기존 {req_vol:.1f}% 경신)"

                candidate_data = {
                    'code': code, 'name': name, 'price': current_price,
                    'score': result['score'], 'rsi': result['rsi'], 'adx': result['adx'], 'cci': result['cci'], 'atr': result.get('atr', 0), 'vol_strength': result.get('vol_strength'),
                    'is_custom_rule': bool(rule), 'rule': rule, 'state': result['state'],
                    'state_reason': result.get('state_reason', ''),
                    'reentry_msg': reentry_msg
                }
                reentry_log = f" [{reentry_msg}]" if reentry_msg else ""
                log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SM={sm_str}, SAR={sar_str}, 체결={vol_val}{rule_msg}{vol_reject_msg}{reentry_log}"
                return {'type': 'candidate', 'data': candidate_data, 'log': log_msg}
            else:
                return {'type': 'log_only', 'log': log_msg}
        except Exception: return None

    def _analyze_candidates(self, targets, holding_codes, rules_map, reentry_hurdles, holding_names_map, holding_groups_map):
        candidates = []
        skipped_stocks = []
        restricted_skipped_stocks = [] # [추가] 트레이딩 제한 스킵 리스트
        correlation_skipped_stocks = [] # [추가] 상관관계 스킵 리스트
        
        # [추가] 트레이딩 제한 종목 로드
        restricted_stocks = load_restricted_stocks()
        
        # [추가] 시장 국면 판단 (적응형 임계값용)
        market_regime_adj = {} # Market Type -> Score Adj
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj
                if self.consecutive_errors == 0:
                    # [추가] 시장 필터링 상태 로그
                    filter_status_str = ""
                    if getattr(config, 'USE_MARKET_FILTER', True):
                        market_stat = self.market_index_status.get(m_type)
                        if market_stat and isinstance(market_stat, dict):
                            is_healthy = market_stat.get('is_healthy', True)
                            filter_status_str = "허용" if is_healthy else "보류"
                            filter_status_str = f" | 필터링: {filter_status_str}"
                    self.log(f"[{m_type}] 시장 국면: {regime} (매수기준 {adj:+.1f}점){filter_status_str}")

        # [추가] 보유 종목의 차트 데이터 수집 (상관계수 분석용)
        holdings_dfs = {}
        use_corr_filter = getattr(config, 'USE_CORRELATION_FILTER', True)
        if use_corr_filter and holding_codes:
            for code in holding_codes:
                is_overseas = not (code.isdigit() and len(code) == 6)
                df = api.get_chart_data(code, is_overseas)
                if df is not None and not df.empty:
                    name = holding_names_map.get(code, code)
                    holdings_dfs[code] = {'name': name, 'df': df}

        # [최적화] 분석 대상 종목 실시간 데이터 일괄 수집 (Micro-Cache 사전 예열)
        codes_to_prefetch = []
        for item in targets:
            code = item['code']
            if code in restricted_stocks or code in holding_codes or self.order_manager.is_pending(code):
                continue
            
            codes_to_prefetch.append(code)
            
        if codes_to_prefetch:
            # [수정] 시장 구분(_get_stock_market_type)에 필요한 현재가 정보를 먼저 일괄 prefetch 합니다.
            # 이렇게 하면 _analyze_candidate_worker 내부에서 개별 API 호출을 방지할 수 있습니다.
            api.prefetch_multiple_current_prices(codes_to_prefetch, is_overseas=False, include_investor=False)

        # [수정] 일괄 예열 캐시를 활용하므로 워커별 딜레이를 대폭 단축 (Rate Limit 안전장치 유지)
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 0.1

        # [병렬 처리] 사용자 작업과의 충돌 및 모의투자 API 제한(2 TPS) 고려
        # (실전: 5개, 모의: 2개 - ThrottledSession이 병목 없이 안전하게 제어함)
        max_workers = 5 if not config.session.is_simulation else 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._analyze_candidate_worker, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay, reentry_hurdles, holdings_dfs, holding_groups_map) for item in targets]
            
            for future in concurrent.futures.as_completed(futures):
                if not self.is_running: break
                res = future.result()
                if res:
                    if res['type'] == 'candidate':
                        self.log(res['log'])
                        candidates.append(res['data'])
                    elif res['type'] == 'log_only':
                        self.log(res['log'])
                    elif res['type'] == 'restricted_skip':
                        restricted_skipped_stocks.append(res['name'])
                    elif res['type'] == 'market_skip':
                        m_type = res.get('market_type', 'KOSPI')
                        if m_type in self.skipped_by_market_filter_count:
                            self.skipped_by_market_filter_count[m_type] += 1
                        skipped_stocks.append(res['name'])
                    elif res['type'] == 'correlation_skip':
                        self.log(res['log'])
                        correlation_skipped_stocks.append(res['name'])

        # [추가] 트레이딩 제한 종목 스킵 로그 기록
        if restricted_skipped_stocks:
            self.log(f"[매수 스킵] 트레이딩 제한 종목 ({len(restricted_skipped_stocks)}개): {', '.join(restricted_skipped_stocks)}")

        # [추가] 시장 필터링 보류 종목 로그 기록
        if skipped_stocks:
            self.log(f"[시장 필터링] 하락장 매수 보류 ({len(skipped_stocks)}종목): {', '.join(skipped_stocks)}")

        # [추가] 상관관계 보류 종목 로그 기록
        if correlation_skipped_stocks:
            self.log(f"[상관관계 보류] 보유 종목과 유사 테마로 매수 보류 ({len(correlation_skipped_stocks)}종목): {', '.join(correlation_skipped_stocks)}")

        # [수정] 우선순위 정렬 (1. 점수 높은 순, 2. RSI 낮은 순)
        # 점수가 같다면 RSI가 낮을수록 상승 여력이 있다고 판단하여 우선순위를 둡니다.
        candidates.sort(key=lambda x: (-x['score'], x['rsi'] if x['rsi'] is not None else 999.0))
        
        # [추가] 선정된 후보군 우선순위 로그 출력
        if candidates:
            self.log(f"[매수 후보 선정] 총 {len(candidates)}종목 (우선순위순):")
            for i, c in enumerate(candidates):
                rsi_disp = f"{c['rsi']:.1f}" if c['rsi'] is not None else "-"
                vol_disp = f"{c['vol_strength']:.1f}%" if c.get('vol_strength') else "-"
                self.log(f"   {i+1}순위: {c['name']} (점수:{c['score']}, RSI:{rsi_disp}, 체결:{vol_disp})")
        
        return candidates


    def _execute_buy_orders(self, candidates, avail_cash, invest_ratio, current_holdings_count, max_holdings):
        for cand in candidates:
            if not self.is_running: break
            
            # [수정] 최소 주문 가능 금액 하향 조정
            if avail_cash < 1000:
                self.log(f"매수 중단: 잔여 예수금 부족 ({avail_cash:,}원)")
                break
            
            # [추가] 최대 보유 종목 수 도달 시 추가 매수 중단
            if current_holdings_count >= max_holdings:
                self.log(f"매수 중단: 최대 보유 종목 수({max_holdings}개) 도달")
                break

            # [추가] 손절률, ATR 여부 및 투자 비중 확인 (개별 룰 or 전역 설정)
            sl_rate = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
            atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
            cand_invest_ratio = invest_ratio

            if cand.get('rule'):
                rule = cand['rule']
                sl_rate = rule.get('stop_loss', sl_rate)
                if rule.get('use_atr_stop') is not None:
                    use_atr_stop = bool(rule['use_atr_stop'])
                if rule.get('atr_stop_multiplier') is not None:
                    atr_mult = rule['atr_stop_multiplier']
                if rule.get('invest_ratio') is not None:
                    cand_invest_ratio = rule['invest_ratio']

            atr_val = cand.get('atr', 0)
            price_val = cand.get('price', 0)
            atr_sl_rate = None # DB 저장용
            
            if use_atr_stop and atr_val > 0 and price_val > 0:
                # [수정] ATR 기반 동적 손절률 계산 (음수 값)
                stop_distance = atr_val * atr_mult
                sl_rate = -((stop_distance / price_val) * 100)

                # [추가] ATR 손절률 최대 한도 설정 (데이터 오류 등으로 인한 과도한 리스크 방지)
                max_atr_sl = config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)
                if max_atr_sl != 0 and sl_rate < max_atr_sl:
                    self.log(f"[리스크 조정] ATR 손절률({sl_rate:.1f}%)이 최대 한도({max_atr_sl}%)를 초과하여 조정됩니다.")
                    sl_rate = max_atr_sl
                
            # [수정] 자산 배분 로직 개선: 마지막 슬롯인 경우 남은 예수금 전액 투자
            remaining_slots = max_holdings - current_holdings_count
            
            # 1. 예산 할당 계산 (변동성 타겟팅 및 리스크 관리 적용)
            calc_amt = self.risk_manager.allocate_budget(avail_cash, cand_invest_ratio, stop_loss_rate=sl_rate, atr=cand.get('atr'), current_price=cand.get('price'))
            
            if remaining_slots == 1:
                # 마지막 종목일 때: 변동성 타겟팅/리스크 관리가 꺼져있다면 잔여 예수금 전액 사용, 켜져 있다면 계산된 금액 준수
                if not getattr(config, 'USE_VOLATILITY_TARGETING', True) and getattr(config, 'SYSTEM_RISK_PER_TRADE', 0) <= 0:
                    invest_amt = avail_cash
                else:
                    invest_amt = calc_amt
            else:
                invest_amt = calc_amt

            # [수정] 지정가 주문을 위해 현재가(정수) 확보
            current_price = int(cand['price'])

            # [수정] 슬리피지 비율 적용 및 호가 정렬 (체결 확률 증대)
            raw_order_price = current_price * (1 + config.SLIPPAGE_RATE)
            order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False))

            # 최소 주문 금액 보정 (할당된 예산이 1주 가격보다 적을 때 가용 예수금 전체를 쓰는 버그 방지)
            if invest_amt < order_price: invest_amt = order_price
            
            # [수정] 단순 계산 대신 API를 통해 정확한 매수 가능 수량 조회
            # 지정가 주문 시 해당 가격 기준으로 조회
            max_qty = api.fetch_buyable_quantity(cand['code'], order_price)
            
            # [추가] API가 0을 반환할 경우 로컬 예수금 기반 Fallback 계산
            if max_qty <= 0 and avail_cash > 0:
                max_qty = int((avail_cash * 0.998) / order_price)
            
            # 자산 배분 비중 적용 수량
            target_qty = int(invest_amt / order_price)
            
            # 실제 주문 수량은 (목표 수량)과 (API 조회 가능 수량) 중 작은 값
            qty = min(target_qty, max_qty)
            
            # [개선] 예수금(로컬) 부족 시 수량 자동 조정
            if avail_cash < (qty * order_price):
                qty = int(avail_cash / order_price)
            
            # [추가] 예수금 부족 로그
            if qty < 1:
                self.log(f"매수 실패: {cand['name']} - 매수 가능 수량 부족 (목표:{target_qty}, 가능:{max_qty}) | 예수금:{avail_cash:,}원, 필요:{order_price:,}원(1주)")
                continue
            
            rsi_val = f"{cand['rsi']:.1f}" if cand['rsi'] is not None else "-"
            adx_val = f"{cand['adx']:.1f}" if cand['adx'] is not None else "-"
            cci_val = f"{cand['cci']:.1f}" if cand.get('cci') is not None else "-"
            vol_val = f"{cand['vol_strength']:.1f}%" if cand.get('vol_strength') is not None else "-"
            
            # [수정] 매수 사유 포맷 분기 (일반/역매수)
            is_mr_buy = cand.get('state') == "역매수"
            state_reason = cand.get('state_reason', '')
            is_super = "슈퍼 모멘텀" in state_reason
            
            reason = "역매수 반등" if is_mr_buy else "조건 만족"
            if is_super:
                reason += "(슈퍼모멘텀)"
                
            if cand.get('reentry_msg'):
                reason += f" [{cand['reentry_msg']}]"
                
            if cand.get('is_custom_rule'):
                reason += " [개별 룰 적용]"
            
            reason += f" [점수:{cand['score']}, RSI:{rsi_val}, 체결강도:{vol_val}]"
            
            atr_msg = ""
            if atr_val > 0 and price_val > 0:
                annual_vol = (atr_val / price_val) * math.sqrt(252) * 100
                atr_msg += f"[ATR:{int(atr_val):,}/변동성:{annual_vol:.1f}%]"
            
            if use_atr_stop:
                if atr_msg: atr_msg += " "
                atr_msg += f"[ATR손절:{sl_rate:.0f}%]"
            
            if atr_msg:
                reason += f" {atr_msg}"

            self.log(f"매수 실행: {cand['name']} - {reason}")
            # [수정] 매수 시 사유와 점수, 그리고 지정가 가격을 DB 저장을 위해 전달
            odno = self.order_manager.send_order(cand['code'], qty, "buy", name=cand['name'], reason=reason, score=cand['score'], price=order_price, rule=cand.get('rule'), stop_loss_rate=sl_rate)
            if odno: 
                # [추가] 매수 주문 성공 시 대기 중인 예약 매수 취소 방어 로직 (중복 진입 방지)
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                target_acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                canceled_cnt = db_manager.db.cancel_reserved_buy_orders(target_cano, target_acnt, cand['code'])
                if canceled_cnt > 0:
                    self.log(f"[예약취소] 신규 매수로 인해 대기 중이던 {cand['name']} 매수 예약 주문 {canceled_cnt}건 자동 취소")
                    api.send_telegram_message(f"🗑 [예약 취소] {cand['name']}({cand['code']}) 신규 매수로 인해 대기 중이던 매수 예약 주문 {canceled_cnt}건이 자동 취소되었습니다.")
                
                self.half_tp_cache.discard(cand['code']) # 신규 매수 시 기존 반익절 캐시 방어적 초기화
                avail_cash -= (qty * order_price)
                current_holdings_count += 1 # [추가] 보유 종목 수 증가 반영
                record = {
                    "type": "buy",
                    "code": cand['code'],
                    "name": cand['name'],
                    "qty": qty,
                    "price": order_price,
                    "reason": reason,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "odno": odno,
                    "stop_loss_rate": sl_rate # [추가] 계산된 손절률 기록 (매도 시 참조 가능하도록)
                }
                self.trade_records.append(record)

    def _update_market_indices_status(self, notify=True):
        """KOSPI, KOSDAQ 지수 상태 업데이트 및 알림"""
        # [수정] analysis 모듈의 공통 함수 사용을 위해 리스트로 변경
        target_indices = ["KOSPI", "KOSDAQ"]
        
        ma_period = getattr(config, 'MARKET_FILTER_MA', 50)
        
        for market_name in target_indices:
            try:
                # [수정] analysis 모듈의 공통 함수 사용 (Fallback 포함)
                df = analysis.get_domestic_index_data(market_name)

                if df is None or df.empty or len(df) < ma_period:
                    self.log(f"{market_name} 지수 데이터 부족/조회 실패. 필터링을 건너뜁니다.")
                    self.market_index_status[market_name] = {"is_healthy": True, "current": 0}
                    continue
                
                ma_val = df['close'].rolling(window=ma_period).mean().iloc[-1]
                current_idx = df['close'].iloc[-1]
                is_healthy = current_idx >= ma_val
                
                self.market_index_status[market_name] = {
                    "is_healthy": is_healthy,
                    "current": current_idx
                }
                
                # 상태 변경 알림
                if not notify:
                    continue

                notified = self.market_status_notified.get(market_name, False)
                if not is_healthy and not notified:
                    api.send_telegram_message(f"📉 [시장 감지] {market_name} 지수가 {ma_period}일 이평선 아래로 하락했습니다.\n해당 시장 종목의 신규 매수를 일시 중단합니다.")
                    self.market_status_notified[market_name] = True
                elif is_healthy and notified:
                    api.send_telegram_message(f"📈 [시장 회복] {market_name} 지수가 {ma_period}일 이평선을 회복했습니다.\n매수를 재개합니다.")
                    self.market_status_notified[market_name] = False
            except Exception as e:
                self.log(f"{market_name} 지수 조회 실패: {e}")
                self.market_index_status[market_name] = {"is_healthy": True, "current": 0}

    def _get_stock_market_type(self, code):
        """종목 코드로 시장 구분(KOSPI/KOSDAQ) 확인 (캐싱 적용)"""
        if code in self.stock_market_map:
            return self.stock_market_map[code]

        # 1. stock.json에 사전 정의된 exchange 정보 직접 탐색 (가장 빠르고 정확함)
        for key in ["stocks_kr", "etfs_kr"]:
            for item in config.session.stock_data.get(key, []):
                if item['code'] == code and "exchange" in item:
                    m_type = item['exchange'].upper()
                    if m_type in ["KOSPI", "KOSDAQ"]:
                        self.stock_market_map[code] = m_type
                        return m_type

        # 2. API 조회를 통한 Fallback (한글 '코스닥' 포함)
        try:
            res = api.get_current_price_data(code, is_overseas=False)
            if res and res.get('rt_cd') == '0':
                market_name = res['output'].get('rprs_mrkt_kor_name', '')
                if "KOSDAQ" in market_name or "코스닥" in market_name:
                    self.stock_market_map[code] = "KOSDAQ"
                    return "KOSDAQ"
        except Exception:
            pass

        # 3. API 조회 실패 또는 정보 누락 시 기본값 'KOSPI'로 설정
        self.stock_market_map[code] = "KOSPI"
        return "KOSPI"

def _select_stock_for_rules():
    """룰 설정을 위한 종목 선택 헬퍼"""
    menu_items = [
        ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
        ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"), ("5", "직접 입력", "Direct Input")
    ]
    choice = utils.show_menu("개별 설정할 대상을 선택하세요", menu_items, default_choice="5")
    if choice.lower() in ['b', 'q']: return None, None, False
    
    menu_map = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map.get(choice, '')}")

    code, name, is_overseas = None, None, False
    
    if choice == '5':
        utils.print_breadcrumb()
        raw_input = Prompt.ask("종목코드(6자리/티커) 입력 [dim](이전: b, 메인: q)[/dim]")
        if raw_input and raw_input.lower() not in ['b', 'q']:
            context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {raw_input}")
            if raw_input.isdigit() and len(raw_input) == 6:
                code = raw_input
                name = api.get_stock_name_by_code(code, False) or code
                is_overseas = False
            else:
                code = raw_input.upper()
                name = api.get_stock_name_by_code(code, True) or code
                is_overseas = True
                
            if not utils.validate_and_confirm_stock(code, name, is_overseas, "이 종목을 선택하시겠습니까?"):
                return None, None, False
    elif choice in ["1", "2", "3", "4"]:
        key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
        s_list = config.session.stock_data.get(key_map[choice], [])
        if s_list:
            idx, item = utils.search_stock_in_list(s_list, title=f"{menu_map[choice]} 목록")
            if not item: return None, None, False
            code, name = item['code'], item['name']
            is_overseas = (choice in ["3", "4"])
            context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {name}")
        else:
            console.print("[yellow]목록이 비어있습니다.[/yellow]")
            return None, None, False
            
    return code, name, is_overseas

def _view_stock_rules():
    """설정된 룰 조회"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
    if not custom_rules:
        console.print("\n[yellow]설정된 개별 룰이 없습니다.[/yellow]")
        return

    table = Table(title="종목별 개별 트레이딩 룰 목록", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목명(코드)", justify="left")
    table.add_column("매수(점수/RSI/체결/비대칭)", justify="center")
    table.add_column("청산(익절/TS/RSI/기한)", justify="center")
    table.add_column("리스크(비중/손절)", justify="center")
    table.add_column("가중치", justify="center")
    table.add_column("메모", justify="left", style="dim")
    table.add_column("수정일", justify="center", style="dim")
    
    for i, r in enumerate(custom_rules):
        w_str = "기본"
        if r.get('weights'):
            w = r['weights']
            # [Fix] 가중치가 JSON 문자열인 경우 딕셔너리로 변환
            if isinstance(w, str):
                try: w = json.loads(w)
                except: pass
            
            if isinstance(w, dict):
                w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"

        sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
        ratio_str = f"{r.get('invest_ratio', getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1)) * 100:.0f}%"
        half_tp_str = " (반익절 O)" if r.get('half_take_profit_use', 1) else " (반익절 X)"

        table.add_row(
            f"{r['name']}({r['code']})",
            f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get('BUY_VOL_STRENGTH', 100.0))}% / {r.get('buy_ask_bid_ratio', config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0))}배",
            f"+{r['take_profit']}%{half_tp_str} / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
            f"{ratio_str} / {sl_str}",
            w_str,
            r.get('memo', ''),
            r['updated_at']
        )
        if (i + 1) % 5 == 0 and (i + 1) < len(custom_rules):
            table.add_section()
    console.print(table)

def _input_and_save_rule(code, name):
    """(내부함수) 룰 입력 및 저장 공통 로직"""
    utils.print_breadcrumb()
    console.print(f"[bold green]선택 종목: {name} ({code})[/bold green]")
    
    # [추가] 현재가 조회 (예상 가격 계산용)
    is_overseas = not (code.isdigit() and len(code) == 6)
    current_price = 0
    # [수정] 단순 조회이므로 status 사용
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]현재가 조회 중...[/cyan]", total=None)
        current_price = api.get_current_price(code, is_overseas)

    if current_price > 0:
        p_fmt = f"${current_price:,.2f}" if is_overseas else f"{int(current_price):,}원"
        console.print(f"[dim]현재가: {p_fmt} (기준)[/dim]")
    
    # 기존 설정 로드
    existing = db_manager.db.get_stock_strategy(code)
    if existing:
        existing = _enrich_rules_with_weights([existing])[0] # [추가] 가중치 보강

    defaults = {
        "buy_score": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "buy_rsi": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "buy_vol_strength": config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0), # [수정] 안전한 접근
        "auto_adjust_ask_bid_ratio": 1 if config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True) else 0,
        "buy_ask_bid_ratio": config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0),
        "sell_score": config.SELL_STRATEGY["SELL_SCORE"],
        "stop_loss": config.SELL_STRATEGY["STOP_LOSS_RATE"],
        "take_profit": config.SELL_STRATEGY["TAKE_PROFIT_RATE"],
        "take_profit_rsi": config.SELL_STRATEGY["TAKE_PROFIT_RSI"],
        "ts_activation": config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0),
        "ts_callback": config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0),
        "memo": "",
        "weights": None,
        "invest_ratio": getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2),
        "time_stop_days": config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10),
        "use_atr_stop": 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0,
        "atr_stop_multiplier": config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0),
        "half_take_profit_use": 1 if config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True) else 0
    }
    
    current = existing if existing else defaults.copy()
    
    # [추가] 기존 데이터에 신규 필드가 누락된 경우 기본값으로 채움 (DB 스키마 변경 대응)
    for key, val in defaults.items():
        if key not in current:
            current[key] = val
            
    # DB에서 가져온 값이 None일 경우 빈 문자열로 처리
    if 'memo' not in current or current['memo'] is None:
        current['memo'] = ""
    
    console.print("\n[설정값 입력 (Enter: 현재값 유지, 이전: b, 메인: q)]")
    
    new_strategy = {}
    
    class QuitInput(Exception): pass

    def ask_val(key, desc, help_text, type_func):
        val = Prompt.ask(f"{desc} [dim](현재: {current[key]})\n[dim]{help_text}[/dim]", default=str(current[key]))
        if val.lower() in ['b', 'q']: raise QuitInput()
        return type_func(val)

    try:
        console.print("\n[bold]1. 기본 매수 타점 설정[/bold]")
        new_strategy['buy_score'] = ask_val('buy_score', "매수 기준 점수 (기본: 7.5점)", "이 점수 이상일 때 매수 진입 (지표 종합 점수)", float)
        new_strategy['buy_rsi'] = ask_val('buy_rsi', "매수 허용 RSI 상한 (기본: 65)", "RSI가 이 값보다 낮아야 매수 (과열 방지)", float)
        new_strategy['buy_vol_strength'] = ask_val('buy_vol_strength', "매수 체결강도 기준(%) (기본: 100.0, 0: 미사용)", "수급 확인 (이 값 이상이어야 매수)", float)
        
        curr_auto = "y" if current.get('auto_adjust_ask_bid_ratio', defaults['auto_adjust_ask_bid_ratio']) else "n"
        val_auto = Prompt.ask(f"매도잔량비 자동 연동 (y: 사용 / n: 미사용) [dim](현재: {curr_auto})\n[dim]체결강도 설정값에 비례하여 최저 1.0배 자동 조정[/dim]", choices=["y", "n", "b", "q"], default=curr_auto)
        if val_auto.lower() in ['b', 'q']: raise QuitInput()
        new_strategy['auto_adjust_ask_bid_ratio'] = 1 if val_auto.lower() == 'y' else 0
        
        default_ratio = config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0)
        new_strategy['buy_ask_bid_ratio'] = ask_val('buy_ask_bid_ratio', f"매도잔량 비대칭성 기준 (기본: {default_ratio}배, 0: 미사용)", "가짜 체결강도 방어 (체결강도 100% 기준 매도/매수잔량 비율)", float)

        console.print("\n[bold]2. 기본 청산 타점 설정[/bold]")
        new_strategy['take_profit'] = ask_val('take_profit', "익절 수익률(%) (기본: 30.0%)", "수익이 이 비율에 도달하면 이익 실현 (0: 미사용)", float)
        
        curr_half_tp = "y" if current.get('half_take_profit_use', defaults['half_take_profit_use']) else "n"
        val = Prompt.ask(f"반익절 사용 (y: 사용 / n: 미사용) [dim](현재: {curr_half_tp})\n[dim]목표 익절 수익률의 절반 도달 시 50% 선매도[/dim]", choices=["y", "n", "b", "q"], default=curr_half_tp)
        if val.lower() in ['b', 'q']: raise QuitInput()
        new_strategy['half_take_profit_use'] = 1 if val.lower() == 'y' else 0
        
        new_strategy['take_profit_rsi'] = ask_val('take_profit_rsi', "익절 RSI 기준 (기본: 75)", "RSI가 이 값을 초과하면 과열로 판단하여 매도", float)
        new_strategy['sell_score'] = ask_val('sell_score', "매도(추세이탈) 기준 점수 (기본: 5.0점)", "점수가 이 값 미만으로 떨어지면 매도", float)
        new_strategy['ts_activation'] = ask_val('ts_activation', "트레일링 스탑 발동 수익률(%) (기본: 15.0%)", "수익률이 이 값 이상일 때 트레일링 스탑 감시 시작", float)
        new_strategy['ts_callback'] = ask_val('ts_callback', "트레일링 스탑 하락 감지율(%) (기본: 4.0%)", "최고가 대비 이 비율만큼 하락 시 매도", float)
        new_strategy['time_stop_days'] = ask_val('time_stop_days', "시간 청산 기한(일) (기본: 10일)", "매수 후 목표 기간 내 수익 미달 시 강제 청산 (0: 미사용)", int)
            
        console.print("\n[bold]3. 리스크 관리 및 자산 비중 설정[/bold]")
        curr_ratio_pct = current.get('invest_ratio', getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.2)) * 100
        val = Prompt.ask(f"종목별 투자 비중(%) [dim](현재: {curr_ratio_pct:.0f})\n[dim]전체 자산 대비 이 종목에 투자할 비중 한도[/dim]", default=str(int(curr_ratio_pct)))
        if val.lower() in ['b', 'q']: raise QuitInput()
        new_strategy['invest_ratio'] = float(val) / 100.0
        
        curr_use_atr = "y" if current.get('use_atr_stop', 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0) else "n"
        val = Prompt.ask(f"손절 방식 (y: ATR 동적 손절 / n: 고정 손절률) [dim](현재: {curr_use_atr})\n[dim]종목의 변동성에 비례하여 손절폭 자동 계산 여부[/dim]", choices=["y", "n", "b", "q"], default=curr_use_atr)
        if val.lower() in ['b', 'q']: raise QuitInput()
        use_atr = (val.lower() == 'y')
        new_strategy['use_atr_stop'] = 1 if use_atr else 0
        
        if use_atr:
            new_strategy['atr_stop_multiplier'] = ask_val('atr_stop_multiplier', "ATR 손절 배수 (기본: 2.0)", "ATR 값의 몇 배를 손절폭으로 할지 설정 (0: 미사용)", float)
            new_strategy['stop_loss'] = current.get('stop_loss', defaults['stop_loss']) # 고정 손절률은 숨김
        else:
            new_strategy['stop_loss'] = ask_val('stop_loss', "손절 수익률(%) (기본: -7.0%)", "손실이 이 비율에 도달하면 손절매 (0: 미사용)", float)
            new_strategy['atr_stop_multiplier'] = current.get('atr_stop_multiplier', defaults['atr_stop_multiplier']) # 배수는 숨김
        
        # [추가] 가중치 설정 입력
        console.print("\n[bold]4. 스코어링 가중치 설정[/bold]")
        curr_weights = current.get('weights')
        
        while True:
            # 현재 설정값 또는 전역 설정값 로드
            temp_weights = curr_weights.copy() if curr_weights else config.SCORING_WEIGHTS.copy()
            
            console.print("[dim]순서: 추세 / 모멘텀 / 강도 / 시너지 (합계 10.0점 설정)[/dim]")
            console.print()
            
            def ask_weight(key, desc, default_val):
                v = Prompt.ask(f"{desc} [dim](현재: {default_val})[/dim]", default=str(default_val))
                if v.lower() in ['b', 'q']: raise QuitInput()
                return float(v)

            try:
                w_trend = ask_weight("TREND", "추세 (TREND)", temp_weights.get('TREND', 4.0))
                w_mom = ask_weight("MOMENTUM", "모멘텀 (MOMENTUM)", temp_weights.get('MOMENTUM', 2.5))
                w_str = ask_weight("STRENGTH", "강도 (STRENGTH)", temp_weights.get('STRENGTH', 1.5))
                w_syn = ask_weight("SYNERGY", "시너지 (SYNERGY)", temp_weights.get('SYNERGY', 2.0))
            except ValueError:
                console.print("[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")
                continue

            total_score = w_trend + w_mom + w_str + w_syn
            
            if abs(total_score - 10.0) > 0.01:
                console.print(f"\n[bold red]경고: 가중치 합계가 {total_score:.1f}점입니다. (합계 10.0점)[/bold red]")
                console.print("[yellow]합계가 10점이 되도록 다시 입력해주세요.[/yellow]")
                # 입력한 값으로 임시 가중치 업데이트하여 재입력 시 보여줌
                curr_weights = {"TREND": w_trend, "MOMENTUM": w_mom, "STRENGTH": w_str, "SYNERGY": w_syn}
                continue
            
            new_strategy['weights'] = {"TREND": w_trend, "MOMENTUM": w_mom, "STRENGTH": w_str, "SYNERGY": w_syn}
            break

        # [추가] 메모 입력
        new_strategy['memo'] = ask_val('memo', "메모 (Memo)", "종목에 대한 투자 아이디어 및 참고사항", str)
        
        # [추가] 기본값과 동일 여부 확인
        if new_strategy == defaults:
            console.print(f"\n[yellow]입력된 설정이 시스템 기본값과 동일합니다.[/yellow]")
            if existing:
                console.print("[dim]변경된 내용이 없어 저장하지 않았습니다. (기존 룰 유지)[/dim]")
                console.print("[dim]기본값을 적용하려면 '삭제' 기능을 이용해주세요.[/dim]")
            else:
                console.print("[dim]별도의 개별 룰로 저장하지 않습니다. (기본 설정 자동 적용)[/dim]")
            return

        db_manager.db.save_stock_strategy(code, name, new_strategy)
        # [추가] 가중치 정보 별도 저장 (DB 스키마 보정)
        _save_rule_weights(code, new_strategy.get('weights'))

        console.print(f"\n[bold green]'{name}' 종목의 트레이딩 룰이 저장되었습니다.[/bold green]")
        
        console.print()
        table = Table(title=f"[{name}] 설정 결과 요약", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("구분", justify="center", style="cyan")
        table.add_column("설정값", justify="left")
        
        auto_str = "ON" if new_strategy.get('auto_adjust_ask_bid_ratio', 1) else "OFF"
        table.add_row("매수 타점", f"점수 {new_strategy['buy_score']}점↑ / RSI {new_strategy['buy_rsi']}↓ / 체결 {new_strategy['buy_vol_strength']}%↑ / 비대칭 {new_strategy['buy_ask_bid_ratio']}배↑ (자동연동: {auto_str})")
        half_tp_str = "ON" if new_strategy['half_take_profit_use'] else "OFF"
        table.add_row("청산 타점", f"익절 +{new_strategy['take_profit']}% (반익절: {half_tp_str}) / 과열 RSI {new_strategy['take_profit_rsi']}↑ / 시간청산 {new_strategy['time_stop_days']}일")
        table.add_row("트레일링", f"+{new_strategy['ts_activation']}% 도달 후 -{new_strategy['ts_callback']}% 하락 시")
        
        if new_strategy['use_atr_stop']:
            sl_disp = f"ATR 동적 손절 (배수: x{new_strategy['atr_stop_multiplier']})"
        else:
            sl_disp = f"고정 손절 ({new_strategy['stop_loss']}%)"
        
        table.add_row("리스크 관리", f"투자 비중 {new_strategy['invest_ratio']*100:.0f}% / {sl_disp}")
        
        w_disp = "기본 설정"
        if new_strategy.get('weights'):
            w = new_strategy['weights']
            w_disp = f"{w['TREND']:.1f}/{w['MOMENTUM']:.1f}/{w['STRENGTH']:.1f}/{w['SYNERGY']:.1f}"
        table.add_row("가중치", w_disp)
        table.add_row("메모", new_strategy['memo'])
        
        console.print(table)
        
    except QuitInput:
        console.print("\n[yellow]입력이 취소되었습니다.[/yellow]")
        return
    except ValueError:
        console.print("\n[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")

def _set_stock_rules():
    """룰 설정 (신규/검색)"""
    code, name, _ = _select_stock_for_rules()
    if not code: return False
    if _input_and_save_rule(code, name) is False: return False

def _modify_stock_rules():
    """룰 변경 (기존 목록에서 선택)"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
    if not custom_rules:
        console.print("\n[yellow]저장된 개별 룰이 없습니다.[/yellow]")
        return

    def _disp_func(i, r):
        sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
        return f"[{i+1}] {r['name']} ({r['code']}) | 매수: {r['buy_score']}점 | 익절: +{r['take_profit']}% | 손절: {sl_str}"
        
    idx, target = utils.search_stock_in_list(custom_rules, title="변경할 룰을 선택하세요", display_func=_disp_func)
    if target:
        context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {target['name']}")
        if _input_and_save_rule(target['code'], target['name']) is False: return False
    else: return False

def _delete_stock_rules():
    """룰 삭제"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    if not custom_rules:
        console.print("\n[yellow]삭제할 룰이 없습니다.[/yellow]")
        return

    idx, target = utils.search_stock_in_list(custom_rules, title="삭제할 룰을 선택하세요")
    if target:
        context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {target['name']}")
        utils.print_breadcrumb()
        if Prompt.ask(f"정말 '{target['name']}'의 룰을 삭제하시겠습니까?", choices=["y", "n"], default="n") == "y":
            db_manager.db.delete_stock_strategy(target['code'])
            console.print(f"\n[bold green]삭제되었습니다.[/bold green]")
            
            console.print("\n[bold cyan]>> 현재 설정된 트레이딩 룰 리스트입니다.[/bold cyan]")
            _view_stock_rules()
        else: return False
    else: return False

def _view_restricted_stocks():
    """트레이딩 제한 종목 목록 및 후행지표 조회"""
    data = load_restricted_stocks()
    if not data:
        console.print("\n[yellow]트레이딩 제한 종목이 없습니다.[/yellow]")
        return

    console.print()
    title = "트레이딩 제한 종목"
    table = Table(title=title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목명", justify="left")
    table.add_column("코드", justify="center", style="dim")
    table.add_column("현재가", justify="right")
    table.add_column("등락률(등락폭)", justify="right")
    table.add_column("52주", justify="right")
    table.add_column("메모", justify="left")
    table.add_column("등록일", justify="center", style="dim")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]데이터 조회 및 지표 계산 중...[/cyan]", total=len(data))

        for code, info in data.items():
            name = info.get('name', code)
            memo = info.get('memo', '')
            reg_date = info.get('date', '-')
            
            # 데이터 조회 및 지표 계산
            is_overseas = info.get('is_overseas')
            if is_overseas is None:
                is_overseas = (len(code) != 6)
            df = api.get_chart_data(code, is_overseas)
            
            price_str = "-"
            diff_str = "-"
            w52_str = "-"
            
            if df is not None and not df.empty:
                current_price = float(df.iloc[-1]['close'])
                prev_price = float(df.iloc[-2]['close']) if len(df) > 1 else current_price
                diff = current_price - prev_price
                rate = (diff / prev_price) * 100 if prev_price > 0 else 0.0
                
                price_str = f"{int(current_price):,}" if not is_overseas else f"{current_price:,.2f}"
                
                c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                if is_overseas:
                    diff_str = f"{c_color}{rate:+.2f}% ({diff:+.2f})[/]"
                else:
                    diff_str = f"{c_color}{rate:+.2f}% ({int(diff):+})[/]"

                w52_pos_val = 0.0
                recent_df = df.tail(250)
                h52 = recent_df['high'].max()
                l52 = recent_df['low'].min()
                if h52 > l52:
                    w52_pos_val = (current_price - l52) / (h52 - l52) * 100
                
                w_color = "[white]"
                if w52_pos_val >= 90: w_color = "[red]"
                elif w52_pos_val >= 80: w_color = "[orange3]"
                elif w52_pos_val <= 20: w_color = "[blue]"
                w52_str = f"{w_color}{w52_pos_val:.1f}%[/]"
            
            table.add_row(name, code, price_str, diff_str, w52_str, memo, reg_date)
            progress.advance(task)

    console.print(table)

def _add_restricted_stock():
    """트레이딩 제한 종목 추가"""
    code, name, is_overseas = _select_stock_for_rules()
    if not code: return False
    
    data = load_restricted_stocks()
    if code in data:
        console.print(f"\n[yellow]이미 제한 목록에 있는 종목입니다.[/yellow]")
        utils.print_breadcrumb()
        if Prompt.ask("메모를 수정하시겠습니까?", choices=["y", "n"], default="y") == "n":
            return False
            
    utils.print_breadcrumb()
    memo = Prompt.ask("제한 사유(메모) 입력")
    
    data[code] = {
        "name": name,
        "memo": memo,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_overseas": is_overseas
    }
    save_restricted_stocks(data)
    console.print(f"\n[green]'{name}' 종목이 트레이딩 제한 목록에 추가되었습니다.[/green]")
    
    console.print("\n[bold cyan]>> 현재 설정된 트레이딩 제한 종목 리스트입니다.[/bold cyan]")
    _view_restricted_stocks()

def _remove_restricted_stock():
    """트레이딩 제한 종목 해제"""
    data = load_restricted_stocks()
    if not data:
        console.print("\n[yellow]삭제할 종목이 없습니다.[/yellow]")
        return False

    console.print()
    table = Table(title="트레이딩 제한 해제 대상 목록", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("No.", justify="right", style="cyan", width=4)
    table.add_column("종목명", justify="left")
    table.add_column("코드", justify="center", style="dim")
    table.add_column("현재가", justify="right")
    table.add_column("등락률(등락폭)", justify="right")
    table.add_column("52주", justify="right")
    table.add_column("메모", justify="left")
    table.add_column("등록일", justify="center", style="dim")

    codes = list(data.keys())
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]데이터 조회 및 지표 계산 중...[/cyan]", total=len(codes))

        for i, code in enumerate(codes):
            info = data[code]
            name = info.get('name', code)
            memo = info.get('memo', '')
            reg_date = info.get('date', '-')
            
            # 데이터 조회 및 지표 계산
            is_overseas = info.get('is_overseas')
            if is_overseas is None:
                is_overseas = (len(code) != 6)
            df = api.get_chart_data(code, is_overseas)
            
            price_str = "-"
            diff_str = "-"
            w52_str = "-"
            
            if df is not None and not df.empty:
                current_price = float(df.iloc[-1]['close'])
                prev_price = float(df.iloc[-2]['close']) if len(df) > 1 else current_price
                diff = current_price - prev_price
                rate = (diff / prev_price) * 100 if prev_price > 0 else 0.0
                
                price_str = f"{int(current_price):,}" if not is_overseas else f"{current_price:,.2f}"
                
                c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                if is_overseas:
                    diff_str = f"{c_color}{rate:+.2f}% ({diff:+.2f})[/]"
                else:
                    diff_str = f"{c_color}{rate:+.2f}% ({int(diff):+})[/]"

                w52_pos_val = 0.0
                recent_df = df.tail(250)
                h52 = recent_df['high'].max()
                l52 = recent_df['low'].min()
                if h52 > l52:
                    w52_pos_val = (current_price - l52) / (h52 - l52) * 100
                
                w_color = "[white]"
                if w52_pos_val >= 90: w_color = "[red]"
                elif w52_pos_val >= 80: w_color = "[orange3]"
                elif w52_pos_val <= 20: w_color = "[blue]"
                w52_str = f"{w_color}{w52_pos_val:.1f}%[/]"
            
            table.add_row(str(i+1), name, code, price_str, diff_str, w52_str, memo, reg_date)
            progress.advance(task)
        
    console.print(table)
    console.print()
    
    utils.print_breadcrumb()
    choice = Prompt.ask("해제할 번호 선택 [dim](이전: b, 메인: q)[/dim]")
    if choice.lower() in ['b', 'q']: return False
    
    if choice.isdigit() and 1 <= int(choice) <= len(codes):
        target_code = codes[int(choice)-1]
        target_name = data[target_code]['name']
        context.USER_ACTION_BREADCRUMB.append(f"[해제] {target_name}")
        del data[target_code]
        save_restricted_stocks(data)
        console.print(f"\n[green]'{target_name}' 종목이 제한 목록에서 해제되었습니다.[/green]")
        
        console.print("\n[bold cyan]>> 현재 설정된 트레이딩 제한 종목 리스트입니다.[/bold cyan]")
        _view_restricted_stocks()

def manage_stock_rules():
    """종목별 트레이딩 룰 관리 메뉴"""
    menu_items = [("1", "룰 조회", "View"), ("2", "룰 설정", "Set"), ("3", "룰 변경", "Modify"), ("4", "룰 삭제", "Delete")]
    choice = utils.show_menu("종목별 트레이딩 룰 관리 (Manage Stock Rules)", menu_items, default_choice="1")
    if choice.lower() in ['b', 'q']: return False
    
    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")

    if choice == "1":
        _view_stock_rules()
    elif choice == "2":
        if _set_stock_rules() is False: return False
    elif choice == "3":
        if _modify_stock_rules() is False: return False
    elif choice == "4":
        if _delete_stock_rules() is False: return False

def manage_restricted_stocks_menu():
    """트레이딩 제한 종목 관리 메뉴"""
    menu_items = [("1", "제한 종목 조회", "List"), ("2", "제한 종목 추가", "Add"), ("3", "제한 종목 해제", "Remove")]
    choice = utils.show_menu("트레이딩 제한 종목 관리 (Restricted Stocks)", menu_items, default_choice="1")
    if choice.lower() in ['b', 'q']: return False
    
    menu_map_dict = dict((k, v) for k, v, _ in menu_items)
    context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map_dict.get(choice, '')}")
    
    if choice == "1": 
        _view_restricted_stocks()
    elif choice == "2": 
        if _add_restricted_stock() is False: return False
    elif choice == "3": 
        if _remove_restricted_stock() is False: return False

def system_trading_menu():
    """시스템 트레이딩 메뉴"""

    trader = AutoTrader()

    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "3"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        
        trader_status = ""
        if trader.is_running:
            if trader.is_market_open():
                trader_status = " [bold green](RUNNING"
            else:
                trader_status = " [bold yellow](WAITING"
        else:
            trader_status = " [bold red](STOPPED"
            
        menu_items = [
            ("1", "트레이딩 실행", "Start"), ("2", "트레이딩 중단", "Stop"), ("3", "트레이딩 상태", f"Status){trader_status}"),
            ("4", "트레이딩 평가", "Report"), ("5", "트레이딩 로그", "Log Viewer"),
            ("6", "종목별 트레이딩 룰", "Rule"), ("7", "트레이딩 제한 종목", "Restrict")
        ]
        
        try:
            choice = utils.show_menu("시스템 트레이딩 (System Trading)", menu_items, default_choice=last_choice)
            
            if choice.lower() in ['b', 'q']: return False
            if choice.lower() == 'h':
                if getattr(utils, 'show_help', None):
                    utils.show_help()
                    utils.pause()
                continue
            
            last_choice = choice
            
            menu_map = dict((k, v) for k, v, _ in menu_items)
            if choice in menu_map:
                context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
                
        except KeyboardInterrupt:
            console.print()
            return False

        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
        
        if choice == "1":
            trader.start()
            utils.pause()
        elif choice == "2":
            trader.stop()
            utils.pause()
        elif choice == "3":
            trader.print_status()
            utils.pause()
        elif choice == "4":
            if trader.print_report() is not False: utils.pause()
        elif choice == "5":
            trader.view_log_file()
        elif choice == "6":
            if manage_stock_rules() is not False: utils.pause()
        elif choice == "7":
            if manage_restricted_stocks_menu() is not False: utils.pause()