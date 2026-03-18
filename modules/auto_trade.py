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

console = config.console

logger = logging.getLogger(__name__)

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

def load_daily_initial_asset():
    today_str = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(DAILY_STATE_FILE):
        try:
            with open(DAILY_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get("date") == today_str and data.get("initial_asset", 0) > 0:
                    return data["initial_asset"]
        except: pass
    return 0

def save_daily_initial_asset(asset_value):
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(DAILY_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"date": today_str, "initial_asset": asset_value}, f)
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
    try:
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
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

def _save_rule_weights_logic(code, weights):
    """가중치 정보를 DB에 직접 저장 (JSON 직렬화)"""
    try:
        _ensure_db_weights_column()
        weights_json = json.dumps(weights) if weights else None
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE stock_strategies SET weights = ? WHERE code = ?", (weights_json, code))
            conn.commit()
    except Exception as e:
        logger.error(f"가중치 저장 실패: {e}")

def _enrich_rules_with_weights_logic(rules):
    """DB에서 weights 컬럼을 조회하여 룰 리스트에 병합"""
    if not rules: return rules
    try:
        _ensure_db_weights_column()
        with sqlite3.connect(config.DB_FILE_PATH) as conn:
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
        if self.thread:
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
        now = datetime.now()
        if now.weekday() > 4: return False # 주말
        current_time = now.strftime("%H%M")
        start_time = getattr(config, 'SYSTEM_TRADING_START_TIME', "0915")
        end_time = getattr(config, 'SYSTEM_TRADING_END_TIME', "1515")
        return start_time <= current_time <= end_time

    def _run_loop(self):
        self.was_active_mode = False
        
        # [수정] 프로그램 시작 직후 API 요청 집중 방지를 위한 초기 지연 (설정값의 3배)
        initial_delay = getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5) * 3
        time.sleep(initial_delay)
        
        while self.is_running:
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
            if not self._is_market_open() and not has_pending_orders:
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
                                    if origin_trade:
                                        db_type_name = origin_trade['type']
                                        profit_amt = origin_trade.get('profit_amt', 0)
                                        profit_rate = origin_trade.get('profit_rate', 0.0)
                                        score = origin_trade.get('strategy_score', 0)
                                        stop_loss_rate = float(origin_trade.get('stop_loss_rate', 0.0))
                                except Exception:
                                    db_type_name = type_name
                                    stop_loss_rate = 0.0
                                
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
                                            is_overseas_stock = not (code.isdigit() and len(code) == 6)
                                            df = api.get_chart_data(code, is_overseas=is_overseas_stock)
                                            if df is not None and not df.empty:
                                                ind = indicators.calculate_indicators(df)
                                                current_price = float(df.iloc[-1]['close'])
                                                
                                                # [추가] prev_rsi 계산 (상태 분류용)
                                                delta = df['close'].diff()
                                                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                                                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                                                prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None

                                                thresholds = None
                                                rule_tag = ""
                                                if rule:
                                                    thresholds = {
                                                        "BUY_SCORE": rule['buy_score'],
                                                        "BUY_RSI_MAX": rule['buy_rsi'],
                                                        "BUY_VOL_STRENGTH": rule.get('buy_vol_strength') # [추가] 개별 체결강도
                                                    }
                                                    rule_tag = " [개별 룰 적용]"

                                                # [수정] 상태 및 사유 조회 (thresholds 적용)
                                                state, _, state_reason = analysis.classify_stock_state(
                                                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                                                    ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'),
                                                    thresholds=thresholds
                                                )

                                                score, _ = analysis.calculate_score(
                                                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                                                    ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
                                                )
                                                
                                                rsi_str = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
                                                adx_str = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
                                                cci_str = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
                                                
                                                strategy_info = f"\n\n📊 [전략 지표]{rule_tag}\n• 점수: {score}점 ({state})\n• 상태: {state_reason}\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                        except Exception as e:
                                            logger.error(f"체결 지표 계산 중 오류: {e}")

                                    # 알림 발송
                                    title_tag = "[체결 알림]"
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

                                    msg = f"✅ {title_tag} {type_name} {name}({code})\n수량: {new_qty}주 / 단가: {price_str} / 금액: {amt_str}{profit_msg}{reason_msg}{cur_info}{strategy_info}{rule_info}"
                                    with utils.AccountContext(cano):
                                        api.send_telegram_message(msg)
                                    
                                    # 로그 기록 (시스템 로거 활용)
                                    if context.SYSTEM_LOGGER:
                                        context.SYSTEM_LOGGER(f"[체결 확인] {type_name} {name}({code}) {new_qty}주 (단가: {price_str})")
                                    
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
                                    
                                    db_manager.db.insert_trade(db_type_name, code, name, tot_ccld_qty, avg_price, odno, order_status="체결", reason="체결 확인", custom_time=trade_time_str, profit_amt=profit_amt, profit_rate=profit_rate, score=score, stop_loss_rate=stop_loss_rate)
                                    
                                    # [추가] 시장가 주문 등의 경우를 위해 원 주문(접수)의 단가도 체결가로 업데이트
                                    # [수정] 전량 체결 시 상태를 '체결'로 업데이트하여 미체결 목록(DB Fallback)에서 제거
                                    update_params = {'price': avg_price}
                                    if ord_qty > 0 and ccld_qty >= ord_qty:
                                        update_params['order_status'] = "체결"
                                    
                                    db_manager.db.update_trade(odno, **update_params)
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
                                reason = "잔고 입고 확인 (API 누락 보정)"
                        
                        # 매도 주문: 잔고가 0이면 체결로 간주 (전량 매도 가정)
                        elif "sell" in type_str.lower() or "매도" in type_str:
                            current_qty = holdings_map.get(code, 0)
                            if current_qty == 0:
                                is_filled = True
                                reason = "잔고 0 확인 (API 누락 보정)"
                        
                        if is_filled:
                            logger.debug(f"[ORDER_DEBUG] 모의투자 잔고 기반 체결 감지: {code} (No.{odno})")
                            self._handle_simulation_fill(trader, trade, odno, code, qty, reason)
                            
        except Exception as e:
            logger.error(f"[Monitor] 모의투자 잔고 기반 체결 확인 중 오류: {e}")

    def _handle_simulation_fill(self, trader, trade, odno, code, qty, reason):
        """모의투자 체결 처리 핸들러"""
        try:
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ORDER_DEBUG] _handle_simulation_fill 진입: {odno} / Code: {code} / Qty: {qty}")
                logger.debug(f"[ORDER_DEBUG] Trade Info: {trade}")

            name = trade.get('name', code)
            price = float(trade.get('price', 0))
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
            
            # 1. DB 업데이트 (원본 주문 상태 변경) -> [수정] 원본 유지 (접수 이력 보존)
            # db_manager.db.update_trade(odno, order_status="체결(추정)")
            
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
                    reason=f"체결 확인 ({reason})", 
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
                                strategy_info = f"\n\n📊 [전략 지표(진입시점)]\n• 점수: {score}점\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                        except: pass

                    exec_amt = int(price * qty)
                    msg = f"✅ {title_tag} {type_name} {name}({code})\n수량: {qty}주 / 단가: {price:,.0f}원(주문가) / 금액: {exec_amt:,}원\n사유: {reason}{cur_info}{strategy_info}{rule_info}"
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

