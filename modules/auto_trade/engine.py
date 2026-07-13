# modules/auto_trade/engine.py
"""매매 엔진: DefaultStrategy(매수/매도 판단) · OrderManager(주문 집행) · RiskManager(리스크 관리)

기존 modules/auto_trade.py 에서 분해. 외부 인터페이스는 패키지(__init__)가 재수출한다.
"""
import threading
import concurrent.futures
import logging
import time
import requests
import json
import jsonio
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

from modules.auto_trade.common import (OrderStatus, get_mystock_log_tail, register_system_odno)

console = config.console

logger = logging.getLogger(__name__)


def _pkg():
    """패키지(modules.auto_trade) 네임스페이스 접근자.

    분해 전에는 모듈 전역 조회였던 상호 호출을 패키지 속성 조회로 유지해,
    테스트의 patch('modules.auto_trade.X') 가 분해 전과 동일하게 내부 호출에도
    적용되도록 한다. (지연 import라 순환 없음)
    """
    import modules.auto_trade as _at
    return _at


class DefaultStrategy:
    """기본 매매 전략 클래스 (매수/매도 판단 로직 분리)"""
    def __init__(self):
        self.trailing_stop_cache = {}

    def analyze_buy(self, code, name, df, current_price, vol_strength=None, thresholds=None, ask_bid_ratio=None):
        """매수 진입 여부 판단"""
        if df is None or df.empty:
            return None

        ind = indicators.calculate_indicators(df)
        # 전일 RSI (상태 분류용) — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
        prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None
        
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
                    vol_reject_reason = f"매도비:{ask_bid_ratio:.2f}<{min_ask_bid_ratio}"
        elif config.session.is_toss:
            # [추가] 토스: 체결강도 미제공 → 호가창 매도잔량비(ask_bid_ratio)만으로 수급 게이트 대체
            #   호가 조회가 실패해 ratio가 없으면 상태(state) 게이트만으로 진입(거래 중단 방지)
            if ask_bid_ratio is not None and min_ask_bid_ratio > 0 and ask_bid_ratio < min_ask_bid_ratio:
                is_vol_ok = False
                vol_reject_reason = f"매도비:{ask_bid_ratio:.2f}<{min_ask_bid_ratio}"

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
            'min_ask_bid_ratio': min_ask_bid_ratio,  # [추가] 재진입 허들 등에서 재사용
            'vol_reject_reason': vol_reject_reason,
            'smart_money': sm_flag,
            'w52_pos': w52_pos  # [추세추종] 매수 후보 우선순위(강한 종목 우선) 정렬용 52주 위치
        }

    def analyze_pyramid(self, profit_rate, state, score, pyramid_count, thresholds=None):
        """[추세추종] 수익 포지션 증액(피라미딩) 여부 판단

        물타기의 정반대: 수익으로 추세가 검증된 포지션에만, 추세가 유지되는 동안 증액한다.
        반환: (증액 여부, 사유 문자열)
        """
        at = config.ANALYSIS_THRESHOLDS
        use = thresholds.get("PYRAMIDING_USE", at.get("PYRAMIDING_USE", False)) if thresholds else at.get("PYRAMIDING_USE", False)
        if not use:
            return False, ""

        trigger = thresholds.get("PYRAMIDING_PROFIT_TRIGGER", at.get("PYRAMIDING_PROFIT_TRIGGER", 10.0)) if thresholds else at.get("PYRAMIDING_PROFIT_TRIGGER", 10.0)
        max_count = thresholds.get("PYRAMIDING_MAX_COUNT", at.get("PYRAMIDING_MAX_COUNT", 1)) if thresholds else at.get("PYRAMIDING_MAX_COUNT", 1)

        if pyramid_count >= max_count:
            return False, ""
        if profit_rate < trigger:
            return False, ""
        # 추세 유지 확인: 신규 진입과 동일한 '매수' 신호가 살아있어야 증액
        if state not in ("매수", "강매수"):
            return False, ""

        return True, f"피라미딩 {pyramid_count + 1}차 (수익률:+{profit_rate:.1f}%, 점수:{score}, 상태:{state})"

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
        ts_activation = thresholds.get("ts_activation", config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0)
        ts_callback = thresholds.get("ts_callback", config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0)
        # [샹들리에 엑시트] TS 동적 콜백 전용 ATR 배수 (손절용 ATR_STOP_MULTIPLIER와 분리)
        ts_atr_mult = thresholds.get("TRAILING_ATR_MULTIPLIER", config.SELL_STRATEGY.get("TRAILING_ATR_MULTIPLIER", 3.0)) if thresholds else config.SELL_STRATEGY.get("TRAILING_ATR_MULTIPLIER", 3.0)
        
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
            # 전일 RSI (상태 분류용) — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
            prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None
            
            is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
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
                    dynamic_callback = (atr_val * ts_atr_mult / highest_price) * 100
                    
                    # [리스크 관리 방어 로직 추가]
                    # 1. 하한선: 너무 작은 변동성으로 인한 조기 털림 방지 (기본 ts_callback 보장)
                    # 2. 상한선: ATR이 너무 커서 도달한 최대 수익의 50% 이상을 반납하는 것 방지
                    max_allowed_callback = max(ts_callback, max_profit_rate * 0.5)
                    actual_ts_callback = min(max(ts_callback, dynamic_callback), max_allowed_callback)
                    
                if drop_rate >= actual_ts_callback:
                    ts_msg = f"트레일링스탑 (최고가:{int(highest_price):,}원, 하락률:-{drop_rate:.1f}%, 기준:-{actual_ts_callback:.1f}%)"

        # 2. 고정 익절/손절 및 시간 청산
        if tp_rate > 0 and use_half_tp and not already_half_sold and profit_rate >= half_tp_rate:
            reason = f"반익절({profit_rate:.1f}%)"
            sell_ratio = 0.5
        elif tp_rate > 0 and profit_rate >= tp_rate:
            if use_half_tp and already_half_sold:
                pass # [수정] 반익절 후 남은 물량은 천장을 해제하고 트레일링 스탑에 맡김 (Let profit run)
            else:
                reason = f"익절({profit_rate}%)"
                
        # [추가] 반익절 이후 Let profit run 시, 최소 수익 보존선 (Profit Lock-in)
        # 목표가를 한 번 뚫고 내려올 경우 TS(예: 4%) 발동 전이라도 목표가-3%에서 즉시 매도하여 수익 방어
        elif tp_rate > 0 and use_half_tp and already_half_sold and highest_price > 0:
            max_profit_rate_so_far = ((highest_price - buy_price) / buy_price) * 100
            profit_lock_rate = tp_rate - 3.0
            if max_profit_rate_so_far >= tp_rate and profit_rate <= profit_lock_rate:
                reason = f"수익보존(목표돌파후 하락, {profit_rate:.1f}%)"
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
                
            # 4. RSI 과열 익절 (TAKE_PROFIT_RSI가 0이면 미사용 - 추세추종 기조)
            if not reason and tp_rsi > 0 and ind.get('rsi') is not None and ind['rsi'] > actual_tp_rsi:
                if is_super:
                    reason = f"RSI과열(슈퍼모멘텀, 기준:{actual_tp_rsi})"
                else:
                    reason = f"RSI과열(기준:{actual_tp_rsi})"
            
            # [추가] 매도 최적화 3번: 방어적 반매도 (하락 반전 신호 발생 시 절반 덜어내기)
            # [개선 #7] 이미 추세가 '매도'로 확정 붕괴된 경우에는 절반만 덜어내지 않고
            #          아래 추세이탈 로직에서 전량 청산하도록 방어적 반매도를 건너뜀
            #          (잔여 물량이 갭하락으로 손실 전환되는 것을 방지).
            if not reason and defensive_half_tp and not already_half_sold and state != "매도":
                if ind.get('psar') is not None and ind.get('ema_5') is not None:
                    # [엣지 케이스 방어] 손실 구간에서의 조기 손절(반손절)을 방지하고 '수익 보전' 목적에 맞게,
                    # 최소한의 의미 있는 수익(time_stop_min_profit, 기본 3.0%) 이상일 때만 발동하도록 안전장치 추가
                    if profit_rate >= time_stop_min_profit and current_price < ind['psar'] and current_price < ind['ema_5']:
                        reason = f"하락반전(방어적 반매도, 수익률:+{profit_rate:.1f}%)"
                        sell_ratio = 0.5

            # 5. 추세 이탈
            # [추세추종] 점수 하락 단독으로는 매도하지 않고 추세 구조 훼손(주가<60일선)을 동시 요구.
            #   스코어는 단기 신호(5>20 EMA, MACD 히스토그램, SAR 등) 비중이 커서 정배열 유지 중의
            #   통상적 눌림목에서도 기준 미만으로 떨어질 수 있음 → 주청산(샹들리에 TS)의 fat-tail
            #   추종을 점수 매도가 조기에 잘라내는 것을 방지. ('매도' 상태는 자체 조건이 이미 엄격하므로 즉시 발동)
            ema60_val = ind.get('ema_60') if ind else None
            structure_broken = ema60_val is None or current_price < ema60_val
            if not reason and (state == "매도" or (score < sell_score_limit and structure_broken)):
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
                        reason = f"추세이탈({state}/점수하락+60일선이탈) [점수:{score}, RSI:{rsi_val}, ADX:{adx_val}, CCI:{cci_val}]"
            
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
        # [추가] 매도 발주 직전 보유수량 {odno: qty} - 모의투자 부분매도 체결 감지용
        #  잔고가 0이 되어야만 체결로 보던 기존 방식은 부분매도를 감지하지 못해
        #  (실시간 감지 실패 → 미체결 타임아웃 우회 경로로 수 분 지연 발생) 보강한다.
        self.sell_pre_qty = {}
        # [최적화] 누적 주문 접수 카운터 — 루프에서 '이번 주기에 주문이 나갔는가'를 판단해
        #  주문이 없으면 루프 말미 잔고/예수금 재조회를 생략하기 위한 단조 증가 값
        self.orders_sent_count = 0
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
                        self.sell_pre_qty.pop(str(odno), None)
                        self.trader.log(f"[OrderState] 주문 종결({status}): {code} (No.{odno})")
                        
                        # 체결 완료 시 지연 후 보유 종목 리스트 갱신 출력
                        if status == OrderStatus.FILLED:
                            def _delayed_log_holdings():
                                time.sleep(1.5) # KIS API 잔고 갱신 대기
                                self.trader.log_current_holdings()
                            threading.Thread(target=_delayed_log_holdings, daemon=True).start()
                            
                        # [추가] 사후 주문 거부(REJECTED) 시 텔레그램 알림 발송
                        elif status == OrderStatus.REJECTED:
                            try:
                                trade = db_manager.db.get_trade_by_odno(odno)
                                if trade:
                                    t_str = trade.get('type', '')
                                    t_type = "매수" if "buy" in t_str.lower() or "매수" in t_str else ("매도" if "sell" in t_str.lower() or "매도" in t_str else "주문")
                                    name = trade.get('name', code)
                                    qty = trade.get('qty', 0)
                                    price = float(trade.get('price', 0))
                                    
                                    is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
                                    price_str = f"${price:,.2f}" if is_overseas else f"{price:,.0f}원"
                                    if price <= 0: price_str = "시장가"
                                    
                                    msg = f"🚫 [{t_type} 사후 거부] {name}({code})\n수량: {qty}주 / 단가: {price_str}\n주문번호: {utils.format_order_no(odno)}\n사유: 사후 주문 거부 (상세 사유는 HTS/MTS 확인)"
                                    api.send_telegram_message(msg)
                            except Exception as e:
                                self.trader.log(f"REJECTED 알림 전송 실패: {e}")
                    else:
                        self.trader.log(f"[OrderState] 상태 변경: {code} (No.{odno}) {current_status} -> {status}")

    def register_manual_order(self, code, odno, pre_qty=None):
        """수동 주문 발생 시 상태 추적 등록 (외부 호출용)

        pre_qty: 매도 주문의 경우 발주 직전 보유수량. 모의투자에서 부분매도
                 체결을 잔고 감소분으로 감지하기 위해 사용한다.
        """
        with self._lock:
            if code not in self.pending_orders:
                self.pending_orders[code] = {}
            self.pending_orders[code][odno] = OrderStatus.ORDER_SENT
            if pre_qty is not None:
                self.sell_pre_qty[str(odno)] = int(pre_qty)

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
                # [추가] 시스템 발주 주문번호를 즉시 기록(DB 큐 비동기 지연으로 인한 외부주문 오판 방지)
                register_system_odno(odno)
                success_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {type_str.upper()} 성공 | {code} | {qty}주 | No.{odno}"

                with self._lock:
                    # 임시 ID 삭제 및 실제 ODNO로 교체
                    if temp_id in self.pending_orders[code]:
                        del self.pending_orders[code][temp_id]
                    self.pending_orders[code][odno] = OrderStatus.ORDER_SENT
                    self.orders_sent_count += 1

                self.trader.trade_history.append(success_msg)
                self.trader.log(f"결과: 성공 (주문번호: {utils.format_order_no(odno)})")
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
                    
                msg += f"\n주문번호: {utils.format_order_no(odno)}"
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
                _pkg().ConclusionMonitor().check_now()
                
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
                    
                    # [추가] 외부 앱(MTS/HTS)에서 들어온 신규 미체결 주문 감지 및 DB 등록
                    trade = db_manager.db.get_trade_by_odno(odno)
                    if not trade:
                        sll_buy_name = item.get('sll_buy_dvsn_cd_name')
                        if not sll_buy_name:
                            sll_buy_cd = item.get('sll_buy_dvsn_cd', '')
                            sll_buy_name = "매수" if sll_buy_cd == "02" else ("매도" if sll_buy_cd == "01" else "주문")
                            
                        price = float(item.get('ord_unpr', 0))
                        t_type = f"{sll_buy_name}(외부)"
                        
                        # DB에 접수 상태로 기록
                        db_manager.db.insert_trade(
                            t_type, code, name, qty, price, odno, 
                            order_status="접수", reason="앱(MTS)/HTS 외부 주문 감지"
                        )
                        
                        # 내부 트래킹(메모리)에 등록
                        with self._lock:
                            if code not in self.pending_orders:
                                self.pending_orders[code] = {}
                            self.pending_orders[code][odno] = OrderStatus.ORDER_SENT
                            
                        self.trader.log(f"[외부 주문 감지] {name}({code}) {sll_buy_name} {qty}주 (No.{odno})")
                        msg = f"📡 [{sll_buy_name} 외부접수] {name}({code})\n수량: {qty}주\n단가: {int(price):,}원\n주문번호: {utils.format_order_no(odno)}\n사유: 앱(MTS)/HTS 등 외부 주문 감지"
                        api.send_telegram_message(msg)
                        
                        trade = db_manager.db.get_trade_by_odno(odno)

                    try:
                        ord_dt = datetime.strptime(f"{now.strftime('%Y%m%d')}{ord_time_str}", "%Y%m%d%H%M%S")
                        elapsed = (now - ord_dt).total_seconds()
                        
                        if elapsed >= cancel_seconds:
                            # [추가] 외부에서 들어온 주문은 시스템이 자동 취소(타임아웃)하지 않도록 보호
                            if trade and "(외부)" in trade.get('type', ''):
                                continue
                                
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
                                
                                # [추가] DB에 취소 이력 남기기 (CANCELED 알림 중복 방지)
                                cancel_odno = res.get('output', {}).get('ODNO') or res.get('output', {}).get('KRX_FWDG_ORD_ORGNO') or f"CANCEL_{odno}"
                                db_manager.db.insert_trade(f"{t_type}취소(자동)", code, name, qty, 0, cancel_odno, org_odno=odno, reason=f"미체결 시간 초과 (자동 취소)", order_status="취소")
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
                                                        except Exception: pass
                                                    elif "sell" in trade.get('type', '').lower() or "매도" in trade.get('type', ''):
                                                        # [추가] 매도 주문인 경우 40330000 에러는 대부분 체결 완료를 의미함
                                                        is_filled = True
                                                    
                                                    if is_filled:
                                                        self.trader.log(f"-> 체결/잔고 확인됨. '체결(추정)'으로 기록합니다.")
                                                        
                                                        fill_price = float(trade['price'])
                                                        is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum()) if code else False
                                                        if fill_price <= 0:
                                                            try:
                                                                cp = api.get_current_price(code, is_overseas=is_overseas)
                                                                if cp > 0: fill_price = float(cp)
                                                            except Exception: pass

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
                                                                        strategy_info = f"\n\n📊 [전략 지표(진입시점)]\n• 점수: {score}점\n• RSI: {rsi_str} / ADX: {adx_str} / CCI: {cci_str}"
                                                                except Exception: pass
                                                            
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
                                                            msg = f"✅ {title_tag} {type_name} {trade['name']}({code})\n수량: {qty}주\n단가: {price_fmt}(추정체결가)\n금액: {amt_fmt}\n주문번호: {utils.format_order_no(odno)}{profit_msg}\n사유: {original_reason}{cur_info}{strategy_info}{rule_info}"
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
        """자산 배분 계산

        3개 레이어가 순차 적용된다. ATR(변동성)이 2~3번에 모두 관여하지만 '목적'이 서로 달라
        의도된 중첩이다(이중 안전장치). 변경 시 아래 의도를 유지할 것:
          1) 기초 비중(invest_ratio): 종목당 명목 상한 (집중 방지). SYSTEM_MAX_HOLDINGS와 곱해 1.0 이하 권장.
          2) 리스크 기반(SYSTEM_RISK_PER_TRADE): '손절 시 계좌 손실액'을 일정 이하로 고정 → 꼬리위험(tail loss) 상한.
             손절폭(ATR 손절이면 ATR 반영)이 넓을수록 비중을 줄인다. min()으로 1)과 결합.
          3) 변동성 타겟팅(TARGET_VOLATILITY): 종목의 연환산 변동성을 목표치로 정규화 → 포트폴리오 변동성 균질화.
             2)가 '최악 손실액'을 막는다면 3)은 '평상시 변동성'을 맞춘다. scale 배수로 곱셈 결합.
        즉 2)는 손실액 캡, 3)은 변동성 정규화로 서로 다른 위험을 통제하므로 단순 중복이 아니다.
        """
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
            
            target_vol = getattr(config, 'TARGET_VOLATILITY', 0.30)
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

        # [Fix] 비정상적인 데이터(갑작스런 반토막 이상 하락 등 API 데이터 누락 의심) 필터링
        # (주로 증권사 API 통신 오류로 인해 주식 평가액이 0으로 수신되어 예수금만 계산될 때 발생합니다.)
        if current_total < self.trader.initial_asset * 0.5:
            self.trader.log(f"⚠️ 비정상적인 자산 급감 감지(API 오류 의심). 손실 한도 체크를 스킵합니다. (현재자산: {current_total:,}원)")
            return

        loss_rate = (current_total - self.trader.initial_asset) / self.trader.initial_asset * 100
        
        if config.FILE_DEBUG_LEVEL == "DEBUG":
            logger.debug(f"[LossCheck] 시작자산:{self.trader.initial_asset:,} -> 현재자산:{current_total:,} | 변동률:{loss_rate:+.2f}% (한도:-{loss_limit_pct}%)")
        
        if loss_rate <= -loss_limit_pct:
            self.trader.log(f"[비상 정지] 일일 손실 한도 초과! (현재: {loss_rate:.2f}% / 제한: -{loss_limit_pct}%)")
            self.trader.log(f"시작 자산: {self.trader.initial_asset:,}원 -> 현재 자산: {current_total:,}원")
            
            # [추가] 화면에 붉은색 경고 출력 (안내 메시지로 충분하므로 중복 [ERROR] 출력 제거)
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

