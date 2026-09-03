# modules/auto_trade/conclusion.py
"""ConclusionMonitor: 주문 체결 감시 및 확정 처리 (WS 체결통보 + 폴링)

기존 modules/auto_trade.py 에서 분해. 외부 인터페이스는 패키지(__init__)가 재수출한다.
"""
import threading
import concurrent.futures
import logging
import time
import requests
import json
from core import jsonio
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
from core import context # [추가]
import api
from core import utils
from core import indicators
from modules import analysis, account # [수정] account 모듈 재사용
import math # [추가] math 모듈
from modules import db_manager # [추가] DB 매니저
from core import trading_cost # [추가] 거래비용 단일 계산
from modules import chart # [추가] 차트 모듈
import re # [추가] 정규식 모듈
import pandas as pd

from modules.auto_trade.common import (OrderStatus, _current_account_type, _enrich_rules_with_weights, _get_trade_account, _norm_odno, add_restricted_stock, is_system_market_open, is_system_odno, is_system_trade, remove_restricted_stock)

console = config.console

logger = logging.getLogger(__name__)


def _recalc_realized(origin_trade, fill_price, fill_qty, is_overseas, fallback_amt, fallback_rate):
    """주문 시점 추정 손익을 '실제 체결가 + 왕복 비용' 기준으로 다시 계산한다.

    [왜] 종전에는 매도 발주 시점의 평가손익(KIS evlu_pfls_amt)이 그대로 실현손익으로
      굳었다. 체결 확인 단계에서 실제 체결가를 이미 알고 있으면서도 손익만 추정치로
      남겨, 승률·손익비·거래 평가가 모두 체계적으로 낙관 방향이었다. 특히 총이익이
      매도비용 부근인 거래는 실제로는 손실인데 '승'으로 집계됐다.

    매수 주문이거나 매입가를 모르면(구버전 기록 등) 기존 값을 그대로 둔다 — 없는
    정보를 추측해 덮어쓰면 조용히 틀린 숫자가 된다.
    """
    try:
        if not origin_trade:
            return fallback_amt, fallback_rate
        type_str = str(origin_trade.get('type') or '')
        if 'sell' not in type_str.lower() and '매도' not in type_str:
            return fallback_amt, fallback_rate

        buy_price = float(origin_trade.get('buy_price') or 0)
        fill_price = float(fill_price or 0)
        fill_qty = int(fill_qty or 0)
        if buy_price <= 0 or fill_price <= 0 or fill_qty <= 0:
            return fallback_amt, fallback_rate

        amt, rate = trading_cost.net_realized_profit(buy_price, fill_price, fill_qty, is_overseas)
        return int(amt), rate
    except Exception as e:
        logger.debug(f"[비용] 실현손익 재계산 실패 — 주문 시점 값 유지: {e}")
        return fallback_amt, fallback_rate


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자.

    분해 전에는 모듈 전역 조회였던 상호 호출을 패키지 속성 조회로 유지해,
    테스트의 patch('modules.auto_trade.X') 가 분해 전과 동일하게 내부 호출에도
    적용되도록 한다. (지연 import라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class ConclusionMonitor:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConclusionMonitor, cls).__new__(cls)
            cls._instance._lock = threading.RLock() # [추가] 스레드 동기화 락
            cls._instance.is_running = False
            cls._instance.thread = None
            cls._instance.order_status = {} # 주문별 체결 수량 추적 {계좌-주문번호: qty}
            cls._instance.cancel_status = {} # [추가] 주문별 취소 수량 추적 {계좌-주문번호: qty}
            cls._instance.processed_sim_fills = set() # [추가] 모의투자 중복 알림 방지 캐시
            cls._instance.paper_backfill_done = False # [추가] 가상투자 당일 원장 1회 복구 여부
            
            # [수정] 적응형 폴링 설정 로드
            cls._instance.active_interval = getattr(config, 'CONCLUSION_CHECK_INTERVAL', 5)
            cls._instance.idle_interval = getattr(config, 'CONCLUSION_CHECK_IDLE_INTERVAL', 300)
            cls._instance.active_duration = getattr(config, 'CONCLUSION_CHECK_ACTIVE_DURATION', 60)
            cls._instance.active_until = 0 # 집중 감시 유지 만료 시간
            
            cls._instance.event = threading.Event() # 즉시 실행 트리거용
            # [추가] 종료 신호. 감시 루프 밖에서 띄우는 보조 스레드(제한 해제 확인 등)가
            #  sleep 대신 이 이벤트를 기다리게 해서, stop() 한 번으로 같이 끝나게 한다.
            #  daemon 스레드는 프로세스가 죽을 때까지 살아 있어서, 종료 후에도 잔고를
            #  조회하고 제한 목록을 건드릴 수 있다(테스트에서는 다음 테스트의 patch 구간을
            #  침범해 간헐 실패를 만들었다).
            cls._instance.shutdown = threading.Event()
            cls._instance.initialized = False # [추가] 초기화 여부
            cls._instance.consecutive_errors = 0 # [추가] 연속 에러 카운트 (Kill Switch용)
        return cls._instance

    def start(self):
        if self.is_running: return

        self.shutdown.clear()   # 재시작 시 이전 종료 신호가 남아 있으면 보조 스레드가 즉시 죽는다
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="ConclusionMonitor")
        self.thread.start()
        self._register_ws_exec_callback()

    def _register_ws_exec_callback(self):
        """[추가] 실시간 체결통보(WebSocket)를 즉시 확인 트리거로 연결한다.

        체결통보가 도착하면 곧바로 check_now()로 기존 REST 체결확정 로직을 깨운다.
        WS가 꺼져있거나/HTS ID 미설정/토스 모드면 콜백이 호출되지 않으므로,
        기존 주기 폴링이 그대로 체결을 처리한다(완전 폴백).
        """
        try:
            from brokers import realtime
            realtime.register_exec_callback(self._on_ws_exec_notice)
        except Exception as e:
            logger.debug(f"[Monitor] 체결통보 콜백 등록 실패(REST 폴링 유지): {e}")

    def _on_ws_exec_notice(self, notice):
        """체결통보 수신 콜백(피드 스레드에서 호출). 즉시 체결 확인을 트리거한다.

        notice의 체결 데이터를 직접 신뢰해 DB에 쓰지 않고(필드 검증 리스크 회피),
        검증된 REST 경로(_check_conclusions)를 즉시 1회 수행하도록 깨우기만 한다.
        WS는 **지연을 줄일 뿐**이며, 통보가 오지 않아도 주기 폴링이 같은 체결을 잡는다.

        [주의] 구독 키는 HTS ID라 같은 ID의 **다른 계좌** 주문도 여기로 들어온다. 지금은
          '깨우기'만 하므로 무해하지만(폴링이 우리 계좌만 본다), 통보 내용을 쓰기 시작하면
          notice['acnt'] 로 계좌를 갈라야 한다.
        """
        try:
            if notice.get('rejected'):
                return  # 거부 통보는 폴링이 처리(미체결 정리 경로)
            self.check_now()  # 집중 감시 모드 진입 + 즉시 폴링 1회
        except Exception as e:
            logger.debug(f"[Monitor] 체결통보 처리 오류: {e}")

    def stop(self):
        self.is_running = False
        self.shutdown.set()  # 보조 스레드(제한 해제 확인)의 대기도 함께 깬다
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
        """국내 정규장 운영 시간 확인 (공용 판정 함수 위임)"""
        return is_system_market_open()

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
            except Exception: pass

            # [수정] 장 운영 시간 외이더라도 미체결 주문이 있으면 모니터링 지속
            # 단, 시스템 초기 1회 실행(초기화)은 장 마감 상태여도 무조건 수행해야 하므로 조건 추가
            if self.initialized and not self._is_market_open() and not has_pending_orders:
                # [추가] 조회를 쉬는 동안에는 '연속 에러' 개념이 성립하지 않으므로 리셋
                # (장애 중 누적된 카운터가 얼어붙어 Kill Switch가 영구히 걸리는 것 방지)
                if self.consecutive_errors:
                    self.consecutive_errors = 0
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
                if config.SCREEN_DEBUG_LEVEL in ["ERROR", "TRACE", "DEBUG"]:
                    config.console.print(f"[bold red][ERROR] 체결 감시 중 오류 발생: {e}[/bold red]")
            
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
        # [관찰 모드] 가상 주문은 증권사에 나가지 않으므로 KIS 체결내역 API로 대사할 수 없다.
        #  대신 paper_broker의 체결 원장을 대사해 주문 상태기계를 닫는다.
        #  (아래 계좌 목록 구성은 cano="PAPER"·acnt_prdt_cd="" 조건에서 어차피 비지만,
        #   '조건이 우연히 False라서 안 돈다'와 '의도적으로 돌지 않는다'는 구분되어야 한다.)
        #  [반환 규약] 호출부가 (rate_limit_hit, has_error)로 언패킹한다. 맨 반환은 안 된다.
        if getattr(config.session, 'is_paper', False):
            self._check_paper_conclusions()
            return False, False

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
            # [수정] 토스는 상품코드(acnt_prdt_cd)가 빈 값이므로 acnt 조건을 토스에서 면제
            #        (가드에 막혀 토스 체결이 전혀 기록되지 않던 문제 해결)
            if config.session.cano and (config.session.acnt_prdt_cd or config.session.is_toss):
                accounts_to_check.append({
                    "cano": config.session.cano,
                    "acnt": config.session.acnt_prdt_cd,
                    "type": "MAIN"
                })
            
            # 2. 자동매매 계좌 (실전 모드이고 별도 설정된 경우)
            if config.session.auto_cano and config.session.auto_acnt_prdt_cd:
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
                    
                    trades = []
                    if data.get('rt_cd') == '0':
                        trades.extend(data.get('output1', []))
                    if ovrs_data.get('rt_cd') == '0':
                        trades.extend(ovrs_data.get('output', []))

                    if trades:

                        for item in trades:
                            odno = item.get('odno')
                            if not odno: continue
                            
                            is_overseas_trade = 'ft_ord_qty' in item or 'ft_ccld_qty' in item
                            
                            # [추가] 주문 상태 파악 및 업데이트 (State Machine)
                            # API 필드: ord_qty(주문), tot_ccld_qty(체결), cncl_cfrm_qty(취소), rmn_qty(잔량)
                            if is_overseas_trade:
                                ord_qty = api.safe_int(item.get('ft_ord_qty'))
                                ccld_qty = api.safe_int(item.get('ft_ccld_qty'))
                                rmn_qty = api.safe_int(item.get('nccs_qty'))
                                # [수정] 해외주식 취소 수량 필드(cncl_cfrm_qty) 누락 대비 계산식 적용
                                cncl_qty = api.safe_int(item.get('cncl_cfrm_qty', ord_qty - ccld_qty - rmn_qty))
                                if cncl_qty < 0: cncl_qty = 0
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
                                _pkg().AutoTrader().update_order_status(code_chk, odno, new_status)
                            
                            tot_ccld_qty = ccld_qty
                            tot_cncl_qty = cncl_qty  # [추가] 취소 수량 누적치
                            
                            order_key = f"{cano}-{odno}"
                            prev_qty = self.order_status.get(order_key, 0)
                            
                            if not hasattr(self, 'cancel_status'): self.cancel_status = {}
                            prev_cncl_qty = self.cancel_status.get(order_key, 0)
                            
                            # [추가] 부분/전량 취소 감지 (수동 취소, 외부 앱 취소, 사후 강제 취소 등)
                            if tot_cncl_qty > prev_cncl_qty:
                                new_cncl_qty = tot_cncl_qty - prev_cncl_qty
                                name = item.get('prdt_name') or item.get('ovrs_item_name') or item.get('item_nm')
                                code = item.get('pdno')
                                
                                origin_trade = db_manager.db.get_trade_by_odno(odno)
                                db_type_name = type_name or ""
                                price_val = avg_price
                                if origin_trade:
                                    db_type_name = origin_trade.get('type', type_name)
                                    if price_val <= 0: price_val = float(origin_trade.get('price', 0))
                                
                                db_type_name = db_type_name or ""
                                
                                # 수동 취소 또는 시스템(타임아웃) 등 이미 알림/저장된 이력인지 확인
                                # [수정] DB 큐를 경유하는 전용 메서드 사용 (워커 스레드 커넥션의 교차 스레드 사용 방지)
                                is_external_cancel = True
                                try:
                                    cancel_record = db_manager.db.get_cancel_record_by_org_odno(odno)
                                    if cancel_record:
                                        rec_reason = cancel_record['reason']
                                        if "수동" in rec_reason or "초과" in rec_reason or "타임아웃" in rec_reason or "외부" in rec_reason:
                                            # 이미 시스템에서 의도했거나 알림을 보낸 취소면 중복 알림 생략
                                            is_external_cancel = False
                                except Exception: pass
                                
                                if not initial:
                                    if is_external_cancel:
                                        is_overseas_stock = not (len(code) == 6 and code[0].isdigit() and code.isalnum()) if code else False
                                        price_str = f"${price_val:,.2f}" if is_overseas_stock else f"{price_val:,.0f}원"
                                        if price_val <= 0: price_str = "시장가"
                                        
                                        cancel_title = "부분 취소" if (rmn_qty > 0 or tot_ccld_qty > 0) else "전량 취소"
                                        t_type = "매수" if "buy" in db_type_name.lower() or "매수" in db_type_name else ("매도" if "sell" in db_type_name.lower() or "매도" in db_type_name else "주문")
                                        
                                        msg = f"⚠️ [{t_type} {cancel_title} 감지] {name}({code})\n취소 수량: {new_cncl_qty}주 / 단가: {price_str}\n주문번호: {utils.format_order_no(odno)}\n사유: 앱(MTS)/HTS 외부 취소 또는 사후 강제 취소"
                                        with utils.AccountContext(cano):
                                            api.send_telegram_message(msg)
                                            
                                        # 외부 취소 이력 DB 등록
                                        db_manager.db.insert_trade(f"{t_type}취소(외부)", code, name, new_cncl_qty, price_val, f"EXT_CAN_{odno}_{tot_cncl_qty}", org_odno=odno, reason=f"외부/사후 취소 감지 ({cancel_title})", order_status="취소")
                                else:
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[Init] 취소 내역 동기화: {name} {tot_cncl_qty}주 (ODNO: {odno})")
                                
                                with self._lock:
                                    self.cancel_status[order_key] = tot_cncl_qty

                            if tot_ccld_qty <= 0: continue
                            
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
                                        # [비용] 주문 시점 추정 손익을 '실제 체결가' 기준으로 다시 계산한다.
                                        profit_amt, profit_rate = _recalc_realized(
                                            origin_trade, avg_price, tot_ccld_qty,
                                            is_overseas_trade, profit_amt, profit_rate)
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
                                        # [수정] DB 큐를 경유하는 전용 메서드 사용 (워커 스레드 커넥션의 교차 스레드 사용 방지)
                                        try:
                                            r_row = db_manager.db.get_reserved_order_by_odno(odno)
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
                                            else:
                                                db_type_name = f"{type_name}(외부)"
                                                reason_to_save = "체결 확인 (앱/HTS 외부 주문)"
                                                actual_reason = "앱(MTS)/HTS 외부 주문 감지"
                                        except Exception as e:
                                            logger.debug(f"[Monitor] 예약 주문 조회 실패: {e}")
                                            db_type_name = f"{type_name}(외부)"
                                            reason_to_save = "체결 확인 (앱/HTS 외부 주문)"
                                            actual_reason = "앱(MTS)/HTS 외부 주문 감지"
                                except Exception:
                                    db_type_name = f"{type_name}(외부)"
                                    stop_loss_rate = 0.0
                                    reason_to_save = "체결 확인 (앱/HTS 외부 주문)"
                                    actual_reason = "앱(MTS)/HTS 외부 주문 감지"
                                
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
                                    except Exception: pass
                                
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
                                    except Exception: pass
                                    
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
                                                
                                                # 전일 RSI (상태 분류용) — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
                                                prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None
                                                
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

                                                # [SSOT] 위 classify_stock_state와 같은 w52_pos를 쓴다
                                                #  (engine.DefaultStrategy.analyze_buy 주석 참조)
                                                score, _ = analysis.calculate_score(
                                                    df=df, ind=ind, weights=thresholds.get('WEIGHTS') if thresholds else None,
                                                    smart_money=sm_flag, w52_pos=w52_pos
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

                                    msg = f"✅ {title_tag} {name}({code})\n수량: {new_qty}주\n단가: {price_str}\n금액: {amt_str}\n주문번호: {utils.format_order_no(odno)}{profit_msg}{reason_msg}{cur_info}{strategy_info}{rule_info}"
                                    with utils.AccountContext(cano):
                                        api.send_telegram_message(msg)
                                    
                                    # 로그 기록 (시스템 로거 활용)
                                    if context.SYSTEM_LOGGER:
                                        context.SYSTEM_LOGGER(f"[체결 확인] {type_name} {name}({code}) {new_qty}주 (단가: {price_str})")
                                    
                                    # [추가] 매도 체결 시 AI 매매 복기 실행
                                    if type_name == "매도" and found_record:
                                        #  [Fix 2026-09-04] 계좌 컨텍스트를 제출 스레드에서
                                        #   싸서 넘긴다. 복기는 db.get_latest_buy_trade 로
                                        #   매수 시점·점수를 읽는데, 그 조회는 계좌로 갈린다
                                        #   (b7fea18). 맨 스레드로 띄우면 threading.local 이
                                        #   상속되지 않아 수동 계좌를 뒤지고, 자동매매가 산
                                        #   종목의 매수 기록을 못 찾아 리포트가 '알 수 없음'이
                                        #   된 채로 AI 에게 넘어간다.
                                        #   캡처는 그 체결의 계좌(cano) 안에서 해야 한다 —
                                        #   위의 AccountContext 는 이미 닫혔고, 감시 루프의
                                        #   기본값을 싸 봐야 같은 실수를 반복한다.
                                        with utils.AccountContext(cano):
                                            _autopsy = utils.inherit_account_context(self._send_trading_autopsy)
                                        threading.Thread(target=_autopsy, args=(code, name, found_record), daemon=True).start()

                                else:
                                    logger.debug(f"[Init] 체결 내역 동기화: {name} {tot_ccld_qty}주 (ODNO: {odno})")
                                    if config.FILE_DEBUG_LEVEL == "DEBUG": logger.debug(f"[ORDER_DEBUG] 초기화 중이라 알림 스킵됨: {odno}")

                                # [추가] 수동매매 제한 종목 자동 연동 (initial 동기화 포함 → 재시작 시 당일 수동 매수 복원)
                                #        알림 발송 여부(initial)와 무관하게 항상 수행한다.
                                # 매수: 외부(앱/HTS) 주문이고 시스템이 낸 주문(ODNO)이 아닐 때만 제한 등록
                                if actual_reason == "앱(MTS)/HTS 외부 주문 감지" and type_name and "매수" in type_name \
                                        and not is_system_odno(odno):
                                    try:
                                        add_restricted_stock(code, name, "수동매매", is_overseas=is_overseas_trade, cano=cano, acnt=acnt, account_type=_current_account_type(cano, acnt))
                                    except Exception as e:
                                        logger.error(f"수동매매 제한 종목 등록 중 오류: {e}")

                                # 매도: 비동기로 잔고 재확인(재시도) 후 전량 매도 시 해당 계좌 제한 해제
                                if type_name and "매도" in type_name:
                                    def _check_and_remove_restriction(t_code, t_cano, t_acnt, t_is_ovrs):
                                        # [수정] 잔고 반영 지연/일시적 조회 실패 대비 재시도 (고정 1회 → 최대 5회)
                                        for attempt in range(5):
                                            # 증권사 API 체결 및 잔고 반영 대기.
                                            #  sleep이 아니라 종료 신호를 기다린다 — 감시가 멈춘 뒤에도
                                            #  최대 15초를 더 살면서 잔고를 조회하고 제한 목록을 건드리는
                                            #  스레드가 남는다(종료 지연·테스트 간섭의 원인).
                                            if self.shutdown.wait(3):
                                                return
                                            try:
                                                qty = None  # None: 조회 실패(미확정), 정수: 확정 잔고
                                                if t_is_ovrs:
                                                    bal = api.get_overseas_balance(t_cano, t_acnt)
                                                    if bal is not None:
                                                        qty = 0
                                                        for item in bal:
                                                            if item.get('ovrs_pdno') == t_code:
                                                                qty = int(float(item.get('ovrs_cblc_qty', 0) or item.get('ord_psbl_qty', 0)))
                                                                break
                                                else:
                                                    bal, _ = api.get_domestic_balance(t_cano, t_acnt)
                                                    if bal is not None:
                                                        qty = 0
                                                        for item in bal:
                                                            if item.get('pdno') == t_code:
                                                                qty = int(item.get('hldg_qty', 0))
                                                                break

                                                if qty is None:
                                                    continue  # 조회 실패 → 재시도

                                                if qty == 0:
                                                    remove_restricted_stock(t_code, cano=t_cano, acnt=t_acnt)
                                                    logger.info(f"[Restriction] {t_code} 전량 매도 확인. 계좌({t_cano}-{t_acnt}) 제한 종목에서 해제되었습니다.")
                                                return  # 잔고 확정(0 또는 보유분 잔존) → 종료
                                            except Exception as e:
                                                logger.error(f"수동매매 제한 해제 검사 중 오류: {e}")
                                        logger.warning(f"[Restriction] {t_code} 잔고 확인 실패로 제한 해제를 보류합니다. (계좌 {t_cano}-{t_acnt})")

                                    threading.Thread(target=_check_and_remove_restriction, args=(code, cano, acnt, is_overseas_trade), daemon=True, name=f"RestrictionCheck-{code}").start()

                                # 상태 업데이트
                                with self._lock:
                                    self.order_status[order_key] = tot_ccld_qty
                                
                                # DB 저장
                                if not db_manager.db.check_trade_exists(odno, "체결"):
                                    if config.FILE_DEBUG_LEVEL == "DEBUG":
                                        logger.debug(f"[ORDER_DEBUG] DB 저장 시도: {odno}")
                                        logger.debug(f"[AutoTrade] 신규 체결 DB 저장 시도: {odno} ({name})")
                                    
                                    db_manager.db.insert_trade(db_type_name, code, name, tot_ccld_qty, avg_price, odno, order_status="체결", reason=reason_to_save, custom_time=trade_time_str, profit_amt=profit_amt, profit_rate=profit_rate, score=score, stop_loss_rate=stop_loss_rate)

                                    # [추가] 매매일지 웹서버로 즉시 전송을 깨운다.
                                    #  적재 자체는 insert_trade 가 같은 트랜잭션에서 끝냈으므로
                                    #  여기서 실패해도 워커가 다음 주기에 자동으로 보낸다.
                                    try:
                                        from modules import journal_sync
                                        journal_sync.trigger()
                                    except Exception as _je:
                                        logger.debug(f"[Journal] 즉시 전송 트리거 실패(무시): {_je}")


                                    # [추가] 시장가 주문 등의 경우를 위해 원 주문(접수)의 단가도 체결가로 업데이트
                                    # 원본 '접수' 기록을 보존하기 위해 order_status는 덮어쓰지 않음
                                    db_manager.db.update_trade(odno, price=avg_price)
                                else:
                                    # [Fix] 부분체결이 여러 폴링 주기에 걸치면(30주 → 100주) 두 번째부터
                                    #  '이미 체결 행이 있다'는 이유로 통째로 스킵되어, trades에는 첫 관측
                                    #  수량만 남고 나머지 증분이 유실됐다. 성과 지표(PF·승률·누적손익),
                                    #  손절률 수량가중평균, 매매일지 전송이 모두 그만큼 어긋난다.
                                    #  tot_ccld_qty는 누적값이므로, 기존 체결 행의 수량·평균단가를 최신
                                    #  누적으로 갱신한다(행을 늘리지 않아 odno 단위 조회는 그대로 동작).
                                    #  '접수' 행은 주문 수량을 보존해야 하므로 where_status로 분리한다.
                                    #  [Fix 2026-09-03] 손익도 함께 갱신한다. 종전에는 수량·단가만
                                    #   고치고 profit_amt 는 **첫 관측 시점의 수량으로 계산된 값**
                                    #   그대로 남았다(실측: 30주 관측 후 100주 체결 → 실현손익 70%
                                    #   과소). 그 값은 성과 지표뿐 아니라
                                    #   db.get_realized_profit_between 을 지나 **입출금 판정**까지
                                    #   간다 — 실현손익을 적게 세면 그 차액이 가짜 입금으로 둔갑해
                                    #   자산 기준선이 밀린다([[daily-asset-baseline-transfers]]).
                                    #   profit_rate 는 수량과 무관해 값이 같지만, 산식이 바뀌어도
                                    #   따라오도록 함께 넘긴다.
                                    _p_amt, _p_rate = _recalc_realized(
                                        origin_trade, avg_price, tot_ccld_qty,
                                        is_overseas_trade, None, None)
                                    db_manager.db.update_trade(odno, qty=tot_ccld_qty, price=avg_price,
                                                               profit_amt=_p_amt, profit_rate=_p_rate,
                                                               where_status="체결")
                                    logger.debug(
                                        f"[ORDER_DEBUG] 부분체결 누적 갱신: {odno} → {tot_ccld_qty}주 "
                                        f"@ {avg_price} (손익 {_p_amt})")
                except Exception as e:
                    logger.error(f"계좌({cano}) 체결 확인 중 오류: {e}")
                    has_error = True
        except Exception as e:
            logger.error(f"체결 확인 중 오류 발생: {e}")
            has_error = True
        return rate_limit_hit, has_error

    def _check_paper_conclusions(self):
        """가상투자: paper_broker 체결 원장을 대사해 대기 주문을 체결로 확정한다.

        가상 주문은 place_order 시점에 이미 전량 체결되지만, 그 사실을 아는 것은
        paper_broker의 원장뿐이다. 트레이더의 주문 상태기계(pending_orders)와 거래
        히스토리는 '접수'에 멈춰 있어 ① 히스토리에 체결이 남지 않고 ② is_pending이
        True로 굳어 그 종목이 매도·손절 판정에서 통째로 빠지며 ③ 결국 고아 주문
        경보까지 뜬다(2026-08-05 실제 관측: 018260·035420).

        원장 대사이므로 '추정'이 아니라 확정 체결이다 — 수량·체결가를 그대로 쓴다.
        """
        trader = _pkg().AutoTrader()
        try:
            from modules import paper_broker
            with trader.order_manager._lock:
                snapshot = {c: dict(o) for c, o in trader.order_manager.pending_orders.items()}

            for code, orders in snapshot.items():
                for odno, status in orders.items():
                    if status != OrderStatus.ORDER_SENT:
                        continue
                    # 발주 직후 선점용 임시 ID(PRE_*)는 원장에 없다 — 조회 자체를 건너뛴다.
                    if str(odno).startswith("PRE_"):
                        continue
                    self._apply_paper_fill(trader, code, odno, paper_broker.get_fill_by_odno(odno))

            # [재기동 복구] pending_orders는 메모리라 재시작하면 비고, 가상 주문은 미체결
            #  목록으로 복원할 수도 없다(관찰 모드의 미체결은 항상 빈 리스트). 그대로 두면
            #  이미 체결된 주문의 히스토리가 '접수'에 영구히 멈춘다. 프로세스당 1회만
            #  당일 원장을 훑는다(매 주기 훑으면 라즈베리파이에서 순수 낭비).
            #  이미 체결 이력이 있는 주문은 _handle_simulation_fill이 알림 없이 건너뛴다.
            if not self.paper_backfill_done:
                self.paper_backfill_done = True
                today = datetime.now().strftime('%Y-%m-%d')
                for fill in paper_broker.get_fills():
                    if not str(fill.get('time') or '').startswith(today):
                        continue
                    self._apply_paper_fill(trader, fill['code'], fill.get('odno'), fill)
        except Exception as e:
            logger.error(f"[Monitor] 가상투자 체결 대사 중 오류: {e}", exc_info=True)

    def _apply_paper_fill(self, trader, code, odno, fill):
        """가상 체결 1건을 주문 상태기계·히스토리에 반영한다."""
        if not odno or not fill:
            return
        trade = db_manager.db.get_trade_by_odno(odno)
        if not trade:
            # 주문 기록 INSERT가 DB 큐에 아직 남아 있는 경우. 다음 주기에 다시 본다.
            return
        self._handle_simulation_fill(
            trader, trade, odno, code, int(fill['qty']), "가상 체결 원장 확인",
            confirmed_fill={'price': fill['price'], 'qty': fill['qty']})

    def _handle_simulation_fill(self, trader, trade, odno, code, qty, reason, confirmed_fill=None):
        """모의투자·가상투자 체결 처리 핸들러.

        confirmed_fill: 체결이 확정적으로 확인된 경우의 {'price':...} (가상투자 원장 대사).
                        주어지면 WS 체결통보와 동일하게 취급해 '(추정)' 라벨을 붙이지 않는다.
        """
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
                except Exception: pass

            # 체결이 확정적으로 확인된 경우(가상투자 원장 대사)에는 '(추정)' 라벨을 떼고
            # 원장의 실제 체결가를 쓴다.
            ws_fill = confirmed_fill
            ws_confirmed = bool(ws_fill)
            if ws_fill and float(ws_fill.get('price') or 0) > 0:
                price = float(ws_fill['price'])  # 추정가 → 실시간 체결통보의 실제 체결가로 대체

            type_str = trade.get('type', '') # [수정] KeyError 방지

            # [추가] None 값 안전 처리 (DB 저장 실패 방지)
            try: profit_amt = int(float(trade.get('profit_amt') or 0))
            except Exception: profit_amt = 0
            try: profit_rate = float(trade.get('profit_rate') or 0.0)
            except Exception: profit_rate = 0.0
            # [비용] price 는 위에서 실제 체결가(WS 체결통보)로 갱신됐다. 손익만 주문 시점
            #  추정치로 남겨두면 체결가와 어긋난 값이 실현손익으로 굳는다.
            profit_amt, profit_rate = _recalc_realized(
                trade, price, qty, is_overseas, profit_amt, profit_rate)
            
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
                    order_status=("체결" if ws_confirmed else "체결(추정)"),
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

                # [체결가 파리티] 원 '접수' 행의 단가도 체결가로 갱신한다 — 실체결 경로가
                #  하는 것과 같다(_check_conclusions의 update_trade(odno, price=avg_price)).
                #  성과 리포트는 접수·체결 행을 (거래일, odno)로 병합하는데, 병합 규칙이
                #  체결가를 채택하는 것은 **접수 단가가 0(시장가)일 때뿐**이다. 지정가로
                #  낸 주문은 접수 단가가 남아, 관찰모드에서만 리포트의 단가·매매금액이
                #  주문가로 굳었다(가상 브로커는 슬리피지를 얹어 체결하므로 항상 어긋난다).
                #  손익은 체결 행에서 병합되므로 단가만 어긋나 더 눈에 띄지 않았다.
                if price > 0:
                    db_manager.db.update_trade(odno, price=price)

                # 3. 알림 발송 (상세 정보 포함)
                try:
                    type_name = "매수" if "buy" in type_str.lower() or "매수" in type_str else "매도"
                    
                    # 개별 룰 조회
                    custom_rules = db_manager.db.get_all_stock_strategies()
                    rules_map = {r['code']: r for r in custom_rules}
                    rule = rules_map.get(code)
                    
                    if ws_confirmed:
                        title_tag = f"[{type_name} 체결]" if type_name else "[체결 알림]"
                    else:
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
                    except Exception: pass

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
                        except Exception: pass
                                
                    if strategy_info:
                        strategy_info += cur_info
                        cur_info = ""
                    elif cur_info:
                        strategy_info = f"\n\n📊 [현재 시장 데이터]{cur_info}"
                        cur_info = ""

                    exec_amt = int(price * qty)
                    price_fmt = f"{price:,.0f}원" if price > 0 else "시장가"
                    price_suffix = "(체결가)" if ws_confirmed else "(추정체결가)"
                    amt_fmt = f"{exec_amt:,}원" if exec_amt > 0 else "-"

                    original_reason = trade.get('reason', reason)
                    profit_msg = ""
                    if type_name == "매도":
                        p_amt = trade.get('profit_amt')
                        p_rate = trade.get('profit_rate')
                        if p_amt is not None and p_rate is not None:
                            profit_msg = f"\n손익: {int(p_amt):+,}원 ({float(p_rate):+.2f}%)"
                            
                    msg = f"✅ {title_tag} {name}({code})\n수량: {qty}주\n단가: {price_fmt}{price_suffix}\n금액: {amt_fmt}\n주문번호: {utils.format_order_no(odno)}{profit_msg}\n사유: {original_reason}{cur_info}{strategy_info}{rule_info}"
                    api.send_telegram_message(msg)
                    mode_label = "가상투자" if getattr(config.session, 'is_paper', False) else "모의투자"
                    logger.info(f"[Monitor] {mode_label} 체결 확인{'(확정)' if ws_confirmed else '(추정)'}: {name} {qty}주 ({reason})")

                    # [추가] 수동 매수 체결 시 트레이딩 제한 종목 자동 등록.
                    #  트레이딩 RUNNING 중 사용자가 수동 매수하면 이 백그라운드
                    #  모니터가 체결을 먼저 잡으므로 여기서도 등록해야 한다.
                    #  [중요] 시스템 주문 판정은 거래 기록의 '(AUTO)' 표기로 한다 —
                    #  ODNO 세트는 메모리라 재기동 후 백필 경로에서 전부 '수동'으로
                    #  오판되고, 자동매매가 자기가 산 종목을 스스로 제한해 버린다.
                    if type_name == "매수" and not is_system_trade(type_str, odno):
                        try:
                            r_cano, r_acnt = _get_trade_account()
                            add_restricted_stock(code, name, "수동매매", is_overseas=is_overseas, cano=r_cano, acnt=r_acnt, account_type=_current_account_type(r_cano, r_acnt))
                        except Exception as e:
                            logger.error(f"수동 매수 제한 종목 등록 오류: {e}")
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
                except Exception: pass
            
            from modules import theme_analysis
            autopsy = theme_analysis.generate_trading_autopsy(code, name, buy_time, buy_score, sell_reason, profit_rate, holding_days)
            if autopsy:
                if autopsy.startswith("⚠️"):
                    api.send_telegram_message(f"📝 [AI 매매 복기 리포트] {name}({code})\n{autopsy}")
                else:
                    api.send_telegram_message(f"📝 [AI 매매 복기 리포트] {name}({code})\n\n{autopsy}")
        except Exception as e:
            logger.error(f"Trading autopsy error: {e}")