class DefaultStrategy:
    """기본 매매 전략 클래스 (매수/매도 판단 로직 분리)"""
    def __init__(self):
        self.trailing_stop_cache = {}

    def analyze_buy(self, code, name, df, current_price, vol_strength=None, thresholds=None):
        """매수 진입 여부 판단"""
        if df is None or df.empty:
            return None

        ind = indicators.calculate_indicators(df)
        # 전일 RSI 계산 (상태 분류용)
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
        loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
        prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None

        state, _, state_reason = analysis.classify_stock_state(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'), thresholds=thresholds
        )
        
        # [수정] 가중치(WEIGHTS) 전달
        score, _ = analysis.calculate_score(current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'), weights=thresholds.get('WEIGHTS') if thresholds else None)
        
        # [추가] 체결강도 조건 체크
        min_vol = thresholds.get("BUY_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]) if thresholds else config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]
        
        # [수정] 체결강도 미달 시 None 반환 대신 action을 wait로 처리하여 로그 출력 보장
        is_vol_ok = True
        if vol_strength is not None and vol_strength < min_vol:
            is_vol_ok = False

        return {
            'action': 'buy' if (state == "매수" and is_vol_ok) else 'wait',
            'state': state,
            'score': score,
            'rsi': ind['rsi'],
            'adx': ind['adx'],
            'cci': ind['cci'],
            'atr': ind.get('atr', 0), # [추가] ATR
            'psar': ind['psar'],
            'macd': ind.get('macd'),
            'macd_signal': ind.get('macd_signal'),
            'obv_trend': ind.get('obv_trend'),
            'vol_strength': vol_strength
        }

    def analyze_sell(self, code, name, df, current_price, buy_price, profit_rate, ts_msg="", thresholds=None, already_half_sold=False, holding_days=0, is_mr_holding=False):
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
        use_half_tp = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
        half_tp_rate = tp_rate / 2.0
        
        # [추가] 시간 청산 설정 로드
        use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
        time_stop_days = thresholds.get("TIME_STOP_DAYS", config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)) if thresholds else config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
        time_stop_min_profit = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)

        # 1. 기술적 지표 분석 (시간 청산 시 매수 상태 확인을 위해 우선 수행)
        if df is not None and not df.empty:
            ind = indicators.calculate_indicators(df)
            # 전일 RSI 계산 (상태 분류용)
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2] if len(df) >= 16 else None

            state, _, state_reason = analysis.classify_stock_state(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'), thresholds=thresholds
            )
            
            # [수정] 가중치(WEIGHTS) 전달
            score, _ = analysis.calculate_score(current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'), weights=thresholds.get('WEIGHTS') if thresholds else None)

        # 2. 고정 익절/손절 및 시간 청산
        if profit_rate >= tp_rate:
            reason = f"익절({profit_rate}%)"
        elif use_half_tp and not already_half_sold and profit_rate >= half_tp_rate:
            reason = f"반익절({profit_rate:.1f}%)"
            sell_ratio = 0.5
        elif profit_rate <= sl_rate:
            reason = f"손절({profit_rate}%)"
        # [수정] 시간 청산 (현재 매수 상태인 경우 청산 보류)
        elif use_time_stop and holding_days >= time_stop_days and profit_rate < time_stop_min_profit:
            if state in ["매수", "역추세매수", "상승"]:
                pass # 상승 또는 매수 신호가 유지 중이면 시간 청산 유예
            else:
                reason = f"시간청산({holding_days}일경과, 기대수익미달)"
        # 3. 트레일링 스탑 (외부에서 계산된 메시지 반영)
        elif ts_msg:
            reason = ts_msg
        
        # 4. RSI 과열 익절
            if not reason and ind.get('rsi') is not None and ind['rsi'] > tp_rsi:
                reason = f"RSI 과열 익절 (RSI: {ind['rsi']:.1f})"
            
            # 5. 추세 이탈
            if not reason and (state == "매도" or score < sell_score_limit):
                rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
                adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
                cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
                if state == "매도":
                    reason = f"매도진입({state_reason}) [점수:{score}, RSI:{rsi_val}]"
                else:
                    # [추가] 역추세 매수 종목은 유예 기간(TIME_STOP_DAYS) 및 허용 손실률(-5%) 내에서는 추세 이탈로 손절하지 않음
                    if is_mr_holding and holding_days <= time_stop_days and profit_rate > -5.0:
                        pass # 유예 기간 적용
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
        
        self.trader.log(f"======== [주문 실행] {type_str.upper()} ========")
        price_log = f"{price:,}원(지정가)" if price > 0 else "시장가(0)"
        self.trader.log(f"대상: {code}, 수량: {qty}, 단가: {price_log}")

        try:
            res_json = api.place_order("domestic", type_str, code, qty, price, ord_dvsn)
            
            if res_json['rt_cd'] == '0':
                odno = res_json['output']['ODNO']
                success_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {type_str.upper()} 성공 | {code} | {qty}주 | No.{odno}"
                
                with self._lock:
                    if code not in self.pending_orders:
                        self.pending_orders[code] = {}
                    self.pending_orders[code][odno] = OrderStatus.ORDER_SENT

                self.trader.trade_history.append(success_msg)
                self.trader.log(f"결과: 성공 (주문번호: {odno})")
                stock_display = f"{name}({code})" if name else code
                
                title_tag = "[주문 접수]"
                if rule:
                    title_tag += " [개별]"
                
                msg = f"🚀 {title_tag} {type_str.upper()} {stock_display} {qty}주 ({price_log})"
                if price > 0:
                    msg += f"\n금액: {int(price * qty):,}원"
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
                err_msg = res_json.get('msg1', 'Unknown Error')
                msg_cd = res_json.get('msg_cd')
                self.trader.log(f"결과: 실패 ({err_msg}) [Code: {msg_cd}]")
                
                stock_display = f"{name}({code})" if name else code
                fail_msg = f"🚫 [주문 실패] {type_str.upper()} {stock_display}\n수량: {qty}주 / 단가: {price_log}\n원인: {err_msg} (Code: {msg_cd})"
                api.send_telegram_message(fail_msg)
                
                if res_json.get('rt_cd') == '9999' or msg_cd in ['OPSQ2000', 'EGW00201']:
                    raise Exception(f"주문 시스템 치명적 오류: {err_msg}")

        except Exception as e:
            self.trader.log(f"결과: 에러 발생 ({str(e)})")
            stock_display = f"{name}({code})" if name else code
            fail_msg = f"🚫 [주문 에러] {type_str.upper()} {stock_display}\n수량: {qty}주 / 단가: {price_log}\n에러: {str(e)}"
            api.send_telegram_message(fail_msg)
            raise e
        finally:
            self.trader.log("========================================")
        return None

    def manage_unfilled_orders(self):
        """오래된 미체결 주문 확인 및 취소"""
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
                                api.send_telegram_message(f"🗑 [주문 취소] {name} {qty}주\n사유: 미체결 시간 초과 ({int(elapsed)}초)")
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
                                                api.send_telegram_message(f"🗑 [주문 취소] {trade['name']} {qty}주\n사유: 미체결 시간 초과 (API 누락 보정)")
                                                
                                                # [수정] 원본 업데이트 제거 -> 취소 히스토리 생성
                                                # db_manager.db.update_trade(odno, order_status="취소")
                                                
                                                # 취소 주문 번호는 API 응답(res)에서 파싱해야 하나, revise_cancel_order는 현재 json을 반환함
                                                cancel_odno = res.get('output', {}).get('ODNO') or res.get('output', {}).get('KRX_FWDG_ORD_ORGNO') or f"CANCEL_{odno}"
                                                
                                                db_manager.db.insert_trade("취소(자동)", code, trade['name'], qty, 0, cancel_odno, org_odno=odno, reason="미체결 시간 초과 (자동 취소)")
                                                
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
                                                    
                                                    if is_filled:
                                                        self.trader.log(f"-> 잔고 확인됨. '체결(추정)'으로 기록합니다.")
                                                        # [수정] 원본 주문 상태 변경 제거 (접수 이력 보존)
                                                        # db_manager.db.update_trade(odno, order_status="체결(추정)")
                                                        # 체결 내역 강제 생성 (히스토리 보정)
                                                        db_manager.db.insert_trade(trade['type'], code, trade['name'], qty, float(trade['price']), odno, order_status="체결(추정)", reason="체결 확인(API누락보정)", custom_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                                                        
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
                                                                        strategy_info = f"\n\n📊 [전략 지표(진입시점)]\n• 점수: {score}점\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                                                except: pass
                                                            
                                                            exec_amt = int(float(trade['price']) * qty)
                                                            msg = f"✅ {title_tag} {type_name} {trade['name']}({code})\n수량: {qty}주 / 단가: {float(trade['price']):,.0f}원(주문가) / 금액: {exec_amt:,}원\n사유: API 누락 보정 (잔고 확인됨){cur_info}{strategy_info}{rule_info}"
                                                            api.send_telegram_message(msg)
                                                        except Exception as e:
                                                            self.trader.log(f"알림 전송 실패: {e}")
                                                    else:
                                                        self.trader.log(f"-> 잔고 없음. '취소/거부'로 기록합니다.")
                                                        # [수정] 원본 업데이트 제거 -> 취소/거부 히스토리 생성
                                                        # db_manager.db.update_trade(odno, order_status="취소/거부")
                                                        db_manager.db.insert_trade("취소/거부(자동)", code, trade['name'], qty, 0, f"REJECT_{odno}", org_odno=odno, reason="체결 확인 실패 (잔고 없음)")

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
            scale_min = getattr(config, 'VOLATILITY_SCALING_MIN', 0.3)
            
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
            
            msg = f"🛑 [비상 정지] 일일 손실 한도 초과\n\n수익률: {loss_rate:.2f}% (제한: -{loss_limit_pct}%)\n현재 자산: {current_total:,}원\n\n자산 보호를 위해 시스템을 정지합니다."
            
            api.send_telegram_message(msg)
            self.trader.stop()

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
            cls._instance.skipped_by_market_filter_count = 0 # [추가] 시장 필터링 보류 종목 수
            cls._instance.strategy = DefaultStrategy() # [추가] 전략 인스턴스
            cls._instance.last_log_date = datetime.now().date() # [추가] 로그 파일 날짜 추적용
            cls._instance.initial_holdings = None # [추가] 초기 조회 잔고 캐시
            cls._instance.initial_summary = None  # [추가] 초기 조회 요약 캐시
            cls._instance.file_logger = config.get_autotrade_logger() # [추가] 파일 로거 초기화
            cls._instance.restricted_notified = {} # [추가] 거래 제한 알림 스로틀링 (종목별 타임스탬프)
            cls._instance.order_manager = OrderManager(cls._instance) # [추가] 주문 매니저
            cls._instance.risk_manager = RiskManager(cls._instance)   # [추가] 리스크 매니저
            cls._instance.half_tp_cache = set()       # [추가] 반익절 실행 여부 추적 캐시
            
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

    def _refine_trade_records(self, records):
        """거래 내역 중복 제거 및 우선순위 적용 (전략 사유 > 체결 확인)"""
        unique_records = {}
        
        for r in records:
            odno = r.get('odno')
            # odno가 없으면 고유 키 생성하여 포함
            if not odno:
                key = f"NO_ODNO_{r['time']}_{r['code']}_{r['type']}_{len(unique_records)}"
                unique_records[key] = r
                continue
                
            if odno not in unique_records:
                unique_records[odno] = r
            else:
                existing = unique_records[odno]
                # 기존 기록이 '체결 확인'이고, 현재 기록이 구체적 사유가 있다면 교체 (정보 보강)
                if existing.get('reason') == '체결 확인' and r.get('reason') != '체결 확인':
                    unique_records[odno] = r
                # 현재 기록이 '체결 확인'이면 무시 (기존의 구체적 사유 유지)
        
        return list(unique_records.values())

    def update_order_status(self, code, odno, status):
        """체결 모니터에서 호출하여 주문 상태 업데이트"""
        self.order_manager.update_order_status(code, odno, status)

    def start(self, interactive=True):
        if self.is_running:
            console.print("\n[yellow]이미 자동매매가 실행 중입니다.[/yellow]")
            return
        
        # [추가] 로그 파일 생성을 보장하기 위해 시작 즉시 로그 기록
        self.log("=== 자동매매 시스템 시작 프로세스 진입 ===")
        
        # [수정] 실전 모드일 경우 자동매매 전용 계좌 설정 확인
        if not config.session.is_simulation:
            if not config.session.auto_app_key or not config.session.auto_cano:
                console.print("[bold red]오류: 실전 투자 모드에서 시스템 트레이딩을 실행하려면 별도의 자동매매 계좌 설정이 필요합니다.[/bold red]")
                console.print("[dim]환경 변수 AUTO_APP_KEY, AUTO_APP_SECRET, AUTO_ACC_NUM을 설정해주세요.[/dim]")
                return
            
            console.print("\n[bold red]!!! 경고: 실전 투자 모드에서 시스템 트레이딩을 시작합니다 !!![/bold red]")
            console.print(f"운용 계좌: [bold yellow]{config.session.auto_cano}-{config.session.auto_acnt_prdt_cd}[/bold yellow] (시스템 트레이딩 전용)")
            
            if interactive:
                if Prompt.ask("위 계좌로 실제 매매가 수행됩니다. 진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
                    console.print("[yellow]시작을 취소했습니다.[/yellow]")
                    return
            else:
                console.print("[bold cyan][텔레그램 명령] 실전 투자 자동매매를 시작합니다.[/bold cyan]")

        # [추가] 텔레그램 메시지 구성을 위한 변수 미리 선언
        holdings = []
        summary = []
        deposit = 0
        asset_check_failed = False # [추가] 자산 조회 실패 여부 플래그

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("[green]시스템 시작 준비 중...[/]", total=None)
            
            self.is_running = True
            self.start_time = datetime.now()
            self.consecutive_errors = 0
            self.was_market_open = self.is_market_open()
            self.market_status_notified = {} # 시작 시 알림 상태 초기화
            
            # [추가] 시작 시점 총 자산 계산 (손실 제한 기준점)
            # [수정] AccountContext 사용
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                # [최적화] 자산 조회 로직 통합 (중복 API 호출 제거)
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                
                try:
                    # [수정] 상위 레벨 재시도 루프 제거 -> API 레벨 재시도(MAX_RETRIES) 활용
                    progress.update(task, description="[green]자산 및 잔고 조회 중...[/]")
                    # 1. 잔고 및 평가금 조회
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    
                    # [추가] 스레드 첫 실행 시 재사용을 위해 저장
                    self.initial_holdings = holdings
                    self.initial_summary = summary
                    
                    # 2. 예수금 조회
                    # [수정] 실전/모의 모두 summary 정보 우선 활용 (안정성 확보)
                    if summary:
                        # [수정] 자산 계산 시 D+2 예수금(가수도금)을 사용하여 매도 대금 미결제분 반영
                        deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                        if deposit == 0: # Fallback
                            deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))
                    
                    if deposit == 0 and not config.session.is_simulation:
                        res = api.get_deposit_balance(target_cano, acnt, skip_balance_check=True)
                        if res:
                            deposit = res['deposit'] + res['foreign_deposit']
                    
                    # 4. 반익절 상태 DB 복원 (시스템 재시작 대비)
                    def _init_half_tp_cache(holdings_list):
                        cache = set()
                        try:
                            with sqlite3.connect(config.DB_FILE_PATH) as conn:
                                cursor = conn.cursor()
                                for h in holdings_list:
                                    code = h['pdno']
                                    cursor.execute("SELECT type, reason FROM trade_history WHERE code = ? ORDER BY time DESC LIMIT 5", (code,))
                                    for row in cursor.fetchall():
                                        t_type, reason = row[0], row[1]
                                        if 'buy' in t_type.lower() or '매수' in t_type: break # 최근 매수 기록 만나면 중단
                                        if ('sell' in t_type.lower() or '매도' in t_type) and reason and '반익절' in reason:
                                            cache.add(code)
                                            break
                        except Exception as e:
                            logger.error(f"반익절 캐시 복원 실패: {e}")
                        return cache
                    
                    if hasattr(db_manager.db, 'execute_custom'):
                        self.half_tp_cache = db_manager.db.execute_custom(_init_half_tp_cache, holdings)
                    else:
                        self.half_tp_cache = _init_half_tp_cache(holdings)

                    # 3. 총 자산 계산
                    stock_eval = 0
                    tot_evlu = 0
                    if summary:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt'))
                        tot_evlu = api.safe_int(summary[0].get('tot_evlu_amt'))
                    
                    # [수정] 모의투자는 API 총평가금 대신 (주식평가 + D+2예수금) 계산값 사용
                    current_calculated_asset = 0
                    if not config.session.is_simulation and tot_evlu > 0:
                        current_calculated_asset = tot_evlu
                    else:
                        current_calculated_asset = deposit + stock_eval
                        
                    if current_calculated_asset > 0:
                        saved_initial = load_daily_initial_asset()
                        if saved_initial > 0:
                            self.initial_asset = saved_initial
                        else:
                            self.initial_asset = current_calculated_asset
                            save_daily_initial_asset(self.initial_asset)
                    else:
                        self.initial_asset = 0

                    asset_check_failed = False

                except Exception as e:
                    logger.error(f"시작 자산 조회 실패: {e}")
                    asset_check_failed = True
                
                # [수정] 시작 시 불필요한 집중 감시 모드 진입 방지 (IDLE_INTERVAL=0 설정 존중)
                # ConclusionMonitor().check_now()
                
                # [추가] 체결 감시 모니터 시작 (체결 확인 및 DB 상태 동기화를 위해 필수)
                ConclusionMonitor().start()
            
            if asset_check_failed:
                self.log("초기 자산 조회 실패 (API 응답 없음 또는 오류)")
                console.print("[bold red]⚠️ 초기 자산 조회 실패: API 응답이 없거나 오류가 발생했습니다.[/bold red]")

            if self.initial_asset > 0:
                saved_msg = " (당일 기준 복원)" if load_daily_initial_asset() > 0 else " (당일 기준 저장)"
                self.log(f"시스템 시작 자산: {self.initial_asset:,}원{saved_msg}")
            
            # [추가] API 모듈에서 로그를 남길 수 있도록 연결
            context.SYSTEM_LOGGER = self.log
            
            progress.update(task, description="[green]트레이딩 스레드 시작 중...[/]")
            self.thread = threading.Thread(target=self._run_loop, daemon=True, name="AutoTrader")
            self.thread.start()

        console.print("\n[green]자동매매 시스템이 시작되었습니다. (백그라운드)[/green]")
        self.log("시스템 시작")
        
        # [수정] 텔레그램 전송 시 AUTO 계좌 정보가 포함되도록 컨텍스트 설정
        # AccountContext는 블록 내에서만 유효하므로, 메시지 생성 시점에만 적용하거나
        # send_telegram_message 내부에서 처리하도록 두는 것이 좋으나, 여기서는 명시적으로 설정
        
        # [수정] 시작 메시지에 보유 종목 및 자산 현황 추가
        msg = f"🟢 [시스템 시작] 자동매매가 시작되었습니다.\n"
        
        if asset_check_failed:
            msg += "⚠️ [경고] 자산 정보를 불러오지 못했습니다. (API 오류)\n"
            
        msg += f"초기 자산: {self.initial_asset:,}원"
        msg += f"\n현재 예수금: {deposit:,}원"
        
        # [추가] 현재 평가금액 정보 (전략 정보 위로 이동)
        stock_eval_amt = 0
        if summary and len(summary) > 0:
            s_data = summary[0]
            # [수정] 현재 평가 금액 = 주식 평가금 + 예수금 (총 자산)
            stock_eval_amt = api.safe_int(s_data.get('scts_evlu_amt'))
            current_total_asset = stock_eval_amt + deposit
            total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
            
            tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
            if tot_pchs == 0 and holdings:
                tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
            
            rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            msg += f"\n현재 평가(자산): {current_total_asset:,}원 (평가손익: {total_profit:+,}원 / {rate:+.2f}%)"

        # [추가] 전략 설정 요약 정보 추가
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]
        sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
        sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1)
        
        msg += "\n\n⚙️ [적용 전략]"
        msg += f"\n• 매수: {buy_score}점↑ & RSI {buy_rsi}↓ & 체결강도 {buy_vol}%↑"
        msg += f"\n• 매도: {sell_score}점 미만 / RSI {tp_rsi} 초과"
        msg += f"\n• 익절: +{tp}% / 손절: {sl}%"
        msg += f"\n• 트레일링: +{ts_act}% 도달 후 -{ts_call}%"
        msg += f"\n• 비중: 종목당 {invest_ratio*100:.0f}%"
            
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
            if stock_eval_amt > 0:
                msg += " (⚠️ 평가금액 존재 - API 데이터 불일치)"

        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            api.send_telegram_message(msg)

    def stop(self, use_status=True):
        if not self.is_running:
            console.print("\n[yellow]실행 중인 자동매매가 없습니다.[/yellow]")
            return
            
        def _stop_logic():
            self.is_running = False
            ConclusionMonitor().stop() # [추가] 체결 감시 모니터 종료
            if self.thread:
                self.thread.join(timeout=10) # [수정] 타임아웃 연장 (DB 락 대기 고려)

        if use_status:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
                transient=True
            ) as progress:
                progress.add_task("[red]시스템 중단 요청 처리 중...[/]", total=None)
                _stop_logic()
        else:
            _stop_logic()

        if self.thread and self.thread.is_alive():
            console.print("\n[bold red]경고: 시스템 트레이딩 스레드가 응답하지 않습니다. (DB/API 작업 지연)[/bold red]")
            console.print("[dim]강제로 중단 절차를 진행합니다. 일부 데이터가 누락될 수 있습니다.[/dim]")

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
                        deposit = res['d2_deposit'] + res['foreign_deposit']
                        is_data_valid = True
                    else:
                        deposit = 0

                    # 2. 잔고 및 평가금 조회
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                    if summary and len(summary) > 0:
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt'))

                    if is_data_valid:
                        final_asset = deposit + stock_eval
                        profit = final_asset - self.initial_asset
                        profit_rate = 0.0 if self.initial_asset <= 0 else (profit / self.initial_asset) * 100

                        msg += f"\n종료 자산: {final_asset:,}원\n최종 예수금: {deposit:,}원\n금일 손익: {profit:+,}원 ({profit_rate:+.2f}%)"
                    else:
                        msg += "\n(⚠️ 종료 시 자산 정보 조회 실패 - 서버 응답 없음)"

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
            
                api.send_telegram_message(msg)
        else:
            msg += "\n(시스템 응답 지연으로 최종 자산 정보 생략)"
            self.log("스레드 종료 지연으로 최종 자산/잔고 조회 생략")
        
        # [추가] 로거 연결 해제 (메시지 전송 후 해제)
        context.SYSTEM_LOGGER = None

    def get_status_message(self):
        """텔레그램 전송용 상태 요약 메시지 생성"""
        status_text = "STOPPED"
        if self.is_running:
            status_text = "RUNNING" if self.is_market_open() else "WAITING"
        
        msg = f"[시스템 상태: {status_text}]\n"
        
        # 자산 정보 조회
        current_asset = None
        deposit = 0
        holdings = []

        target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
        with utils.AccountContext(target_cano):
            try:
                # [최적화] API 호출 통합 (중복 조회 제거)
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                
                # 1. 잔고 조회 (평가금 포함)
                holdings, summary = api.get_domestic_balance(target_cano, acnt)
                
                # 2. 예수금 및 총 자산 계산
                if config.session.is_simulation:
                    if summary:
                        # [수정] D+2 예수금(가수도금) 사용 (매도 대금 포함, /balance와 통일)
                        deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                        stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        tot_evlu = api.safe_int(summary[0].get('tot_evlu_amt', 0))
                        
                        # 총 자산: API 제공값 우선, 없으면 계산
                        if not config.session.is_simulation and tot_evlu > 0:
                            current_asset = tot_evlu
                        else:
                            current_asset = deposit + stock_eval
                else:
                    res = api.get_deposit_balance(target_cano, acnt)
                    if res:
                        deposit = res['d2_deposit']
                        stock_eval = 0
                        if summary: stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                        total_cash = res['deposit'] + res['foreign_deposit']
                        current_asset = total_cash + stock_eval
            except: pass

        if current_asset is not None:
            tot_profit = 0
            tot_pchs = 0
            if summary:
                tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt'))
                tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl'))
            
            if tot_pchs == 0 and holdings:
                tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
            
            rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
            
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                saved_initial = load_daily_initial_asset()
                if saved_initial > 0:
                    self.initial_asset = saved_initial
            
            if self.initial_asset > 0:
                msg += f"금일 시작 자산: {self.initial_asset:,}원\n"
            else:
                msg += f"금일 시작 자산: - (미설정)\n"
                
            msg += f"금일 현재 자산: {current_asset:,}원\n"
            
            if self.initial_asset > 0:
                daily_profit = current_asset - self.initial_asset
                daily_profit_rate = (daily_profit / self.initial_asset) * 100
                msg += f"금일 현재 손익: {daily_profit:+,}원 ({daily_profit_rate:+.2f}%)\n"
                
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
            msg += f"금일 실현 손익: {realized_profit:+,}원 ({realized_rate:+.2f}%)\n"
            msg += f"금일 평가 손익: {tot_profit:+,}원 ({rate:+.2f}%)\n"
            msg += f"주문 가능: {deposit:,}원\n"
        else:
            msg += "자산 정보 조회 실패\n"
            
        # [수정] 현재 시장 상황 정보
        msg += "\n[시장 상황]\n"
        regime_map = {"Bull": "🔴 강세장", "Bear": "🔵 약세장", "Sideways": "🟡 횡보장"}

        for m_type, label in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
            try:
                regime, _ = analysis.get_market_regime(m_type)
                regime_str = regime_map.get(regime, regime)
                msg += f"• {label}: {regime_str}\n"
            except Exception:
                msg += f"• {label}: 확인 불가\n"

        # [추가] 시장 지수 요약 정보 및 필터링 상태 (시장 상황 아래 배치)
        use_filter = getattr(config, 'USE_MARKET_FILTER', True)
        filter_str = "ON" if use_filter else "OFF"
        msg += f"\n[시장 지수 및 필터링 (필터: {filter_str})]\n"
        
        try:
            for name, m_type in [("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")]:
                df = analysis.get_domestic_index_data(m_type)
                if df is not None and not df.empty:
                    curr = df.iloc[-1]['close']
                    prev = df.iloc[-2]['close'] if len(df) > 1 else curr
                    rate = ((curr - prev) / prev) * 100
                    
                    filter_msg = ""
                    if use_filter:
                        # 대기 상태(WAITING) 시 메모리 캐시 누락 방지를 위해 실시간 데이터로 직접 계산
                        ma_period = getattr(config, 'MARKET_FILTER_MA', 50)
                        if len(df) >= ma_period:
                            ma_val = df['close'].rolling(window=ma_period).mean().iloc[-1]
                            is_healthy = curr >= ma_val
                            filter_msg = " [🟢허용]" if is_healthy else " [🚫보류]"
                        else:
                            filter_msg = " [데이터부족]"
                            
                    msg += f"• {name}: {curr:,.2f} ({rate:+.2f}%){filter_msg}\n"
        except: pass
        
        if use_filter and self.skipped_by_market_filter_count > 0:
            msg += f"⚠️ 하락장 방어 중 (최근 {self.skipped_by_market_filter_count}종목 신규 매수 보류)\n"

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
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("[green]트레이딩 상태 및 자산 정보 조회 중...[/]", total=None)
                
                progress.update(task, description="[green]총 추정 자산 계산 중...[/]")
                current_asset = self._get_total_estimated_asset()
                
                progress.update(task, description="[green]보유 종목 및 잔고 조회 중...[/]")
                # [추가] 보유 종목 확인
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                try:
                    holdings, summary = api.get_domestic_balance(target_cano, acnt)
                except: 
                    holdings = []
                    summary = []
                
                progress.update(task, description="[green]예수금 정보 확인 중...[/]")
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
                            deposit = res['d2_deposit']
                except: pass
                
                progress.update(task, description="[green]시장 국면(KOSPI/KOSDAQ) 분석 중...[/]")
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
                        progress.update(task, description="[green]시장 지수(KOSPI/KOSDAQ) 상태 업데이트 중...[/]")
                        self._update_market_indices_status(notify=False)

        console.print()
        table = Table(title=f"시스템 트레이딩 상태 ({status_text})", title_style=status_color, box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
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
        table.add_row("시장 국면", f"KOSPI: {k_regime_str} (보정: {kospi_adj:+.1f}점) / KOSDAQ: {q_regime_str} (보정: {kosdaq_adj:+.1f}점)")

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
            if self.skipped_by_market_filter_count > 0:
                table.add_row("시장 필터링", f"[bold blue]{self.skipped_by_market_filter_count}종목 매수 보류[/] (하락장)")

        table.add_section()
        
        # [추가] 개별 종목 룰 설정 현황
        custom_rules = db_manager.db.get_all_stock_strategies()
        custom_rules = _enrich_rules_with_weights(custom_rules) # [Fix] 가중치 JSON 파싱
        rule_table = None
        
        # 보유 종목 코드 집합 생성 (강조 표시용)
        held_codes = set()
        if holdings:
            for h in holdings:
                if int(h.get('hldg_qty', 0)) > 0:
                    held_codes.add(h.get('pdno'))

        if custom_rules:
            rule_summary = f"총 {len(custom_rules)}개 종목 개별 설정됨"
            table.add_row("개별 룰 설정", rule_summary)
            
            # 별도 테이블로 상세 표시
            rule_table = Table(title="종목별 개별 트레이딩 룰 목록", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            rule_table.add_column("종목명(코드)", justify="left")
            rule_table.add_column("매수(점수/RSI/체결)", justify="center")
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
                ratio_str = f"{r.get('invest_ratio', getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1)) * 100:.0f}%"

                rule_table.add_row(
                    name_disp,
                    f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', 100.0)}%",
                    f"+{r['take_profit']}% / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
                    f"{ratio_str} / {sl_str}",
                    w_str,
                    r.get('updated_at', '-')
                )
                if (i + 1) % 5 == 0 and (i + 1) < len(custom_rules):
                    rule_table.add_section()
            table.add_section()

        # 3. 자산 현황
        if current_asset is not None:
            # [추가] 메모리에 초기 자산이 없으면 당일 백업 파일에서 복구 시도
            if self.initial_asset <= 0:
                saved_initial = load_daily_initial_asset()
                if saved_initial > 0:
                    self.initial_asset = saved_initial
                    
            if self.initial_asset > 0:
                tot_profit = 0
                tot_pchs = 0
                if summary:
                    tot_profit = api.safe_int(summary[0].get('evlu_pfls_smtl_amt'))
                    tot_pchs = api.safe_int(summary[0].get('pchs_amt_smtl'))
                
                if tot_pchs == 0 and holdings:
                    tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in holdings if int(h.get('hldg_qty', 0)) > 0)
                
                rate = (tot_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                color = "[red]" if tot_profit > 0 else ("[blue]" if tot_profit < 0 else "[white]")
                
                table.add_row("금일 시작 자산", f"{self.initial_asset:,}원")
                table.add_row("금일 현재 자산", f"{current_asset:,}원")
                table.add_row("금일 평가 손익", f"{color}{tot_profit:+,}원 ({rate:+.2f}%)[/]")
            else:
                table.add_row("금일 시작 자산", "- (미설정)")
                table.add_row("금일 현재 자산", f"{current_asset:,}원")
                table.add_row("금일 평가 손익", "-")
            
            table.add_row("현재 예수금", f"{deposit:,}원")
        else:
            table.add_row("자산 정보", "[bold red]조회 실패 (KIS 서버 응답 없음/장애 가능성)[/bold red]")
            if self.initial_asset > 0:
                table.add_row("금일 시작 자산", f"{self.initial_asset:,}원")

        table.add_section()

        # 4. 설정 및 상태 정보 (재구성)
        # 매수 조건
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        buy_vol = config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"]
        table.add_row("매수 조건", f"{buy_score}점↑ / RSI {buy_rsi}↓ / 체결강도 {buy_vol}%↑")

        # [추가] 역추세 매수 표시
        use_mr = config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)
        if use_mr:
            mr_rsi = config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
            mr_disp = config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
            mr_vol = config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
            table.add_row("", f"역추세매수 [green]ON[/] (RSI {mr_rsi}↓ / 이격도 {mr_disp}%↓ / 체결 {mr_vol}%↑)")

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
        
        cond_str = f"익절 (+{tp}%)"
        if use_half_tp: cond_str += f" / 반익절 (+{tp/2:.1f}%, 50%)"
        cond_str += f" / 손절 ({sl}%)"
        if use_atr: cond_str += f" / ATR손절 (x{atr_mult})"
        
        table.add_row("", cond_str)
        table.add_row("", f"트레일링스탑 (+{ts_act}%/-{ts_call}%)")
        
        if use_time_stop:
            table.add_row("", f"시간청산 [green]ON[/] ({time_stop_days}일 경과 & 수익률 +{time_stop_min}% 미만)")

        # 투자 설정
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1)
        if invest_ratio <= 0: invest_ratio = 0.1
        max_holdings = getattr(config, 'SYSTEM_MAX_HOLDINGS', 10)
        table.add_row("투자 설정", f"비중 {invest_ratio*100:.0f}% (최대 {max_holdings}종목)")

        # 손실 제한
        loss_limit = getattr(config, 'SYSTEM_DAILY_LOSS_LIMIT', 0.0)
        if loss_limit > 0:
            safety_msg = "[green]안전[/green]"
            daily_info = ""
            if current_asset is not None and self.initial_asset > 0:
                profit = current_asset - self.initial_asset
                rate = (profit / self.initial_asset) * 100
                
                p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                daily_info = f" | 금일 현재 손익: {p_color}{profit:+,}원 ({rate:+.2f}%)[/]"
                
                if rate <= -loss_limit: safety_msg = "[bold red]위험 (한도 초과)[/bold red]"
                elif rate <= -(loss_limit * 0.8): safety_msg = "[bold orange3]주의 (한도 임박)[/bold orange3]"
            table.add_row("손실 제한", f"-{loss_limit}% (상태: {safety_msg}{daily_info})")
        else:
            daily_info = ""
            if current_asset is not None and self.initial_asset > 0:
                profit = current_asset - self.initial_asset
                rate = (profit / self.initial_asset) * 100
                p_color = "[red]" if profit > 0 else ("[blue]" if profit < 0 else "[white]")
                daily_info = f" (금일 현재 손익: {p_color}{profit:+,}원 ({rate:+.2f}%)[/])"
            table.add_row("손실 제한", f"미사용{daily_info}")

        # 연속 에러
        err_cnt = self.consecutive_errors
        max_err = getattr(config, 'SYSTEM_MAX_CONSECUTIVE_ERRORS', 5)
        if err_cnt == 0:
            err_display = f"[dim green]{err_cnt} / {max_err}회[/]"
        else:
            err_color = "[red]" if err_cnt >= max_err else "[yellow]"
            err_display = f"{err_color}{err_cnt} / {max_err}회[/]"
        table.add_row("연속 에러", err_display)
        
        # 금일 매매 & 실현 손익
        # [수정] 메모리 대신 DB에서 조회하여 재시작 시에도 정확한 카운트 및 실현 손익 표시
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
            
        p_color = "[red]" if today_profit > 0 else ("[blue]" if today_profit < 0 else "[white]")
        table.add_row("금일 매매", f"[red]매수 {buy_cnt}건[/] / [blue]매도 {sell_cnt}건[/] (금일 실현 손익: {p_color}{today_profit:+,}원[/])")

        console.print(table)
        
        if rule_table:
            console.print()
            console.print(rule_table)
            console.print()
        
        # [추가] 보유 종목 리스트 출력
        # [수정] 보유수량 0 초과인 종목만 필터링
        valid_holdings = [h for h in holdings if int(h.get('hldg_qty', 0)) > 0] if holdings else []

        if valid_holdings:
            console.print("\n[bold]보유 종목 리스트[/bold]")
            h_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
            h_table.add_column("종목명(코드)", justify="left")
            h_table.add_column("시장", justify="center")
            h_table.add_column("수량", justify="right")
            h_table.add_column("매입가", justify="right")
            h_table.add_column("현재가", justify="right")
            h_table.add_column("평가손익", justify="right")
            h_table.add_column("수익률", justify="right")
            
            for item in valid_holdings:
                name = item['prdt_name']
                code = item['pdno']
                market_type = self._get_stock_market_type(code)
                qty = int(item['hldg_qty'])
                buy_price = float(item['pchs_avg_pric'])
                cur_price = int(item['prpr'])
                profit = int(item['evlu_pfls_amt'])
                rate = float(item['evlu_pfls_rt'])
                
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

    def print_report(self):
        console.print("\n[bold yellow]=== 시스템 트레이딩 리포트 (Trading Report) ===[/]")
        
        grid = Table.grid(padding=(0, 2))
        grid.add_column(justify="left")
        grid.add_column(justify="left", style="dim")
        grid.add_row("[1] 일간 (오늘)", "(Daily)")
        grid.add_row("[2] 주간 (최근 7일)", "(Weekly)")
        grid.add_row("[3] 월간 (최근 30일)", "(Monthly)")
        grid.add_row("[4] 기간 직접 입력", "(Custom Days)")
        console.print(grid)
        console.print()
        
        choice = Prompt.ask("조회할 기간을 선택하세요 [dim](Enter: 4)[/dim]", choices=["1", "2", "3", "4"], default="4")
        
        days = None
        if choice == "1": days = 0
        elif choice == "2": days = 7
        elif choice == "3": days = 30
        elif choice == "4":
            val = Prompt.ask("조회할 기간(일) 입력 [dim](Enter: 전체 내역)[/dim]", default="")
            if val.strip() and val.isdigit():
                days = int(val)
            else:
                days = None # 전체 내역

        self._load_trade_records(days=days)
        
        if not self.trade_records:
            console.print("\n[yellow]선택한 기간에 해당하는 매매 기록이 없습니다.[/yellow]")
            return
            
        stats = self._calculate_statistics()
        
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
        self._print_current_holdings()
        self._print_stock_details()

    def _load_trade_records(self, days=None):
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[green]DB에서 매매 내역 조회 및 분석 중...[/]", total=None)
            
            # [수정] DB에서 매매 내역 조회 (수동 매매 포함을 위해 is_auto 필터 제거)
            # 시스템 매매와 수동 매매를 모두 포함하여 평가
            limit = 500
            start_date = None
            
            if days is not None:
                limit = None
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            
            # [수정] 자동매매 계좌 번호로 필터링 (시스템 트레이딩 내역만 조회)
            target_account = None
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
        
        if stats['sell_trades_exist']:
            win_rate = stats['win_rate']
            total_profit = stats['total_profit']
            avg_profit_rate = stats['avg_profit_rate']
            
            msg += f"[수익 현황]\n"
            msg += f"총 매매: {stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})\n"
            msg += f"승률: {win_rate:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)\n"
            msg += f"총 실현 손익: {total_profit:+,}원\n"
            msg += f"평균 수익률: {avg_profit_rate:+.2f}%\n"
            msg += f"평균 보유: {stats['avg_holding_str']}\n"
            
            if holdings_summary:
                msg += f"\n[현재 보유 현황]\n"
                msg += f"총 매입금액: {holdings_summary['tot_pchs']:,}원\n"
                msg += f"총 평가금액: {holdings_summary['tot_evlu']:,}원\n"
                msg += f"총 평가손익: {holdings_summary['tot_profit']:+,}원 ({holdings_summary['rate']:+.2f}%)\n"

            msg += f"\n[최고/최악 거래]\n"
            if stats.get('best_trade'):
                b = stats['best_trade']
                msg += f"Best: {b['name']} ({b['profit_amt']:+,}원 / {b['profit_rate']:+.2f}%)\n"
            if stats.get('worst_trade'):
                w = stats['worst_trade']
                msg += f"Worst: {w['name']} ({w['profit_amt']:+,}원 / {w['profit_rate']:+.2f}%)\n"
            
            msg += f"\n[매도 사유 분포]\n"
            reasons = stats.get('sell_reasons', {})
            total_sells = stats['sell_count']
            if total_sells > 0:
                for r, count in reasons.most_common():
                    msg += f"{r}: {count}건 ({count/total_sells*100:.0f}%)\n"
        else:
            msg += f"[수익 현황]\n"
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
        
        # [추가] Best/Worst 및 사유 분석 변수
        best_trade = None
        worst_trade = None
        sell_reasons = Counter()
        
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
            elif r['type'] == 'sell':
                # 매도 시 매수 기록과 매칭 (FIFO: 먼저 산 것을 먼저 판다고 가정)
                if code in buy_times and buy_times[code]:
                    buy_dt = buy_times[code].pop(0)
                    diff = (dt - buy_dt).total_seconds()
                    total_holding_seconds += diff
                    holding_count += 1
        
        for t in sell_trades:
            profit = t.get('profit_amt', 0)
            rate = t.get('profit_rate', 0.0)
            total_profit += profit
            total_profit_rate += rate
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
            "avg_profit_rate": avg_profit_rate,
            "win_rate": win_rate,
            "avg_holding_str": avg_holding_str,
            "sell_trades_exist": len(sell_trades) > 0,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "sell_reasons": sell_reasons
        }

    def _print_summary_table(self, stats, holdings_summary=None):
        summary_table = Table(box=box.HORIZONTALS, show_header=False, border_style="dim")
        summary_table.add_column("항목", style="cyan", justify="left")
        summary_table.add_column("값", justify="left")
        
        summary_table.add_row("총 매매 실행", f"{stats['total_trades']}건 (매수 {stats['buy_count']} / 매도 {stats['sell_count']})")
        
        if stats['sell_trades_exist']:
            summary_table.add_row("승률 (Win Rate)", f"{stats['win_rate']:.1f}% ({stats['win_trades']}승 {stats['loss_trades']}패)")
            tp = stats['total_profit']
            summary_table.add_row("총 실현 손익", f"[red]{tp:+,}원[/]" if tp > 0 else f"[blue]{tp:+,}원[/]")
            apr = stats['avg_profit_rate']
            summary_table.add_row("평균 수익률", f"[red]{apr:+.2f}%[/]" if apr > 0 else f"[blue]{apr:+.2f}%[/]")
            summary_table.add_row("평균 보유 기간", stats['avg_holding_str'])
        
        if holdings_summary:
            summary_table.add_section()
            summary_table.add_row("총 매입금액", f"{holdings_summary['tot_pchs']:,}원")
            summary_table.add_row("총 평가금액", f"{holdings_summary['tot_evlu']:,}원")
            hp = holdings_summary['tot_profit']
            hr = holdings_summary['rate']
            hc = "[red]" if hp > 0 else ("[blue]" if hp < 0 else "[white]")
            summary_table.add_row("총 평가손익", f"{hc}{hp:+,}원 ({hr:+.2f}%)[/]")
            
        console.print(summary_table)

    def _print_current_holdings(self):
        try:
            # 컨텍스트 설정 (시스템 트레이딩 계좌 조회)
            target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            with utils.AccountContext(target_cano):
                acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
                holdings, _ = api.get_domestic_balance(target_cano, acnt)
                
                if holdings:
                    console.print("\n[bold]현재 보유 종목 현황[/bold]")
                    h_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
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
            console.print("\n[bold]종목별 성과 분석[/bold]")
            s_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
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
        console.print("\n[bold]상세 매매 내역 (최신순)[/bold]")
        detail_table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
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
            
            # 단가 포맷팅 (정수/실수 구분)
            price_val = r['price']
            if price_val.is_integer():
                price_str = f"{int(price_val):,}"
            else:
                price_str = f"{price_val:,.2f}"
            
            trade_amt = int(r['price'] * r['qty']) # [추가] 매매 총액 계산
            
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
                f"{trade_amt:,}", # [추가]
                profit_display,
                reason_display
            )
            
            # [추가] 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(records):
                detail_table.add_section()
            
        console.print(detail_table)

    def view_log_file(self):
        """현재 날짜의 시스템 트레이딩 로그 파일을 실시간으로 출력합니다."""
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

        console.print(f"\n[bold cyan]=== 실시간 로그 모니터링 ({filename}) ===[/bold cyan]")
        console.print("[dim]종료하려면 Ctrl+C를 누르세요.[/dim]\n")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            progress.add_task("[green]로그 파일 로딩 중...[/]", total=None)

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
        now = datetime.now()
        if now.weekday() > 4: return False # 주말
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
        while self.is_running:
            try:
                target_cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
                with utils.AccountContext(target_cano):
                    self.log("모니터링 주기 시작...")
                    
                    # [추가] Kill Switch: 체결 감시 시스템 상태 점검
                    # 체결 확인이 불가능한 상태에서는 신규 주문도 위험하므로 중단
                    if not ConclusionMonitor().is_healthy():
                        raise Exception(f"체결 감시 시스템 불안정 (연속 에러 {ConclusionMonitor().consecutive_errors}회)")
                    
                    # [추가] 현재 운용 계좌 정보 로깅
                    if target_cano:
                        acc_type = "모의투자" if config.session.is_simulation else "실전투자(자동)"
                        self.log(f"운용 계좌: {target_cano} [{acc_type}]")
                    
                    current_market_status = self.is_market_open()
                    
                    # [추가] 장 시작/마감 상태 변경 감지 및 로그
                    if self.was_market_open is not None:
                        if not self.was_market_open and current_market_status:
                            self.log("=" * 80)
                            self.log(f"📢 [거래 시작] 시스템 트레이딩 거래가 시작되었습니다. ({datetime.now().strftime('%H:%M')})")
                            self.log("=" * 80)
                            
                            msg = "🔔 [장 시작] 거래 가능 시간이 되었습니다."
                            msg += self._get_holdings_message(target_cano)
                            api.send_telegram_message(msg)
                        elif self.was_market_open and not current_market_status:
                            self.log("=" * 80)
                            self.log(f"💤 [거래 종료] 시스템 트레이딩 거래가 종료되었습니다. ({datetime.now().strftime('%H:%M')})")
                            self.log("=" * 80)
                            
                            msg = "🌙 [장 마감] 거래 시간이 종료되었습니다."
                            msg += self._get_holdings_message(target_cano)
                            api.send_telegram_message(msg)
                    
                    # [변경] 장 마감 시 분석 중단 (트래픽 감소)
                    if not current_market_status:
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
                        # [추가] 보유 종목 상태 로깅 및 자산 안전장치 체크
                        self._monitor_account_status(holdings, summary, deposit_res)
                    
                    self.was_market_open = current_market_status
                    
                    self.log("모니터링 완료. 대기 중...")
                
                # 설정된 주기만큼 대기 (중단 요청 시 즉시 반응)
                # [확인] 설정된 간격(현재 180초)마다 위 로직을 반복합니다.
                interval = getattr(config, 'SYSTEM_TRADING_INTERVAL', 60)
                
                # [수정] 대기 시간 중에도 주기적으로 미체결 주문 관리 수행 (5초 단위)
                # 긴 대기 시간(예: 3분) 동안 미체결 주문이 방치되는 것을 방지
                for _ in range(interval): 
                    if not self.is_running: break
                    
                    # 5초마다 미체결 관리 호출 (단, 루프 시작 직후는 제외)
                    if _ > 0 and _ % 5 == 0:
                        self.order_manager.manage_unfilled_orders()
                        
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
                
                self.log("-" * 125)
                self.log(header)
                self.log("-" * 125)
                
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
                
                self.log("-" * 125)
                if summary and len(summary) > 0:
                    s_data = summary[0]
                    total_profit = api.safe_int(s_data.get('evlu_pfls_smtl_amt'))
                    total_eval = api.safe_int(s_data.get('scts_evlu_amt'))
                    
                    # 총 자산 계산 (예수금 + 평가금)
                    current_total = 0
                    deposit_d2 = 0
                    if deposit_res:
                        deposit_d2 = deposit_res['d2_deposit']
                        # 총 자산 계산 시에는 원화+외화 예수금 합산
                        # [수정] 자산 왜곡 방지를 위해 D+2 예수금 사용 (매도 대금 포함)
                        cash = deposit_d2 + deposit_res['foreign_deposit']
                        current_total = cash + total_eval
                    
                    # [추가] 일일 손실 제한 체크
                    if current_total > 0:
                        # [Fix] 초기 자산 로드 실패(0원) 시, 첫 유효 조회 값으로 보정
                        if self.initial_asset == 0:
                            saved_initial = load_daily_initial_asset()
                            if saved_initial > 0:
                                self.initial_asset = saved_initial
                                self.log(f"[시스템 보정] 기존 초기 자산 기록 로드: {self.initial_asset:,}원")
                            else:
                                self.initial_asset = current_total
                                save_daily_initial_asset(self.initial_asset)
                                self.log(f"[시스템 보정] 초기 자산 정보 갱신 및 저장: {self.initial_asset:,}원")

                        tot_pchs = api.safe_int(s_data.get('pchs_amt_smtl'))
                        if tot_pchs == 0 and valid_holdings:
                            tot_pchs = sum(int(int(h['hldg_qty']) * float(h['pchs_avg_pric'])) for h in valid_holdings)
                        profit_rate = (total_profit / tot_pchs * 100) if tot_pchs > 0 else 0.0
                        
                        self.log(f"   [자산 현황] 총자산: {current_total:,}원 | 평가손익: {total_profit:+,}원 ({profit_rate:+.2f}%)")
                        self.log(f"              예수금(D+2): {deposit_d2:,}원 | 주식평가: {total_eval:,}원")

                        daily_profit = current_total - self.initial_asset
                        daily_profit_rate = (daily_profit / self.initial_asset * 100) if self.initial_asset > 0 else 0.0
                        self.log(f"              금일 현재 손익: {daily_profit:+,}원 ({daily_profit_rate:+.2f}%) | 금일 시작 자산: {self.initial_asset:,}원")

                        self.risk_manager.check_loss_limit(current_total)
                    else:
                        self.log(f"   총 평가금액: {total_eval:,}원  |  총 평가손익: {total_profit:+,}원")
                    
        except Exception: pass

    def _get_total_estimated_asset(self):
        """현재 총 추정 자산(예수금 + 주식평가금) 계산"""
        try:
            # [수정] 상위 레벨 재시도 루프 제거 -> API 레벨 재시도 활용
            cano = config.session.auto_cano if not config.session.is_simulation else config.session.cano
            acnt = config.session.auto_acnt_prdt_cd if not config.session.is_simulation else config.session.acnt_prdt_cd
            
            # 1. 잔고 및 평가금 조회
            _, summary = api.get_domestic_balance(cano, acnt)
            
            stock_eval = 0
            deposit = 0
            
            if summary and len(summary) > 0: 
                stock_eval = api.safe_int(summary[0].get('scts_evlu_amt', 0))
                # [수정] 자산 계산 시 D+2 예수금(가수도금) 우선 사용
                deposit = api.safe_int(summary[0].get('prvs_rcdl_excc_amt', 0))
                if deposit == 0:
                    deposit = api.safe_int(summary[0].get('dnca_tot_amt', 0))

            # [추가] API 제공 총자산 우선 사용
            tot_evlu = api.safe_int(summary[0].get('tot_evlu_amt', 0)) if summary else 0
            if not config.session.is_simulation and tot_evlu > 0:
                return tot_evlu

            # 2. 실전투자면 별도 API 시도 (잔고 API의 예수금 갱신 지연 대비)
            # [수정] deposit이 0이 아니더라도 정확한 값을 위해 조회
            if not config.session.is_simulation:
                res = api.get_deposit_balance(cano, acnt, skip_balance_check=True)
                if res:
                    deposit = res['deposit'] + res['foreign_deposit']
            
            return deposit + stock_eval
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

        # [추가] Rate Limit 준수를 위한 딜레이 설정
        # 모의투자: 초당 2건 -> 0.5초 + 여유 / 실전투자: 초당 20건 -> 0.05초 + 여유
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 1.2  # 20% 여유 버퍼
        
        # [추가] 시장 국면 판단 (적응형 임계값용) - 매도 분석 시에도 상태 분류를 위해 필요
        market_regime_adj = {}
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj

        for item in holdings:
            if not self.is_running: break
            code = item['pdno']; name = item['prdt_name']
            
            # [추가] 미체결/진행 중인 주문이 있으면 스킵 (중복 매도 방지)
            if self.order_manager.is_pending(code):
                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    self.log(f"[분석스킵] {name}: 진행 중인 주문 존재")
                continue
            
            # [수정] 보유수량(hldg_qty) 대신 주문가능수량(ord_psbl_qty) 사용
            # 미체결 매도 주문이 있을 경우 중복 매도를 방지하기 위함
            qty = api.safe_int(item.get('ord_psbl_qty'))
            profit_rate = float(item['evlu_pfls_rt'])
            current_price = float(item['prpr'])
            buy_price = float(item['pchs_avg_pric'])
            
            # [추가] API 호출 전 대기 (Rate Limit 방지)
            time.sleep(safe_delay)
            
            if qty <= 0: 
                if config.FILE_DEBUG_LEVEL == "DEBUG":
                    self.log(f"[분석스킵] {name}: 주문 가능 수량 0 (미체결 매도 주문 존재 가능성)")
                continue # 주문 가능 수량이 없으면 스킵
            
            # [추가] 종목별 룰 적용
            rule = rules_map.get(code)
            
            # [추가] 적응형 임계값 보정치 계산
            market_type = self._get_stock_market_type(code)
            score_adj = market_regime_adj.get(market_type, 0.0)
            
            # 기본값 설정
            ts_activation = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
            ts_callback = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
            
            thresholds = None
            if rule:
                ts_activation = rule['ts_activation']
                ts_callback = rule['ts_callback']
                # Strategy에 전달할 임계값 딕셔너리 구성 (키 이름 매핑)
                thresholds = {
                    "TAKE_PROFIT_RATE": rule['take_profit'],
                    "STOP_LOSS_RATE": rule['stop_loss'],
                    "TAKE_PROFIT_RSI": rule['take_profit_rsi'],
                    "SELL_SCORE": rule['sell_score'],
                    "WEIGHTS": rule.get('weights'), # [추가] 가중치 전달
                    "BUY_SCORE": rule['buy_score'], # [수정] 개별 룰은 매도 분석 시에도 시장 보정 무시 (절대값)
                    "TIME_STOP_DAYS": rule.get('time_stop_days', config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10))
                }
            else:
                # 전역 설정 사용 시에도 가중치와 보정된 매수 기준 전달
                thresholds = {
                    "WEIGHTS": config.SCORING_WEIGHTS,
                    "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj,
                    "TIME_STOP_DAYS": config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
                }

            # [트레일링 스탑 로직] - 상태 관리가 필요하므로 AutoTrader에서 계산 후 Strategy에 전달
            # [추가] ATR 손절 사용 여부 확인 및 적용
            use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
            if rule and rule.get('use_atr_stop') is not None:
                use_atr_stop = bool(rule['use_atr_stop'])

            applied_sl_rate = None
            
            if use_atr_stop:
                last_buy = db_manager.db.get_latest_buy_trade(code)
                if last_buy and last_buy.get('stop_loss_rate'):
                    val = float(last_buy['stop_loss_rate'])
                    if val != 0.0: applied_sl_rate = val
            
            # [추가] ATR 손절률이 있으면 thresholds에 반영 (개별 룰보다 우선 적용)
            if applied_sl_rate is not None:
                if thresholds is None:
                    thresholds = {}
                thresholds["STOP_LOSS_RATE"] = applied_sl_rate
                
            # [추가] 보유 기간(일수) 계산을 위해 DB에서 최근 매수 기록 확인
            holding_days = 0
            is_mr_holding = False # [추가] 역추세 진입 여부
            last_buy = db_manager.db.get_latest_buy_trade(code)
            if last_buy and last_buy.get('time'):
                if '역추세' in str(last_buy.get('reason', '')):
                    is_mr_holding = True
                try:
                    buy_dt = datetime.strptime(last_buy['time'], "%Y-%m-%d %H:%M:%S")
                    holding_days = (datetime.now() - buy_dt).days
                except: pass

            ts_msg = ""
            # [최적화] 메모리 캐시 활용하여 DB 조회/쓰기 최소화
            with self._lock:
                cached_highest = self.trailing_stop_cache.get(code)
                if cached_highest is None:
                    # 캐시에 없으면 DB 조회 (최초 1회)
                    val = db_manager.db.get_highest_price(code)
                    cached_highest = val if val is not None else 0.0
                    self.trailing_stop_cache[code] = cached_highest
            
            highest_price = cached_highest
            
            # 현재가가 매수가보다 높고, 기록된 최고가보다 높을 때만 DB 업데이트
            if current_price > buy_price:
                if highest_price == 0.0 or current_price > highest_price:
                    db_manager.db.update_highest_price(code, current_price)
                    with self._lock:
                        self.trailing_stop_cache[code] = current_price # 캐시 갱신
                    highest_price = current_price
            
            if highest_price and highest_price > 0:
                max_profit_rate = ((highest_price - buy_price) / buy_price) * 100
                if max_profit_rate >= ts_activation:
                    drop_rate = ((highest_price - current_price) / highest_price) * 100
                    if drop_rate >= ts_callback:
                        ts_msg = f"트레일링스탑 (최고가:{int(highest_price):,}원, 최고가 대비 하락률:-{drop_rate:.1f}%)"

            # [전략 실행] 매도 분석 위임
            df = api.get_chart_data(code, is_overseas=False)
            already_half_sold = code in self.half_tp_cache
            result = self.strategy.analyze_sell(code, name, df, current_price, buy_price, profit_rate, ts_msg, thresholds=thresholds, already_half_sold=already_half_sold, holding_days=holding_days, is_mr_holding=is_mr_holding)
            
            # [로그] 분석 결과 기록
            ind = result['ind']
            rsi_val = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "-"
            adx_val = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "-"
            cci_val = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "-"
            action_str = "매도" if result['action'] == 'sell' else "보유"
            
            rule_msg = " [개별 룰 적용]" if rule else ""
            
            # [추가] 가중치 및 보정된 매수 기준 점수 정보 로깅
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
                
                target_sell_qty = max(1, int(qty * sell_ratio)) if sell_ratio < 1.0 else qty
                
                # [추가] 개별 룰 적용 시 사유에 표시
                if rule:
                    reason += " [개별 룰 적용]"
                
                # [추가] ATR 손절 적용 시 사유에 표시
                if applied_sl_rate is not None and "손절" in reason:
                    reason = reason.replace("손절", "ATR손절")
                
                # [수정] 슬리피지 비율 적용 (매도 체결 확률 증대)
                raw_order_price = current_price * (1 - config.SLIPPAGE_RATE)
                order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False))
                if order_price <= 0: order_price = int(current_price)

                if not is_market_open:
                    self.log(f"[장마감] 매도 신호 감지 (주문 미전송): {name} - {reason}")
                    continue

                # [추가] 매도 주문 전 실제 매도 가능 수량 재조회 (미체결 등 변동 고려)
                real_qty = api.fetch_sellable_quantity(code)
                if real_qty < target_sell_qty:
                    if real_qty > 0:
                        self.log(f"매도 수량 조정: {name} {target_sell_qty}주 -> {real_qty}주 (주문 가능 수량 변동)")
                        target_sell_qty = real_qty
                    else:
                        self.log(f"매도 중단: {name} 주문 가능 수량 부족 (미체결 존재 가능성)")
                        continue

                self.log(f"매도 실행: {name} - {reason}")
                # [수정] 매도 시 수익 정보와 사유, 점수 등을 DB 저장을 위해 전달
                odno = self.order_manager.send_order(code, target_sell_qty, "sell", name=name, profit_amt=int(item['evlu_pfls_amt']), profit_rate=profit_rate, reason=reason, score=score, price=order_price, rule=rule)
                if odno:
                    # 매도 성공 시 기록 (추정치)
                    record = {
                        "type": "sell",
                        "code": code,
                        "name": name,
                        "qty": target_sell_qty,
                        "price": float(order_price),
                        "profit_rate": profit_rate,
                        "profit_amt": int(item['evlu_pfls_amt']),
                        "reason": reason,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "odno": odno
                    }
                    self.trade_records.append(record)
                    
                    if sell_ratio < 1.0:
                        self.half_tp_cache.add(code) # 반익절 완료 상태 등록
                    else:
                        self.half_tp_cache.discard(code) # 전량 매도 시 캐시 클리어
                        db_manager.db.delete_trailing_stop(code)
                        with self._lock:
                            if code in self.trailing_stop_cache: # 캐시 삭제
                                del self.trailing_stop_cache[code]

    def _check_buy_conditions(self, holdings, deposit_res, is_market_open=True):
        # [수정] 매수 대상 확장을 위해 국내 주식 및 국내 ETF 리스트 병합
        targets = config.session.stock_data.get("stocks_kr", []) + config.session.stock_data.get("etfs_kr", [])
        if not targets: return
        
        # [추가] 필터링 카운트 초기화 (매 주기마다 갱신)
        self.skipped_by_market_filter_count = 0
        skipped_stocks = [] # [추가] 시장 필터링으로 보류된 종목 리스트
        
        # [추가] 보유 종목 조회 (중복 매수 방지)
        holding_codes = set()
        if holdings:
            for h in holdings:
                holding_codes.add(h['pdno'])
        
        # [수정] 최대 보유 종목 수 체크 (투자 비중에 따라 자동 계산)
        invest_ratio = getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1)
        if invest_ratio <= 0: invest_ratio = 0.1 # 0 이하일 경우 기본값 10%

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

        # 1. 후보 분석
        candidates = self._analyze_candidates(targets, holding_codes, rules_map)
        
        # 2. 매수 집행
        if candidates:
            if not is_market_open:
                self.log(f"[장마감] 매수 후보 감지 (주문 미전송): {len(candidates)}종목")
                for cand in candidates:
                     self.log(f"   - {cand['name']} ({cand['score']}점)")
                return

            self._execute_buy_orders(candidates, avail_cash, invest_ratio, len(holding_codes), max_holdings)

    def _analyze_candidate_worker(self, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay):
        """(내부함수) 매수 후보 분석용 단일 워커"""
        try:
            # [추가] 시스템 트레이딩 스레드임을 마킹 (API 우선순위 획득용)
            context.trade_context.is_system_trading = True
            
            # [추가] API 호출 전 대기 (Rate Limit 방지 - 스레드별 분산 효과)
            time.sleep(safe_delay)
            
            code = item['code']; name = item['name']
            
            # 1. 트레이딩 제한 종목 체크
            if code in restricted_stocks:
                return {'type': 'restricted_skip', 'name': name}
            
            # 2. 진행 중인 주문 체크
            if self.order_manager.is_pending(code):
                return None

            # 3. 보유 종목 체크
            if code in holding_codes: return None
            
            # 4. 시장 지수 필터링 (종목별 적용)
            if getattr(config, 'USE_MARKET_FILTER', True):
                market_type = self._get_stock_market_type(code)
                market_stat = self.market_index_status.get(market_type)
                if market_stat and isinstance(market_stat, dict):
                    if not market_stat.get('is_healthy', True):
                        return {'type': 'market_skip', 'name': name}
            
            # 5. 데이터 조회 및 분석
            df = api.get_chart_data(code, is_overseas=False)
            if df is None or df.empty: return None
            
            current_price = float(df.iloc[-1]['close'])
            
            # 실시간 체결강도 조회 (매수 유력 시 호출하는 게 좋지만, 로직 단순화를 위해 수행)
            # [최적화] 여기서 에러나도 무시하고 진행
            vol_strength = None
            try: vol_strength = api.get_realtime_vol_strength(code)
            except: pass
            
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
                    "WEIGHTS": rule.get('weights')
                }
            else:
                thresholds = {
                    "BUY_SCORE": base_buy_score + score_adj,
                    "WEIGHTS": config.SCORING_WEIGHTS
                }
            
            # 전략 실행
            result = self.strategy.analyze_buy(code, name, df, current_price, vol_strength=vol_strength, thresholds=thresholds)
            if not result: return None
            
            # 로그 출력을 위한 문자열 구성
            rsi_val = f"{result['rsi']:.1f}" if result['rsi'] is not None else "-"
            adx_val = f"{result['adx']:.1f}" if result['adx'] is not None else "-"
            cci_val = f"{result['cci']:.1f}" if result['cci'] is not None else "-"
            sar_val = result.get('psar')
            sar_str = "상승" if sar_val and current_price > sar_val else "하락"
            macd_val = result.get('macd'); sig_val = result.get('macd_signal')
            macd_str = "골든" if macd_val is not None and sig_val is not None and macd_val > sig_val else "데드"
            obv_trend = result.get('obv_trend')
            obv_str = "상승" if obv_trend is True else ("하락" if obv_trend is False else "-")
            vol_val = f"{result['vol_strength']:.1f}%" if result.get('vol_strength') else "-"
            rule_msg = " [개별 룰 적용]" if rule else ""
            
            log_msg = f"[분석] {name}({code}): 현재가={current_price:,.0f}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_val}, ADX={adx_val}, CCI={cci_val}, OBV={obv_str}, SAR={sar_str}, MACD={macd_str}, 체결={vol_val}{rule_msg}"
            
            if result['action'] == "buy":
                candidate_data = {
                    'code': code, 'name': name, 'price': current_price,
                    'score': result['score'], 'rsi': result['rsi'], 'adx': result['adx'], 'cci': result['cci'], 'atr': result.get('atr', 0), 'vol_strength': result.get('vol_strength'),
                    'is_custom_rule': bool(rule), 'rule': rule
                }
                return {'type': 'candidate', 'data': candidate_data, 'log': log_msg}
            else:
                return {'type': 'log_only', 'log': log_msg}
        except Exception: return None

    def _analyze_candidates(self, targets, holding_codes, rules_map):
        candidates = []
        skipped_stocks = []
        restricted_skipped_stocks = [] # [추가] 트레이딩 제한 스킵 리스트
        
        # [추가] 트레이딩 제한 종목 로드
        restricted_stocks = load_restricted_stocks()
        
        # [추가] Rate Limit 준수를 위한 딜레이 설정
        tps = config.SIM_TX_PER_SECOND if config.session.is_simulation else config.REAL_TX_PER_SECOND
        safe_delay = (1.0 / tps) * 1.2  # 20% 여유 버퍼
        
        # [추가] 시장 국면 판단 (적응형 임계값용)
        market_regime_adj = {} # Market Type -> Score Adj
        if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
            for m_type in ["KOSPI", "KOSDAQ"]:
                regime, adj = analysis.get_market_regime(m_type)
                market_regime_adj[m_type] = adj
                if self.consecutive_errors == 0:
                    self.log(f"[{m_type}] 시장 국면: {regime} (매수기준 {adj:+.1f}점)")

        # [병렬 처리] 사용자 작업과의 충돌 및 모의투자 API 제한(2 TPS) 고려
        # (실전: 5개, 모의: 1개 - 순차 처리로 안정성 확보)
        max_workers = 5 if not config.session.is_simulation else 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._analyze_candidate_worker, item, holding_codes, rules_map, restricted_stocks, market_regime_adj, safe_delay) for item in targets]
            
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
                        self.skipped_by_market_filter_count += 1
                        skipped_stocks.append(res['name'])

        # [추가] 트레이딩 제한 종목 스킵 로그 기록
        if restricted_skipped_stocks:
            self.log(f"[매수 스킵] 트레이딩 제한 종목 ({len(restricted_skipped_stocks)}개): {', '.join(restricted_skipped_stocks)}")

        # [추가] 시장 필터링 보류 종목 로그 기록
        if skipped_stocks:
            self.log(f"[시장 필터링] 하락장 매수 보류 ({len(skipped_stocks)}종목): {', '.join(skipped_stocks)}")

        # [수정] 우선순위 정렬 (1. 점수 높은 순, 2. RSI 낮은 순)
        # 점수가 같다면 RSI가 낮을수록 상승 여력이 있다고 판단하여 우선순위를 둡니다.
        candidates.sort(key=lambda x: (-x['score'], x['rsi']))
        
        # [추가] 선정된 후보군 우선순위 로그 출력
        if candidates:
            log_cnt = min(len(candidates), 3)
            self.log(f"[매수 후보 선정] 총 {len(candidates)}종목 중 우선순위 상위 {log_cnt}개:")
            for i in range(log_cnt):
                c = candidates[i]
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

            # 최소 주문 금액 보정 (너무 적으면 1주라도 살 수 있게)
            if invest_amt < cand['price']: invest_amt = avail_cash
            
            # [수정] 지정가 주문을 위해 현재가(정수) 확보
            current_price = int(cand['price'])

            # [수정] 슬리피지 비율 적용 및 호가 정렬 (체결 확률 증대)
            raw_order_price = current_price * (1 + config.SLIPPAGE_RATE)
            order_price = int(utils.adjust_to_tick(raw_order_price, is_overseas=False))
            
            # [수정] 단순 계산 대신 API를 통해 정확한 매수 가능 수량 조회
            # 지정가 주문 시 해당 가격 기준으로 조회
            max_qty = api.fetch_buyable_quantity(cand['code'], order_price)
            
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
            
            rsi_val = f"{cand['rsi']:.1f}" if cand['rsi'] else "-"
            adx_val = f"{cand['adx']:.1f}" if cand['adx'] else "-"
            cci_val = f"{cand['cci']:.1f}" if cand.get('cci') else "-"
            vol_val = f"{cand['vol_strength']:.1f}%" if cand.get('vol_strength') else "-"
            
            # [수정] 사유 포맷 변경 (줄바꿈 제거)
            reason = "조건 만족"
            if cand.get('is_custom_rule'):
                reason += " [개별 룰 적용]"
            
            reason += f" [점수:{cand['score']}, RSI:{rsi_val}, 체결강도:{vol_val}]"
            
            atr_msg = ""
            if atr_val > 0 and price_val > 0:
                annual_vol = (atr_val / price_val) * math.sqrt(252) * 100
                atr_msg += f"[ATR:{int(atr_val):,}/변동성:{annual_vol:.1f}%]"
            
            if use_atr_stop:
                if atr_msg: atr_msg += " "
                atr_msg += f"[ATR손절:{sl_rate:.2f}%]"
            
            if atr_msg:
                reason += f" {atr_msg}"

            self.log(f"매수 실행: {cand['name']} - {reason}")
            # [수정] 매수 시 사유와 점수, 그리고 지정가 가격을 DB 저장을 위해 전달
            odno = self.order_manager.send_order(cand['code'], qty, "buy", name=cand['name'], reason=reason, score=cand['score'], price=order_price, rule=cand.get('rule'), stop_loss_rate=sl_rate)
            if odno: 
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
        
        ma_period = getattr(config, 'MARKET_FILTER_MA', 20)
        
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
        if code in self.stock_market_map: return self.stock_market_map[code]
        
        try:
            res = api.get_current_price_data(code, is_overseas=False)
            if res and res.get('rt_cd') == '0':
                market_name = res['output'].get('rprs_mrkt_kor_name', '')
                if "코스닥" in market_name:
                    self.stock_market_map[code] = "KOSDAQ"
                    return "KOSDAQ"
                elif "유가증권" in market_name or "KOSPI" in market_name:
                    self.stock_market_map[code] = "KOSPI"
                    return "KOSPI"
        except: pass
        
        return "KOSPI" # 기본값

def _select_stock_for_rules():
    """룰 설정을 위한 종목 선택 헬퍼"""
    console.print("\n[bold]개별 설정할 대상을 선택하세요:[/bold]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 국내 주식", "(Domestic Stock)")
    grid.add_row("[2] 국내 ETF", "(Domestic ETF)")
    grid.add_row("[3] 미국 주식", "(US Stock)")
    grid.add_row("[4] 미국 ETF", "(US ETF)")
    grid.add_row("[5] 직접 입력", "(Direct Input)")
    console.print(grid)
    console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "q"], default="5")
    if choice.lower() == 'q': return None, None, False

    code, name, is_overseas = None, None, False
    
    if choice == '5':
        raw_input = Prompt.ask("종목코드(6자리/티커) 입력 [dim](취소: q)[/dim]")
        if raw_input and raw_input.lower() != 'q':
            if raw_input.isdigit() and len(raw_input) == 6:
                code = raw_input
                name = api.get_stock_name_by_code(code, False) or code
                is_overseas = False
            else:
                code = raw_input.upper()
                name = api.get_stock_name_by_code(code, True) or code
                is_overseas = True
    elif choice in ["1", "2", "3", "4"]:
        key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
        s_list = config.session.stock_data.get(key_map[choice], [])
        if s_list:
            for i, s in enumerate(s_list):
                console.print(f"[{i+1}] {s['name']} ({s['code']})")
            console.print()
            sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
            if sel.lower() != 'q' and sel.isdigit() and 1 <= int(sel) <= len(s_list):
                item = s_list[int(sel)-1]
                code, name = item['code'], item['name']
                is_overseas = (choice in ["3", "4"])
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
    table.add_column("매수(점수/RSI/체결)", justify="center")
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

        table.add_row(
            f"{r['name']}({r['code']})",
            f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', 100.0)}%",
            f"+{r['take_profit']}% / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
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
    console.print(f"\n[bold green]선택 종목: {name} ({code})[/bold green]")
    
    # [추가] 현재가 조회 (예상 가격 계산용)
    is_overseas = not (code.isdigit() and len(code) == 6)
    current_price = 0
    # [수정] 단순 조회이므로 status 사용
    with console.status("[bold green]현재가 조회 중...[/]"):
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
        "sell_score": config.SELL_STRATEGY["SELL_SCORE"],
        "stop_loss": config.SELL_STRATEGY["STOP_LOSS_RATE"],
        "take_profit": config.SELL_STRATEGY["TAKE_PROFIT_RATE"],
        "take_profit_rsi": config.SELL_STRATEGY["TAKE_PROFIT_RSI"],
        "ts_activation": config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0),
        "ts_callback": config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0),
        "memo": "",
        "weights": None,
        "invest_ratio": getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1),
        "time_stop_days": config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10),
        "use_atr_stop": 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0,
        "atr_stop_multiplier": config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    }
    
    current = existing if existing else defaults.copy()
    
    # [추가] 기존 데이터에 신규 필드가 누락된 경우 기본값으로 채움 (DB 스키마 변경 대응)
    for key, val in defaults.items():
        if key not in current:
            current[key] = val
            
    # DB에서 가져온 값이 None일 경우 빈 문자열로 처리
    if 'memo' not in current or current['memo'] is None:
        current['memo'] = ""
    
    console.print("\n[설정값 입력 (Enter: 현재값 유지)]")
    
    new_strategy = {}
    
    class QuitInput(Exception): pass

    def ask_val(key, desc, type_func):
        val = Prompt.ask(f"{desc} [dim](현재: {current[key]})[/dim]", default=str(current[key]))
        if val.lower() == 'q': raise QuitInput()
        return type_func(val)

    try:
        console.print("\n[bold]1. 기본 매수 및 청산 타점 설정[/bold]")
        new_strategy['buy_score'] = ask_val('buy_score', "매수 기준 점수 (종합 점수)", float)
        new_strategy['buy_rsi'] = ask_val('buy_rsi', "매수 허용 RSI 상한", float)
        new_strategy['buy_vol_strength'] = ask_val('buy_vol_strength', "매수 체결강도 하한(%)", float)
        new_strategy['take_profit'] = ask_val('take_profit', "목표 익절 수익률(%)", float)
        new_strategy['take_profit_rsi'] = ask_val('take_profit_rsi', "과열 매도 RSI 기준", float)
        new_strategy['ts_activation'] = ask_val('ts_activation', "트레일링 스탑 발동 수익률(%)", float)
        new_strategy['ts_callback'] = ask_val('ts_callback', "트레일링 스탑 하락 감지율(%)", float)
        new_strategy['time_stop_days'] = ask_val('time_stop_days', "시간 청산 기한 (보유 허용 일수)", int)
            
        console.print("\n[bold]2. 리스크 관리 및 자산 비중 설정[/bold]")
        curr_ratio_pct = current.get('invest_ratio', getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1)) * 100
        val = Prompt.ask(f"종목별 투자 비중(%) [dim](현재: {curr_ratio_pct:.0f})[/dim]", default=str(int(curr_ratio_pct)))
        if val.lower() == 'q': raise QuitInput()
        new_strategy['invest_ratio'] = float(val) / 100.0
        
        curr_use_atr = "y" if current.get('use_atr_stop', 1 if config.SELL_STRATEGY.get("USE_ATR_STOP", True) else 0) else "n"
        val = Prompt.ask(f"손절 방식 선택 (y: 개별 ATR 손절 / n: 고정 손절률) [dim](현재: {curr_use_atr})[/dim]", choices=["y", "n", "q"], default=curr_use_atr)
        if val.lower() == 'q': raise QuitInput()
        use_atr = (val.lower() == 'y')
        new_strategy['use_atr_stop'] = 1 if use_atr else 0
        
        if use_atr:
            new_strategy['atr_stop_multiplier'] = ask_val('atr_stop_multiplier', "ATR 손절 배수 (기본 2.0)", float)
            new_strategy['stop_loss'] = current.get('stop_loss', defaults['stop_loss']) # 고정 손절률은 숨김
        else:
            new_strategy['stop_loss'] = ask_val('stop_loss', "고정 손절 수익률(%) (예: -5.0)", float)
            new_strategy['atr_stop_multiplier'] = current.get('atr_stop_multiplier', defaults['atr_stop_multiplier']) # 배수는 숨김
            
        # 숨김 처리된 세부 지표는 기존 값 또는 기본값 유지
        new_strategy['sell_score'] = current.get('sell_score', defaults['sell_score'])
        
        # [추가] 가중치 설정 입력
        console.print("\n[스코어링 가중치 설정]")
        curr_weights = current.get('weights')
        
        while True:
            # 현재 설정값 또는 전역 설정값 로드
            temp_weights = curr_weights.copy() if curr_weights else config.SCORING_WEIGHTS.copy()
            
            console.print("[dim]순서: 추세 / 모멘텀 / 강도 / 시너지 (합계 10.0점 설정)[/dim]")
            
            def ask_weight(key, desc, default_val):
                v = Prompt.ask(f"{desc} [dim](현재: {default_val})[/dim]", default=str(default_val))
                if v.lower() == 'q': raise QuitInput()
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
        new_strategy['memo'] = ask_val('memo', "메모 (Memo)", str)
        
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
        
        table.add_row("매수 타점", f"점수 {new_strategy['buy_score']}점↑ / RSI {new_strategy['buy_rsi']}↓ / 체결 {new_strategy['buy_vol_strength']}%↑")
        table.add_row("청산 타점", f"익절 +{new_strategy['take_profit']}% / 과열 RSI {new_strategy['take_profit_rsi']}↑ / 시간청산 {new_strategy['time_stop_days']}일")
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
    if not code: return
    _input_and_save_rule(code, name)

def _modify_stock_rules():
    """룰 변경 (기존 목록에서 선택)"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    custom_rules = _enrich_rules_with_weights(custom_rules) # [추가] 가중치 보강
    if not custom_rules:
        console.print("\n[yellow]저장된 개별 룰이 없습니다.[/yellow]")
        return

    console.print("\n[bold]변경할 룰을 선택하세요:[/bold]")
    
    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("No.", justify="right", style="cyan", width=4)
    table.add_column("종목명(코드)", justify="left")
    table.add_column("매수(점수/RSI/체결)", justify="center")
    table.add_column("청산(익절/TS/RSI/기한)", justify="center")
    table.add_column("리스크(비중/손절)", justify="center")
    table.add_column("가중치", justify="center")
    table.add_column("수정일", justify="center", style="dim")
    
    for i, r in enumerate(custom_rules):
        w_str = "기본"
        if r.get('weights'):
            w = r['weights']
            if isinstance(w, str):
                try: w = json.loads(w)
                except: pass
            if isinstance(w, dict):
                w_str = f"{w.get('TREND',0):.1f}/{w.get('MOMENTUM',0):.1f}/{w.get('STRENGTH',0):.1f}/{w.get('SYNERGY',0):.1f}"
                
        sl_str = f"ATR(x{r.get('atr_stop_multiplier', 2.0)})" if r.get('use_atr_stop') else f"{r['stop_loss']}%"
        ratio_str = f"{r.get('invest_ratio', getattr(config, 'SYSTEM_INVEST_PER_STOCK', 0.1)) * 100:.0f}%"

        table.add_row(
            str(i+1),
            f"{r['name']} ({r['code']})",
            f"{r['buy_score']}점 / {r.get('buy_rsi', 65.0)} / {r.get('buy_vol_strength', 100.0)}%",
            f"+{r['take_profit']}% / TS(+{r['ts_activation']}/-{r['ts_callback']}) / {r.get('take_profit_rsi', 75.0)} / {r.get('time_stop_days', 10)}일",
            f"{ratio_str} / {sl_str}",
            w_str,
            r['updated_at']
        )
        if (i + 1) % 5 == 0 and (i + 1) < len(custom_rules):
            table.add_section()
            
    console.print(table)
    
    sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]", default="q")
    if sel.lower() == 'q': return
    
    if sel.isdigit() and 1 <= int(sel) <= len(custom_rules):
        target = custom_rules[int(sel)-1]
        _input_and_save_rule(target['code'], target['name'])
    else:
        console.print("[red]잘못된 번호입니다.[/red]")

def _delete_stock_rules():
    """룰 삭제"""
    custom_rules = db_manager.db.get_all_stock_strategies()
    if not custom_rules:
        console.print("\n[yellow]삭제할 룰이 없습니다.[/yellow]")
        return

    console.print("\n[bold]삭제할 룰을 선택하세요:[/bold]")
    for i, r in enumerate(custom_rules):
        console.print(f"[{i+1}] {r['name']} ({r['code']})")
    
    console.print()
    sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
    if sel.lower() == 'q': return
    
    if sel.isdigit() and 1 <= int(sel) <= len(custom_rules):
        target = custom_rules[int(sel)-1]
        if Prompt.ask(f"정말 '{target['name']}'의 룰을 삭제하시겠습니까?", choices=["y", "n"], default="n") == "y":
            db_manager.db.delete_stock_strategy(target['code'])
            console.print(f"\n[bold green]삭제되었습니다.[/bold green]")
    else:
        console.print("[red]잘못된 번호입니다.[/red]")

def _view_restricted_stocks():
    """트레이딩 제한 종목 목록 및 후행지표 조회"""
    data = load_restricted_stocks()
    if not data:
        console.print("\n[yellow]트레이딩 제한 종목이 없습니다.[/yellow]")
        return

    # [추가] 적응형 임계값 준비
    market_regime_adj = {}
    use_adaptive = False
    if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
        try:
            with console.status("[dim]시장 국면 분석 중...[/dim]"):
                _, kospi_adj = analysis.get_market_regime("KOSPI")
                _, kosdaq_adj = analysis.get_market_regime("KOSDAQ")
                market_regime_adj["KOSPI"] = kospi_adj
                market_regime_adj["KOSDAQ"] = kosdaq_adj
                use_adaptive = True
        except:
            use_adaptive = False

    console.print()
    title = "트레이딩 제한 종목"
    if use_adaptive:
        title += " [bold magenta](*)[/]"
    table = Table(title=title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목명(코드)", justify="left")
    table.add_column("현재가", justify="right")
    table.add_column("메모", justify="left")
    table.add_column("점수", justify="center")
    table.add_column("상태", justify="center")
    table.add_column("추세SMO", justify="center")
    table.add_column("RSI", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("CCI", justify="right")
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
        task = progress.add_task("[green]데이터 조회 및 지표 계산 중...[/]", total=len(data))

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
            score_str = "-"
            state_str = "-"
            trend_str = "-"
            rsi_str = "-"
            adx_str = "-"
            cci_str = "-"
            
            if df is not None and not df.empty:
                current_price = float(df.iloc[-1]['close'])
                price_str = f"{int(current_price):,}" if not is_overseas else f"{current_price:,.2f}"
                
                ind = indicators.calculate_indicators(df)
                
                # 전일 RSI 계산 (상태 분류용)
                prev_rsi = None
                if len(df) >= 16:
                    delta = df['close'].diff()
                    gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                    loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                    try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
                    except: pass

                # [추가] 적응형 임계값 적용
                thresholds = None
                if use_adaptive and not is_overseas:
                    market_type = AutoTrader()._get_stock_market_type(code)
                    score_adj = market_regime_adj.get(market_type, 0.0)
                    
                    if score_adj != 0:
                        thresholds = {
                            "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj,
                            "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                            "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
                        }

                # 상태 및 점수 계산
                state, state_color, _ = analysis.classify_stock_state(
                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                    ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'), thresholds=thresholds
                )
                
                score, _ = analysis.calculate_score(
                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                    ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
                )
                
                s_color = state_color.replace('[', '').replace(']', '')
                score_str = f"[{s_color}]{score}점[/]"
                state_str = f"[{s_color}]{state}[/]"
                
                # 추세SMO (SAR/MACD/OBV)
                sar_val = ind.get('psar')
                sar_icon = "[red]⬆[/]" if sar_val and current_price > sar_val else "[blue]⬇[/]"
                
                macd_val = ind.get('macd')
                sig_val = ind.get('macd_signal')
                macd_icon = "-"
                if macd_val is not None and sig_val is not None:
                    zero_sign = "+" if macd_val > 0 else "-"
                    cross_char = "G" if macd_val > sig_val else "D"
                    m_color = "red" if macd_val > sig_val else "blue"
                    macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"
                
                obv_trend = ind.get('obv_trend')
                obv_icon = "-"
                if obv_trend is True: obv_icon = "[red]▲[/]"
                elif obv_trend is False: obv_icon = "[blue]▼[/]"
                
                trend_str = f"{sar_icon} {macd_icon} {obv_icon}"

                # 지표 포맷팅
                rsi_val = ind['rsi']
                rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
                if rsi_val is not None:
                    if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[magenta]{rsi_str}[/]"
                    elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[red]{rsi_str}[/]"
                    elif 45 <= rsi_val < 55: rsi_str = f"[orange3]{rsi_str}[/]"
                    elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: rsi_str = f"[yellow]{rsi_str}[/]"
                    else: rsi_str = f"[blue]{rsi_str}[/]"

                adx_val = ind['adx']
                adx_str = f"{adx_val:.1f}" if adx_val is not None else "-"
                if adx_val is not None:
                    if adx_val >= 40: adx_str = f"[magenta]{adx_str}[/]" 
                    elif adx_val >= 30: adx_str = f"[red]{adx_str}[/]"     
                    elif adx_val >= 20: adx_str = f"[orange3]{adx_str}[/]"
                    elif adx_val >= 15: adx_str = f"[yellow]{adx_str}[/]"
                    else: adx_str = f"[white]{adx_str}[/]"

                cci_val = ind['cci']
                cci_str = f"{cci_val:.1f}" if cci_val is not None else "-"
                if cci_val is not None:
                    if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[red]{cci_str}[/]"
                    elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[orange3]{cci_str}[/]"
                    elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: cci_str = f"[yellow]{cci_str}[/]"
                    else: cci_str = f"[blue]{cci_str}[/]"
            
            table.add_row(f"{name}({code})", price_str, memo, score_str, state_str, trend_str, rsi_str, adx_str, cci_str, reg_date)
            progress.advance(task)

    console.print(table)
    
    if use_adaptive:
        console.print("[dim](*) 적응형 임계값(시장 국면 보정)이 적용된 결과입니다.[/dim]")

def _add_restricted_stock():
    """트레이딩 제한 종목 추가"""
    code, name, is_overseas = _select_stock_for_rules()
    if not code: return
    
    data = load_restricted_stocks()
    if code in data:
        console.print(f"\n[yellow]이미 제한 목록에 있는 종목입니다.[/yellow]")
        if Prompt.ask("메모를 수정하시겠습니까?", choices=["y", "n"], default="y") == "n":
            return
            
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
        return

    console.print()
    table = Table(title="트레이딩 제한 해제 대상 목록", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("No.", justify="right", style="cyan", width=4)
    table.add_column("종목명(코드)", justify="left")
    table.add_column("현재가", justify="right")
    table.add_column("메모", justify="left")
    table.add_column("점수", justify="center")
    table.add_column("상태", justify="center")
    table.add_column("추세SMO", justify="center")
    table.add_column("RSI", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("CCI", justify="right")
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
        task = progress.add_task("[green]데이터 조회 및 지표 계산 중...[/]", total=len(codes))

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
            score_str = "-"
            state_str = "-"
            trend_str = "-"
            rsi_str = "-"
            adx_str = "-"
            cci_str = "-"
            
            if df is not None and not df.empty:
                current_price = float(df.iloc[-1]['close'])
                price_str = f"{int(current_price):,}" if not is_overseas else f"{current_price:,.2f}"
                
                ind = indicators.calculate_indicators(df)
                
                # 전일 RSI 계산 (상태 분류용)
                prev_rsi = None
                if len(df) >= 16:
                    delta = df['close'].diff()
                    gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                    loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                    try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
                    except: pass

                # 상태 및 점수 계산
                state, state_color, _ = analysis.classify_stock_state(
                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                    ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
                )
                
                score, _ = analysis.calculate_score(
                    current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                    ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
                )
                
                s_color = state_color.replace('[', '').replace(']', '')
                score_str = f"[{s_color}]{score}점[/]"
                state_str = f"[{s_color}]{state}[/]"
                
                # 추세SMO (SAR/MACD/OBV)
                sar_val = ind.get('psar')
                sar_icon = "[red]⬆[/]" if sar_val and current_price > sar_val else "[blue]⬇[/]"
                
                macd_val = ind.get('macd')
                sig_val = ind.get('macd_signal')
                macd_icon = "-"
                if macd_val is not None and sig_val is not None:
                    zero_sign = "+" if macd_val > 0 else "-"
                    cross_char = "G" if macd_val > sig_val else "D"
                    m_color = "red" if macd_val > sig_val else "blue"
                    macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"
                
                obv_trend = ind.get('obv_trend')
                obv_icon = "-"
                if obv_trend is True: obv_icon = "[red]▲[/]"
                elif obv_trend is False: obv_icon = "[blue]▼[/]"
                
                trend_str = f"{sar_icon} {macd_icon} {obv_icon}"

                # 지표 포맷팅
                rsi_val = ind['rsi']
                rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
                if rsi_val is not None:
                    if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[magenta]{rsi_str}[/]"
                    elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[red]{rsi_str}[/]"
                    elif 45 <= rsi_val < 55: rsi_str = f"[orange3]{rsi_str}[/]"
                    elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: rsi_str = f"[yellow]{rsi_str}[/]"
                    else: rsi_str = f"[blue]{rsi_str}[/]"

                adx_val = ind['adx']
                adx_str = f"{adx_val:.1f}" if adx_val is not None else "-"
                if adx_val is not None:
                    if adx_val >= 40: adx_str = f"[magenta]{adx_str}[/]" 
                    elif adx_val >= 30: adx_str = f"[red]{adx_str}[/]"     
                    elif adx_val >= 20: adx_str = f"[orange3]{adx_str}[/]"
                    elif adx_val >= 15: adx_str = f"[yellow]{adx_str}[/]"
                    else: adx_str = f"[white]{adx_str}[/]"

                cci_val = ind['cci']
                cci_str = f"{cci_val:.1f}" if cci_val is not None else "-"
                if cci_val is not None:
                    if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[red]{cci_str}[/]"
                    elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[orange3]{cci_str}[/]"
                    elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: cci_str = f"[yellow]{cci_str}[/]"
                    else: cci_str = f"[blue]{cci_str}[/]"
            
            table.add_row(str(i+1), f"{name}({code})", price_str, memo, score_str, state_str, trend_str, rsi_str, adx_str, cci_str, reg_date)
            progress.advance(task)
        
    console.print(table)
    console.print()
    
    choice = Prompt.ask("해제할 번호 선택 [dim](취소: q)[/dim]")
    if choice.lower() == 'q': return
    
    if choice.isdigit() and 1 <= int(choice) <= len(codes):
        target_code = codes[int(choice)-1]
        target_name = data[target_code]['name']
        del data[target_code]
        save_restricted_stocks(data)
        console.print(f"\n[green]'{target_name}' 종목이 제한 목록에서 해제되었습니다.[/green]")
        
        console.print("\n[bold cyan]>> 현재 설정된 트레이딩 제한 종목 리스트입니다.[/bold cyan]")
        _view_restricted_stocks()

def manage_stock_rules():
    """종목별 트레이딩 룰 관리 메뉴"""
    console.print("\n[bold cyan]=== 종목별 트레이딩 룰 관리 (Manage Stock Rules) ===[/]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 룰 조회", "(View)")
    grid.add_row("[2] 룰 설정", "(Set)")
    grid.add_row("[3] 룰 변경", "(Modify)")
    grid.add_row("[4] 룰 삭제", "(Delete)")
    console.print(grid)
    console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "q"], default="1")
    if choice.lower() == 'q': return

    if choice == "1":
        _view_stock_rules()
    elif choice == "2":
        _set_stock_rules()
    elif choice == "3":
        _modify_stock_rules()
    elif choice == "4":
        _delete_stock_rules()

def manage_restricted_stocks_menu():
    """트레이딩 제한 종목 관리 메뉴"""
    console.print("\n[bold cyan]=== 트레이딩 제한 종목 관리 (Restricted Stocks) ===[/]")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 제한 종목 조회", "(List)")
    grid.add_row("[2] 제한 종목 추가", "(Add)")
    grid.add_row("[3] 제한 종목 해제", "(Remove)")
    console.print(grid)
    console.print()
    
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "q", "Q"], default="1")
    if choice.lower() == 'q': return
    
    if choice == "1": _view_restricted_stocks()
    elif choice == "2": _add_restricted_stock()
    elif choice == "3": _remove_restricted_stock()

def system_trading_menu():
    """시스템 트레이딩 메뉴"""

    trader = AutoTrader()

    console.print("\n[bold yellow]=== 시스템 트레이딩 (System Trading) ===[/]")
    console.print("[dim]안내: 시스템 트레이딩은 '국내주식' 및 '국내ETF' 리스트를 대상으로 작동합니다.[/dim]")
    console.print(f"현재 상태: {'[green]실행 중[/green]' if trader.is_running else '[red]중지됨[/red]'}")
    console.print()
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="left", style="dim")
    grid.add_row("[1] 트레이딩 실행", "(Start)")
    grid.add_row("[2] 트레이딩 중단", "(Stop)")
    grid.add_row("[3] 트레이딩 상태", "(Status)")
    grid.add_row("[4] 트레이딩 평가", "(Report)")
    grid.add_row("[5] 트레이딩 로그", "(Log Viewer)")
    grid.add_row("[6] 종목별 트레이딩 룰", "(Rule)")
    grid.add_row("[7] 트레이딩 제한 종목", "(Restrict)")
    console.print(grid)
    console.print()
    
    try:
        choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "6", "7", "q"], default="3")
        
        menu_map = {"1": "실행", "2": "중단", "3": "상태", "4": "평가", "5": "로그", "6": "룰설정", "7": "거래제한"}
        if choice in menu_map:
            context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")
            
    except KeyboardInterrupt:
        console.print()
        return

    if choice.lower() == 'q': return
    
    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
    
    if choice == "1":
        trader.start()
    elif choice == "2":
        trader.stop()
    elif choice == "3":
        trader.print_status()
    elif choice == "4":
        trader.print_report()
    elif choice == "5":
        trader.view_log_file()
    elif choice == "6":
        manage_stock_rules()
    elif choice == "7":
        manage_restricted_stocks_menu()