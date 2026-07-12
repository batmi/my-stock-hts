# modules/analysis.py
from rich.table import Table
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn, DownloadColumn, TransferSpeedColumn
from rich import box
import config
import context
import api
import logging
import contextlib
import indicators
import utils
import caching
import time
from datetime import datetime
import urllib.request
import sys
import zipfile
import threading # [추가]
import os
import pandas as pd
import concurrent.futures
import shutil
import sqlite3
import json
import math
import re
from modules import db_manager

logger = logging.getLogger(__name__)

# [Pylance 에러 방지용] 잔여 코드 및 중복 함수로 인한 미정의 변수 참조 경고 차단
reserved_codes = set()
m_codes = set()
restricted_stocks = set()
rules_map = {}

# [리팩토링] 스마트머니 캐시 (종목코드 -> (flag, reason)) — 공용 TTLCache 사용
_SMART_MONEY_CACHE = caching.TTLCache()

# [최적화] 시장 국면(get_market_regime) 단기 캐시 — 자동매매 주기당 반복 호출 제거
_MARKET_REGIME_CACHE = caching.TTLCache(max_size=8)
_MARKET_REGIME_TTL_SEC = 60.0

def clear_smart_money_cache():
    """스마트머니 수급 캐시 초기화 (수동 갱신용)"""
    _SMART_MONEY_CACHE.clear()

def check_smart_money_turnaround(code, is_overseas=False):
    """외국인/기관 수급 턴어라운드 및 쌍끌이 발생 여부 확인"""
    if is_overseas: return False, ""

    cached = _SMART_MONEY_CACHE.get(code, ttl=3600)  # 1시간(60분) 유지
    if isinstance(cached, tuple):
        return cached

    try:
        inv_list = api.get_investor_trend(code)
        if not inv_list or len(inv_list) < 3: 
            return False, ""
            
        flag = False
        reason = ""
        
        for i in range(min(2, len(inv_list))): # [수정] 최근 5일 -> 당일(0)과 전일(1) 2일만 검사
            f_net = api.safe_int(inv_list[i].get('frgn_ntby_qty', 0))
            i_net = api.safe_int(inv_list[i].get('orgn_ntby_qty', 0))
            
            if f_net > 0 and i_net > 0:
                flag, reason = True, "쌍끌이 매수"
                break
                
            if i + 2 < len(inv_list):
                f_prev1 = api.safe_int(inv_list[i+1].get('frgn_ntby_qty', 0))
                f_prev2 = api.safe_int(inv_list[i+2].get('frgn_ntby_qty', 0))
                if f_net > 0 and f_prev1 < 0 and f_prev2 < 0:
                    flag, reason = True, "외국인 턴어라운드"
                    break
                    
                i_prev1 = api.safe_int(inv_list[i+1].get('orgn_ntby_qty', 0))
                i_prev2 = api.safe_int(inv_list[i+2].get('orgn_ntby_qty', 0))
                if i_net > 0 and i_prev1 < 0 and i_prev2 < 0:
                    flag, reason = True, "기관 턴어라운드"
                    break
        
        _SMART_MONEY_CACHE.set(code, (flag, reason))
        return flag, reason
    except Exception as e:
        logger.debug(f"Smart Money Check Error for {code}: {e}")
        return False, ""

def calculate_score(price=None, ema20=None, ema60=None, ema120=None, sar=None, rsi=None, adx=None, cci=None, obv_trend=None, macd=None, macd_signal=None, weights=None, smart_money=False, plus_di=None, minus_di=None, df=None, ind=None, ema_5=None, macd_hist=None, prev_macd_hist=None, prev_cci=None, vol_spike=False, vol_trend=False, w52_pos=None, mom_ret=None):
    """퀀트 멀티팩터 스코어링 모델 (10점 만점)"""
    if weights is None: weights = config.SCORING_WEIGHTS

    # r_* 는 '각 팩터의 세부 항목 기본배점 합(=설계상 만점)' 대비 사용자 가중치의 스케일 배수다.
    # 분모는 세부항목 기본배점 합(고정값)이며 분자만 가중치로 바뀐다.
    #   예) TREND 세부항목 기본합=4.0. 가중치 4.0이면 r_trend=1.0 → 추세 팩터가 4.0점 만점.
    # 4개 팩터 기본배점: 추세4.0 + 모멘텀2.5 + 강도1.5 + 시너지2.0 = 10.0 (총점 10점)
    r_trend = weights.get("TREND", 4.0) / 4.0
    r_mom = weights.get("MOMENTUM", 2.5) / 2.5
    r_str = weights.get("STRENGTH", 1.5) / 1.5
    r_syn = weights.get("SYNERGY", 2.0) / 2.0

    score = 0
    details = []
    
    # [Fix] df가 전달되지 않는 백테스팅 환경을 위한 변수 초기화
    vol_spike_flag = vol_spike
    vol_trend_flag = vol_trend

    if df is not None and ind is not None:
        if not df.empty: price = float(df.iloc[-1]['close'])
        ema20 = ind.get('ema_20')
        ema60 = ind.get('ema_60')
        ema120 = ind.get('ema_120')
        sar = ind.get('psar')
        rsi = ind.get('rsi')
        adx = ind.get('adx')
        cci = ind.get('cci')
        obv_trend = ind.get('obv_trend')
        macd = ind.get('macd')
        macd_signal = ind.get('macd_signal')
        if plus_di is None: plus_di = ind.get('plus_di')
        if minus_di is None: minus_di = ind.get('minus_di')
        
        # [추가] 인자로 넘어오지 않은 세부 지표들을 ind 딕셔너리에서 직접 추출하여 일관성(SSOT) 확보
        if ema_5 is None: ema_5 = ind.get('ema_5')
        if prev_cci is None: prev_cci = ind.get('prev_cci')
        if macd_hist is None: macd_hist = ind.get('macd_hist')
        if prev_macd_hist is None: prev_macd_hist = ind.get('prev_macd_hist')

    import numpy as np
    
    if df is not None and not df.empty:
        # [Early] 선행 지표 동적 계산
        if ema_5 is None:
            ema_5 = df['close'].ewm(span=config.INDICATOR_PARAMS.get('EMA_SHORT', 5), adjust=False).mean().iloc[-1]
            
        if macd is not None and macd_signal is not None and (macd_hist is None or prev_macd_hist is None):
            fast = config.INDICATOR_PARAMS.get('MACD_FAST', 12)
            slow = config.INDICATOR_PARAMS.get('MACD_SLOW', 26)
            sig = config.INDICATOR_PARAMS.get('MACD_SIGNAL', 9)
            macd_series = df['close'].ewm(span=fast, adjust=False).mean() - df['close'].ewm(span=slow, adjust=False).mean()
            signal_series = macd_series.ewm(span=sig, adjust=False).mean()
            hist_series = macd_series - signal_series
            if len(hist_series) > 0: macd_hist = hist_series.iloc[-1]
            if len(hist_series) > 1: prev_macd_hist = hist_series.iloc[-2]

        if plus_di is None or minus_di is None:
            try:
                high_diff = df['high'].diff()
                low_diff = df['low'].diff()
                pos_dm = np.where((high_diff > 0) & (high_diff > -low_diff), high_diff, 0.0)
                neg_dm = np.where((low_diff < 0) & (-low_diff > high_diff), -low_diff, 0.0)
                tr1 = df['high'] - df['low']
                tr2 = (df['high'] - df['close'].shift()).abs()
                tr3 = (df['low'] - df['close'].shift()).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                adx_period = config.INDICATOR_PARAMS.get('ADX_PERIOD', 14)
                atr = tr.ewm(alpha=1/adx_period, adjust=False).mean()
                atr_val = atr.iloc[-1]
                if atr_val == 0 or np.isnan(atr_val): atr_val = 1.0 # 0 나누기 방어
                plus_di = 100 * pd.Series(pos_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
                minus_di = 100 * pd.Series(neg_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
            except Exception: pass

        if prev_cci is None and len(df) > 20:
            try:
                window = config.INDICATOR_PARAMS.get('CCI_WINDOW', 20)
                # [최적화] 동일 수식의 벡터화 버전에 위임 (rolling.apply 대비 100배 이상 빠름, 결과 동일)
                cci_series = indicators.get_cci_full_series(df, window)
                if len(cci_series) > 1: prev_cci = cci_series.iloc[-2]
            except Exception: pass

        vol_ma_period = config.INDICATOR_PARAMS.get('VOLUME_MA_PERIOD', 20)
        if len(df) >= vol_ma_period:
            vol_ma20 = df['volume'].rolling(window=vol_ma_period).mean().iloc[-1]
            vol_ma5 = df['volume'].rolling(window=5).mean().iloc[-1]
            
            if vol_ma5 > vol_ma20:
                vol_trend_flag = True
                
            if not vol_spike_flag:
                vol_ratio = config.INDICATOR_PARAMS.get('VOLUME_SPIKE_RATIO', 2.0)
                vol = df['volume'].iloc[-1]
                opn = df['open'].iloc[-1]
                if price is not None and vol_ma20 > 0 and vol >= (vol_ma20 * vol_ratio) and price > opn:
                    vol_spike_flag = True
                
    if price is None:
        return 0.0, details

    # 1. Trend Factor (4.0점)
    # [개선 #2] 이동평균선(EMA) 기반 신호들은 상호 상관(collinearity)이 매우 높아
    #          정배열 상승장에서 동시 충족되며 추세추종으로 점수가 편향됨.
    #          → MA 포지션 점수 합계를 상한(2.5점)으로 제한하고, 나머지 1.5점은
    #            상대적으로 독립적인 확인 신호(MACD 0선/추세확산, SAR)로 채워
    #            'MA 군집 단독'으로는 TREND 만점을 받지 못하도록 재구성.
    ma_trend_score = 0.0
    ma_details = []
    if ema20 is not None and price > ema20:
        s = round(0.5 * r_trend, 2); ma_trend_score += s; ma_details.append(f"EMA: 현재가 > 20일선 (+{s:.2f})")
    if ema20 is not None and ema60 is not None and ema120 is not None and ema20 > ema60 and ema60 > ema120:
        s = round(1.0 * r_trend, 2); ma_trend_score += s; ma_details.append(f"EMA: 20/60/120 정배열 (+{s:.2f})")
    if ema20 is not None and ema_5 is not None and ema_5 > ema20:
        s = round(0.5 * r_trend, 2); ma_trend_score += s; ma_details.append(f"EMA: 5일선 > 20일선 (+{s:.2f})")
    if ema20 is not None and ema60 is not None:
        if ema20 <= ema60 and price > ema60:
            s = round(0.5 * r_trend, 2); ma_trend_score += s; ma_details.append(f"EMA: 60일선 돌파 [초기] (+{s:.2f})")
        elif ema_5 is not None and price > ema_5 and ema_5 > ema20 and ema20 > ema60:
            s = round(0.5 * r_trend, 2); ma_trend_score += s; ma_details.append(f"EMA: 단기 급등 추세 (+{s:.2f})")
    if ema120 is not None and price > ema120:
        s = round(0.5 * r_trend, 2); ma_trend_score += s; ma_details.append(f"EMA: 장기 지지(현재가>120일선) (+{s:.2f})")

    # [개선 #2] MA 포지션 점수 상한 적용 (상관 신호의 과대 가점 방지)
    ma_cap = round(2.5 * r_trend, 2)
    details.extend(ma_details)
    if ma_trend_score > ma_cap:
        details.append(f"[상한] EMA 군집 신호 상한 적용 ({ma_cap:.2f})")
        ma_trend_score = ma_cap
    score += ma_trend_score

    # [개선 #2] MACD 0선 위(추세 확립): MA 포지션과 독립적인 추세 확인 신호
    if macd is not None and macd > 0:
        s = round(0.5 * r_trend, 2); score += s; details.append(f"MACD: 0선 위 (추세 확립) (+{s:.2f})")

    # [수정] 단순 MACD > Signal 상태 유지가 아닌, 신규 골든크로스 또는 0선 위 확산 추세일 때만 점수 부여 (인플레이션 방지)
    if macd_hist is not None and prev_macd_hist is not None:
        if macd_hist > 0 and prev_macd_hist <= 0:
            s = round(0.5 * r_trend, 2); score += s; details.append(f"MACD: 신규 골든크로스 (+{s:.2f})")
        elif macd is not None and macd > 0 and macd_hist > prev_macd_hist and macd_hist > 0:
            s = round(0.5 * r_trend, 2); score += s; details.append(f"MACD: 상승 추세 확산 (+{s:.2f})")

    if sar is not None and price > sar:
        s = round(0.5 * r_trend, 2); score += s; details.append(f"SAR: 상승 추세 (+{s:.2f})")
    
    # 2. Momentum Factor (2.5점)
    score_rsi_mid = config.INDICATOR_PARAMS.get('SCORE_RSI_MID', 50)
    score_rsi_strong = config.INDICATOR_PARAMS.get('SCORE_RSI_STRONG', 60)
    score_rsi_overheat = config.INDICATOR_PARAMS.get('SCORE_RSI_OVERHEAT', 80)
    score_rsi_rebound = config.INDICATOR_PARAMS.get('SCORE_RSI_REBOUND', 40)

    if rsi is not None:
        # [Fix] RSI 상단 제한(75) 해제로 과열 구간에서도 기본 강세 점수는 유지 (스코어 클리프 방지)
        if rsi >= score_rsi_mid:
            s = round(0.5 * r_mom, 2); score += s; details.append(f"RSI: 강세 구간 (+{s:.2f})")
            # [개선 #6] 과열 구간(>=80)에서는 추가 '모멘텀 확장' 가점을 동결하여
            #          이미 과열된 종목에 고점매수 신호가 강화되는 것을 방지.
            if score_rsi_strong <= rsi < score_rsi_overheat:
                s = round(0.5 * r_mom, 2); score += s; details.append(f"RSI: 모멘텀 확장 (+{s:.2f})")
        elif score_rsi_rebound <= rsi < score_rsi_mid:
            s = round(0.5 * r_mom, 2); score += s; details.append(f"RSI: 상승 여력 구간 (+{s:.2f})")
            
    cci_lower = config.INDICATOR_PARAMS.get('CCI_LOWER', -100)
    cci_strong = config.INDICATOR_PARAMS.get('SCORE_CCI_STRONG', 0)
    cci_mom = config.INDICATOR_PARAMS.get('SCORE_CCI_MOMENTUM', 50)
    if cci is not None:
        if cci > cci_strong:
            s = round(0.5 * r_mom, 2); score += s; details.append(f"CCI: 상승 추세 (+{s:.2f})")
        if prev_cci is not None and prev_cci <= cci_lower and cci > cci_lower:
            s = round(0.5 * r_mom, 2); score += s; details.append(f"CCI: 과매도권 탈출 (+{s:.2f})")
        elif cci >= cci_mom:
            s = round(0.5 * r_mom, 2); score += s; details.append(f"CCI: 모멘텀 심화 (+{s:.2f})")
            
    if plus_di is not None and minus_di is not None and plus_di > minus_di:
        s = round(0.5 * r_mom, 2); score += s; details.append(f"DMI: +DI > -DI 크로스 (+{s:.2f})")

    # 3. Strength & Volume Factor (1.5점)
    adx_min = config.INDICATOR_PARAMS.get('SCORE_ADX_MIN', 20)
    if adx is not None and adx >= adx_min:
        s = round(0.5 * r_str, 2); score += s; details.append(f"ADX: 추세 형성 (+{s:.2f})")
        
    if vol_spike_flag or vol_trend_flag:
        s = round(0.5 * r_str, 2); score += s
        if vol_spike_flag:
            details.append(f"VOL: 거래량 폭증/양봉 (+{s:.2f})")
        else:
            details.append(f"VOL: 거래량 추세 상승 (+{s:.2f})")
        
    if obv_trend or smart_money:
        s = round(0.5 * r_str, 2); score += s; details.append(f"수급: OBV/SM 개선 (+{s:.2f})")

    # 4. Synergy Bonus (2.0점)
    # [수정] 시너지 보너스를 확산(macd_hist > prev_macd_hist) 조건 단독에서 완화. MACD가 시그널 위에 있고(macd_hist > 0) 심한 축소가 아닐 때 유지하여 단기 노이즈로 인한 2.0점 증발 방어
    is_macd_expanding = False
    if macd_hist is not None and prev_macd_hist is not None:
        is_macd_expanding = (macd_hist > 0 and macd_hist >= prev_macd_hist * 0.8)
    
    if ema60 is not None and price > ema60 and is_macd_expanding and (adx is not None and adx >= adx_min):
        s = round(1.0 * r_syn, 2)
        score += s
        details.append(f"추세 시작: 주가>60일선+MACD확산+ADX 20↑ (+{s:.2f})")
        
    # Momentum Thrust
    if is_macd_expanding and (rsi is not None and rsi >= score_rsi_strong) and obv_trend:
        s = round(1.0 * r_syn, 2)
        score += s
        details.append(f"모멘텀 폭발: MACD확산+RSI 60↑+OBV (+{s:.2f})")

    # 5. 추세 악화 감점 (Deterioration Penalty)
    # [개선 #1] 기존 스코어는 '가산 전용'이라 추세가 꺾여도 후행 지표(EMA 정배열 등)가
    #          점수를 떠받쳐 매도 신호(SELL_SCORE 미달)가 지연되는 구조적 약점이 있음.
    #          명확한 하락 반전 신호에 대해 감점을 부여하여 점수가 악화를 적시 반영하도록 보정.
    penalty = 0.0
    if macd is not None and macd_signal is not None and macd < macd_signal:
        p = round(0.5 * r_trend, 2); penalty -= p; details.append(f"감점: MACD 데드크로스 (-{p:.2f})")
    if macd_hist is not None and prev_macd_hist is not None and macd_hist < 0 and macd_hist < prev_macd_hist:
        p = round(0.5 * r_mom, 2); penalty -= p; details.append(f"감점: MACD 하락 가속(0선 이하 확대) (-{p:.2f})")
    if plus_di is not None and minus_di is not None and minus_di > plus_di:
        p = round(0.5 * r_str, 2); penalty -= p; details.append(f"감점: -DI 우위(매도세 강화) (-{p:.2f})")

    score += penalty
    if score < 0:
        score = 0.0

    return round(score, 2), details

# [추가] 시장 지수 데이터 공유 캐시
# get_market_regime은 자동매매/체결감시/예약감시/텔레그램 등 여러 스레드에서 동시에 호출되며,
# 매 호출마다 2년치 지수차트(inquire-daily-indexchartprice)를 재조회하면 모의투자(2 TPS) 서버에
# 요청 폭주(EGW00201)가 발생한다. 이를 막기 위해 아래 3중 방어를 적용한다.
#   1) single-flight : 동일 지수의 동시 캐시 미스 시 1개 스레드만 실제 조회(스탬피드 차단).
#   2) negative cache: 조회 실패(빈 결과)도 짧게 기록해 폭주 중 재조회 폭발을 억제하고,
#                      직전 정상 데이터를 stale 폴백으로 보존한다.
#   3) stale-while-revalidate: TTL 만료 시 옛 값을 즉시 반환하고 백그라운드 1스레드로만 갱신
#                      → 5분 주기 캐시 절벽/블로킹 제거.
_INDEX_DATA_CACHE = {}          # {market_type: {'df': df, 'time': ts, 'fail_time': ts}}
_INDEX_DATA_CACHE_LOCK = threading.Lock()   # 캐시 딕셔너리 보호
_INDEX_DATA_CACHE_TTL = 300     # 정상 데이터 유효시간(5분) - 장중 국면 판단에는 충분히 신선함
_INDEX_DATA_NEG_TTL = 30        # 실패 후 재조회 억제시간(초) - 폭주 자기증식 차단

_INDEX_FETCH_LOCKS = {}         # {market_type: Lock} single-flight 동기 조회 잠금
_INDEX_FETCH_LOCKS_GUARD = threading.Lock()
_INDEX_REFRESH_INFLIGHT = set() # 백그라운드 재검증 진행 중인 market_type
_INDEX_REFRESH_GUARD = threading.Lock()

def _index_cache_enabled():
    """테스트(pytest) 환경에서는 모킹된 지수 데이터가 캐시에 고착되지 않도록 캐시를 비활성화한다."""
    return "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ

def _get_index_fetch_lock(market_type):
    """market_type별 single-flight 잠금을 반환한다(없으면 생성)."""
    with _INDEX_FETCH_LOCKS_GUARD:
        lock = _INDEX_FETCH_LOCKS.get(market_type)
        if lock is None:
            lock = threading.Lock()
            _INDEX_FETCH_LOCKS[market_type] = lock
        return lock

def _lookup_index_cache(market_type):
    """캐시 상태를 (status, df)로 반환한다.
    - 'fresh'   : TTL 이내 정상 데이터 → 즉시 반환
    - 'suppress': 최근 실패(음성 TTL 이내) → 재조회 없이 직전 정상 데이터(또는 None) 반환
    - 'stale'   : TTL 경과한 정상 데이터 존재 → 즉시 반환 후 백그라운드 갱신
    - 'miss'    : 쓸만한 데이터 없음 → 동기 조회 필요
    """
    now = time.time()
    with _INDEX_DATA_CACHE_LOCK:
        entry = _INDEX_DATA_CACHE.get(market_type)
        if not entry:
            return 'miss', None
        df = entry.get('df')
        has_good = df is not None and not df.empty
        # 최근 실패가 음성 TTL 이내면 재조회를 억제(폭주 차단)하고 가진 데이터를 반환
        if (now - entry.get('fail_time', 0)) < _INDEX_DATA_NEG_TTL:
            return 'suppress', df
        if has_good:
            if (now - entry.get('time', 0)) < _INDEX_DATA_CACHE_TTL:
                return 'fresh', df
            return 'stale', df
        return 'miss', None

def _store_index_cache(market_type, df):
    """조회 결과를 캐시에 반영한다. 정상 데이터는 갱신하고, 실패(빈 결과)는
    직전 정상 데이터를 보존한 채 실패 시각만 기록(음성 캐시)한다."""
    if not _index_cache_enabled():
        return
    now = time.time()
    with _INDEX_DATA_CACHE_LOCK:
        entry = _INDEX_DATA_CACHE.get(market_type) or {'df': None, 'time': 0, 'fail_time': 0}
        if df is not None and not df.empty:
            entry['df'] = df
            entry['time'] = now
            entry['fail_time'] = 0
        else:
            # 실패: 직전 정상 df/time은 보존, 실패 시각만 갱신
            entry['fail_time'] = now
        _INDEX_DATA_CACHE[market_type] = entry

def _trigger_async_refresh(market_type):
    """stale 데이터 제공 후 백그라운드에서 1스레드로만 캐시를 재검증한다."""
    if not _index_cache_enabled():
        return
    with _INDEX_REFRESH_GUARD:
        if market_type in _INDEX_REFRESH_INFLIGHT:
            return  # 이미 갱신 중 → 중복 기동 방지
        _INDEX_REFRESH_INFLIGHT.add(market_type)

    def _worker():
        try:
            fresh = _fetch_domestic_index_data(market_type)
            _store_index_cache(market_type, fresh)
        except Exception as e:
            logger.debug(f"[MARKET_INDEX_DEBUG] {market_type} 비동기 재검증 실패: {e}")
            _store_index_cache(market_type, None)  # 실패 기록(음성 캐시)
        finally:
            with _INDEX_REFRESH_GUARD:
                _INDEX_REFRESH_INFLIGHT.discard(market_type)

    threading.Thread(target=_worker, name=f"IndexRefresh-{market_type}", daemon=True).start()

def _fetch_domestic_index_data(market_type):
    """국내 지수 데이터를 실제 조회한다(캐시 미적용). KIS API -> yfinance Fallback."""
    kis_code = "0001"
    yf_ticker = "^KS11"

    if market_type == "KOSDAQ":
        kis_code = "1001"
        yf_ticker = "^KQ11"
    elif market_type == "KOSPI200":
        kis_code = "2001"
        yf_ticker = "^KS200"
    elif market_type == "KOSDAQ150":
        kis_code = "2203"
        yf_ticker = "^KQ150"

    df = None
    try:
        # 1. KIS API 조회 (토스 모드에서는 KIS를 사용하지 않고 yfinance Fallback 사용)
        if config.session.is_toss:
            df = None
        else:
            df = api.get_domestic_index_chart(kis_code)

        # [Fix] KIS API 컬럼명 표준화 및 타입 변환
        if df is not None and not df.empty:
            rename_map = {
                'stck_bsop_date': 'date',
                'bstp_nmix_prpr': 'close',
                'bstp_nmix_oprc': 'open',
                'bstp_nmix_hgpr': 'high',
                'bstp_nmix_lwpr': 'low',
                'acml_vol': 'volume'
            }
            df.rename(columns=rename_map, inplace=True)

            for col in ['close', 'open', 'high', 'low', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            df.attrs['source'] = 'KIS' # [추가] 데이터 소스 명시

    except Exception as e:
        logger.debug(f"[MARKET_INDEX_DEBUG] {market_type} KIS API 조회 실패: {e}")

    # 2. Fallback 체크
    # [수정] 지수 데이터는 국면 판단(EMA, REGIME_MA_PERIOD)과 시장 필터링(SMA, MARKET_FILTER_MA)이
    #  함께 사용하므로 두 기간 중 큰 값을 기준으로 충분성을 판단한다.
    #  (KIS 지수 차트는 약 50일치만 제공 → MARKET_FILTER_MA 60일 설정 시 yfinance로 자동 대체)
    ma_period = config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20)
    if getattr(config, 'USE_MARKET_FILTER', True):
        ma_period = max(ma_period, getattr(config, 'MARKET_FILTER_MA', 50))

    if df is None or df.empty or len(df) < ma_period:
        logger.debug(f"[MARKET_INDEX_DEBUG] {market_type} KIS API 데이터 부족/실패({len(df) if df is not None else 0}건) -> yfinance({yf_ticker}) Fallback 시도")

        # [Fix] KOSDAQ150은 yfinance 티커(^KQ150)가 불안정/미지원이므로 Fallback을 수행하지 않음
        #   (토스 모드는 KIS 대안이 없어 데이터 없음 → 화면에서 '-' 처리)
        if market_type == "KOSDAQ150":
            logger.debug(f"[MARKET_INDEX_DEBUG] KOSDAQ150({yf_ticker}) yfinance Fallback을 건너뜁니다 (티커 미지원).")
            return df

        try:
            df = api.get_chart_data(yf_ticker, is_overseas=True)
            if df is not None:
                df.attrs['source'] = 'YFINANCE' # [추가] 데이터 소스 명시
        except Exception as e:
            logger.debug(f"[MARKET_INDEX_DEBUG] {market_type} yfinance Fallback 실패: {e}")

    return df

def get_domestic_index_data(market_type, force_refresh=False):
    """국내 지수 데이터 조회 (KIS API -> yfinance Fallback, 공유 캐시 적용)"""
    # 테스트 환경: 캐시/스탬피드 방어 없이 매번 직접 조회(모킹 데이터 고착 방지)
    if not _index_cache_enabled():
        return _fetch_domestic_index_data(market_type)

    # 1. 빠른 경로: 락 없이 캐시 상태만 판정
    if not force_refresh:
        status, df = _lookup_index_cache(market_type)
        if status in ('fresh', 'suppress'):
            return df
        if status == 'stale':
            _trigger_async_refresh(market_type)  # stale 즉시 반환 + 백그라운드 갱신
            return df
        # status == 'miss': 동기 조회 필요

    # 2. single-flight: market_type별 1개 스레드만 실제 조회, 나머지는 대기 후 결과 공유
    fetch_lock = _get_index_fetch_lock(market_type)
    with fetch_lock:
        stale_df = None
        # 대기 중 다른 스레드가 캐시를 채웠을 수 있으니 재확인(강제 갱신 제외)
        if not force_refresh:
            status, df = _lookup_index_cache(market_type)
            if status in ('fresh', 'suppress'):
                return df
            if status == 'stale':
                _trigger_async_refresh(market_type)
                return df
            stale_df = df  # 'miss' → 폴백용(보통 None)

        df = _fetch_domestic_index_data(market_type)
        _store_index_cache(market_type, df)

        # 조회 실패(빈 결과) 시 직전 정상 데이터(stale)로 폴백
        if (df is None or df.empty) and stale_df is not None and not stale_df.empty:
            return stale_df
        return df

def get_market_regime(market_type="KOSPI"):
    """시장 국면 판단 (Bull/Bear/Sideways)

    자동매매는 매 주기(매도검사·매수스캔 각각) 시장별로 호출하므로, 짧은 TTL 캐시로
    지수 차트 조회 + 지표 재계산 중복을 제거한다. (국면은 초 단위로 변하지 않음)
    데이터 부족/오류 폴백("Sideways", 0.0)은 일시 장애일 수 있어 캐시하지 않는다.
    """
    cached = _MARKET_REGIME_CACHE.get(market_type, ttl=_MARKET_REGIME_TTL_SEC)
    if cached is not None:
        return cached

    try:
        # [수정] 공통 함수를 통해 데이터 조회 (Fallback 적용)
        df = get_domestic_index_data(market_type)

        # [수정] 설정된 MA 기간 가져오기
        ma_period = config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20)

        if df is None or df.empty or len(df) < ma_period:
            return "Sideways", 0.0 # 데이터 부족 시 횡보로 가정

        current_price = float(df.iloc[-1]['close'])

        # 지표 계산
        adx_threshold = config.MARKET_REGIME_PARAMS.get("REGIME_ADX_THRESHOLD", 20)

        ma_val = df['close'].ewm(span=ma_period, adjust=False).mean().iloc[-1]

        # MA 기울기 (최근 5일)
        ma_series = df['close'].ewm(span=ma_period, adjust=False).mean()
        slope = (ma_series.iloc[-1] - ma_series.iloc[-5]) / 5

        # ADX 계산
        ind = indicators.calculate_indicators(df)
        adx = ind['adx']

        # 국면 판단 로직
        # 1. 강세장: 지수 > MA & 기울기 > 0 & ADX > 기준
        if current_price > ma_val and slope > 0 and adx >= adx_threshold:
            result = ("Bull", config.MARKET_REGIME_PARAMS.get("BULL_SCORE_ADJ", -1.0))
        # 2. 약세장: 지수 < MA
        elif current_price < ma_val:
            result = ("Bear", config.MARKET_REGIME_PARAMS.get("BEAR_SCORE_ADJ", 1.0))
        # 3. 횡보장: 그 외
        else:
            result = ("Sideways", config.MARKET_REGIME_PARAMS.get("SIDEWAYS_SCORE_ADJ", 0.0))

        _MARKET_REGIME_CACHE.set(market_type, result)
        return result

    except Exception as e:
        logger.error(f"시장 국면 판단 오류: {e}")
        return "Sideways", 0.0

def classify_stock_state(price=None, ema20=None, ema60=None, ema120=None, sar=None, rsi=None, prev_rsi=None, adx=None, cci=None, obv_trend=None, macd=None, macd_signal=None, thresholds=None, w52_pos=None, smart_money=False, plus_di=None, minus_di=None, df=None, ind=None, ema_5=None, macd_hist=None, prev_macd_hist=None, prev_cci=None, vol_spike=False, vol_trend=False, is_yangbong=False):
    if df is not None and ind is not None:
        if not df.empty: price = float(df.iloc[-1]['close'])
        ema20 = ind.get('ema_20')
        ema60 = ind.get('ema_60')
        ema120 = ind.get('ema_120')
        sar = ind.get('psar')
        rsi = ind.get('rsi')
        adx = ind.get('adx')
        cci = ind.get('cci')
        obv_trend = ind.get('obv_trend')
        macd = ind.get('macd')
        macd_signal = ind.get('macd_signal')
        if plus_di is None: plus_di = ind.get('plus_di')
        if minus_di is None: minus_di = ind.get('minus_di')

        # [추가] 인자로 넘어오지 않은 세부 지표들을 ind 딕셔너리에서 직접 추출
        if ema_5 is None: ema_5 = ind.get('ema_5')
        if prev_cci is None: prev_cci = ind.get('prev_cci')
        if macd_hist is None: macd_hist = ind.get('macd_hist')
        if prev_macd_hist is None: prev_macd_hist = ind.get('prev_macd_hist')

        if plus_di is None or minus_di is None:
            import numpy as np
            try:
                high_diff = df['high'].diff()
                low_diff = df['low'].diff()
                pos_dm = np.where((high_diff > 0) & (high_diff > -low_diff), high_diff, 0.0)
                neg_dm = np.where((low_diff < 0) & (-low_diff > high_diff), -low_diff, 0.0)
                tr1 = df['high'] - df['low']
                tr2 = (df['high'] - df['close'].shift()).abs()
                tr3 = (df['low'] - df['close'].shift()).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                adx_period = config.INDICATOR_PARAMS.get('ADX_PERIOD', 14)
                atr = tr.ewm(alpha=1/adx_period, adjust=False).mean()
                atr_val = atr.iloc[-1]
                if atr_val == 0 or np.isnan(atr_val): atr_val = 1.0 # 0 나누기 방어
                plus_di = 100 * pd.Series(pos_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
                minus_di = 100 * pd.Series(neg_dm).ewm(alpha=1/adx_period, adjust=False).mean().iloc[-1] / atr_val
            except Exception: pass
            
    if price is None or ema60 is None or sar is None or rsi is None:
        return "-", "[dim]", "데이터 부족"
    
    # [수정] 1순위 절대 방어 필터를 가장 위로 끌어올림 (역추세 매수보다 우선 적용하여 떨어지는 칼날 완벽 방어)
    if plus_di is not None and minus_di is not None and minus_di > plus_di:
        if adx is not None and adx >= 45: # ADX 45 이상의 초강력 하락장
            return "매도", "[blue]", "초강력 투매 패닉 구간 (ADX 과열 및 -DI 우위)"

    # [추가] 2. 낙폭과대(역추세) 반등 조건 확인
    use_mr = thresholds.get("USE_MEAN_REVERSION", config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)) if thresholds else config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)
    if use_mr and ema20 is not None and prev_rsi is not None and rsi is not None:
        mr_rsi = thresholds.get("MR_RSI_MAX", config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
        mr_disp = thresholds.get("MR_DISPARITY_MAX", config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
        
        disparity = (price / ema20) * 100
        
        is_yangbong_flag = is_yangbong
        if df is not None and not df.empty:
            is_yangbong_flag = df.iloc[-1]['close'] > df.iloc[-1]['open']

        # [개선 #3] 떨어지는 칼날(데드캣) 방어 강화 — 하락 가속도 둔화 확인.
        #          기존 조건(RSI 반등+양봉+수급)만으로는 ADX<45 의 추세적 하락 중에도
        #          역매수가 발동될 수 있어 방어가 얇음. df가 있을 때, 당일 저가가
        #          직전 5거래일 저점을 추가로 경신(=신저가 갱신, 낙하 지속)하면 진입 보류.
        mr_decel_ok = True
        if df is not None and not df.empty and len(df) >= 6:
            try:
                prior_low = df['low'].iloc[-6:-1].min()
                today_low = df['low'].iloc[-1]
                if today_low < prior_low:
                    mr_decel_ok = False
            except Exception:
                pass

        # [수정] RSI 반등 + 양봉 + 수급 확인(OBV 상승 또는 스마트머니 유입) + 하락 둔화를 모두 만족해야 역매수 발동
        # 조건 미충족 시 단순 기술적 반등(데드캣)으로 간주하여 진입 보류
        if rsi <= mr_rsi and rsi > prev_rsi and disparity <= mr_disp and is_yangbong_flag and (obv_trend or smart_money) and mr_decel_ok:
            return "역매수", "[magenta]", "낙폭과대 (역매수 반등 신호 + 양봉 + 수급 확인 + 하락 둔화)"

    # [수정] 가중치 적용 점수 계산 (thresholds에 weights가 포함되어 있을 수 있음)
    weights = thresholds.get("WEIGHTS") if thresholds else None
    score, _ = calculate_score(
        price=price, ema20=ema20, ema60=ema60, ema120=ema120, sar=sar, rsi=rsi, adx=adx, cci=cci, 
        obv_trend=obv_trend, macd=macd, macd_signal=macd_signal, weights=weights, smart_money=smart_money, 
        plus_di=plus_di, minus_di=minus_di, df=df, ind=ind, ema_5=ema_5, macd_hist=macd_hist, 
        prev_macd_hist=prev_macd_hist, prev_cci=prev_cci, vol_spike=vol_spike, vol_trend=vol_trend
    )

    # [수정] config.py의 설정값을 사용하여 상태 판정
    if thresholds:
        buy_score = thresholds.get("BUY_SCORE", config.ANALYSIS_THRESHOLDS["BUY_SCORE"])
        rise_score = thresholds.get("RISE_SCORE", config.ANALYSIS_THRESHOLDS["RISE_SCORE"])
        buy_rsi_max = thresholds.get("BUY_RSI_MAX", config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"])
        interest_min = thresholds.get("INTEREST_SIGNAL_MIN", config.ANALYSIS_THRESHOLDS.get("INTEREST_SIGNAL_MIN", 3))
        interest_ma60_near = thresholds.get("INTEREST_MA60_NEAR", config.ANALYSIS_THRESHOLDS.get("INTEREST_MA60_NEAR", 0.97))

        use_super = thresholds.get("SUPER_MOMENTUM_USE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True))
        super_score = thresholds.get("SUPER_MOMENTUM_SCORE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5))
        super_w52 = thresholds.get("SUPER_MOMENTUM_W52_POS", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0))
        super_rsi = thresholds.get("SUPER_BUY_RSI_MAX", config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 75.0))
    else:
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        buy_rsi_max = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        
        interest_min = config.ANALYSIS_THRESHOLDS.get("INTEREST_SIGNAL_MIN", 3)
        interest_ma60_near = config.ANALYSIS_THRESHOLDS.get("INTEREST_MA60_NEAR", 0.97)

        use_super = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True)
        super_score = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5)
        super_w52 = config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0)
        super_rsi = config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 75.0)

    reasons = []
    is_severe_danger = False
    
    # [수정] 위험 조건 완화: 장기(120)와 중기(60) 이평선을 모두 이탈해야 '위험'으로 간주 (변동성 감소)
    if ema120 is not None and price < ema120 and price < ema60: 
        is_severe_danger = True
        reasons.append("이평선 완전 이탈(60&120)")
    elif rsi <= (config.INDICATOR_PARAMS["RSI_LOWER"] - 10): 
        is_severe_danger = True # 위험 기준은 하한선보다 더 낮게 설정 (예: 20)
        reasons.append(f"RSI 초과매도({rsi:.1f})")
        
    # [수정] ADX 과열 중 RSI 하락은 '위험'보다는 '주의'로 이동
    if is_severe_danger: return "매도", "[blue]", ", ".join(reasons)
    
    is_caution = False
    # [추가] 위험형 주의(hard): '관심(태동)'으로도 분류 불가한 명백한 하락/과열 신호.
    #        약세형(soft: 단순 장기이평 이탈)과 분리하여, 60/120일선 아래여도
    #        추세 전환 초기 신호가 있으면 '관심'으로 건질 수 있도록 한다.
    hard_caution = False
    # [수정] 주의 조건: 60일선 이탈 또는 120일선 이탈 → 약세형(soft, 관심 후보 가능)
    if price < ema60:
        is_caution = True
        reasons.append("60일선 이탈")
    if ema120 is not None and price < ema120:
        is_caution = True
        reasons.append("120일선 이탈")
    if sar is not None and sar > price:
        is_caution = True; hard_caution = True
        reasons.append("SAR 매도신호")
    if rsi >= (config.INDICATOR_PARAMS["RSI_UPPER"] + 10):
        is_caution = True; hard_caution = True
        reasons.append(f"RSI 과열({rsi:.1f})")
    elif rsi <= config.INDICATOR_PARAMS["RSI_LOWER"]:
        is_caution = True; hard_caution = True
        reasons.append(f"RSI 침체({rsi:.1f})")
    elif adx is not None and prev_rsi is not None and adx >= 40 and rsi < prev_rsi:
        is_caution = True; hard_caution = True
        reasons.append(f"ADX과열({adx:.1f})+RSI하락")

    if macd is not None and macd_signal is not None and macd < macd_signal:
        is_caution = True; hard_caution = True
        reasons.append("MACD 데드크로스")
    # [추가] DMI 매도세 우위 필터
    if plus_di is not None and minus_di is not None and minus_di > plus_di:
        is_caution = True; hard_caution = True
        reasons.append("-DI 우위(매도세 강함)")

    # 2순위: 얼리 스테이지 및 기본 매수 조건 (위험 필터를 모두 통과해야 함)
    is_super = use_super and score >= super_score and w52_pos is not None and w52_pos >= super_w52
    actual_buy_rsi_max = super_rsi if is_super else buy_rsi_max

    if score >= buy_score and rsi < actual_buy_rsi_max:
        # [핵심 방어 로직] 하락 반전 신호(고점 꺾임)를 방어하기 위한 강력 필터
        # 다음 조건 중 하나라도 해당되면 아무리 점수가 높아도 "매수"가 아닌 "주의"로 강등
        down_trend_flags = []
        if sar is not None and sar > price: down_trend_flags.append("SAR 매도신호")
        if macd is not None and macd_signal is not None and macd < macd_signal: down_trend_flags.append("MACD 데드크로스")
        if plus_di is not None and minus_di is not None and minus_di > plus_di: down_trend_flags.append("-DI 우위")
        
        if down_trend_flags:
            is_caution = True; hard_caution = True
        else:
            if is_super: return "강매수", "[magenta]", "매수 조건 충족 (슈퍼 모멘텀 적용)"
            else: return "매수", "[red]", "매수 조건 충족 (얼리 스테이지 반등 포함)"

    # [상승] 주의(약세+위험) 신호가 전혀 없고 점수가 양호 → 추세 정렬이 완성된 강한 상태
    if not is_caution and score >= rise_score:
        return "상승", "[orange3]", "상승 추세 (점수 양호)"

    # [관심(태동)] 추세 정렬은 미완성('상승' 미달)이나, 위험형 신호가 없고
    #   추세 전환 초기 신호가 INTEREST_SIGNAL_MIN개 이상 포착된 종목.
    #   60/120일선 아래(약세형 주의)여도 초기 반등 여력을 빠르게 포착하기 위함.
    #   수동 스윙 매매 모니터링 대상으로 표시한다.
    if not hard_caution and interest_min > 0:
        interest_signals = []
        if ema_5 is not None and ema20 is not None and ema_5 > ema20:
            interest_signals.append("단기 골든크로스(5>20)")
        if macd_hist is not None and prev_macd_hist is not None and macd_hist > prev_macd_hist:
            interest_signals.append("MACD 히스토그램 개선")
        elif macd is not None and macd_signal is not None and macd > macd_signal:
            interest_signals.append("MACD 골든크로스")
        if plus_di is not None and minus_di is not None and plus_di > minus_di:
            interest_signals.append("+DI 우위")
        if rsi is not None and prev_rsi is not None and rsi >= config.INDICATOR_PARAMS.get("RSI_MID", 50) and rsi > prev_rsi:
            interest_signals.append("RSI 50선 상향")
        if cci is not None and prev_cci is not None and cci > prev_cci and cci > -100:
            interest_signals.append("CCI 개선")
        if vol_trend or vol_spike or obv_trend or smart_money:
            interest_signals.append("수급 유입")
        if price is not None and ema60 is not None and price >= ema60 * interest_ma60_near:
            interest_signals.append("60일선 근접/돌파")

        if len(interest_signals) >= interest_min:
            return "관심", "[green]", "추세 전환 초기 신호 " + str(len(interest_signals)) + "개 (" + ", ".join(interest_signals) + ")"

    if is_caution: return "주의", "[yellow]", ", ".join(reasons)
    return "관망", "[white]", "방향성 탐색 구간"

@contextlib.contextmanager
def _get_db_connection():
    """SQLite 연결 컨텍스트 매니저.

    sqlite3의 기본 `with conn:` 구문은 트랜잭션(commit/rollback)만 관리하고
    연결 자체는 닫지 않아 ResourceWarning(unclosed database)이 발생한다.
    이 컨텍스트 매니저로 감싸 `with _get_db_connection() as conn:` 종료 시
    연결을 확실히 닫는다.
    """
    conn = sqlite3.connect(config.DB_FILE_PATH)
    try:
        yield conn
    finally:
        conn.close()

def _init_analysis_db_logic():
    try:
        with _get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_analysis_cache (
                    market_type TEXT PRIMARY KEY,
                    updated_at TEXT,
                    params TEXT,
                    data TEXT
                )
            """)
            conn.commit()
    except Exception: pass

def _save_analysis_result_logic(market_type, results, params):
    try:
        _init_analysis_db()
        with _get_db_connection() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data_json = json.dumps(results, ensure_ascii=False)
            params_json = json.dumps(params, ensure_ascii=False)
            
            cursor.execute("""
                INSERT OR REPLACE INTO market_analysis_cache (market_type, updated_at, params, data)
                VALUES (?, ?, ?, ?)
            """, (market_type, now_str, params_json, data_json))
            conn.commit()
    except Exception as e:
        config.console.print(f"[dim red]분석 결과 저장 실패: {e}[/dim red]")

def _load_analysis_result_logic(market_type):
    try:
        _init_analysis_db()
        with _get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT updated_at, params, data FROM market_analysis_cache WHERE market_type = ?", (market_type,))
            row = cursor.fetchone()
            if row:
                return {
                    'updated_at': row[0],
                    'params': json.loads(row[1]),
                    'data': json.loads(row[2])
                }
    except Exception as e:
        config.console.print(f"[dim red]분석 결과 로드 실패: {e}[/dim red]")
    return None

# [수정] 큐 시스템을 통한 실행 래퍼 함수들
def _init_analysis_db():
    _init_analysis_db_logic()

def _save_analysis_result(market_type, results, params):
    if hasattr(db_manager.db, 'execute_custom'):
        db_manager.db.execute_custom(_save_analysis_result_logic, market_type, results, params)
    else:
        _save_analysis_result_logic(market_type, results, params)

def _load_analysis_result(market_type):
    if hasattr(db_manager.db, 'execute_custom'):
        return db_manager.db.execute_custom(_load_analysis_result_logic, market_type)
    else:
        return _load_analysis_result_logic(market_type)

def _get_master_stock_list(market_type):
    """(내부함수) 마스터 파일 다운로드 및 파싱하여 종목 리스트 반환"""
    base_dir = getattr(config, 'DATA_DIR', 'data')
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)

    if market_type == 'KOSPI':
        url = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
        filename = "kospi_code.mst"
    else:
        url = "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"
        filename = "kosdaq_code.mst"

    zip_path = os.path.join(base_dir, f"{filename}.zip")
    extract_path = os.path.join(base_dir, filename)
    
    stock_list = []

    try:
        # [수정] 파일이 존재하고 오늘 다운로드된 것이라면 다운로드 스킵
        need_download = True
        if os.path.exists(zip_path) and os.path.exists(extract_path):
            file_time = datetime.fromtimestamp(os.path.getmtime(zip_path))
            if file_time.date() == datetime.now().date():
                need_download = False
                config.console.print(f"[dim]{market_type} 마스터 파일이 최신입니다. (기존 파일 사용)[/dim]")

        if need_download:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                "•",
                DownloadColumn(),
                "•",
                TransferSpeedColumn(),
                "•",
                TimeRemainingColumn(),
                console=config.console
            ) as progress:
                task_id = progress.add_task(f"[cyan]{market_type} 마스터 파일 다운로드...[/cyan]", total=None)
                
                def report_hook(block_num, block_size, total_size):
                    progress.update(task_id, total=total_size, completed=block_num * block_size)
                
                urllib.request.urlretrieve(url, zip_path, reporthook=report_hook)

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=config.console,
                transient=True
            ) as progress:
                progress.add_task(f"[cyan]{market_type} 데이터 압축 해제 중...[/cyan]", total=None)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(base_dir)
        
        # [수정] 파일 파싱 시 Progress Bar 적용 (파일 크기 기준)
        file_size = os.path.getsize(extract_path)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task(f"[cyan]{market_type} 종목 리스트 파싱 중...[/cyan]", total=file_size)
            
            with open(extract_path, 'rb') as f:
                for line in f:
                    progress.advance(task, advance=len(line))
                    try:
                        code = line[0:9].decode('cp949').strip()
                        name = line[21:61].decode('cp949').strip()
                        
                        # [수정] 영문이 포함된 최신 ETF(예: 0080G0)를 지원하기 위해 숫자로 시작하는 6자리 영문/숫자 코드로 완화
                        if len(code) == 6 and code[0].isdigit() and code.isalnum():
                            stock_list.append({'code': code, 'name': name})
                    except Exception:
                        continue
    except Exception as e:
        config.console.print(f"[red]{market_type} 마스터 파일 처리 실패: {e}[/red]")
        
    return stock_list

# [추가] 마스터 코드 기반 시장 구분 캐시
_MASTER_KOSDAQ_CODES = None
_MASTER_KOSPI_CODES = None

def _get_market_type_by_master(code):
    """마스터 파일(KOSPI/KOSDAQ)을 참조하여 종목의 시장 구분을 정확히 반환합니다.

    KOSDAQ/KOSPI 마스터 양쪽을 모두 조회한다.
      - KOSDAQ 마스터에 있으면 'KOSDAQ'
      - KOSPI 마스터에 있으면 'KOSPI'
      - 둘 다 없으면(신규상장/누락 등) 보수적으로 'KOSPI'로 폴백
    """
    global _MASTER_KOSDAQ_CODES, _MASTER_KOSPI_CODES
    if _MASTER_KOSDAQ_CODES is None:
        try:
            k_list = _get_master_stock_list("KOSDAQ")
            _MASTER_KOSDAQ_CODES = set(s['code'] for s in k_list)
        except Exception as e:
            logger.debug(f"KOSDAQ 마스터 목록 로드 실패: {e}")
            _MASTER_KOSDAQ_CODES = set()
    if _MASTER_KOSPI_CODES is None:
        try:
            p_list = _get_master_stock_list("KOSPI")
            _MASTER_KOSPI_CODES = set(s['code'] for s in p_list)
        except Exception as e:
            logger.debug(f"KOSPI 마스터 목록 로드 실패: {e}")
            _MASTER_KOSPI_CODES = set()

    if code in _MASTER_KOSDAQ_CODES:
        return "KOSDAQ"
    if code in _MASTER_KOSPI_CODES:
        return "KOSPI"
    # 어느 마스터에도 없으면(신규상장 등) 보수적 폴백
    logger.debug(f"마스터 미발견 종목({code}) → KOSPI로 폴백 처리")
    return "KOSPI"

def diagnose_stock(target_code=None, target_name=None, target_is_overseas=False):
    """특정 종목에 대해 시스템 트레이딩 로직을 진단(시뮬레이션)합니다."""
    
    code, name, is_overseas = None, None, False

    if target_code:
        code, name, is_overseas = target_code, target_name, target_is_overseas
    else:
        menu_items = [
            ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
            ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"), ("5", "직접 입력", "Direct Input")
        ]
        choice = utils.show_menu("개별 종목 분석 (Individual Analysis)", menu_items, default_choice="1")
        if choice.lower() in ['b', 'q']: return False
        
        menu_map = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map.get(choice, '')}")

        if choice == '5':
            # 직접 입력 로직
            from modules import manage
            # manage.get_current_price는 출력을 포함하므로, 여기서는 간단히 입력만 받음
            utils.print_breadcrumb()
            raw_input = Prompt.ask("종목코드(6자리/티커) 또는 종목명 [dim](이전: b, 메인: q)[/dim]")
            config.console.print()
            if not raw_input or raw_input.lower() in ['b', 'q']: return
            context.USER_ACTION_BREADCRUMB.append(f"[직접입력] {raw_input}")
            
            # manage 모듈의 _resolve_stock 로직과 유사하게 처리하거나 utils 활용
            # 여기서는 utils가 없으므로 telegram_bot의 로직을 참고하여 간단히 구현
            if len(raw_input) == 6 and raw_input[0].isdigit() and raw_input.isalnum():
                code = raw_input
                name = api.get_stock_name_by_code(code, False) or code
                is_overseas = False
            elif all(ord(c) < 128 for c in raw_input): # 해외 티커 가정
                code = raw_input.upper()
                name = api.get_stock_name_by_code(code, True) or code
                is_overseas = True
            else:
                # 한글 종목명 검색 시도 (config.session.stock_data 활용)
                found = False
                for key in ['stocks_kr', 'etfs_kr']:
                    for item in config.session.stock_data.get(key, []):
                        if item['name'] == raw_input:
                            code, name, is_overseas = item['code'], item['name'], False
                            found = True; break
                    if found: break
                if not found:
                    for key in ['stocks_us', 'etfs_us']:
                        for item in config.session.stock_data.get(key, []):
                            if item['name'].lower() == raw_input.lower():
                                code, name, is_overseas = item['code'], item['name'], True
                                found = True; break
                        if found: break
                
                if not found:
                    config.console.print(f"[red]'{raw_input}'을(를) 찾을 수 없습니다. 코드로 입력해주세요.[/red]")
                    return False
                    
            if not utils.validate_and_confirm_stock(code, name, is_overseas, "이 종목으로 분석을 진행하시겠습니까?"):
                return False
        else:
            # 리스트 선택
            key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
            target_key = key_map.get(choice)
            stock_list = config.session.stock_data.get(target_key, [])
            
            if not stock_list:
                config.console.print("[yellow]등록된 종목이 없습니다.[/yellow]")
                return False
                
            idx, item = utils.search_stock_in_list(stock_list, title=f"{menu_map[choice]} 목록")
            if not item: return False
            code, name = item['code'], item['name']
            is_overseas = (choice in ["3", "4"])
            context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {name}")

    if not code: return False

    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    # [수정] 종목 선택 후 데이터 분석 시작 (UI 응답성 개선)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        # [추가] 개별 룰 로드 및 설정 준비
        custom_rule = db_manager.db.get_stock_strategy(code)
        rule_applied = False
        
        # 기본값 설정
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        weights = config.SCORING_WEIGHTS
        
        if custom_rule:
            rule_applied = True
            buy_score = custom_rule['buy_score']
            buy_rsi = custom_rule['buy_rsi']
            if custom_rule.get('weights'):
                try:
                    w_data = custom_rule['weights']
                    if isinstance(w_data, str): weights = json.loads(w_data)
                    elif isinstance(w_data, dict): weights = w_data
                except Exception: pass
            if custom_rule.get('buy_vol_strength'):
                buy_vol = custom_rule['buy_vol_strength']

        # [추가] stock.json에서 표준 시장(거래소) 이름 가져오기
        std_market = None
        for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
            for item in config.session.stock_data.get(key, []):
                if item.get('code') == code and "exchange" in item:
                    std_market = item['exchange'].upper()
                    break
            if std_market: break

        foreign_rate_str = "[dim]-[/dim]"
        market_str = std_market if std_market else ("해외" if is_overseas else "KOSPI")
        # [추가] 적응형 임계값 적용 (시장 국면 보정)
        score_adj = 0.0
        is_domestic_index = not is_overseas and code in ["KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"]
        
        from modules import market
        all_idx_codes = [c for n, c in market.ALL_INDICES]
        is_index = is_domestic_index or (is_overseas and code in all_idx_codes)
        
        task = progress.add_task("[cyan]분석 준비 중...[/cyan]", total=None)

        if not is_overseas and not is_domestic_index:
            progress.update(task, description="[cyan]시장 국면 및 수급 정보 조회 중...[/cyan]")
            try:
                # API로 시장 구분 및 외인 소진율 확인
                cp = api.get_current_price_data(code, False)
                if cp.get('rt_cd') == '0':
                    foreign_rate_str = f"{cp['output'].get('hts_frgn_ehrt', '-')}%"
                    
                    # [수정] 국내 주식 현재가 API 응답에는 시장구분 필드가 없으므로 마스터 파일을 이용해 판별
                    market_type = _get_market_type_by_master(code)
                        
                    # [수정] std_market이 잘못 캐시되어 있는 경우를 대비하여 실시간 API 조회값을 최우선 반영
                    market_str = market_type
                    
                    if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
                        regime, score_adj = get_market_regime(market_type)
                        if score_adj != 0 and not rule_applied: # [수정] 개별 룰이 없을 때만 보정 적용
                            buy_score += score_adj
            except Exception: pass
        elif is_domestic_index:
            market_type = "KOSDAQ" if "KOSDAQ" in code else "KOSPI"
            market_str = code
            if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
                try:
                    regime, score_adj = get_market_regime(market_type)
                    if score_adj != 0 and not rule_applied:
                        buy_score += score_adj
                except Exception: pass
        else:
            if not std_market:
                cached_ex = config.session.exchange_cache.get(code)
                if cached_ex:
                    if cached_ex in ["NAS", "NASD"]: market_str = "NASDAQ"
                    elif cached_ex in ["NYS", "NYSE"]: market_str = "NYSE"
                    elif cached_ex in ["AMS", "AMEX"]: market_str = "AMEX"
                    else: market_str = cached_ex
                elif is_index:
                    try:
                        import yfinance as yf
                        tk = yf.Ticker(code)
                        ex = getattr(tk.fast_info, 'exchange', None)
                        if not ex:
                            ex = tk.info.get('exchange')
                        if ex:
                            ex_str = str(ex).upper()
                            ex_map = {
                                "NMS": "NASDAQ", "NYQ": "NYSE", "ASE": "AMEX",
                                "PNK": "OTC", "CMX": "COMEX", "NYM": "NYMEX",
                            "CBT": "CBOT", "CCY": "Currency (통화/환율)",
                            "CCC": "Crypto (암호화폐)",
                                "PHX": "PHLX (필라델피아)", "SNP": "S&P Index",
                            "CBOE": "CBOE (시카고옵션)", "DJI": "Dow Jones",
                            "CME": "CME (시카고상품거래소)", "NYB": "NYBOT (뉴욕상품거래소)",
                            "OSA": "Osaka (오사카)", "TAI": "Taiwan (대만)",
                            "HKG": "Hong Kong (홍콩)", "SHH": "Shanghai (상해)",
                            "FRA": "Frankfurt (프랑크푸르트)", "STO": "STOXX (유럽)"
                            }
                            market_str = ex_map.get(ex_str, ex_str)
                    except Exception:
                        pass

        # [추가] 임계값 및 가중치 딕셔너리 구성
        thresholds = {
            "BUY_SCORE": buy_score,
            "BUY_RSI_MAX": buy_rsi,
            "RISE_SCORE": rise_score,
            "WEIGHTS": weights
        }

        # 1. [최적화] 데이터 병렬 조회 (차트 캐시 확인 및 체결강도 동시 호출)
        progress.update(task, description=f"[cyan]{name}({code}) 지표 및 수급 데이터 병렬 수집 중...[/cyan]")
        
        df = None
        vol_strength = None
        inv_data = None
        ask_bid_ratio = None

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            if is_domestic_index:
                fut_chart = ex.submit(get_domestic_index_data, code)
                fut_vol = None
                fut_inv = None
                fut_ab = None
            else:
                fut_chart = ex.submit(api.get_chart_data, code, is_overseas=is_overseas)
                fut_vol = ex.submit(api.get_realtime_vol_strength, code) if not is_overseas else None
                fut_inv = ex.submit(api.get_investor_trend, code) if not is_overseas else None
                # 수급 게이트용 비율만 필요 → WS 우선(get_ask_bid_ratio)으로 호가 REST 절감
                fut_ab = ex.submit(api.get_ask_bid_ratio, code, False) if not is_overseas else None

            df = fut_chart.result()
            vol_strength = fut_vol.result() if fut_vol else None
            inv_data = fut_inv.result() if fut_inv else None
            ask_bid_ratio = fut_ab.result() if fut_ab else None

        # [Fix] 국내 지수 df는 공유 캐시 객체이므로 복사 후 사용
        #  (apply_realtime_price의 당일 봉 덮어쓰기/추가가 캐시를 오염시키지 않도록)
        if is_domestic_index and df is not None:
            df = df.copy()

        if df is None or df.empty:
            config.console.print("[red]차트 데이터를 불러올 수 없습니다.[/red]")
            return
            
        # [추가] 실시간 현재가 조회 및 차트 데이터 최신화 (점수 불일치 방지)
        try:
            rt_price = api.get_current_price(code, is_overseas=is_overseas)
            indicators.apply_realtime_price(df, rt_price, market_date=utils.market_today(is_overseas))
        except Exception: pass

        # 2. 지표 계산
        progress.update(task, description="[cyan]기술적 지표 계산 및 상태 분류 중...[/cyan]")
        ind = indicators.calculate_indicators(df)
        
        # 전일 RSI — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
        prev_rsi = ind.get('prev_rsi') if df is not None and not df.empty and len(df) >= 16 else None

        current_price = float(df.iloc[-1]['close'])
        prev_price = float(df.iloc[-2]['close']) if len(df) > 1 else current_price
        diff = current_price - prev_price
        rate = (diff / prev_price) * 100 if prev_price > 0 else 0.0
        
        # 52주 위치 계산 (슈퍼 모멘텀 판정용)
        w52_pos = 0.0
        h52 = 0.0
        l52 = 0.0
        high_52_rate = 0.0
        if len(df) > 0:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100
            if h52 > 0:
                high_52_rate = ((current_price - h52) / h52) * 100
        
        sm_flag, sm_reason = (False, "") if is_domestic_index else check_smart_money_turnaround(code, is_overseas)
        
        # 3. 상태 분류 및 점수 계산
        state, state_color, state_reason = classify_stock_state(
            df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
        )
        
        score, details = calculate_score(
            df=df, ind=ind, weights=weights, smart_money=sm_flag
        )

    # 4. 결과 출력
    config.console.print()
    
    # [추가] 종목 메모 출력 (존재 시 패널 형태로 상단에 표시)
    memo_data_list = utils.get_stock_memos(code)
    if memo_data_list:
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.console import Group
        from rich.text import Text
        
        renderables = []
        for i, m in enumerate(memo_data_list):
            if i > 0:
                renderables.append(Rule(style="dim"))
            text = Text.from_markup(f"[dim]{m['updated_at']}[/dim]\n{m['memo']}")
            renderables.append(text)
            
        config.console.print(Panel(Group(*renderables), title=f"{name} ({code})", border_style="cyan", expand=False))
        config.console.print()

    # [추가] TradingView 종합 평가 및 배당 수익률 등 추가 데이터 조회
    tv_rating_str = "조회 불가"
    div_yield_str = "[dim]-[/dim]"
    try:
        from tradingview_screener import Query, Column
        tv_market = 'america' if is_overseas else 'korea'
        _, tv_df = Query().set_markets(tv_market).select('Recommend.All', 'dividend_yield_recent').where(Column('name') == code).limit(1).get_scanner_data()
        if tv_df is not None and not tv_df.empty:
            rating_val = tv_df.iloc[0].get('Recommend.All')
            if pd.notna(rating_val):
                if rating_val >= 0.5: tv_rating_str = f"[bold red]Strong Buy (강력 매수)[/bold red] ({rating_val:+.2f})"
                elif rating_val >= 0.1: tv_rating_str = f"[red]Buy (매수)[/red] ({rating_val:+.2f})"
                elif rating_val > -0.1: tv_rating_str = f"[white]Neutral (중립)[/white] ({rating_val:+.2f})"
                elif rating_val > -0.5: tv_rating_str = f"[blue]Sell (매도)[/blue] ({rating_val:+.2f})"
                else: tv_rating_str = f"[bold blue]Strong Sell (강력 매도)[/bold blue] ({rating_val:+.2f})"
            
            div_val = tv_df.iloc[0].get('dividend_yield_recent')
            if pd.notna(div_val) and div_val > 0:
                div_yield_str = f"{div_val:.2f}%"
    except Exception: pass

    # [테이블 1] 기술적 지표 분석
    tech_title = f"기술적 지표 분석: {name} ({code})"

    table_tech = Table(title=tech_title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table_tech.add_column("지표", justify="left", style="cyan", width=15)
    table_tech.add_column("값 (상태)", justify="left")
    table_tech.add_column("해석/기준", justify="left", style="dim")

    # 시장 정보
    table_tech.add_row("시장", market_str, "소속 거래소")

    # 현재가
    curr_price_color = "[white]"
    if ind.get('ema_20') is not None and ind.get('ema_60') is not None:
        if ind['ema_20'] > ind['ema_60']:
            curr_price_color = "[red]" if current_price > ind['ema_20'] else "[white]"
        elif ind['ema_20'] < ind['ema_60']:
            curr_price_color = "[blue]" if current_price < ind['ema_20'] else "[orange3]"

    if is_index:
        price_str_tech = f"{current_price:,.0f}" if current_price >= 1000 else f"{current_price:,.2f}"
        h52_str = f"{h52:,.0f}" if h52 >= 1000 else f"{h52:,.2f}"
        l52_str = f"{l52:,.0f}" if l52 >= 1000 else f"{l52:,.2f}"
    else:
        price_str_tech = f"${current_price:,.2f}" if is_overseas else f"{current_price:,.0f}원"
        h52_str = f"${h52:,.2f}" if is_overseas else f"{int(h52):,}원"
        l52_str = f"${l52:,.2f}" if is_overseas else f"{int(l52):,}원"
        
    table_tech.add_row("현재가", f"{curr_price_color}{price_str_tech}[/]", "이평선 배열 및 위치 기반")

    # ATR (변동성)
    atr_val = ind.get('atr', 0)
    vol_str = "-"
    if atr_val > 0 and current_price > 0:
        annual_vol = (atr_val / current_price) * math.sqrt(252) * 100
        vol_str = f"{annual_vol:.1f}%"
    
    table_tech.add_row("변동성 (ATR)", f"{int(atr_val):,} ({vol_str})", "연환산 변동성 (리스크)")

    # 52주 위치
    w_color = "[white]"
    if w52_pos >= 90: w_color = "[red]"
    elif w52_pos >= 80: w_color = "[orange3]"
    elif w52_pos <= 30: w_color = "[blue]"
    elif w52_pos <= 50: w_color = "[yellow]"
    
    table_tech.add_row("52주 위치", f"{w_color}{w52_pos:.1f}%[/] [dim]({l52_str} ~ {h52_str})[/dim]", "최고가/최저가 밴드 내 현 위치")

    # SAR
    sar_val = ind.get('psar')
    if sar_val is not None and not math.isnan(sar_val):
        sar_pos = "주가 아래 (상승)" if sar_val < current_price else "주가 위 (하락)"
        sar_color = "[red]" if sar_val < current_price else "[blue]"
    else:
        sar_pos = "-"
        sar_color = "[dim]"
    table_tech.add_row("SAR 위치", f"{sar_color}{sar_pos}[/]", "파라볼릭 추세 전환")

    # MACD
    macd_val = ind.get('macd')
    sig_val = ind.get('macd_signal')
    
    macd_str = "[dim]-[/dim]"
    macd_desc = "추세 확인"
    if macd_val is not None and sig_val is not None and not math.isnan(macd_val) and not math.isnan(sig_val):
        m_color = "[red]" if macd_val >= sig_val else "[blue]"
        macd_str = f"{m_color}{macd_val:+.2f}[/]"
        
        cross_desc = "골든 (매수 우위)" if macd_val >= sig_val else "데드 (매도 우위)"
        phase_desc = "상승 국면" if macd_val > 0 else "하락 국면"
        macd_desc = f"{cross_desc} / {phase_desc}"
        
        # 시그널 값도 참고용으로 작게 표시
        macd_str += f" [dim](Sig: {sig_val:+.2f})[/dim]"
            
    table_tech.add_row("MACD (12/26/9)", macd_str, macd_desc)

    # OBV
    obv_trend = ind.get('obv_trend')
    obv_val = ind.get('obv')
    vol_sum = df['volume'].tail(5).sum() if df is not None and 'volume' in df.columns else 0
    
    if df is None or len(df) < config.INDICATOR_PARAMS.get("OBV_MA_PERIOD", 5):
        obv_trend = None
        obv_val = None
        
    if vol_sum == 0 or obv_trend is None or obv_val is None or math.isnan(obv_val):
        obv_trend_str = '-'
        obv_color = "[dim]"
        obv_desc = "거래량 데이터 없음"
        obv_formatted = f"{obv_color}{obv_trend_str}[/]"
    else:
        obv_trend_str = '상승' if obv_trend else '하락'
        obv_color = "[red]" if obv_trend else "[blue]"
        obv_desc = "이동평균 상회 여부"
        abs_val = abs(obv_val)
        if abs_val >= 999_950_000_000: obv_str = f"{obv_val/1_000_000_000_000:,.1f}T"
        elif abs_val >= 999_950_000: obv_str = f"{obv_val/1_000_000_000:,.1f}B"
        elif abs_val >= 999_500: obv_str = f"{obv_val/1_000_000:,.1f}M"
        elif abs_val >= 999.5: obv_str = f"{obv_val/1_000:,.0f}K"
        else: obv_str = f"{obv_val:,.0f}"
        obv_formatted = f"{obv_color}{obv_trend_str}[/] [dim]({obv_str})[/dim]"
        
    table_tech.add_row("OBV 추세", obv_formatted, obv_desc)
    
    # RSI
    rsi_val = ind.get('rsi')
    if rsi_val is not None:
        rsi_str = f"{rsi_val:.2f}"
        rsi_desc = ""
        if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: 
            rsi_str = f"[magenta]{rsi_str}[/]"
            rsi_desc = "과열 (추격금지)"
        elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: 
            rsi_str = f"[red]{rsi_str}[/]"
            rsi_desc = "강세 유지"
        elif 45 <= rsi_val < 55: 
            rsi_str = f"[orange3]{rsi_str}[/]"
            rsi_desc = "강세 조정 (진입후보)"
        elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: 
            rsi_str = f"[yellow]{rsi_str}[/]"
            rsi_desc = "약세/하락전환 가능"
        else: 
            rsi_str = f"[blue]{rsi_str}[/]"
            rsi_desc = "침체 (과매도)"
    else:
        rsi_str = "[dim]-[/dim]"
        rsi_desc = "데이터 부족"
    table_tech.add_row("RSI (14)", f"{rsi_str} [dim]({rsi_desc})[/dim]", "과매수(70)/과매도(30)")

    # CCI
    cci_val = ind.get('cci')
    if cci_val is not None:
        cci_str = f"{cci_val:.2f}"
        cci_desc = ""
        if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: 
            cci_str = f"[red]{cci_str}[/]"
            cci_desc = "과열 (추격 금물)"
        elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: 
            cci_str = f"[orange3]{cci_str}[/]"
            cci_desc = "상승 추세"
        elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: 
            cci_str = f"[yellow]{cci_str}[/]"
            cci_desc = "반등 시도"
        else: 
            cci_str = f"[blue]{cci_str}[/]"
            cci_desc = "과매도 (저점 탐색)"
    else:
        cci_str = "[dim]-[/dim]"
        cci_desc = "데이터 부족"
    table_tech.add_row("CCI (20)", f"{cci_str} [dim]({cci_desc})[/dim]", "추세 및 과매수/매도")

    # ADX
    adx_val = ind.get('adx')
    if adx_val is not None:
        adx_str = f"{adx_val:.2f}"
        adx_desc = ""
        if adx_val >= 40: 
            adx_str = f"[magenta]{adx_str}[/]" 
            adx_desc = "과열 (조정 주의)"
        elif adx_val >= 30: 
            adx_str = f"[red]{adx_str}[/]"     
            adx_desc = "강한 추세"
        elif adx_val >= 20: 
            adx_str = f"[orange3]{adx_str}[/]"
            adx_desc = "안정적 추세"
        elif adx_val >= 15: 
            adx_str = f"[yellow]{adx_str}[/]"
            adx_desc = "추세 형성 중"
        else: 
            adx_str = f"[white]{adx_str}[/]"
            adx_desc = "추세 없음 (횡보)"
    else:
        adx_str = "[dim]-[/dim]"
        adx_desc = "데이터 부족"
    table_tech.add_row("ADX (14)", f"{adx_str} [dim]({adx_desc})[/dim]", "추세 강도 (25 이상 강세)")

    # DMI
    plus_di = ind.get('plus_di')
    minus_di = ind.get('minus_di')
    if plus_di is not None and minus_di is not None:
        if plus_di > minus_di:
            dmi_str = f"[red]{plus_di:.1f}[/] / [dim]{minus_di:.1f}[/]"
            dmi_desc = "+DI 우위"
        elif minus_di > plus_di:
            dmi_str = f"[dim]{plus_di:.1f}[/] / [blue]{minus_di:.1f}[/]"
            dmi_desc = "-DI 우위"
        else: 
            dmi_str = f"{plus_di:.1f} / {minus_di:.1f}"
            dmi_desc = "중립"
    else:
        dmi_str = "[dim]- / -[/dim]"
        dmi_desc = "데이터 부족"
    table_tech.add_row("DMI (+DI/-DI)", f"{dmi_str} [dim]({dmi_desc})[/dim]", "매수/매도 세력 강도 (+DI 상승)")

    # [수정] 외인 소진율 및 배당 수익률 위치 변경 (이평 배열 위로 이동)
    if not is_overseas and not is_domestic_index:
        table_tech.add_row("외인 소진율", foreign_rate_str, "외국인 보유 비중")

    if not is_domestic_index:
        table_tech.add_row("배당 수익률", div_yield_str, "최근 연환산 배당수익률")

    # 이평 배열
    ema_align = "알 수 없음"
    ema_color = "[white]"
    if ind['ema_20'] is not None and ind['ema_60'] is not None and ind['ema_120'] is not None:
        if ind['ema_20'] > ind['ema_60'] > ind['ema_120']: 
            ema_align = "정배열 (20>60>120)"; ema_color = "[red]"
        elif ind['ema_20'] < ind['ema_60'] < ind['ema_120']: 
            ema_align = "역배열 (20<60<120)"; ema_color = "[blue]"
        else: 
            ema_align = "혼조세"; ema_color = "[yellow]"
    else:
        ema_align = "데이터 부족"; ema_color = "[dim]"
    table_tech.add_row("이평 배열", f"{ema_color}{ema_align}[/]", "5/20/60/120일선 배열")
    
    def _fmt_ema(v): return f"{int(v):,}" if not is_overseas else f"${v:,.2f}"
    
    v5 = ind.get('ema_5')
    v20 = ind.get('ema_20')
    v60 = ind.get('ema_60')
    v120 = ind.get('ema_120')

    c5 = "[red]" if (v5 is not None and v20 is not None and v5 > v20) else ("[blue]" if v5 is not None else "")
    c20 = "[red]" if (v20 is not None and v60 is not None and v20 > v60) else ("[blue]" if v20 is not None else "")
    c60 = "[red]" if (v60 is not None and v120 is not None and v60 > v120) else ("[blue]" if v60 is not None else "")
    
    c120 = ""
    if df is not None and not df.empty and len(df) > 121:
        try:
            ema120_series = df['close'].ewm(span=120, adjust=False).mean()
            if ema120_series.iloc[-1] > ema120_series.iloc[-2]: c120 = "[red]"
            else: c120 = "[blue]"
        except Exception: pass
    elif v120 is not None:
        c120 = "[blue]"
        
    e5_disp = f"{c5}{_fmt_ema(v5)}[/]" if v5 is not None else "-"
    e20_disp = f"{c20}{_fmt_ema(v20)}[/]" if v20 is not None else "-"
    e60_disp = f"{c60}{_fmt_ema(v60)}[/]" if v60 is not None else "-"
    e120_disp = f"{c120}{_fmt_ema(v120)}[/]" if v120 is not None else "-"

    table_tech.add_row("주요 이평선", f"5선: {e5_disp} | 20선: {e20_disp} | 60선: {e60_disp} | 120선: {e120_disp}", "지수이동평균(EMA) 가격")

    # 이격도
    d_20 = (current_price / ind['ema_20'] * 100) if ind['ema_20'] is not None else 0
    d_60 = (current_price / ind['ema_60'] * 100) if ind['ema_60'] is not None else 0
    d_120 = (current_price / ind['ema_120'] * 100) if ind['ema_120'] is not None else 0
    
    def dc(val): return "[red]" if val >= 100 else "[blue]"
    
    disp_msg = f"20선({dc(d_20)}{d_20:.1f}%[/]) 60선({dc(d_60)}{d_60:.1f}%[/]) 120선({dc(d_120)}{d_120:.1f}%[/])"
    
    disp_upper = config.ANALYSIS_THRESHOLDS.get("DISPARITY_UPPER", 110)
    disp_lower = config.ANALYSIS_THRESHOLDS.get("DISPARITY_LOWER", 90)

    disp_eval = ""
    if d_20 >= disp_upper: disp_eval = "[bold red]단기 과열[/]"
    elif d_20 <= disp_lower: disp_eval = "[bold blue]과매도[/]"
    else: disp_eval = "[white]적정 범위[/]"
    
    table_tech.add_row("이격도", disp_msg, f"{disp_eval} [dim](현재가/이평선)[/dim]")

    # [수정] 스마트머니를 표의 가장 아래로 이동
    if not is_overseas and not is_domestic_index:
        inv_str = "[dim]-[/dim]"
        if inv_data and len(inv_data) > 0:
            item = inv_data[0]
            p = api.safe_int(item.get('prsn_ntby_qty'))
            f = api.safe_int(item.get('frgn_ntby_qty'))
            o = api.safe_int(item.get('orgn_ntby_qty'))
            def _fmt_i(val):
                if val == 0: return "[dim]-[/dim]"
                abs_val = abs(val)
                if abs_val >= 1_000_000: s = f"{val/1_000_000:+.1f}M"
                elif abs_val >= 1000: s = f"{val/1000:+.0f}K"
                else: s = f"{val:+,}"
                return f"[red]{s}[/]" if val > 0 else f"[blue]{s}[/]"
            inv_str = f"개인: {_fmt_i(p)} | 외인: {_fmt_i(f)} | 기관: {_fmt_i(o)}"
            
        table_tech.add_row("수급", inv_str, "당일 개인/외국인/기관 순매수량")
        
        sm_str = f"[red]{sm_reason}[/]" if sm_flag else "[dim]특이사항 없음[/]"
        table_tech.add_row("스마트머니", sm_str, "외인/기관 쌍끌이 및 순매수 전환")

    config.console.print(table_tech)
    config.console.print()
    
    # [테이블 2] 시스템 트레이딩 판단 결과
    if not is_index:
        logic_title = "시스템 트레이딩 판단 결과"
        changes_summary = None
            
        if rule_applied:
            changes = []
            
            def_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            def_buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
            def_buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
            def_sell_score = config.SELL_STRATEGY["SELL_SCORE"]
            def_tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
            def_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
            
            if custom_rule.get('buy_score') != def_buy_score:
                changes.append(f"매수점수({def_buy_score}->{custom_rule['buy_score']})")
            if custom_rule.get('buy_rsi') != def_buy_rsi:
                changes.append(f"매수RSI({def_buy_rsi}->{custom_rule['buy_rsi']})")
            if custom_rule.get('buy_vol_strength') and custom_rule['buy_vol_strength'] != def_buy_vol:
                changes.append(f"체결강도({def_buy_vol}%->{custom_rule['buy_vol_strength']}%)")
            
            if custom_rule.get('sell_score') != def_sell_score:
                changes.append(f"매도점수({def_sell_score}->{custom_rule['sell_score']})")
            if custom_rule.get('take_profit') != def_tp:
                changes.append(f"익절({def_tp}%->{custom_rule['take_profit']}%)")
            if custom_rule.get('stop_loss') != def_sl:
                changes.append(f"손절({def_sl}%->{custom_rule['stop_loss']}%)")
                
            if custom_rule.get('weights'):
                changes.append("가중치")

            if changes:
                changes_summary = ", ".join(changes)

        table_logic = Table(title=logic_title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table_logic.add_column("항목", justify="center", style="cyan", width=15)
        table_logic.add_column("결과", justify="center", width=30)
        table_logic.add_column("상세 내용 / 사유", justify="left", style="dim")

        s_color = state_color.replace('[', '').replace(']', '')
        score_str = f"[bold {s_color}]{score:.2f}점[/]"
        
        details_str = ""
        if details:
            details_str = "\n".join([f"[green]* {d}[/green]" for d in details])
        else:
            details_str = "[dim]획득한 점수가 없습니다.[/dim]"
        
        table_logic.add_row("종합 점수", score_str, details_str)
        
        w_val = f"{weights.get('TREND', 4.0):.1f} / {weights.get('MOMENTUM', 2.5):.1f} / {weights.get('STRENGTH', 1.5):.1f} / {weights.get('SYNERGY', 2.0):.1f}"
        w_desc = "추세 / 모멘텀 / 강도 / 시너지"
        if rule_applied and custom_rule.get('weights'):
            w_desc += " [magenta](개별 설정)[/]"
        else:
            w_desc += " [dim](시스템 설정)[/dim]"
        table_logic.add_row("적용 가중치", w_val, w_desc)
        
        table_logic.add_row("상태 분류", f"[bold {s_color}]{state}[/]", state_reason)
        
        buy_score_limit = buy_score
        
        is_mr_state = (state == "역매수")
        if is_mr_state:
            buy_vol_limit = thresholds.get("MR_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0))
        else:
            buy_vol_limit = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
            if rule_applied and custom_rule.get('buy_vol_strength'):
                buy_vol_limit = custom_rule['buy_vol_strength']
                
        min_ask_bid_ratio = config.ANALYSIS_THRESHOLDS.get("BUY_ASK_BID_RATIO", 1.0)
        if rule_applied and custom_rule.get('buy_ask_bid_ratio') is not None:
            min_ask_bid_ratio = custom_rule['buy_ask_bid_ratio']
            
        auto_adjust = config.ANALYSIS_THRESHOLDS.get("AUTO_ADJUST_ASK_BID_RATIO", True)
        if rule_applied and custom_rule.get('auto_adjust_ask_bid_ratio') is not None:
            auto_adjust = bool(custom_rule['auto_adjust_ask_bid_ratio'])

        if auto_adjust and min_ask_bid_ratio > 0 and buy_vol_limit > 0:
            ratio_multiplier = buy_vol_limit / 100.0
            min_ask_bid_ratio = round(min_ask_bid_ratio * ratio_multiplier, 2)
                
        use_super = thresholds.get("SUPER_MOMENTUM_USE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True))
        super_score = thresholds.get("SUPER_MOMENTUM_SCORE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5))
        super_w52 = thresholds.get("SUPER_MOMENTUM_W52_POS", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0))
        super_rsi = thresholds.get("SUPER_BUY_RSI_MAX", config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 75.0))
        
        is_super = use_super and score >= super_score and w52_pos >= super_w52
        buy_rsi_limit = super_rsi if is_super else thresholds["BUY_RSI_MAX"]

        is_buy_score = score >= buy_score_limit
        is_buy_rsi = (ind['rsi'] is not None) and (ind['rsi'] < buy_rsi_limit)
        is_safe_state = state not in ["매도", "주의", "-"]
        is_buy_vol = True
        is_ask_bid_ok = True
        if vol_strength is not None:
            is_buy_vol = vol_strength >= buy_vol_limit
        if ask_bid_ratio is not None and min_ask_bid_ratio > 0:
            is_ask_bid_ok = ask_bid_ratio >= min_ask_bid_ratio
            
        is_buy_all_ok = is_buy_score and is_buy_rsi and is_safe_state and is_buy_vol and is_ask_bid_ok
        
        if is_mr_state:
            buy_result = "[bold magenta]매수 가능 (역추세)[/]" if (is_buy_vol and is_ask_bid_ok) else "[bold blue]매수 불가 (체결/잔량 미달)[/]"
        else:
            if state == "강매수":
                buy_result = "[bold magenta]매수 가능 (슈퍼모멘텀)[/]" if is_buy_all_ok else "[bold blue]매수 불가[/]"
            else:
                buy_result = "[bold red]매수 가능[/]" if is_buy_all_ok else "[bold blue]매수 불가[/]"
        
        buy_reason_list = []
        if not is_safe_state: buy_reason_list.append(f"진입 불가 상태 ({state})")
        if not is_buy_score and not is_mr_state:
            if score_adj != 0 and not rule_applied:
                origin_score = round(buy_score_limit - score_adj, 2)
                buy_reason_list.append(f"점수 미달 (기준: {buy_score_limit} 이상 [설정: {origin_score}, 시장보정 {score_adj:+.1f}점])")
            else:
                buy_reason_list.append(f"점수 미달 (기준: {buy_score_limit}점 이상)")
        if not is_buy_rsi and not is_mr_state:
            if ind['rsi'] is None: buy_reason_list.append("RSI 데이터 부족")
            else:
                rsi_reason = f"RSI 과열 (기준: {buy_rsi_limit} 미만)"
                if is_super: rsi_reason += " [슈퍼모멘텀 완화 적용됨]"
                buy_reason_list.append(rsi_reason)
        if not is_buy_vol: buy_reason_list.append(f"체결강도 미달 ({vol_strength:.1f}% < {buy_vol_limit}%)")
        if not is_ask_bid_ok: buy_reason_list.append(f"매도잔량비 미달 ({ask_bid_ratio:.2f}배 < {min_ask_bid_ratio}배)")
        
        buy_reason = ", ".join(buy_reason_list) if buy_reason_list else ("역추세 반등 확인" if is_mr_state else "모든 매수 조건 충족")
        
        table_logic.add_row("매수 판단", buy_result, buy_reason)
        
        sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]
        is_sell_signal = (score < sell_score_limit) or (state == "매도")
        
        sell_result = "[bold blue]매도(추세이탈)[/]" if is_sell_signal else "[bold green]보유(추세유지)[/]"
        
        if state == "매도":
            sell_reason = "매도 상태 진입 (필터링 조건)"
        elif score < sell_score_limit:
            sell_reason = f"점수 하락 (기준: {sell_score_limit}점 미만)"
        else:
            sell_reason = "추세 유지 중 (주의/관망 상태라도 점수 유지 시 보유)"
        
        table_logic.add_row("보유 판단", sell_result, sell_reason)
        
        # [토스] 체결강도 미제공 → '체결강도' 행을 숨기고 아래 '매도잔량 비율'을 수급 지표로 사용
        if not config.session.is_toss:
            vol_str = "-"
            vol_eval = ""
            if vol_strength is not None:
                v_color = "[red]" if is_buy_vol else "[blue]"
                vol_str = f"{v_color}{vol_strength:.1f}%[/]"
                vol_eval = "[bold red]양호[/]" if is_buy_vol else "[bold blue]미달[/]"
            table_logic.add_row("체결강도", vol_str, f"{vol_eval} (기준: {buy_vol_limit}% 이상)")

        if not is_overseas and not is_index:
            ask_bid_str = "-"
            ask_bid_eval = ""
            if ask_bid_ratio is not None and min_ask_bid_ratio > 0:
                ab_color = "[red]" if is_ask_bid_ok else "[blue]"
                ask_bid_str = f"{ab_color}{ask_bid_ratio:.2f}배[/]"
                ask_bid_eval = "[bold red]양호[/]" if is_ask_bid_ok else "[bold blue]미달[/]"
                table_logic.add_row("매도잔량 비율", ask_bid_str, f"{ask_bid_eval} (기준: {min_ask_bid_ratio}배 이상)")
            elif min_ask_bid_ratio <= 0:
                table_logic.add_row("매도잔량 비율", "[dim]미사용[/]", "-")
            else:
                table_logic.add_row("매도잔량 비율", "-", "데이터 확인 불가")

        rule_res = "[bold magenta]적용[/]" if rule_applied else "[dim]미적용[/]"
        rule_desc = f"[dim]{changes_summary}[/dim]" if changes_summary else "-"
        table_logic.add_row("개별 룰", rule_res, rule_desc)

        table_logic.add_section()
        table_logic.add_row("TradingView 의견", tv_rating_str, "TradingView Technical Rating (-1~1)")

        config.console.print(table_logic)
    
    config.console.print()

    # [추가] 기간별 시세 30일치 출력
    _print_period_price_30(code, is_overseas)
    
    # [추가] 상세 차트 분석 여부 확인
    config.console.print()
    if Prompt.ask("📊 상세 차트 분석 데이터를 출력하시겠습니까?", choices=["y", "n"], default="n") == 'y':
        from modules import chart
        if is_domestic_index:
            yf_ticker = "^KS11"
            if code == "KOSDAQ": yf_ticker = "^KQ11"
            elif code == "KOSPI200": yf_ticker = "^KS200"
            elif code == "KOSDAQ150": yf_ticker = "^KQ150"
            chart.generate_visual_chart(yf_ticker, name, is_overseas=True)
        else:
            chart.generate_visual_chart(code, name, is_overseas=is_overseas)

    config.console.print()
    ai_prompt_msg = "🤖 AI 지수 심층 진단을 수행하시겠습니까?" if is_index else "🤖 AI 종목 심층 진단을 수행하시겠습니까?"
    if Prompt.ask(ai_prompt_msg, choices=["y", "n"], default="n") == 'y':
        from modules import theme_analysis
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.padding import Padding
        
        rsi_val_str = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
        adx_val_str = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
        cci_val_str = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
        
        plus_di = ind.get('plus_di')
        minus_di = ind.get('minus_di')
        dmi_str = "-"
        if plus_di is not None and minus_di is not None:
            if plus_di > minus_di:
                dmi_str = f"+DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
            elif minus_di > plus_di:
                dmi_str = f"-DI 우위 ({plus_di:.1f} / {minus_di:.1f})"
            else:
                dmi_str = f"중립 ({plus_di:.1f} / {minus_di:.1f})"

        if is_index:
            tech_info = (
                f"• 현재가: {price_str_tech}\n"
                f"• 시스템 상태: {state} (사유: {state_reason})\n"
                f"• 핵심 지표: RSI {rsi_val_str} | ADX {adx_val_str} | CCI {cci_val_str} | DMI {dmi_str}"
            )
        else:
            tech_info = (
                f"• 현재가: {price_str_tech}\n"
                f"• 시스템 상태: {state} (사유: {state_reason})\n"
                f"• 퀀트 점수: {score}점 / 10점 만점\n"
                f"• 핵심 지표: RSI {rsi_val_str} | ADX {adx_val_str} | CCI {cci_val_str} | DMI {dmi_str}"
            )
        
        title_str = f"🤖 AI 지수 심층 진단: {name}({code})" if is_index else f"🤖 AI 종목 심층 진단: {name}({code})"

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            if is_index:
                progress.add_task(f"[cyan]Google Gemini가 매크로 모멘텀을 결합하여 지수 심층 진단 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
                answer = theme_analysis.analyze_index_with_gemini(code, name, tech_info)
            else:
                progress.add_task(f"[cyan]Google Gemini가 기업 모멘텀을 결합하여 종목 심층 진단 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
                answer = theme_analysis.analyze_stock_with_gemini(code, name, tech_info)
            
        if answer:
            if answer.startswith("⚠️"):
                config.console.print(f"\n{answer}")
            else:
                md = Markdown(answer)
                
                if is_index:
                    idx_table = Table(title="지수 분석 정보", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
                    idx_table.add_column("지수명", justify="left", style="white", no_wrap=True)
                    idx_table.add_column("지수", justify="right")
                    idx_table.add_column("등락폭 (등락률)", justify="right")
                    idx_table.add_column("52주 고점", justify="right")
                    idx_table.add_column("EMA(5)", justify="right")
                    idx_table.add_column("EMA(20)", justify="right")
                    idx_table.add_column("EMA(60)", justify="right")
                    idx_table.add_column("EMA(120)", justify="right")
                    idx_table.add_column("추세SMO", justify="center")
                    idx_table.add_column("RSI", justify="right")
                    idx_table.add_column("CCI", justify="right")
                    idx_table.add_column("ADX", justify="right")
                    idx_table.add_column("OBV", justify="right")
                    
                    curr_fmt = f"{current_price:,.2f}"
                    curr_str = f"{curr_price_color}{curr_fmt}[/]"
                    
                    diff_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                    diff_str_fmt = f"{diff:+.2f}"
                    change_str = f"{diff_color}{diff_str_fmt} ({rate:+.2f}%)[/]"

                    h_color = "[white]"
                    if high_52_rate > -3.0: h_color = "[red]"
                    elif high_52_rate < -20.0: h_color = "[blue]"
                    h52_fmt = f"{h52:,.0f}" if h52 >= 1000 else f"{h52:,.2f}"
                    high_52_str = f"[dim]{h52_fmt}[/] ({h_color}{high_52_rate:.1f}%[/])"

                    def fmt_val(val, color_tag):
                        if val is None or math.isnan(val): return "[dim]-[/dim]"
                        s = f"{val:,.0f}" if val >= 1000 else f"{val:,.2f}"
                        return f"{color_tag}{s}[/]" if color_tag else s
                        
                    ema5_color = "[white]"
                    if ind.get('ema_5') is not None and ind.get('ema_20') is not None:
                        ema5_color = "[red]" if ind['ema_5'] > ind['ema_20'] else "[blue]"

                    ema20_color = "[white]"
                    if ind.get('ema_20') is not None and ind.get('ema_60') is not None:
                        ema20_color = "[red]" if ind['ema_20'] > ind['ema_60'] else "[blue]"

                    ema60_color = "[white]"
                    if ind.get('ema_60') is not None and ind.get('ema_120') is not None:
                        ema60_color = "[red]" if ind['ema_60'] > ind['ema_120'] else "[blue]"

                    ema120_color = "[white]"
                    if df is not None and not df.empty and len(df) > 121:
                        try:
                            ema120_series = df['close'].ewm(span=120, adjust=False).mean()
                            if ema120_series.iloc[-1] > ema120_series.iloc[-2]:
                                ema120_color = "[red]"
                            else:
                                ema120_color = "[blue]"
                        except Exception: pass
                    
                    t_sar = "[dim]-[/dim]"
                    if ind.get('psar') is not None:
                        t_sar = "[red]⬆[/]" if current_price > ind['psar'] else "[blue]⬇[/]"
                    m_val = ind.get('macd')
                    s_val = ind.get('macd_signal')
                    t_macd = "[dim]-[/dim]"
                    if m_val is not None and s_val is not None:
                        zs = "+" if m_val > 0 else "-"
                        cc = "G" if m_val > s_val else "D"
                        mc = "red" if m_val > s_val else "blue"
                        t_macd = f"[{mc}]{zs}{cc}[/]"
                        
                    obv_trend = ind.get('obv_trend')
                    obv_val = ind.get('obv')
                    vol_sum = df['volume'].tail(5).sum() if df is not None and 'volume' in df.columns else 0
                    
                    if df is None or len(df) < config.INDICATOR_PARAMS.get("OBV_MA_PERIOD", 5):
                        obv_trend = None
                        obv_val = None
                        
                    if vol_sum == 0 or obv_trend is None or obv_val is None or math.isnan(obv_val):
                        obv_icon = "[dim]-[/dim]"
                        obv_disp = "[dim]-[/dim]"
                    else:
                        obv_icon = "[red]▲[/]" if obv_trend else "[blue]▼[/]"
                        obv_c = "red" if obv_trend else "blue"
                        abs_val = abs(obv_val)
                        if abs_val >= 999_950_000_000: obv_str = f"{obv_val/1_000_000_000_000:,.1f}T"
                        elif abs_val >= 999_950_000: obv_str = f"{obv_val/1_000_000_000:,.1f}B"
                        elif abs_val >= 999_500: obv_str = f"{obv_val/1_000_000:,.1f}M"
                        elif abs_val >= 999.5: obv_str = f"{obv_val/1_000:,.0f}K"
                        else: obv_str = f"{obv_val:,.0f}"
                        obv_disp = f"[{obv_c}]{obv_str}[/]"
                        
                    trend_str = f"{t_sar} {t_macd} {obv_icon}"
                    
                    val_rsi = ind.get('rsi')
                    t_rsi_str = f"{val_rsi:.1f}" if val_rsi is not None else "[dim]-[/dim]"
                    if val_rsi is not None:
                        if val_rsi >= config.INDICATOR_PARAMS["RSI_UPPER"]: t_rsi_str = f"[magenta]{t_rsi_str}[/]"
                        elif 55 <= val_rsi < config.INDICATOR_PARAMS["RSI_UPPER"]: t_rsi_str = f"[red]{t_rsi_str}[/]"
                        elif 45 <= val_rsi < 55: t_rsi_str = f"[orange3]{t_rsi_str}[/]"
                        elif config.INDICATOR_PARAMS["RSI_LOWER"] < val_rsi < 45: t_rsi_str = f"[yellow]{t_rsi_str}[/]"
                        else: t_rsi_str = f"[blue]{t_rsi_str}[/]"

                    val_cci = ind.get('cci')
                    t_cci_str = f"{val_cci:.1f}" if val_cci is not None else "[dim]-[/dim]"
                    if val_cci is not None:
                        if val_cci >= config.INDICATOR_PARAMS["CCI_UPPER"]: t_cci_str = f"[red]{t_cci_str}[/]"
                        elif 0 < val_cci < config.INDICATOR_PARAMS["CCI_UPPER"]: t_cci_str = f"[orange3]{t_cci_str}[/]"
                        elif config.INDICATOR_PARAMS["CCI_LOWER"] < val_cci <= 0: t_cci_str = f"[yellow]{t_cci_str}[/]"
                        else: t_cci_str = f"[blue]{t_cci_str}[/]"

                    val_adx = ind.get('adx')
                    t_adx_str = f"{val_adx:.1f}" if val_adx is not None else "[dim]-[/dim]"
                    if val_adx is not None:
                        if val_adx >= 40: t_adx_str = f"[magenta]{t_adx_str}[/]" 
                        elif val_adx >= 30: t_adx_str = f"[red]{t_adx_str}[/]"     
                        elif val_adx >= 20: t_adx_str = f"[orange3]{t_adx_str}[/]"
                        elif val_adx >= 15: t_adx_str = f"[yellow]{t_adx_str}[/]"
                        else: t_adx_str = f"[white]{t_adx_str}[/]"

                    idx_table.add_row(
                        name, curr_str, change_str, high_52_str, 
                        fmt_val(ind.get('ema_5'), ema5_color), 
                        fmt_val(ind.get('ema_20'), ema20_color), 
                        fmt_val(ind.get('ema_60'), ema60_color), 
                        fmt_val(ind.get('ema_120'), ema120_color), 
                        trend_str, t_rsi_str, t_cci_str, t_adx_str, obv_disp
                    )
                    config.console.print()
                    config.console.print(idx_table)
                else:
                    table_title = "미국 주식 분석 정보" if is_overseas else "국내 주식 분석 정보"
                    print_table(table_title, [(name, code)], is_overseas=is_overseas)
                
                panel = Panel(md, title=title_str, border_style="cyan", padding=(1, 2), width=120)
                config.console.print()
                config.console.print(Padding(panel, (0, 4)))
        else:
            config.console.print("[red]분석 결과를 생성하지 못했습니다.[/red]")

def _diagnose_group_stock_worker(item, market_filter, restricted_stocks, rules_map, reserved_codes=None, m_codes=None):
    """(내부함수) 관심 종목 일괄 분석용 단일 워커 (병렬 처리용)"""
    if reserved_codes is None: reserved_codes = set()
    if m_codes is None: m_codes = set()
    try:
        code = item['code']
        name = item['name']
        
        # 1. [최적화] 시장 구분 확인(선택), 차트 데이터, 체결강도 병렬(동시) 조회 (누락 방지 포함)
        for attempt in range(2):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                fut_cp = ex.submit(api.get_current_price_data, code, False) if market_filter else None
                fut_chart = ex.submit(api.get_chart_data, code, is_overseas=False)
                fut_vol = ex.submit(api.get_realtime_vol_strength, code)
                
                cp_data = fut_cp.result() if fut_cp else None
                df = fut_chart.result()
                try: vol_strength = fut_vol.result()
                except Exception: vol_strength = None
            
            is_cp_valid = True if not market_filter else (cp_data and cp_data.get('rt_cd') == '0')
            if is_cp_valid and df is not None and not df.empty:
                break
            time.sleep(0.5)

        if market_filter and cp_data:
            if cp_data.get('rt_cd') != '0': return None
            
            # [수정] 현재가 데이터에는 시장 정보가 없으므로 마스터 파일 기반으로 필터링
            m_type = _get_market_type_by_master(code)
            if market_filter == "KOSPI" and m_type != "KOSPI": return None
            if market_filter == "KOSDAQ" and m_type != "KOSDAQ": return None

        if df is None or df.empty: return None
        
        # [추가] 실시간 현재가 조회 및 차트 당일 고가/저가/종가 최신화
        try:
            if cp_data and cp_data.get('rt_cd') == '0':
                # [수정] NXT 장 현재가(ats_prpr)가 있으면 우선 반영, 없으면 정규장 현재가 반영
                nxt_price = float(cp_data['output'].get('ats_prpr', 0) or 0)
                krx_price = float(cp_data['output'].get('stck_prpr', 0) or 0)
                rt_price = nxt_price if nxt_price > 0 else krx_price
            else:
                rt_price = api.get_current_price(code, is_overseas=False)

            indicators.apply_realtime_price(df, rt_price, market_date=utils.market_today(False))
        except Exception: pass

        ind = indicators.calculate_indicators(df)
        current_price = float(df.iloc[-1]['close'])
        
        # 전일 RSI (상태 분류용) — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
        prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None

        # 52주 위치 계산 (슈퍼 모멘텀 판정용)
        w52_pos = 0.0
        if len(df) > 0:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100

        sm_flag, sm_reason = check_smart_money_turnaround(code, is_overseas=False)

        # 3. 점수 및 상태 계산
        rule = rules_map.get(code)
        thresholds = None
        weights = None
        
        if rule:
            thresholds = {
                "BUY_SCORE": rule['buy_score'],
                "BUY_RSI_MAX": rule['buy_rsi'],
                "BUY_VOL_STRENGTH": rule.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)),
                "WEIGHTS": rule.get('weights'),
                "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
            }
            weights = rule.get('weights')

        state, state_color, state_reason = classify_stock_state(
            df=df, ind=ind, prev_rsi=prev_rsi, thresholds=thresholds, w52_pos=w52_pos, smart_money=sm_flag
        )
        
        score, _ = calculate_score(
            df=df, ind=ind, weights=weights, smart_money=sm_flag
        )
        
        # [추가] 개별 룰 여부 확인
        is_custom_rule = code in rules_map
        is_restricted = code in restricted_stocks
        is_reserved = code in reserved_codes
        is_memo = code in m_codes
        
        return {
            'code': code, 'name': name, 'price': current_price,
            'score': score, 'state': state, 'state_color': state_color,
            'rsi': ind['rsi'], 'adx': ind['adx'], 'cci': ind['cci'],
            'vol_strength': vol_strength, 'is_custom_rule': is_custom_rule,
            'is_restricted': is_restricted,
            'is_reserved': is_reserved,
            'is_memo': is_memo
        }
    except Exception:
        return None

def diagnose_group_stocks(market_filter=None):
    """등록된 종목들에 대해 일괄 분석을 수행합니다."""
    # 대상: 국내 주식 + 국내 ETF
    targets = config.session.stock_data.get('stocks_kr', []) + config.session.stock_data.get('etfs_kr', [])
    
    if not targets:
        config.console.print("[yellow]등록된 국내 종목이 없습니다.[/yellow]")
        return
        
    # [추가] 개별 룰 로드 (전체 조회 최적화)
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {}
    for r in custom_rules:
        r_dict = dict(r)
        if r_dict.get('weights') and isinstance(r_dict['weights'], str):
            try: r_dict['weights'] = json.loads(r_dict['weights'])
            except Exception: r_dict['weights'] = None
        rules_map[r_dict['code']] = r_dict

    # [추가] 트레이딩 제한 종목 로드
    from modules import auto_trade
    restricted_stocks = auto_trade.get_restricted_stocks()
    any_restricted = False
    
    # [추가] 예약 매매 및 메모 마커 조회
    try:
        pending_reserves = db_manager.db.get_pending_reserved_orders()
        reserved_codes = set(o['code'] for o in pending_reserves)
    except Exception:
        reserved_codes = set()
    m_codes = utils.get_memo_codes()

    results = []
    
    title_suffix = f" ({market_filter})" if market_filter else " (전체)"
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task(f"[cyan]등록된 종목 병렬 분석 중{title_suffix}...[/cyan]", total=len(targets))
        
        # [최적화] ThrottledSession 제어 기반으로 모의투자(2) / 실전(4) 통합 병렬 처리 허용
        max_w = config.analysis_max_workers()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
            futures = [executor.submit(_diagnose_group_stock_worker, item, market_filter, restricted_stocks, rules_map, reserved_codes, m_codes) for item in targets]
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res: results.append(res)
                progress.advance(task)

    # 결과 출력
    if not results:
        config.console.print(f"[yellow]해당 조건({market_filter})에 맞는 종목이 없거나 데이터를 불러올 수 없습니다.[/yellow]")
        return

    used_marks = set()
    # 정렬 기준 개선: 1. 점수 높은 순, 2. RSI 낮은 순 (상승 여력)
    # RSI가 None인 경우 맨 뒤로 보내기 위해 999 처리
    results.sort(key=lambda x: (-x['score'], x['rsi'] if x['rsi'] is not None else 999))
    
    table_title = f"전체 종목 분석 결과{title_suffix}"
    
    # [추가] 적용된 가중치 정보 표시 (검증용)
    if config.SCORING_WEIGHTS:
        w = config.SCORING_WEIGHTS
        w_str = f"{w.get('TREND', 4.0)}/{w.get('MOMENTUM', 2.5)}/{w.get('STRENGTH', 1.5)}/{w.get('SYNERGY', 2.0)}"
        table_title += f" [dim](가중치: {w_str})[/dim]"

    table = Table(title=table_title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목명(코드)", justify="left")
    table.add_column("현재가", justify="right")
    table.add_column("점수", justify="center")
    table.add_column("상태", justify="center")
    table.add_column("RSI", justify="right")
    table.add_column("CCI", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("체결강도", justify="right")
    
    for r in results:
        s_color = r['state_color'].replace('[', '').replace(']', '')
        score_str = f"[{s_color}]{r['score']:.2f}점[/]"
        state_str = f"[{s_color}]{r['state']}[/]"
        
        rsi_val = r['rsi']
        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
        if rsi_val is not None:
            if rsi_val >= 70: rsi_str = f"[magenta]{rsi_str}[/]"
            elif rsi_val <= 30: rsi_str = f"[blue]{rsi_str}[/]"
            
        adx_str = f"{r['adx']:.1f}" if r['adx'] is not None else "-"
        cci_str = f"{r['cci']:.1f}" if r['cci'] is not None else "-"
        
        vol_val = r.get('vol_strength')
        vol_str = f"{vol_val:.1f}%" if vol_val else "-"
        
        name_display = r['name']
        marks = []
        if r.get('is_restricted'):
            marks.append("-")
            used_marks.add('-')
        if r.get('is_custom_rule'):
            marks.append("+")
            used_marks.add('+')
        if r.get('is_memo'):
            marks.append("=")
            used_marks.add('=')
        if r.get('is_reserved'):
            marks.append("[magenta]*[/magenta]")
            used_marks.add('*')
            
        if marks:
            name_display += f"[dim]{''.join(marks)}[/dim]"
        
        table.add_row(
            f"{name_display}({r['code']})",
            f"{int(r['price']):,}원",
            score_str,
            state_str,
            rsi_str,
            cci_str,
            adx_str
        )
        
    config.console.print(table, crop=False)
    sys.stdout.flush()
    config.console.print()
    

def get_analysis_params(use_vol=True):
    """분석에 사용할 파라미터를 사용자로부터 입력받습니다. (매수 체결강도 옵션 연동)"""
    params = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS.copy() # [추가] 가중치 포함 (복사본 사용)
    }
    
    config.console.print("\n[bold]분석 파라미터 설정 (Enter: 현재값 유지, 이전: b, 메인: q)[/bold]")
    
    config.console.print("\n[bold]1. 기본 매수 타점 설정[/bold]")
    val = Prompt.ask(f"매수 기준 점수 (기본: {params['BUY_SCORE']}점)\n[dim]이 점수 이상일 때 매수 진입 (지표 종합 점수)[/dim]", default=str(params['BUY_SCORE']))
    if val.lower() in ['b', 'q']: return None
    try: params['BUY_SCORE'] = float(val)
    except Exception: pass
    
    val = Prompt.ask(f"매수 허용 RSI 상한 (기본: {params['BUY_RSI_MAX']})\n[dim]RSI가 이 값보다 낮아야 매수 (과열 방지)[/dim]", default=str(params['BUY_RSI_MAX']))
    if val.lower() in ['b', 'q']: return None
    if val.isdigit(): params['BUY_RSI_MAX'] = int(val)
    
    if use_vol:
        current_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        val = Prompt.ask(f"매수 체결강도 기준(%) (기본: {current_vol}, 0: 미사용)\n[dim]수급 확인 (이 값 이상이어야 매수)[/dim]", default=str(current_vol))
        if val.lower() in ['b', 'q']: return None
        try: params['BUY_VOL_STRENGTH'] = float(val)
        except Exception: params['BUY_VOL_STRENGTH'] = current_vol
    else:
        params['BUY_VOL_STRENGTH'] = 0.0

    config.console.print("\n[bold]2. 스캐닝 필터 설정[/bold]")
    val = Prompt.ask(f"상승 추세 기준 점수 (기본: {params['RISE_SCORE']}점)\n[dim]매수에는 미달하지만 관망/상승으로 판단할 점수 기준[/dim]", default=str(params['RISE_SCORE']))
    if val.lower() in ['b', 'q']: return None
    try: params['RISE_SCORE'] = float(val)
    except Exception: pass

    config.console.print("\n[bold]3. 스코어링 가중치 설정[/bold]")
    curr_weights = params['WEIGHTS'].copy()
    while True:
        config.console.print("[dim]순서: 추세 / 모멘텀 / 강도 / 시너지 (합계 10점 권장)[/dim]")
        config.console.print()
        
        try:
            def ask_w(key, desc, default_v):
                v = Prompt.ask(f"{desc} [dim](현재: {default_v})[/dim]", default=str(default_v))
                if v.lower() in ['b', 'q']: raise ValueError("quit")
                return float(v)

            w_trend = ask_w("TREND", "추세 (TREND)", curr_weights.get('TREND', 4.0))
            w_mom = ask_w("MOMENTUM", "모멘텀 (MOMENTUM)", curr_weights.get('MOMENTUM', 2.5))
            w_str = ask_w("STRENGTH", "강도 (STRENGTH)", curr_weights.get('STRENGTH', 1.5))
            w_syn = ask_w("SYNERGY", "시너지 (SYNERGY)", curr_weights.get('SYNERGY', 2.0))
            
            total_score = w_trend + w_mom + w_str + w_syn
            
            if abs(total_score - 10.0) > 0.01:
                config.console.print(f"\n[bold red]경고: 가중치 합계가 {total_score:.1f}점입니다. (권장: 10.0점)[/bold red]")
                config.console.print("[yellow]합계가 10점이 되도록 다시 입력해주세요.[/yellow]")
                curr_weights = {"TREND": w_trend, "MOMENTUM": w_mom, "STRENGTH": w_str, "SYNERGY": w_syn}
                continue
            
            params['WEIGHTS'] = {"TREND": w_trend, "MOMENTUM": w_mom, "STRENGTH": w_str, "SYNERGY": w_syn}
            break
        except ValueError as e:
            if str(e) == "quit": return None
            config.console.print("[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")
            continue

    config.console.print("\n[bold]4. 최종 출력 대상 선택[/bold]")
    filter_choice = Prompt.ask("출력 대상 선택 (1: 매수, 2: 상승, 3: 매수+상승) [dim](이전: b, 메인: q)[/dim]", choices=["1", "2", "3", "b", "q"], default="1")
    if filter_choice.lower() in ['b', 'q']: return None
    if filter_choice == '1': params['OUTPUT_FILTER'] = 'BUY'
    elif filter_choice == '2': params['OUTPUT_FILTER'] = 'RISE'
    else: params['OUTPUT_FILTER'] = 'ALL'

    return params

def _analyze_stock_worker(stock, params=None, restricted_stocks=None, rules_map=None, reserved_codes=None, m_codes=None):
    """(내부함수) 단일 종목 분석 워커 (멀티스레드용)"""
    if restricted_stocks is None: restricted_stocks = {}
    if rules_map is None: rules_map = {}
    if reserved_codes is None: reserved_codes = set()
    if m_codes is None: m_codes = set()
    
    code = stock['code']
    name = stock['name']
    is_custom_rule = stock.get('is_custom_rule', False) # [추가]
    
    # [최적화] 시스템 트레이딩(AutoTrader)과의 API 대역폭 경합 방지 (유동적 Pacing)
    # 모의투자는 0.3초, 실전은 0.05초의 지연을 주어 백그라운드 자동매매가 즉시 호출될 수 있는 틈을 양보합니다.
    delay = 0.3 if config.session.is_simulation else 0.05
    time.sleep(delay)

    try:
        # API 호출 (KIS API Rate Limit 처리 및 누락 방지 재시도)
        df = None
        for attempt in range(2):
            df = api.get_chart_data(code, is_overseas=False)
            if df is not None and not df.empty:
                break
            time.sleep(0.5)
            
        if df is None: return {'error': 'API 응답 없음'}
        if df.empty: return {'error': '차트 데이터 없음 (거래정지 등)'}
        
        current_price = float(df.iloc[-1]['close'])
        ind = indicators.calculate_indicators(df)
        
        # 전일 RSI — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
        prev_rsi = ind.get('prev_rsi') if len(df) >= 16 else None

        # 52주 위치 계산 (슈퍼 모멘텀 판정용)
        w52_pos = 0.0
        if len(df) > 0:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100

        sm_flag, sm_reason = check_smart_money_turnaround(code, is_overseas=False)

        # 상태 분류 및 점수 계산
        state, state_color, state_reason = classify_stock_state(
            df=df, ind=ind, prev_rsi=prev_rsi, thresholds=params, w52_pos=w52_pos, smart_money=sm_flag
        )
        
        if state == "-": return {'error': '지표 계산용 데이터 부족 (신규상장 등)'}

        # [추가] 초기 상태 보존 (로그 출력 시 체결강도 미달로 관망으로 변경되더라도 원본 상태 표시)
        initial_state = state
        initial_state_color = state_color

        # [수정] 사용자 설정 가중치 적용
        weights = params.get('WEIGHTS') if params else None
        score, _ = calculate_score(df=df, ind=ind, weights=weights, smart_money=sm_flag)

        # [수정] 체결강도 조회 최적화: 필터 조건에 맞는 종목만 조회
        vol_strength = None
        
        # 조회 대상 상태 정의 (기본: 매수, 상승)
        target_vol_states = ["매수", "강매수", "역매수", "상승"]
        
        if params:
            filter_mode = params.get("OUTPUT_FILTER", "ALL")
            if filter_mode == "BUY": target_vol_states = ["매수", "강매수"]
            elif filter_mode == "RISE": target_vol_states = ["상승"]
        
        # 현재 상태가 조회 대상에 포함될 때만 체결강도 API 호출
        use_vol = True
        if params is not None and not params.get("USE_VOL", True):
            use_vol = False
            
        if use_vol and state in target_vol_states:
            # [추가] 조회 실패 시 재시도 로직 (최대 2회)
            for _ in range(2):
                try:
                    vol_strength = api.get_realtime_vol_strength(code)
                    if vol_strength is not None: break
                except Exception: time.sleep(0.1)

        # [수정] 매수(강매수, 역추세포함) 또는 상승 상태일 경우 체결강도 기준 엄격히 체크 (필터링)
        if state in ["매수", "강매수", "역매수", "상승"]:
            try:
                if not use_vol:
                    min_vol = 0.0
                elif state == "역매수":
                    min_vol = params.get("MR_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)) if params else config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
                elif params and 'BUY_VOL_STRENGTH' in params:
                    min_vol = params.get('BUY_VOL_STRENGTH', 100.0)
                else:
                    min_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
                
                if min_vol > 0:
                    if vol_strength is None:
                        state = "관망"
                        state_color = "[white]"
                        state_reason = "체결강도 확인 불가 (API 응답 지연)"
                    elif vol_strength < min_vol:
                        state = "관망"
                        state_color = "[white]"
                        state_reason = f"체결강도 미달({vol_strength:.1f}% < {min_vol}%)"
            except Exception: pass

        # 필터링 조건 확인
        is_target = False
        if params:
            filter_mode = params.get("OUTPUT_FILTER", "BUY")
            target_states = []
            if filter_mode == "BUY": target_states = ["매수", "강매수"]
            elif filter_mode == "RISE": target_states = ["상승", "관심"]
            elif filter_mode == "ALL": target_states = ["매수", "강매수", "상승", "관심"]
            if state in target_states:
                is_target = True
        else:
            is_target = True # params가 없으면(엑셀 저장 등) 모두 유효

        obv_val = ind.get('obv')
        obv_trend = ind.get('obv_trend')
        vol_sum = df['volume'].tail(5).sum() if df is not None and 'volume' in df.columns else 0
        
        if df is None or len(df) < config.INDICATOR_PARAMS.get("OBV_MA_PERIOD", 5):
            obv_trend = None
            obv_val = None
            
        if vol_sum == 0 or obv_val is None or math.isnan(obv_val):
            obv_trend = None
            obv_val = None

        return {
            'code': code, 'name': name, 'price': current_price,
            'score': score, 'state': initial_state, 'state_color': initial_state_color, 'state_reason': state_reason,
            'rsi': ind['rsi'], 'adx': ind['adx'], 'cci': ind['cci'], 'obv_trend': obv_trend,
            'psar': ind['psar'], 'macd': ind.get('macd'), 'macd_signal': ind.get('macd_signal'),
            'is_target': is_target, 
            'vol_strength': vol_strength,
            'w52_pos': w52_pos,
            'is_custom_rule': is_custom_rule # [추가]
        }
    except Exception as e:
        return {'error': f'분석 중 예외 발생: {e}'}

def analyze_market_stocks(market_type):
    """선택한 시장의 전체 종목을 분석하고 매수 가능 종목을 출력합니다."""
    
    # 1. DB에서 기존 분석 결과 확인
    cached_data = _load_analysis_result(market_type)
    buy_candidates = []
    params = None
    use_cache = False
    
    # [추가] 개별 룰 로드
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {r['code']: True for r in custom_rules}
    
    # [추가] 트레이딩 제한 종목 로드
    from modules import auto_trade
    restricted_stocks = auto_trade.get_restricted_stocks()
    
    if cached_data:
        updated_at = cached_data['updated_at']
        c_params = cached_data['params']
        
        config.console.print(f"\n[bold cyan]기존 분석 결과가 존재합니다.[/bold cyan]")
        config.console.print(f"• 분석 일시: {updated_at}")
        
        w = c_params.get('WEIGHTS', config.SCORING_WEIGHTS)
        w_str = f"{w.get('TREND', 4.0)}/{w.get('MOMENTUM', 2.5)}/{w.get('STRENGTH', 1.5)}/{w.get('SYNERGY', 2.0)}"
        
        # [수정] 매수 점수 표시 (보정 정보 포함)
        buy_score_val = c_params.get('BUY_SCORE')
        buy_score_str = f"{buy_score_val}점"
        if c_params.get('SCORE_ADJ'):
            buy_score_str += f" (시장보정 {c_params['SCORE_ADJ']:+.1f}점)"

        config.console.print(f"• 분석 조건: 매수 {buy_score_str}, RSI {c_params.get('BUY_RSI_MAX')}, 체결 {c_params.get('BUY_VOL_STRENGTH', 100)}%, 상승 {c_params.get('RISE_SCORE')}점, 가중치 {w_str}")
        
        config.console.print()
        choice = Prompt.ask("기존 결과를 보시겠습니까? [dim](이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="y")
        if choice in ['b', 'q']: return False
        if choice == "y":
            buy_candidates = cached_data['data']
            params = c_params
            use_cache = True
            config.console.print(f"[dim]DB에서 {len(buy_candidates)}개의 종목 정보를 로드했습니다.[/dim]")

    # 2. 새로 분석 (캐시 미사용 시)
    if not use_cache:
        stock_list = _get_master_stock_list(market_type)
        config.console.print(f"\n[bold]{market_type} 전체 종목 수: {len(stock_list)}개[/bold]")
        
        # [추가] stock_list에 is_custom_rule 정보 주입
        for s in stock_list:
            s['is_custom_rule'] = s['code'] in rules_map
        
        c_buy = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        c_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        c_rise = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        c_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        
        w = config.SCORING_WEIGHTS
        w_str = f"{w.get('TREND', 4.0)}/{w.get('MOMENTUM', 2.5)}/{w.get('STRENGTH', 1.5)}/{w.get('SYNERGY', 2.0)}"

        config.console.print(f"현재 설정: 매수 {c_buy}점 / RSI {c_rsi} / 체결 {c_vol}% / 상승 {c_rise}점 / 가중치 {w_str}")

        config.console.print()
        
        # [추가] ETF 종목 포함 여부 확인 (KOSPI에서만 질문)
        if market_type == "KOSPI":
            include_etf_choice = Prompt.ask("ETF 종목을 포함하여 분석하시겠습니까? [dim](y: 포함, n: 제외, 이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="n")
            if include_etf_choice in ['b', 'q']: return False
        else:
            include_etf_choice = 'n'
        
        if include_etf_choice == 'n' and market_type == "KOSPI":
            etf_keywords = [
                "KODEX ", "TIGER ", "KBSTAR ", "RISE ", "ACE ", "ARIRANG ", "PLUS ", 
                "KOSEF ", "HANARO ", "SOL ", "TIMEFOLIO ", "히어로즈 ", "마이티 ", "TREX ", 
                "TRUSTON ", "FOCUS ", "UNTACT ", "WOORI ", "WON ", "BNK ", "KINDEX ", 
                "네비게이터 ", "TIME ", "KIWOOM ", "HK ", "1Q ", "KoAct ", "ITF ", 
                "VITA ", "UNICORN ", "더제이 ", "파워 ", "MIDAS ", "에셋플러스 ", 
                "KCGI ", "DAISHIN343 ", "아이엠에셋 ", "대신 ", "유진 ", "IBK ",
                "ETN ", "스팩 ", "SPAC ", "리츠 ", "REIT "
            ]
            original_len = len(stock_list)
            stock_list = [s for s in stock_list if not any(kw in s['name'] for kw in etf_keywords)]
            config.console.print(f"[dim]이름 기반 ETF/ETN 등 1차 제외 완료: {original_len}개 -> {len(stock_list)}개[/dim]\n")
            
        # [수정] 매수 체결강도 사용 여부 확인 프롬프트 간결화 및 기본값(n) 변경
        # [추가] 토스증권은 체결강도(수급)를 제공하지 않으므로 프롬프트 없이 무조건 미사용 처리
        if config.session.is_toss:
            config.console.print("[dim]토스증권은 체결강도(수급)를 제공하지 않아 해당 조건을 미사용합니다.[/dim]")
            use_vol = False
        else:
            use_vol_choice = Prompt.ask("매수 체결강도(수급) 조건을 사용하여 분석하시겠습니까? [dim](y: 사용, n: 미사용, 이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="n")
            if use_vol_choice in ['b', 'q']: return False
            use_vol = (use_vol_choice == 'y')
            
        config.console.print()
            
        # 파라미터 설정
        change_settings = Prompt.ask("분석 조건을 변경하시겠습니까? [dim](이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="n")
        if change_settings in ['b', 'q']: return False

        if change_settings == 'y':
            params = get_analysis_params(use_vol=use_vol)
            if params is None: return False
            params['INCLUDE_ETF'] = (include_etf_choice == 'y')
            params['USE_VOL'] = use_vol
        else:
            params = {
                "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
                "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                "BUY_VOL_STRENGTH": config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0) if use_vol else 0.0,
                "MR_VOL_STRENGTH": config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0) if use_vol else 0.0,
                "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
                "OUTPUT_FILTER": "BUY",
                "WEIGHTS": config.SCORING_WEIGHTS,
                "INCLUDE_ETF": (include_etf_choice == 'y'),
                "USE_VOL": use_vol
            }
            config.console.print(f"[dim]기본 설정으로 진행합니다. (매수: {params['BUY_SCORE']}점, RSI: {params['BUY_RSI_MAX']}, 체결: {params['BUY_VOL_STRENGTH']}%, 상승: {params['RISE_SCORE']}점)[/dim]")
        
        # 설정 백업 및 적용
        original_thresholds = config.ANALYSIS_THRESHOLDS.copy()
        config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = params["BUY_SCORE"]
        config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = params["BUY_RSI_MAX"]
        config.ANALYSIS_THRESHOLDS["RISE_SCORE"] = params["RISE_SCORE"]
        config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"] = params["BUY_VOL_STRENGTH"]
        
        config.console.print("\n[bold cyan]━━━ 전체 종목 분석 시작 (중단: Ctrl+C) ━━━[/bold cyan]")

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
                console=config.console
            ) as progress:
                task = progress.add_task(f"[cyan]{market_type} 분석 중...[/cyan]", total=len(stock_list))
                
                # [최적화] 실전: 4개 스레드 병렬 처리, 모의: 2개 스레드 병렬 처리
                completed_count = 0
                
                def _process_result(stock_info, res_data):
                    if res_data and 'error' not in res_data:
                        rsi_val = res_data['rsi']
                        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
                        adx_str = f"{res_data['adx']:.1f}" if res_data['adx'] is not None else "-"
                        cci_str = f"{res_data['cci']:.1f}" if res_data['cci'] is not None else "-"
                        obv_trend = res_data.get('obv_trend')
                        obv_str = "상승" if obv_trend is True else ("하락" if obv_trend is False else "-")
                        
                        sar_val = res_data.get('psar')
                        if sar_val is not None:
                            sar_str = "상승" if res_data['price'] > sar_val else "하락"
                        else:
                            sar_str = "-"
                        
                        macd_val = res_data.get('macd')
                        sig_val = res_data.get('macd_signal')
                        macd_str = "골든" if macd_val is not None and sig_val is not None and macd_val > sig_val else "데드"
                        
                        vol_str = ""
                        if res_data.get('vol_strength') is not None:
                            vol_str = f", 체결={res_data['vol_strength']:.0f}%"
                        else:
                            vol_str = ", 체결=확인생략"
                        
                        log_msg = f"[{completed_count}/{len(stock_list)}] [분석] {res_data['name']}({res_data['code']}): 현재가={int(res_data['price']):,}, 점수={res_data['score']:.2f}, 상태={res_data['state']}, RSI={rsi_str}, CCI={cci_str}, ADX={adx_str}, OBV={obv_str}, SAR={sar_str}, MACD={macd_str}{vol_str}"
                        
                        if res_data['is_target']:
                            log_style = "bold green" if res_data['state'] in ["매수", "강매수", "역매수"] else "bold orange3"
                            progress.console.print(f"[{log_style}]{log_msg}[/{log_style}]")
                            buy_candidates.append(res_data)
                        else:
                            progress.console.print(f"[dim]{log_msg}[/dim]")
                    else:
                        err_msg = res_data.get('error', '알 수 없는 오류') if res_data else "데이터 부족 또는 API 응답 없음"
                        progress.console.print(f"[dim red][{completed_count}/{len(stock_list)}] [실패] {stock_info['name']}({stock_info['code']}) - {err_msg}[/dim red]")

                # [최적화] 전체 종목 분석 시 모의투자(2) / 실전투자(4) 통합 멀티스레드 적용
                max_w = config.analysis_max_workers()
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                    futures = {executor.submit(_analyze_stock_worker, stock, params, restricted_stocks, rules_map, reserved_codes, m_codes): stock for stock in stock_list}
                    for future in concurrent.futures.as_completed(futures):
                        completed_count += 1
                        stock = futures[future]
                        try:
                            result = future.result()
                            _process_result(stock, result)
                        except Exception as e:
                            progress.console.print(f"[dim red][{completed_count}/{len(stock_list)}] [오류] {stock['name']}({stock['code']}) - {e}[/dim red]")
                        
                        progress.advance(task)
                    
        except KeyboardInterrupt:
            config.console.print("\n[yellow]분석이 사용자에 의해 중단되었습니다.[/yellow]")
        finally:
            # 설정 복구
            config.ANALYSIS_THRESHOLDS = original_thresholds

    # 결과 테이블 출력
    if not buy_candidates:
        config.console.print("\n[yellow]조건을 만족하는 종목이 없습니다.[/yellow]")
        return

    # [추가] 선별된 종목에 대해 업종 정보 보강 (캐시에 없거나 새로 분석한 경우)
    need_sector_fetch = False
    if buy_candidates and 'sector' not in buy_candidates[0]:
        need_sector_fetch = True
        
    if need_sector_fetch:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task("[cyan]선별된 종목의 업종 정보를 조회 중...[/cyan]", total=len(buy_candidates))
            
            # 병렬 처리로 업종 정보 조회
            def fetch_sector(item):
                delay = 0.3 if config.session.is_simulation else 0.05
                time.sleep(delay)
                try:
                    res = api.get_current_price_data(item['code'], is_overseas=False)
                    if res.get('rt_cd') == '0':
                        return res['output'].get('bstp_kor_isnm', '-')
                except Exception: pass
                return '-'

            # [최적화] 업종 정보 조회 시 모의투자(2) / 실전투자(4) 통합 멀티스레드 적용
            max_w_sec = config.analysis_max_workers()
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w_sec) as executor:
                future_to_idx = {executor.submit(fetch_sector, item): i for i, item in enumerate(buy_candidates)}
                for future in concurrent.futures.as_completed(future_to_idx):
                    buy_candidates[future_to_idx[future]]['sector'] = future.result()
                    progress.advance(task)
        
        # [추가] 업종(Sector) 정보를 기반으로 확실하게 2차 제외
        if not use_cache and not params.get('INCLUDE_ETF', True):
            original_len = len(buy_candidates)
            buy_candidates = [
                item for item in buy_candidates 
                if not any(kw in str(item.get('sector', '')).upper() for kw in ['ETF', 'ETN', '스팩', 'SPAC', '리츠', 'REIT', '인프라투용', '투자회사'])
            ]
            if len(buy_candidates) < original_len:
                config.console.print(f"[dim]업종 기반 ETF/ETN 등 2차 제외 완료: {original_len}개 -> {len(buy_candidates)}개[/dim]")

        # 새로 분석했거나 sector 정보가 추가된 경우 DB 저장
        if not use_cache:
            _save_analysis_result(market_type, buy_candidates, params)

    # 정렬 기준 개선: 1. 점수 높은 순, 2. RSI 낮은 순 (상승 여력)
    # RSI가 None인 경우 맨 뒤로 보내기 위해 999 처리
    buy_candidates.sort(key=lambda x: (-x['score'], x['rsi'] if x['rsi'] is not None else 999))
    
    filter_mode = params.get("OUTPUT_FILTER", "BUY")
    if filter_mode == "BUY": filter_str = "매수"
    elif filter_mode == "RISE": filter_str = "상승"
    else: filter_str = "매수/상승"
    config.console.print(f"\n[bold]분석 결과: {filter_str} 종목 {len(buy_candidates)}개[/bold]")
    
    # [수정] 페이징 처리 및 컬럼 포맷 변경 (한 줄 출력, 말줄임 방지)
    # 터미널 높이에 따라 페이지 크기 자동 조절
    try:
        terminal_lines = shutil.get_terminal_size().lines
        # 테이블 헤더, 타이틀, 여백, 프롬프트 공간 등을 고려하여 제외 (약 13줄)
        page_size = max(5, terminal_lines - 13)
    except Exception:
        page_size = 15

    total_items = len(buy_candidates)
    total_pages = (total_items + page_size - 1) // page_size
    
    for page in range(total_pages):
        start_idx = page * page_size
        end_idx = min((page + 1) * page_size, total_items)
        page_items = buy_candidates[start_idx:end_idx]
        any_restricted_in_page = False
        
        table = Table(title=f"{market_type} 유망 종목 ({filter_str}) - 페이지 {page+1}/{total_pages}", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("No.", justify="right", width=4)
        table.add_column("종목명(코드)", justify="left", no_wrap=True)
        table.add_column("업종", justify="center", no_wrap=True)
        table.add_column("현재가", justify="right")
        table.add_column("52주(위치)", justify="right")
        table.add_column("점수", justify="center")
        table.add_column("상태", justify="center")
        table.add_column("추세SMO", justify="center")
        table.add_column("RSI", justify="right")
        table.add_column("CCI", justify="right")
        table.add_column("ADX", justify="right")
        table.add_column("체결강도", justify="right")
        
        for i, item in enumerate(page_items):
            rsi_val = item['rsi']
            rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
            if rsi_val is not None:
                if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[magenta]{rsi_str}[/]"
                elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[red]{rsi_str}[/]"
                elif 45 <= rsi_val < 55: rsi_str = f"[orange3]{rsi_str}[/]"
                elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: rsi_str = f"[yellow]{rsi_str}[/]"
                else: rsi_str = f"[blue]{rsi_str}[/]"

            adx_val = item['adx']
            adx_str = f"{adx_val:.1f}" if adx_val is not None else "-"
            if adx_val is not None:
                if adx_val >= 40: adx_str = f"[magenta]{adx_str}[/]" 
                elif adx_val >= 30: adx_str = f"[red]{adx_str}[/]"     
                elif adx_val >= 20: adx_str = f"[orange3]{adx_str}[/]"
                elif adx_val >= 15: adx_str = f"[yellow]{adx_str}[/]"
                else: adx_str = f"[white]{adx_str}[/]"

            cci_val = item['cci']
            cci_str = f"{cci_val:.1f}" if cci_val is not None else "-"
            if cci_val is not None:
                if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[red]{cci_str}[/]"
                elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[orange3]{cci_str}[/]"
                elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: cci_str = f"[yellow]{cci_str}[/]"
                else: cci_str = f"[blue]{cci_str}[/]"
            
            # SAR 상태
            sar_val = item.get('psar')
            sar_icon = "[red]⬆[/]" if sar_val and item['price'] > sar_val else "[blue]⬇[/]"
            
            # MACD 상태
            macd_val = item.get('macd')
            sig_val = item.get('macd_signal')
            macd_icon = "-"
            if macd_val is not None and sig_val is not None:
                zero_sign = "+" if macd_val > 0 else "-"
                cross_char = "G" if macd_val > sig_val else "D"
                m_color = "red" if macd_val > sig_val else "blue"
                macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"

            s_color = item.get('state_color', '[white]').replace('[', '').replace(']', '')
            display_state = item['state']
            
            # 52주 위치 색상
            pos = item.get('w52_pos', 0)
            w_color = "[white]"
            if pos >= 90: w_color = "[red]"
            elif pos >= 80: w_color = "[orange3]"
            elif pos <= 20: w_color = "[blue]"
            
            obv_trend = item.get('obv_trend')
            obv_icon = "-"
            if obv_trend is True: obv_icon = "[red]▲[/]"
            elif obv_trend is False: obv_icon = "[blue]▼[/]"
            
            trend_str = f"{sar_icon} {macd_icon} {obv_icon}"
            
            vol_val = item.get('vol_strength')
            vol_str = "-"
            if vol_val is not None:
                std_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
                v_color = "[red]" if vol_val >= std_vol else "[blue]"
                vol_str = f"{v_color}{vol_val:.1f}%[/]"
            
            name_display = item['name']
            marks = []
            if item['code'] in restricted_stocks: marks.append("-")
            if item.get('is_custom_rule'): marks.append("+")
            if item['code'] in m_codes: marks.append("=")
            if item['code'] in reserved_codes: marks.append("[magenta]*[/magenta]")
            
            if marks:
                name_display += f"[dim]{''.join(marks)}[/dim]"

            table.add_row(
                str(start_idx + i + 1),
                f"{name_display} [dim]({item['code']})[/dim]",
                item.get('sector', '-'),
                f"{int(item['price']):,}원",
                f"{w_color}{pos:.1f}%[/]",
                f"[{s_color}]{item['score']}[/]",
                f"[{s_color}]{display_state}[/]",
                trend_str,
                rsi_str,
                cci_str,
                adx_str,
                vol_str
            )
            
            # 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(page_items):
                table.add_section()
                
        config.console.print(table, crop=False)
        sys.stdout.flush()
        
        if page < total_pages - 1:
                if Prompt.ask(f"[dim]다음 페이지를 보시겠습니까? (이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="y").lower() in ['b', 'q', 'n']:
                    break

    # 상세 분석 이동 기능
    from modules import chart
    
    while True:
        config.console.print("\n[dim]개별 분석 및 상세 차트 분석을 보려면 종목 번호를 입력하세요 (Enter: 메뉴복귀, 이전: b, 메인: q)[/dim]")
        choice = Prompt.ask("선택", default="b", show_default=False)
        
        if choice.lower() in ['b', 'q']:
            return False
            
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(buy_candidates):
                selected = buy_candidates[idx]
                code = selected['code']
                name = selected['name']
                
                # [수정] 차트 분석 전 개별 종목 분석 결과 출력
                config.console.print(f"\n[bold green]>> {name}({code}) 개별 종목 심층 분석 실행[/bold green]")
                diagnose_stock(code, name, target_is_overseas=False)
            else:
                config.console.print("[red]잘못된 번호입니다. 리스트에 있는 번호를 입력해주세요.[/red]")
        else:
            config.console.print("[red]올바른 번호를 입력해주세요.[/red]")

def save_all_market_analysis():
    """코스피/코스닥 전 종목 분석 결과를 엑셀로 저장"""
    # [최적화] openpyxl은 이 함수에서만 사용 → 지연 임포트로 프로그램 시작 시간 단축
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter


    config.console.print("\n[bold]전체 종목 분석결과 저장 (Export to Excel)[/bold]")
    config.console.print("[dim]코스피 및 코스닥 전 종목을 분석하여 파일로 저장합니다.[/dim]")
    config.console.print("[dim]시간이 오래 걸릴 수 있습니다. (중단: Ctrl+C)[/dim]\n")
    
    if Prompt.ask("진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
        return False

    # [추가] 개별 룰 로드 (전체 조회 최적화)
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {}
    for r in custom_rules:
        r_dict = dict(r)
        if r_dict.get('weights') and isinstance(r_dict['weights'], str):
            try: r_dict['weights'] = json.loads(r_dict['weights'])
            except Exception: r_dict['weights'] = None
        rules_map[r_dict['code']] = r_dict
    
    # [추가] 예약 매매 및 메모 마커 조회
    reserved_codes = set()
    try:
        pending_reserves = db_manager.db.get_pending_reserved_orders()
        reserved_codes = set(o['code'] for o in pending_reserves)
    except Exception: pass
    m_codes = utils.get_memo_codes()

    # [추가] 트레이딩 제한 종목 로드
    from modules import auto_trade
    restricted_stocks = auto_trade.get_restricted_stocks()

    markets = ["KOSPI", "KOSDAQ"]
    results = {} # market -> list of dict

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=config.console
        ) as progress:
            
            for market_type in markets:
                stock_list = _get_master_stock_list(market_type)
                if not stock_list: continue
                
                results[market_type] = []
                
                # 1. 기술적 분석 (Chart Data)
                analyzed_data = []
                task = progress.add_task(f"[cyan]{market_type} 기술적 분석 중...[/cyan]", total=len(stock_list))

                max_w = config.analysis_max_workers()

                # 1. 기술적 분석 병렬 처리
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                    futures = {executor.submit(_analyze_stock_worker, stock, None, restricted_stocks, rules_map, reserved_codes, m_codes): stock for stock in stock_list}
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result()
                            if result and 'error' not in result: analyzed_data.append(result)
                        except Exception: pass
                        progress.advance(task)
                
                # 2. 업종 정보 조회 (Price Data) 및 데이터 정제
                if analyzed_data:
                    task_sector = progress.add_task(f"[cyan]{market_type} 업종 정보 조회 및 정리 중...[/cyan]", total=len(analyzed_data))
                    
                    def fetch_sector_and_format(item):
                        sector = "-"
                        try:
                            res = api.get_current_price_data(item['code'], is_overseas=False)
                            if res.get('rt_cd') == '0':
                                sector = res['output'].get('bstp_kor_isnm', '-')
                        except Exception: pass
                        
                        # 데이터 포맷팅 (소수점 1자리, 정수 등)
                        rsi = round(item['rsi'], 1) if item['rsi'] is not None else None
                        adx = round(item['adx'], 1) if item['adx'] is not None else None
                        cci = round(item['cci'], 1) if item['cci'] is not None else None
                        w52 = int(item['w52_pos']) if item['w52_pos'] is not None else 0
                        vol = round(item['vol_strength'], 1) if item.get('vol_strength') else None
                        
                        # SAR/MACD 상태
                        sar_val = item.get('psar')
                        if sar_val is not None and not math.isnan(sar_val):
                            sar_state = "상승" if item['price'] > sar_val else "하락"
                        else:
                            sar_state = "-"
                        
                        macd_state = "-"
                        macd_val = item.get('macd')
                        sig_val = item.get('macd_signal')
                        if macd_val is not None and sig_val is not None and not math.isnan(macd_val) and not math.isnan(sig_val):
                            macd_state = "골든" if macd_val > sig_val else "데드"

                        name_display = item['name']
                        marks = []
                        if item['code'] in restricted_stocks: marks.append("-")
                        if item.get('is_custom_rule'): marks.append("+")
                        if item['code'] in m_codes: marks.append("=")
                        if item['code'] in reserved_codes: marks.append("*")
                        if marks: name_display += "".join(marks)

                        # [추가] 비고 (개별 룰 요약)
                        note = ""
                        if item['code'] in rules_map:
                            rule = rules_map[item['code']]
                            changes = []
                            
                            # 전역 설정값
                            def_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
                            def_buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
                            def_buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
                            def_sell_score = config.SELL_STRATEGY["SELL_SCORE"]
                            def_tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
                            def_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]

                            # 비교
                            if rule.get('buy_score') != def_buy_score: changes.append(f"매수점수({rule['buy_score']})")
                            if rule.get('buy_rsi') != def_buy_rsi: changes.append(f"매수RSI({rule['buy_rsi']})")
                            if rule.get('buy_vol_strength') and rule['buy_vol_strength'] != def_buy_vol: changes.append(f"체결({rule['buy_vol_strength']}%)")
                            def_ask_ratio = config.ANALYSIS_THRESHOLDS.get('BUY_ASK_BID_RATIO', 1.0)
                            if rule.get('buy_ask_bid_ratio') is not None and rule['buy_ask_bid_ratio'] != def_ask_ratio: changes.append(f"매도잔량비({rule['buy_ask_bid_ratio']}배)")
                            def_auto = config.ANALYSIS_THRESHOLDS.get('AUTO_ADJUST_ASK_BID_RATIO', True)
                            if rule.get('auto_adjust_ask_bid_ratio') is not None and bool(rule['auto_adjust_ask_bid_ratio']) != def_auto: changes.append(f"자동연동({bool(rule['auto_adjust_ask_bid_ratio'])})")
                            if rule.get('sell_score') != def_sell_score: changes.append(f"매도점수({rule['sell_score']})")
                            if rule.get('take_profit') != def_tp: changes.append(f"익절({rule['take_profit']}%)")
                            if rule.get('stop_loss') != def_sl: changes.append(f"손절({rule['stop_loss']}%)")
                            if rule.get('weights'): changes.append("가중치")
                            
                            if changes:
                                note = f"개별룰: {', '.join(changes)}"
                            else:
                                note = "개별룰 적용"

                        return {
                            "종목코드": item['code'],
                            "종목명": name_display,
                            "업종": sector,
                            "현재가(원)": item['price'],
                            "52주위치(%)": w52,
                            "점수": item['score'],
                            "상태": item['state'],
                            "상태사유": item['state_reason'],
                            "RSI": rsi,
                            "CCI": cci,
                            "ADX": adx,
                            "SAR": sar_state,
                            "MACD": macd_state,
                            "OBV": "상승" if item['obv_trend'] is True else ("하락" if item['obv_trend'] is False else "-"),
                            "체결강도": vol,
                            "비고": note # [추가]
                        }

                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                        futures_sector = {executor.submit(fetch_sector_and_format, item): item for item in analyzed_data}
                        for future in concurrent.futures.as_completed(futures_sector):
                            try:
                                formatted_result = future.result()
                                results[market_type].append(formatted_result)
                            except Exception: pass
                            progress.advance(task_sector)

        # 엑셀 저장
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = os.path.join(config.DATA_DIR, f"market_analysis_{timestamp}.xlsx")
        
        if not any(results.values()):
            config.console.print("\n[red]저장할 데이터가 없습니다. (마스터 파일 오류 또는 분석 실패)[/red]")
            return

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task(f"[cyan]엑셀 파일 저장 중... ({os.path.basename(filename)})[/cyan]", total=len(results))
            
            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                for market_type, data in results.items():
                    if data:
                        # 점수 높은 순 정렬
                        data.sort(key=lambda x: (-x['점수'], x['RSI'] if x['RSI'] is not None else 999))
                        df = pd.DataFrame(data)
                        df.to_excel(writer, sheet_name=market_type, index=False)
                        
                        # 엑셀 서식 적용 (필터, 컬럼 너비, 색상 등)
                        ws = writer.sheets[market_type]
                        ws.auto_filter.ref = ws.dimensions
                        
                        # 헤더에서 컬럼 인덱스 찾기
                        header = [c.value for c in ws[1]]
                        try:
                            col_price = header.index("현재가(원)") + 1
                            col_state = header.index("상태") + 1
                            col_score = header.index("점수") + 1
                            
                            # [수정] 모든 컬럼 너비 자동 조절
                            for i, col_name in enumerate(header):
                                col_idx = i + 1
                                col_letter = get_column_letter(col_idx)
                                
                                # 헤더 텍스트 길이 고려
                                s_header = str(col_name)
                                max_width = len(s_header) + sum(0.7 for c in s_header if ord(c) > 127)
                                
                                for row in range(2, ws.max_row + 1):
                                    val = ws.cell(row=row, column=col_idx).value
                                    if val:
                                        s_val = str(val)
                                        length = len(s_val) + sum(0.7 for c in s_val if ord(c) > 127)
                                        if length > max_width: max_width = length
                                
                                limit = 100 if col_name == "비고" else 60
                                ws.column_dimensions[col_letter].width = min(max_width * 1.2, limit)

                            for row in range(2, ws.max_row + 1):
                                # 현재가 쉼표 포맷
                                ws.cell(row=row, column=col_price).number_format = '#,##0'
                                
                                # 점수 소수점 2자리 포맷
                                ws.cell(row=row, column=col_score).number_format = '0.00'
                                
                                # 상태 컬럼 색상 적용
                                cell = ws.cell(row=row, column=col_state)
                                val = cell.value
                                if val in ["매수", "강매수"]: cell.font = Font(color="FF0000", bold=True)
                                elif val == "상승": cell.font = Font(color="FF8C00", bold=True)
                                elif val == "주의": cell.font = Font(color="DAA520", bold=True)
                                elif val == "매도": cell.font = Font(color="0000FF", bold=True)
                        except ValueError: pass
                    
                    progress.advance(task)
        
        config.console.print(f"\n[bold green]저장 완료: {filename}[/bold green]")
        
    except KeyboardInterrupt:
        config.console.print("\n[yellow]작업이 중단되었습니다.[/yellow]")
    except Exception as e:
        config.console.print(f"\n[bold red]오류 발생: {e}[/bold red]")

def _fetch_chart_data(item, is_overseas):
    """(내부함수) print_table 1단계: 과거(전체) 일봉 차트 데이터 수신.

    캐시 적중(6시간 이내·당일) 시 즉시 반환되고, 캐시 미스 때만 실제 250봉 다운로드가 발생한다.
    → '데이터 수신' 프로그래스 바는 실제 전체 데이터를 받아오는 동안에만 길어진다.
    """
    name, code = item
    try:
        return api.get_chart_data(code, is_overseas, 'daily', False)
    except Exception as e:
        logger.error(f"[{code}] 차트 데이터 수신 오류: {e}")
        return None

def _collect_table_data(item, title, is_overseas, use_investor_data, chart_df=None, preloaded_curr=None):
    """(내부함수) print_table 2단계 전반부: 당일 실시간 데이터(현재가/체결강도/수급/상세) 수신.

    chart_df가 주어지면(1단계에서 수신) 차트는 재수신하지 않는다. 실제 지표 분석/행 포맷은
    _analyze_table_row가 담당한다. (메뉴1처럼 '데이터 수신' / '실시간 데이터 수신 및 분석' 단계 분리)
    preloaded_curr: 멀티시세(30종목/1콜) 프리페치로 확보한 현재가 응답({'rt_cd','output'}).
      주어지면 종목별 현재가 REST를 생략한다(TPS 절감). 없으면 종전대로 개별 조회.
    """
    name, code = item
    bundle = {'curr_data': None, 'chart_df': chart_df, 'inv_list': None,
              'rt_strength': None, 'ask_bid_ratio': None, 'detail': None}
    try:
        cached_ex = config.session.exchange_cache.get(code, "NAS") if is_overseas else None
        curr_data = inv_list = detail = None
        rt_strength = None
        ask_bid_ratio = None  # [토스] 체결강도 미제공 → 매도잔량비로 대체 표시

        # [최적화] 필요한 다수의 API를 병렬(Fan-out)로 일제히 호출하여 체감 속도 극대화
        for attempt in range(2):
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                # [실시간/통합] 현재가·체결강도를 KRX/NXT 한 번의 호출로 통합 조회한다(include_nxt=True).
                # cache_ttl=0 → 예열 캐시를 재사용하지 않는 라이브 호출(25초 예열 폐지). 시스템 트레이딩과
                # 동일 캐시 키(cp_{code}_J / vol_{code})를 공유해 동시 조회 시 중복 호출이 합쳐진다.
                # (모의투자(VTS)는 NXT 미지원이라 내부에서 NX 조회를 건너뛴다 → 정규장 시세만 표시)
                # [멀티시세] 프리페치된 현재가가 있으면 종목별 REST 생략
                # [최적화] fast_info_ttl=30: 해외 장외가 병합이 직전 TV 일괄 예열 캐시를 재사용
                #  (종목별 TV 단건 재조회 제거. 개요 화면 장외가 최대 ~30초 지연 허용)
                fut_curr = ex.submit(api.get_current_price_data, code, is_overseas, True, 0, 30.0) if preloaded_curr is None else None
                # [최적화] 차트는 1단계에서 받았으면 재수신하지 않는다(미제공 시에만 캐시 경로로 조회).
                fut_chart = ex.submit(api.get_chart_data, code, is_overseas, 'daily', False) if chart_df is None else None
                fut_inv = ex.submit(api.get_investor_trend, code) if not is_overseas and use_investor_data else None
                fut_vol = ex.submit(api.get_realtime_vol_strength, code, is_overseas, cached_ex, True, 0) if not is_overseas and not use_investor_data else None
                fut_detail = ex.submit(api.fetch_overseas_detail_price, code, cached_ex) if is_overseas else None
                # [토스] 체결강도 대체 지표(매도잔량비)용 호가 조회
                fut_ab = ex.submit(api.get_ask_bid_ratio, code, False) if (config.session.is_toss and not is_overseas) else None

                curr_data = fut_curr.result() if fut_curr is not None else preloaded_curr
                if fut_chart is not None:
                    chart_df = fut_chart.result()
                inv_list = fut_inv.result() if fut_inv else None
                if fut_vol:
                    try: rt_strength = fut_vol.result()
                    except Exception: pass
                if fut_ab:
                    try: ask_bid_ratio = fut_ab.result()
                    except Exception: pass
                detail = fut_detail.result() if fut_detail else None

            if curr_data and curr_data.get('rt_cd') == '0' and chart_df is not None and not chart_df.empty:
                break
            time.sleep(0.5)

        bundle.update({'curr_data': curr_data, 'chart_df': chart_df, 'inv_list': inv_list,
                       'rt_strength': rt_strength, 'ask_bid_ratio': ask_bid_ratio, 'detail': detail})
    except Exception as e:
        logger.error(f"[{code}] 데이터 수집 오류: {e}")
    return bundle

def _analyze_table_row(item, title, is_overseas, use_investor_data, restricted_stocks, rules_map, market_regime_adj, reserved_codes, m_codes, bundle):
    """(내부함수) print_table 2단계: 수집된 데이터(bundle)로 지표 분석 및 행 포맷."""
    try:
        name, code = item
        w52_pos_str, per_str, pbr_str, shar_str = "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]"
        foreign_rate_str = "[dim]-[/dim]"
        inv_str = "[dim]-[/dim]"
        cached_ex = config.session.exchange_cache.get(code, "NAS") if is_overseas else None
        strength_display = ""
        ask_bid_ratio = None  # [토스] 체결강도 미제공 → 매도잔량비로 대체 표시

        # [수정] 타이틀 기반으로 주식/ETF 컨텍스트 정확히 구분 (데이터 처리 및 컬럼 매칭용)
        # 기존: 코드 형태(숫자 여부)로만 판단하여 ETF(QQQ 등)를 주식으로 오인하는 문제 해결
        is_us_stock_context = is_overseas and ("주식" in title)
        is_us_etf_context = is_overseas and ("ETF" in title)

        # [최적화] 1단계(_collect_table_data)에서 수집한 원천 데이터를 사용한다.
        curr_data = bundle.get('curr_data')
        chart_df = bundle.get('chart_df')
        inv_list = bundle.get('inv_list')
        rt_strength = bundle.get('rt_strength')
        ask_bid_ratio = bundle.get('ask_bid_ratio')
        detail = bundle.get('detail')

        # [추가] 차트 데이터 당일 종가/고가/저가 실시간 갱신 (점수 0.5점 오차 방지)
        try:
            rt_price = 0.0
            if curr_data and curr_data.get('rt_cd') == '0':
                if is_overseas: 
                    rt_price = float(curr_data['output'].get('last', 0) or 0)
                else: 
                    nxt_price = float(curr_data['output'].get('ats_prpr', 0) or 0)
                    krx_price = float(curr_data['output'].get('stck_prpr', 0) or 0)
                    rt_price = nxt_price if nxt_price > 0 else krx_price

            indicators.apply_realtime_price(chart_df, rt_price, market_date=utils.market_today(is_overseas))
        except Exception: pass

        # [추가] 토스: 현재가 API가 등락(전일대비)/52주 고저를 제공하지 않으므로 차트(캔들)에서 보강한다.
        # (이 함수는 이미 chart_df를 확보하므로 추가 API 호출 없이 out에 주입한다)
        if config.session.is_toss and curr_data and curr_data.get('rt_cd') == '0' \
           and chart_df is not None and not chart_df.empty:
            _o = curr_data['output']
            try:
                _h52 = float(chart_df['high'].max()); _l52 = float(chart_df['low'].min())
                _cur = float(chart_df['close'].iloc[-1])
                _prev = float(chart_df['close'].iloc[-2]) if len(chart_df) >= 2 else _cur
                if is_overseas:
                    # 52주 위치는 가격 기반이므로 차트로 산출(detail 경로 유지),
                    # PER/PBR/시가총액은 토스 미제공이라 N/A로 둔다.
                    if not detail:
                        detail = {}
                    detail.setdefault('h52p', _h52)
                    detail.setdefault('l52p', _l52)
                    detail.setdefault('last', _cur)
                    if _prev > 0:
                        _o['diff'] = _cur - _prev
                        _o['rate'] = (_cur - _prev) / _prev * 100
                else:
                    _o['w52_hgpr'] = str(_h52); _o['w52_lwpr'] = str(_l52)
                    if _prev > 0:
                        _o['stck_sdpr'] = str(int(_prev))  # 기준가(전일종가) → diff = 현재가 - 기준가
            except Exception: pass

        # [멀티시세] 이 TR은 52주 고저(w52_*)를 제공하지 않으므로 차트(250봉)로 보강한다.
        # (개별 현재가 API의 액면분할 보정 경로와 동일한 산출 방식)
        if not is_overseas and curr_data and curr_data.get('rt_cd') == '0' \
           and curr_data.get('output', {}).get('_src') == 'multi' \
           and chart_df is not None and not chart_df.empty:
            _o = curr_data['output']
            try:
                _o['w52_hgpr'] = str(float(chart_df['high'].tail(250).max()))
                _o['w52_lwpr'] = str(float(chart_df['low'].tail(250).min()))
            except Exception: pass

        ind = indicators.calculate_indicators(chart_df)

        if not is_overseas:
            if use_investor_data and inv_list:
                p = api.safe_int(inv_list[0].get('prsn_ntby_qty'))
                f = api.safe_int(inv_list[0].get('frgn_ntby_qty'))
                i = api.safe_int(inv_list[0].get('orgn_ntby_qty'))
                def fmt_inv(val):
                    if val == 0: return "[dim]-[/dim]"
                    abs_val = abs(val)
                    if abs_val >= 1_000_000_000: s = f"{val/1_000_000_000:,.1f}B"
                    if abs_val >= 1_000_000: s = f"{val/1_000_000:,.1f}M"
                    elif abs_val >= 1000: s = f"{val/1000:,.0f}K"
                    else: s = f"{val:,}"
                    return f"[red]{s}[/]" if val > 0 else f"[blue]{s}[/]"
                inv_str = f"{fmt_inv(p)} {fmt_inv(f)} {fmt_inv(i)}"
            if not use_investor_data:
                if config.session.is_toss:
                    # 토스: 체결강도 미제공 → 매도잔량비(매도/매수 총잔량)로 대체 표시(숫자만)
                    # 색상: 기준 1.0배 중심 5단계(체결강도 100% 밴딩과 동일 방향, 높을수록 빨강 계열)
                    if ask_bid_ratio is not None:
                        if ask_bid_ratio >= 2.0: ab_color = "[magenta]"
                        elif ask_bid_ratio >= 1.5: ab_color = "[red]"
                        elif ask_bid_ratio > 1.0: ab_color = "[orange3]"
                        elif ask_bid_ratio == 1.0: ab_color = "[white]"
                        elif ask_bid_ratio >= 0.7: ab_color = "[yellow]"
                        else: ab_color = "[blue]"
                        strength_display = f" {ab_color}[{ask_bid_ratio:.2f}][/]"
                    else:
                        strength_display = " [dim][-][/dim]"
                elif rt_strength is not None:
                    if rt_strength >= 150: s_color = "[magenta]"
                    elif rt_strength >= 120: s_color = "[red]"
                    elif rt_strength > 100: s_color = "[orange3]"
                    elif rt_strength == 100: s_color = "[white]"
                    elif rt_strength >= 80: s_color = "[yellow]"
                    else: s_color = "[blue]"
                    strength_display = f" {s_color}[{rt_strength:,.0f}%][/]"
                else: strength_display = " [dim][0%][/dim]"
            if curr_data and curr_data.get('rt_cd') == '0':
                out = curr_data.get('output', {})
                foreign_rate_str = f"{out.get('hts_frgn_ehrt', '-')}%"
                try:
                    h52, l52, c = float(out.get('w52_hgpr', 0)), float(out.get('w52_lwpr', 0)), float(out.get('stck_prpr', 0))
                    if h52 > l52:
                        pos = (c - l52)/(h52 - l52)*100
                        if pos >= 90: w_color = "[red]"
                        elif pos >= 80: w_color = "[orange3]"
                        elif pos <= 30: w_color = "[blue]"
                        elif pos <= 50: w_color = "[yellow]"
                        else: w_color = "[white]"
                        w52_pos_str = f"{w_color}{pos:.1f}%[/]"
                except Exception: pass
        else:
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ANALYSIS_DEBUG] {code} Detail: {detail} | StockCtx:{is_us_stock_context} EtfCtx:{is_us_etf_context}")

            if detail:
                if is_us_stock_context: 
                    per_str = detail.get('perx', '[dim]-[/dim]')
                    pbr_str = detail.get('pbrx', '[dim]-[/dim]') if detail.get('pbrx') != '-' else '[dim]-[/dim]'
                if is_us_etf_context:
                    try:
                        shar_val = float(detail.get('shar', 0))
                        shar_str = f"{shar_val/1_000_000:.1f}M" if shar_val >= 1_000_000 else f"{shar_val:,.0f}"
                    except Exception: pass
                try:
                    h52, l52, c = float(detail.get('h52p', 0)), float(detail.get('l52p', 0)), float(detail.get('last', 0))
                    if h52 > l52:
                        pos = (c - l52)/(h52 - l52)*100
                        if pos >= 90: w_color = "[red]"
                        elif pos >= 80: w_color = "[orange3]"
                        elif pos <= 30: w_color = "[blue]"
                        elif pos <= 50: w_color = "[yellow]"
                        else: w_color = "[white]"
                        w52_pos_str = f"{w_color}{pos:.1f}%[/]"
                except Exception: pass

        if curr_data and curr_data.get('rt_cd') == '0':
            out = curr_data['output']
            if is_overseas:
                curr = float(out.get('last', 0) or 0)
                rate = float(out.get('rate', 0) or 0)
                diff = float(out.get('diff', 0) or 0)
                if rate < 0 and diff > 0: diff = -diff
                curr_fmt = f"${curr:,.2f}"
                diff_str = f"{diff:+.2f}"
            else:
                nxt_curr = int(out.get('ats_prpr', 0) or 0)
                krx_curr = int(out.get('stck_prpr', 0) or 0)
                base_price = int(out.get('stck_sdpr', 0) or 0)
                curr = nxt_curr if nxt_curr > 0 else krx_curr
                
                if base_price > 0:
                    diff = curr - base_price
                    rate = (diff / base_price) * 100
                else:
                    try: rate = float(out.get('prdy_ctrt', 0))
                    except Exception: rate = 0.0
                    try: diff = int(out.get('prdy_vrss', 0))
                    except Exception: diff = 0
                    
                curr_fmt = f"{curr:,}"
                diff_str = f"{diff:+}"

            rate_color = "[red]" if rate > 0 else ("[blue]" if rate < 0 else "[white]")
            rate_str = f"{rate_color}{diff_str} ({rate:+.2f}%)[/]{strength_display}"

            # 전일 RSI — calculate_indicators가 계산한 값 재사용 (중복 계산 제거·SSOT)
            prev_rsi_val = ind.get('prev_rsi') if chart_df is not None and not chart_df.empty and len(chart_df) >= 16 else None

            # 적응형 임계값 적용
            thresholds = None
            rule = rules_map.get(code)
            if rule:
                # 개별 룰이 존재하는 경우 개별 룰의 임계값을 최우선 적용
                thresholds = {
                    "BUY_SCORE": rule['buy_score'],
                    "BUY_RSI_MAX": rule['buy_rsi'],
                    "BUY_VOL_STRENGTH": rule.get('buy_vol_strength', config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)),
                    "WEIGHTS": rule.get('weights'),
                    "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
                }
            elif market_regime_adj and not is_overseas:
                # 개별 룰이 없으면 시장 국면에 따른 보정값 적용
                mrkt_name = str(curr_data['output'].get('rprs_mrkt_kor_name') or curr_data['output'].get('rprs_mrkt_eng_name') or '')
                score_adj = 0.0
                if "코스닥" in mrkt_name or "KOSDAQ" in mrkt_name.upper():
                    score_adj = market_regime_adj.get("KOSDAQ", 0.0)
                else:
                    score_adj = market_regime_adj.get("KOSPI", 0.0)
                
                if score_adj != 0:
                    thresholds = {
                        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj,
                        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
                    }
                    
            # 52주 위치 계산 (슈퍼 모멘텀 마킹용)
            w52_pos_val = 0.0
            if not is_overseas:
                try:
                    h52, l52, c = float(curr_data['output'].get('w52_hgpr', 0)), float(curr_data['output'].get('w52_lwpr', 0)), float(curr_data['output'].get('stck_prpr', 0))
                    if h52 > l52: w52_pos_val = (c - l52)/(h52 - l52)*100
                except Exception: pass
            else:
                try:
                    if detail:
                        h52, l52, c = float(detail.get('h52p', 0)), float(detail.get('l52p', 0)), float(detail.get('last', 0))
                        if h52 > l52: w52_pos_val = (c - l52)/(h52 - l52)*100
                except Exception: pass

            sm_flag, sm_reason = check_smart_money_turnaround(code, is_overseas)
            class_name, class_color, _ = classify_stock_state(df=chart_df, ind=ind, prev_rsi=prev_rsi_val, thresholds=thresholds, w52_pos=w52_pos_val, smart_money=sm_flag)

            def fmt(v): return f"{v:,.2f}" if is_overseas else f"{int(v):,}"
            def fmt_idx(val): return f"{int(val):,}" if val is not None else "[dim]-[/dim]"

            curr_price_color = "[white]"
            if ind.get('ema_20') is not None and ind.get('ema_60') is not None:
                if ind['ema_20'] > ind['ema_60']:
                    curr_price_color = "[red]" if curr > ind['ema_20'] else "[white]"
                elif ind['ema_20'] < ind['ema_60']:
                    curr_price_color = "[blue]" if curr < ind['ema_20'] else "[orange3]"
            curr_str = f"{curr_price_color}{curr_fmt}[/]"

            # [수정] 이평선 색상 규칙 단순화 (계층적 분석)
            ema5_color = "[white]"
            if ind.get('ema_5') is not None and ind.get('ema_20') is not None:
                ema5_color = "[red]" if ind['ema_5'] > ind['ema_20'] else "[blue]"

            ema20_color = "[white]"
            if ind.get('ema_20') is not None and ind.get('ema_60') is not None:
                ema20_color = "[red]" if ind['ema_20'] > ind['ema_60'] else "[blue]"

            ema60_color = "[white]"
            if ind.get('ema_60') is not None and ind.get('ema_120') is not None:
                ema60_color = "[red]" if ind['ema_60'] > ind['ema_120'] else "[blue]"

            ema120_color = "[white]"
            if chart_df is not None and not chart_df.empty and len(chart_df) > 121:
                try:
                    ema120_series = chart_df['close'].ewm(span=120, adjust=False).mean()
                    if ema120_series.iloc[-1] > ema120_series.iloc[-2]:
                        ema120_color = "[red]"
                    else:
                        ema120_color = "[blue]"
                except Exception: pass

            ema_5_str = f"{ema5_color}{fmt_idx(ind.get('ema_5'))}[/]"
            ema_20_str = f"{ema20_color}{fmt_idx(ind.get('ema_20'))}[/]"
            ema_60_str = f"{ema60_color}{fmt_idx(ind.get('ema_60'))}[/]"
            ema_120_str = f"{ema120_color}{fmt_idx(ind.get('ema_120'))}[/]"

            # SAR 상태
            sar_val = ind.get('psar')
            if sar_val is not None and not math.isnan(sar_val):
                sar_icon = "[red]⬆[/]" if curr > sar_val else "[blue]⬇[/]"
            else:
                sar_icon = "[dim]-[/dim]"
            
            # MACD 상태
            macd_val = ind.get('macd')
            sig_val = ind.get('macd_signal')
            macd_icon = "[dim]-[/dim]"
            if macd_val is not None and sig_val is not None and not math.isnan(macd_val) and not math.isnan(sig_val):
                zero_sign = "+" if macd_val > 0 else "-"
                cross_char = "G" if macd_val > sig_val else "D"
                m_color = "red" if macd_val > sig_val else "blue"
                macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"

            # OBV 상태 및 Value
            obv_trend = ind.get('obv_trend')
            obv_val = ind.get('obv')
            vol_sum = chart_df['volume'].tail(5).sum() if chart_df is not None and 'volume' in chart_df.columns else 0
            
            if chart_df is None or len(chart_df) < config.INDICATOR_PARAMS.get("OBV_MA_PERIOD", 5):
                obv_trend = None
                obv_val = None
                
            if vol_sum == 0 or obv_trend is None or obv_val is None or math.isnan(obv_val):
                obv_icon = "[dim]-[/dim]"
                obv_disp = "[dim]-[/dim]"
            else:
                obv_icon = "[red]▲[/]" if obv_trend else "[blue]▼[/]"
                obv_c = "red" if obv_trend else "blue"
                abs_val = abs(obv_val)
                if abs_val >= 999_950_000_000: obv_str = f"{obv_val/1_000_000_000_000:,.1f}T"
                elif abs_val >= 999_950_000: obv_str = f"{obv_val/1_000_000_000:,.1f}B"
                elif abs_val >= 999_500: obv_str = f"{obv_val/1_000_000:,.1f}M"
                elif abs_val >= 999.5: obv_str = f"{obv_val/1_000:,.0f}K"
                else: obv_str = f"{obv_val:,.0f}"
                obv_disp = f"[{obv_c}]{obv_str}[/]"

            trend_str = f"{sar_icon} {macd_icon} {obv_icon}"

            rsi_str = f"{ind.get('rsi'):.1f}" if ind.get('rsi') is not None else "[dim]-[/dim]"
            if ind.get('rsi') is not None:
                if ind.get('rsi') >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[magenta]{rsi_str}[/]"
                elif 55 <= ind.get('rsi') < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[red]{rsi_str}[/]"
                elif 45 <= ind.get('rsi') < 55: rsi_str = f"[orange3]{rsi_str}[/]"
                elif config.INDICATOR_PARAMS["RSI_LOWER"] < ind.get('rsi') < 45: rsi_str = f"[yellow]{rsi_str}[/]"
                else: rsi_str = f"[blue]{rsi_str}[/]"

            adx_str = f"{ind.get('adx'):.1f}" if ind.get('adx') is not None else "[dim]-[/dim]"
            if ind.get('adx') is not None:
                if ind.get('adx') >= 40: adx_str = f"[magenta]{adx_str}[/]" 
                elif ind.get('adx') >= 30: adx_str = f"[red]{adx_str}[/]"     
                elif ind.get('adx') >= 20: adx_str = f"[orange3]{adx_str}[/]"
                elif ind.get('adx') >= 15: adx_str = f"[yellow]{adx_str}[/]"
                else: adx_str = f"[white]{adx_str}[/]"

            cci_str = f"{ind.get('cci'):.1f}" if ind.get('cci') is not None else "[dim]-[/dim]"
            if ind.get('cci') is not None:
                if ind.get('cci') >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[red]{cci_str}[/]"
                elif 0 < ind.get('cci') < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[orange3]{cci_str}[/]"
                elif config.INDICATOR_PARAMS["CCI_LOWER"] < ind.get('cci') <= 0: cci_str = f"[yellow]{cci_str}[/]"
                else: cci_str = f"[blue]{cci_str}[/]"

            final_name_str = name
            if ind.get('ema_5') is not None and ind.get('ema_20') is not None and ind.get('ema_60') is not None and ind.get('adx') is not None and ind.get('rsi') is not None and ind.get('cci') is not None:
                all_ema_green = (ind.get('ema_5') > ind.get('ema_20') and ind.get('ema_20') > ind.get('ema_60'))
                all_ema_red = (ind.get('ema_5') < ind.get('ema_20') and ind.get('ema_20') < ind.get('ema_60'))
                price_above_ema5 = (curr > ind.get('ema_5'))
                if ind.get('adx') >= 40 and ind.get('rsi') >= config.INDICATOR_PARAMS["RSI_UPPER"] and ind.get('cci') >= config.INDICATOR_PARAMS["CCI_UPPER"]: final_name_str = f"[magenta]{name}[/]"
                elif all_ema_green and price_above_ema5 and ind.get('adx') >= 30 and ind.get('rsi') >= 55 and ind.get('cci') >= config.INDICATOR_PARAMS["CCI_UPPER"]: final_name_str = f"[red]{name}[/]"
                elif all_ema_red and price_above_ema5 and ind.get('adx') >= 20 and ind.get('rsi') >= 45 and ind.get('cci') >= 0: final_name_str = f"[orange3]{name}[/]"
                elif (ind.get('ema_20') > ind.get('ema_60') and ind.get('ema_60') > ind.get('ema_5')) and ind.get('adx') >= 30 and ind.get('rsi') <= config.INDICATOR_PARAMS["RSI_LOWER"] and ind.get('cci') <= config.INDICATOR_PARAMS["CCI_UPPER"]: final_name_str = f"[blue]{name}[/]"
            
            # 제한 종목 표시
            is_restricted = False
            marks = []
            if code in restricted_stocks:
                marks.append("-")
                is_restricted = True

            # 개별 룰 적용 종목 표시
            is_custom_rule = False
            if code in rules_map:
                marks.append("+")
                is_custom_rule = True
                
            is_memo = False
            if code in m_codes:
                marks.append("=")
                is_memo = True
            is_reserved = False
            if code in reserved_codes:
                marks.append("[magenta]*[/magenta]")
                is_reserved = True
            if marks:
                final_name_str += f"[dim]{''.join(marks)}[/dim]"

            row_data = [final_name_str, f"{code}", f"{class_color}{class_name}[/]", curr_str, rate_str, w52_pos_str, ema_5_str, ema_20_str, ema_60_str, ema_120_str, trend_str, rsi_str, cci_str, adx_str]
            if not is_overseas:
                if use_investor_data: row_data.append(inv_str)
                else: row_data.append(obv_disp)
            else:
                if is_us_stock_context: row_data.extend([per_str, pbr_str])
                elif is_us_etf_context: row_data.append(shar_str)
            return row_data, is_restricted, is_custom_rule, is_memo, is_reserved
        else:
            return [name, code, "[dim]-[/dim]", "실패", *["[dim]-[/dim]"] * (14 if not is_overseas else (12 if is_us_stock_context else 11))], False, False, False, False
    except Exception as e:
        logger.error(f"[{code}] 분석 오류: {e}")
        return [name, code, "[red]Error[/]", "[dim]-[/dim]", *["[dim]-[/dim]"] * (14 if not is_overseas else (12 if is_us_stock_context else 11))], False, False, False, False

def _realtime_and_analyze(item, title, is_overseas, use_investor_data, restricted_stocks, rules_map, market_regime_adj, reserved_codes, m_codes, chart_df, preloaded_curr=None):
    """(내부함수) print_table 2단계: 당일 실시간 데이터 수신 + 지표 분석.

    1단계에서 받은 과거 차트(chart_df)를 받아 현재가/체결강도/수급을 수신하고 지표를 계산한다.
    preloaded_curr가 있으면(멀티시세 프리페치) 종목별 현재가 REST를 생략한다.
    """
    bundle = _collect_table_data(item, title, is_overseas, use_investor_data, chart_df=chart_df, preloaded_curr=preloaded_curr)
    return _analyze_table_row(item, title, is_overseas, use_investor_data, restricted_stocks,
                              rules_map, market_regime_adj, reserved_codes, m_codes, bundle)

def _print_table_worker(item, title, is_overseas, use_investor_data, restricted_stocks, rules_map, market_regime_adj, reserved_codes, m_codes):
    """(호환용) 단일 종목 수집+분석을 한 번에 수행한다. (수집/분석 단계 분리 이전 호출부·테스트 호환)"""
    bundle = _collect_table_data(item, title, is_overseas, use_investor_data)
    return _analyze_table_row(item, title, is_overseas, use_investor_data, restricted_stocks,
                              rules_map, market_regime_adj, reserved_codes, m_codes, bundle)

def print_table(title, data_list, is_overseas=False, market_regime_adj=None):
    is_domestic_etf = ("ETF" in title and not is_overseas)
    use_investor_data = False
    if not is_overseas and data_list:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]수급 데이터 확인 중 (KIS API)...[/cyan]", total=None)
            test_data = api.get_investor_trend(data_list[0][1])
            if test_data:
                sample = test_data[0]
                if any(api.safe_int(sample.get(k)) != 0 for k in ['prsn_ntby_qty', 'frgn_ntby_qty', 'orgn_ntby_qty']): use_investor_data = True
    
    # [이동] 적응형 임계값 준비 (테이블 생성 전으로 이동)
    use_adaptive = False
    if not is_overseas and config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
        use_adaptive = True
        if market_regime_adj is None:
            market_regime_adj = {}
            try:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    console=config.console,
                    transient=True
                ) as progress:
                    progress.add_task("[cyan]시장 국면 분석 중 (KIS API)...[/cyan]", total=None)
                    _, kospi_adj = get_market_regime("KOSPI")
                    _, kosdaq_adj = get_market_regime("KOSDAQ")
                    market_regime_adj["KOSPI"] = kospi_adj
                    market_regime_adj["KOSDAQ"] = kosdaq_adj
            except Exception:
                use_adaptive = False
        elif not market_regime_adj:
            use_adaptive = False
    elif market_regime_adj is None:
        market_regime_adj = {}

    failed_list = []
    display_title = f"\n{title}"
    table = Table(title=display_title, box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
    table.add_column("종목명", justify="left", style="white", no_wrap=True)
    table.add_column("코드", justify="center", style="dim")
    table.add_column("분류", justify="center") 
    table.add_column("현재가", justify="right")
    col_header = "등락폭 (등락률)"
    if not is_overseas and not use_investor_data:
        col_header += " [매도비]" if config.session.is_toss else " [강도]"
    table.add_column(col_header, justify="right")
    table.add_column("52주", justify="right")
    table.add_column("EMA(5)", justify="right")
    table.add_column("EMA(20)", justify="right")
    table.add_column("EMA(60)", justify="right")
    table.add_column("EMA(120)", justify="right")
    table.add_column("추세SMO", justify="center")
    table.add_column("RSI", justify="right")
    table.add_column("CCI", justify="right")
    table.add_column("ADX", justify="right")
    
    is_us_stock = is_overseas and ("주식" in title)
    is_us_etf = is_overseas and ("ETF" in title)
    
    # [추가] 개별 룰 로드
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {}
    for r in custom_rules:
        r_dict = dict(r)
        if r_dict.get('weights') and isinstance(r_dict['weights'], str):
            try: r_dict['weights'] = json.loads(r_dict['weights'])
            except Exception: r_dict['weights'] = None
        rules_map[r_dict['code']] = r_dict
    any_custom_rule = False
    
    # [추가] 트레이딩 제한 종목 로드
    from modules import auto_trade
    restricted_stocks = auto_trade.get_restricted_stocks()
    any_restricted = False
    
    # [추가] 예약 매매 및 메모 마커 조회
    reserved_codes = set()
    try:
        pending_reserves = db_manager.db.get_pending_reserved_orders()
        reserved_codes = set(o['code'] for o in pending_reserves)
    except Exception: pass
    m_codes = utils.get_memo_codes()
    
    if not is_overseas:
        if use_investor_data: table.add_column("수급(개/외/기)", justify="center")
        else: table.add_column("OBV", justify="right")
    else:
        if is_us_stock:
            table.add_column("PER", justify="right", style="dim")
            table.add_column("PBR", justify="right", style="dim") 
        elif is_us_etf:
            table.add_column("상장주수", justify="right", style="dim")

    # [최적화] 통신+연산 통합 처리를 위한 스레드 수 안정화
    if is_overseas:
        max_w = 4 if config.session.is_simulation else 5 # 야후 API 동시 호출 차단 방지
    else:
        # KIS API 동시 호출 제한(TPS) 방지를 위해 실전투자 시 5로 하향
        max_w = 4 if config.session.is_simulation else 5
    # [수정] 메뉴1처럼 진행 상태를 '데이터 수집'과 '지표 분석' 2단계로 분리하여 운영자 인지성을 높인다.
    _fail_cols = 14 if not is_overseas else (12 if is_us_stock else 11)
    def _fail_row(idx):
        name, code = data_list[idx]
        return ([name, code, "[dim]-[/dim]", "실패", *["[dim]-[/dim]"] * _fail_cols], False, False, False, False)

    try:
        used_marks = set()
        charts = [None] * len(data_list)
        results = [None] * len(data_list)

        # 1단계: 데이터 수신 (과거 전체 일봉). 캐시 적중 시 즉시 통과, 캐시 미스 때만 실제 다운로드.
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=config.console,
            transient=True
        ) as progress:
            task_d = progress.add_task(f"[cyan]{title} (데이터 수신)[/cyan]", total=len(data_list))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                fut_map = {
                    executor.submit(_fetch_chart_data, item, is_overseas): i
                    for i, item in enumerate(data_list)
                }
                for future in concurrent.futures.as_completed(fut_map):
                    idx = fut_map[future]
                    try:
                        charts[idx] = future.result()
                    except Exception as e:
                        logger.error(f"Chart fetch error: {e}")
                        charts[idx] = None
                    progress.advance(task_d)

        # [멀티시세] 국내 그룹 현재가를 30종목/1콜로 프리페치 (종목당 현재가 REST 제거 → TPS 절감)
        #  NXT 병합(ats_prpr)이 필요 없는 경우에만 사용: 모의투자(NXT 미지원) 또는 실전 정규장(phase 'skip').
        #  실패/미지원 시 None → 워커가 종전대로 종목별 조회(동일 출력 폴백).
        multi_prices = None
        if not is_overseas and data_list and not config.session.is_toss and getattr(config, 'USE_MULTI_PRICE', True):
            _use_multi = config.session.is_simulation
            if not _use_multi:
                try: _use_multi = api._nxt_quote_phase() == 'skip'
                except Exception: _use_multi = False
            if _use_multi:
                try:
                    multi_prices = api.get_multi_current_prices([c for _, c in data_list])
                except Exception:
                    multi_prices = None

        def _preloaded(code):
            if multi_prices and code in multi_prices:
                return {'rt_cd': '0', 'output': multi_prices[code]}
            return None

        # 2단계: 실시간 데이터 수신 및 분석 (당일 현재가/체결강도/수급 수신 + 지표 연산)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=config.console,
            transient=True
        ) as progress:
            task_a = progress.add_task(f"[cyan]{title} (실시간 데이터 수신 및 분석)[/cyan]", total=len(data_list))

            # [최적화] 해외 그룹: TradingView 일괄 조회(HTTP 1회)로 fast_info 마이크로 캐시를 예열한 뒤
            #  워커를 기동한다. 워커의 장외가 병합(get_current_price_data → get_yf_fast_info)이
            #  종목별 TV 단건 조회 대신 캐시 적중으로 처리되어 체감 속도를 높인다.
            #  백그라운드 워머(OverviewWarmer, 실전 15초 주기)가 방금 예열해 둔 경우에는 재조회도 생략.
            #  (별도 '예열 중' 스피너 없이 본 프로그래스 바 안에서 수행 — 예열 중에는 0% 스피너로 표시)
            if is_overseas and data_list:
                try:
                    warm_fresh_sec = max(5, int(getattr(config, 'OVERVIEW_WARM_INTERVAL_SEC', 15))) + 5
                    api.prefetch_multiple_current_prices(
                        [c for _, c in data_list], is_overseas=True, skip_if_fresh_sec=warm_fresh_sec
                    )
                except Exception as e:
                    logger.debug(f"[print_table] 해외 일괄 예열 실패: {e}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                fut_map = {
                    executor.submit(
                        _realtime_and_analyze, item, title, is_overseas, use_investor_data,
                        restricted_stocks, rules_map, market_regime_adj, reserved_codes, m_codes, charts[i],
                        _preloaded(item[1])
                    ): i
                    for i, item in enumerate(data_list)
                }
                for future in concurrent.futures.as_completed(fut_map):
                    idx = fut_map[future]
                    try:
                        results[idx] = future.result()
                    except Exception as e:
                        logger.error(f"Analyze worker error: {e}")
                        results[idx] = _fail_row(idx)
                    progress.advance(task_a)

        # 결과 테이블 추가
        for idx, result_item in enumerate(results):
            if not result_item:
                failed_list.append(data_list[idx])
                continue

            row_data, is_res, is_cust, is_mem, is_rsv = result_item

            if is_res: used_marks.add('-')
            if is_cust: used_marks.add('+')
            if is_mem: used_marks.add('=')
            if is_rsv: used_marks.add('*')

            if len(row_data) > 3 and (row_data[3] == "실패" or "Error" in str(row_data[2])):
                failed_list.append(data_list[idx])

            table.add_row(*row_data)
            if table.row_count % 5 == 0 and table.row_count < len(data_list):
                table.add_section()

    except Exception as e:
        logger.error(f"데이터 분석 중 오류: {e}")

    try:
        config.console.print(table, crop=False)
        
        mark_desc = []
        if '-' in used_marks: mark_desc.append("[dim]([/dim] - [dim]) 시스템 트레이딩 제한 종목[/dim]")
        if '+' in used_marks: mark_desc.append("[dim]([/dim] + [dim]) 개별 룰 적용 종목[/dim]")
        if '=' in used_marks: mark_desc.append("[dim]([/dim] = [dim]) 메모 설정 종목[/dim]")
        if '*' in used_marks: mark_desc.append("[dim]([/dim] * [dim]) 예약 매매 설정 종목[/dim]")
        if mark_desc:
            config.console.print(f" [dim]※[/dim] {' [dim]|[/dim] '.join(mark_desc)}")

        sys.stdout.flush()
    except Exception as e:
        logger.error(f"테이블 출력 중 오류(tmux 리사이즈 등): {e}")
        config.console.print(f"[red]테이블 출력 실패: {e}[/red]")

    return failed_list

def show_stock_analysis():
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "5"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        utils.clear_screen()
        menu_items = [
            ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
            ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"),
            ("5", "전체 보기", "View All"), ("6", "개별 종목 분석", "Individual Analysis"),
            ("7", "전체 종목 분석", "Market Analysis")
        ]
        choice_str = utils.show_menu("종목 시세 분석 (Stock Analysis)", menu_items, default_choice=last_choice, custom_prompt="번호 입력 [dim](예: 1,3 또는 12 / 반복: 1@)[/dim]")
        if choice_str.lower() in ['b', 'q']: return False
        if choice_str.lower() == 'h':
            if getattr(utils, 'show_help', None):
                utils.show_help()
                utils.pause()
            continue
        

        interval = 0
        if choice_str.endswith('@'):
            interval = 60
            choice_str = choice_str.rstrip('@')

        raw_choices = [c.strip() for c in choice_str.split(',') if c.strip()]
        choices = []
        for c in raw_choices:
            if c.isdigit() and len(c) > 1:
                choices.extend(list(c))
            else:
                choices.append(c)

        if not choices: continue

        if '6' in choices:
            last_choice = choice_str # [수정] 정상 처리된 유효한 입력만 기억
            context.USER_ACTION_BREADCRUMB.append("[6] 개별분석")
            if diagnose_stock() is not False: utils.pause()
            continue

        if '7' in choices:
            last_choice = choice_str # [수정] 정상 처리된 유효한 입력만 기억
            context.USER_ACTION_BREADCRUMB.append("[7] 전체분석")
            sub_menu = [("1", "코스피", "KOSPI"), ("2", "코스닥", "KOSDAQ"), ("3", "전체 종목 분석 결과 저장", "Save to Excel")]
            sub_choice = utils.show_menu("분석할 시장을 선택하세요", sub_menu, default_choice="1")
            
            if sub_choice.lower() in ['b', 'q']: continue
            
            if sub_choice == "3":
                sub_map = dict((k, v) for k, v, _ in sub_menu)
                context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {sub_map.get(sub_choice, '')}")
                if save_all_market_analysis() is not False: utils.pause()
                continue

            market_type = "KOSPI" if sub_choice == "1" else "KOSDAQ"
            context.USER_ACTION_BREADCRUMB.append(f"[시장선택] {market_type}")
            
            if analyze_market_stocks(market_type) is not False: utils.pause()
            continue

        selected_groups = set()
        group_names = []
        
        for c in choices:
            if c == '1': 
                selected_groups.add('stocks_kr')
                group_names.append("국내주식")
            elif c == '2': 
                selected_groups.add('etfs_kr')
                group_names.append("국내ETF")
            elif c == '3': 
                selected_groups.add('stocks_us')
                group_names.append("미국주식")
            elif c == '4': 
                selected_groups.add('etfs_us')
                group_names.append("미국ETF")
            elif c == '5': 
                selected_groups.update(['stocks_kr', 'etfs_kr', 'stocks_us', 'etfs_us'])
                group_names.append("전체보기")
        
        if not selected_groups:
            config.console.print("[red]잘못된 입력입니다.[/red]")
            time.sleep(1)
            continue

        last_choice = choice_str # [수정] 정상 처리된 유효한 입력만 기억
        context.USER_ACTION_BREADCRUMB.append(f"[{choice_str}] {','.join(group_names)}")

        # [추가] 최초 클론 등으로 stock.json이 없으면 기본 관심종목(삼성전자)으로 자동 생성
        if not os.path.exists(config.STOCK_DATA_FILE):
            config.session.save_stock_config({
                "stocks_kr": [{"name": "삼성전자", "code": "005930", "exchange": "KOSPI"}],
                "etfs_kr": [], "stocks_us": [], "etfs_us": []
            })
            config.session.load_stock_config()  # exchange 캐시 재구성
            config.console.print("[yellow]관심종목 파일(json/stock.json)이 없어 기본 종목(삼성전자)으로 새로 생성했습니다.[/yellow]\n")

        target_list = []
        order_map = [
            ('stocks_kr', "국내 주식 기술적 분석", False),
            ('etfs_kr', "국내 ETF 기술적 분석", False),
            ('stocks_us', "미국 주식 기술적 분석", True),
            ('etfs_us', "미국 ETF 기술적 분석", True)
        ]

        for key, title, is_ovs in order_map:
            if key in selected_groups:
                d_list = [(x['name'], x['code']) for x in config.session.stock_data.get(key, [])]
                target_list.append((title, d_list, is_ovs))

        # [추가] 조회 대상 종목이 하나도 없을 때 빈 화면 대신 안내 메시지 출력
        if not any(d_list for _, d_list, _ in target_list):
            if not any(config.session.stock_data.get(k) for k in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]):
                config.console.print("[yellow]관심종목에 추가된 종목이 없습니다. [7] 관심 종목 관리 메뉴에서 종목을 추가해주세요.[/yellow]")
            else:
                config.console.print("[yellow]선택한 분류에 등록된 종목이 없습니다.[/yellow]")
            utils.pause()
            continue

        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

        try:
            while True:
                if interval > 0:
                    now_str = datetime.now().strftime("%H:%M:%S")
                    config.console.print(f"\n[dim]조회 시간: {now_str}[/dim]")
                
                # [최적화] 조회 주기마다 한 번만 시장 국면 분석 수행 (중복 API 호출 방지)
                shared_regime_adj = None
                has_domestic = any(not is_ovs for _, _, is_ovs in target_list)
                if has_domestic and config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
                    shared_regime_adj = {}
                    try:
                        with Progress(
                            SpinnerColumn(),
                            TextColumn("[progress.description]{task.description}"),
                            BarColumn(),
                            console=config.console,
                            transient=True
                        ) as progress:
                            progress.add_task("[cyan]시장 국면 분석 중 (KIS API)...[/cyan]", total=None)
                            _, k_adj = get_market_regime("KOSPI")
                            _, q_adj = get_market_regime("KOSDAQ")
                            shared_regime_adj["KOSPI"] = k_adj
                            shared_regime_adj["KOSDAQ"] = q_adj
                    except Exception:
                        pass

                failed_targets = []

                try:
                    for title, d_list, is_ovs in target_list:
                        if d_list: 
                            failed = print_table(title, d_list, is_ovs, market_regime_adj=shared_regime_adj)
                            if failed:
                                failed_targets.append((title, failed, is_ovs))
                except Exception as e:
                    logger.error(f"분석 루프 실행 중 오류: {e}")
                    config.console.print(f"[red]분석 중 오류 발생: {e}[/red]")
                
                if interval <= 0:
                    if failed_targets:
                        total_failed = sum(len(f_list) for _, f_list, _ in failed_targets)
                        if Prompt.ask(f"\n[yellow]⚠️ 조회 실패한 {total_failed}개 종목을 다시 시도하시겠습니까?[/yellow]", choices=["y", "n"], default="y") == "y":
                            config.console.print()
                            target_list = failed_targets
                            continue
                    break 

                config.console.print() 
                try:
                    for remaining in range(interval, -1, -1):
                        config.console.print(f"[bold yellow]다음 조회까지 {remaining}초 대기 중입니다. (중단: Ctrl+C)[/]   ", end="\r")
                        time.sleep(1)
                except KeyboardInterrupt:
                    config.console.print("\n[yellow]반복 조회를 중단하고 메뉴로 돌아갑니다.[/yellow]")
                    break
        except KeyboardInterrupt: config.console.print("\n[yellow]작업이 취소되었습니다.[/yellow]")
        except Exception as e:
            logger.error(f"분석 기능 실행 중 치명적 오류: {e}")
            config.console.print(f"\n[bold red]오류 발생: {e}[/bold red]")
            
        if interval > 0:
            return False
        else:
            utils.pause()

def get_snapshot(code, is_overseas):
    """주문 시점의 종목 상태 스냅샷 생성 (DB 저장용)"""
    snapshot = {}
    try:
        # 1. 차트 데이터 및 지표
        df = api.get_chart_data(code, is_overseas)
        if df is not None and not df.empty:
            ind = indicators.calculate_indicators(df)
            # numpy float 등을 일반 float으로 변환하여 저장
            snapshot['indicators'] = {k: (float(v) if v is not None else None) for k, v in ind.items()}
            snapshot['price'] = float(df.iloc[-1]['close'])
        
        # 2. 환율 (해외인 경우)
        if is_overseas:
            snapshot['exchange_rate'] = utils.get_exchange_rate()
            
        snapshot['market'] = "Overseas" if is_overseas else "Domestic"
        
    except Exception as e:
        snapshot['error'] = str(e)
        
    return snapshot

def _print_period_price_common(code, is_overseas, limit=20):
    """기간별 시세 출력 공통 함수"""
    def _fmt_vol(v):
        val = float(v)
        if val == 0: return "[dim]-[/dim]"
        abs_val = abs(val)
        if abs_val >= 999_950_000_000: return f"{val/1_000_000_000_000:,.1f}T"
        elif abs_val >= 999_950_000: return f"{val/1_000_000_000:,.1f}B"
        elif abs_val >= 999_500: return f"{val/1_000_000:,.1f}M"
        elif abs_val >= 999.5: return f"{val/1_000:,.0f}K"
        return f"{val:,.0f}"

    # [수정] 단순 조회이므로 status 사용
    df = None
    investor_map = {} # [추가]
    
    is_domestic_index = not is_overseas and code in ["KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150"]
    
    from modules import market
    all_idx_codes = [c for n, c in market.ALL_INDICES]
    is_global_index = is_overseas and code in all_idx_codes
    is_index = is_domestic_index or is_global_index

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]기간별 시세 데이터 조회 중...[/cyan]", total=None)
        if is_domestic_index:
            df = get_domestic_index_data(code)
            # [Fix] 공유 캐시 객체 보호: 아래에서 ma/diff/OBV 등 컬럼을 추가하므로 복사본 사용
            if df is not None:
                df = df.copy()
        else:
            df = api.get_chart_data(code, is_overseas)
            
        # [추가] 수급 데이터 조회
        frgn_rates_map = {} # [추가] 실제 지분율 맵
        if not is_overseas and not is_domestic_index:
            try:
                inv_data = api.get_investor_trend(code)
                if inv_data:
                    for item in inv_data:
                        investor_map[item['stck_bsop_date']] = item
                        
                # 외인 소진율 데이터 조회 및 병합 (최근 30일)
                frate_data = api.get_daily_foreign_rate(code)
                if frate_data:
                    for item in frate_data:
                        d_key = item['stck_bsop_date']
                        if d_key in investor_map:
                            investor_map[d_key]['hts_frgn_ehrt'] = item.get('hts_frgn_ehrt')
                        else:
                            investor_map[d_key] = {'hts_frgn_ehrt': item.get('hts_frgn_ehrt')}
                            
                # [추가] 실제 외국인 지분율 역산 로직
                cp_res = api.get_current_price_data(code, is_overseas=False)
                if cp_res.get('rt_cd') == '0':
                    out = cp_res['output']
                    lstn_stcn = api.safe_int(out.get('lstn_stcn'))
                    frgn_hldn_qty = api.safe_int(out.get('frgn_hldn_qty'))
                    
                    if lstn_stcn > 0:
                        sorted_dates = sorted(list(investor_map.keys()), reverse=True)
                        current_hldn = frgn_hldn_qty
                        for d_key in sorted_dates:
                            frgn_rates_map[d_key] = (current_hldn / lstn_stcn) * 100
                            f_net = api.safe_int(investor_map[d_key].get('frgn_ntby_qty'))
                            current_hldn -= f_net
            except Exception: pass

    if df is None or df.empty: return

    # 이동평균선 계산
    for w in [5, 20, 60, 120]:
        df[f'ma{w}'] = df['close'].ewm(span=w, adjust=False).mean()

    # 등락폭/등락률 계산 (get_chart_data는 기본 제공하지 않음)
    df['diff'] = df['close'].diff()
    df['rate'] = df['close'].pct_change() * 100

    # OBV 및 OBV_MA 계산
    df['OBV'] = indicators.get_obv_full_series(df)
    df['OBV_MA'] = df['OBV'].ewm(span=config.INDICATOR_PARAMS["OBV_MA_PERIOD"], adjust=False).mean()

    # 최신순 정렬 및 limit 적용
    df_sorted = df.sort_values('date', ascending=False)
    if limit:
        recent_df = df_sorted.head(limit)
    else:
        recent_df = df_sorted

    title_prefix = "[해외주식]" if is_overseas else "[국내주식]"
    if is_index:
        idx_name = code
        if is_domestic_index:
            d_map = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "KOSPI200": "코스피200", "KOSDAQ150": "코스닥150"}
            idx_name = d_map.get(code, code)
        else:
            idx_name = next((n for n, c in market.ALL_INDICES if c == code), code)
        title_prefix = f"[{idx_name}]"
        
    period_str = f"(최근 {limit}일)" if limit else "(전체)"
    table = Table(title=f"{title_prefix} 기간별 시세 {period_str}", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("종가", justify="right")
    table.add_column("등락폭 (등락률)", justify="right")
    table.add_column("시가", justify="right")
    table.add_column("고가", justify="right")
    table.add_column("저가", justify="right")
    table.add_column("5일선", justify="right")
    table.add_column("20일선", justify="right")
    table.add_column("60일선", justify="right")
    table.add_column("120일선", justify="right")
    table.add_column("OBV", justify="right") # [이동]
    if not is_overseas and not is_domestic_index:
        table.add_column("외인률", justify="right") # [추가]
        table.add_column("수급(개/외/기)", justify="center") # [수정]

    for i, (idx, row) in enumerate(recent_df.iterrows()):
        date_str = str(row['date'])
        if len(date_str) == 8: date_str = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        
        close = row['close']
        diff = row['diff'] if not pd.isna(row['diff']) else 0
        rate = row['rate'] if not pd.isna(row['rate']) else 0
        
        def fmt_p(val): 
            if is_index:
                return f"{val:,.0f}" if val >= 1000 else f"{val:,.2f}"
            return f"{val:,.2f}" if is_overseas else f"{int(val):,}"
            
        def fmt_diff(val): 
            if is_index:
                return f"{val:+.0f}" if abs(val) >= 1000 else f"{val:+.2f}"
            return f"{val:+.2f}" if is_overseas else f"{int(val):+}"
        
        c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
        diff_str = f"{c_color}{fmt_diff(diff)} ({rate:+.2f}%)[/]"
        
        # [수정] 이동평균선 색상 규칙 변경 (이평선 간 배열 기준)
        ma5_val, ma20_val = row['ma5'], row['ma20']
        ma60_val, ma120_val = row['ma60'], row['ma120']

        def get_ma_color(val, ma_type):
            if pd.isna(val): return "dim"
            
            if ma_type == 5:
                if pd.isna(ma20_val): return "dim"
                return "red" if val > ma20_val else "blue"
            elif ma_type == 20:
                if pd.isna(ma60_val): return "white"
                return "red" if val > ma60_val else "blue"
            elif ma_type == 60:
                if pd.isna(ma120_val): return "white"
                return "red" if val > ma120_val else "blue"
            elif ma_type == 120:
                if i + 1 < len(recent_df):
                    prev_ma120 = recent_df.iloc[i+1]['ma120']
                    if not pd.isna(prev_ma120):
                        return "red" if val > prev_ma120 else "blue"
                return "white"
            return "dim"

        def fmt_ma(val, color):
            if pd.isna(val): return "[dim]-[/dim]"
            return f"[{color}]{fmt_p(val)}[/]"

        # OBV 포맷팅
        obv_val = row['OBV']
        obv_ma_val = row['OBV_MA']
        if pd.isna(obv_val):
            obv_disp = "[dim]-[/dim]"
        else:
            obv_trend = obv_val > obv_ma_val if not pd.isna(obv_ma_val) else None
            if obv_trend is None:
                obv_c = "white"
            else:
                obv_c = "red" if obv_trend else "blue"
            
            abs_val = abs(obv_val)
            if abs_val >= 999_950_000_000: obv_str = f"{obv_val/1_000_000_000_000:,.1f}T"
            elif abs_val >= 999_950_000: obv_str = f"{obv_val/1_000_000_000:,.1f}B"
            elif abs_val >= 999_500: obv_str = f"{obv_val/1_000_000:,.1f}M"
            elif abs_val >= 999.5: obv_str = f"{obv_val/1_000:,.0f}K"
            else: obv_str = f"{obv_val:,.0f}"
            obv_disp = f"[{obv_c}]{obv_str}[/]"

        # [추가] 수급 데이터 포맷팅
        inv_str = "[dim]-[/dim]"
        foreign_rate_str = "[dim]-[/dim]"
        if not is_overseas and not is_domestic_index:
            d_key = str(row['date']).replace('-', '')[:8]
            if d_key in investor_map:
                item = investor_map[d_key]
                p = api.safe_int(item.get('prsn_ntby_qty'))
                f = api.safe_int(item.get('frgn_ntby_qty'))
                o = api.safe_int(item.get('orgn_ntby_qty'))
                
                # [수정] 역산된 실제 외국인 지분율 우선 적용
                if d_key in frgn_rates_map:
                    foreign_rate_str = f"{frgn_rates_map[d_key]:.2f}%"
                else:
                    f_rate = item.get('hts_frgn_ehrt')
                    if f_rate is not None and str(f_rate).strip():
                        try: foreign_rate_str = f"{float(f_rate):.2f}%"
                        except Exception: pass

                def _fmt_i(val):
                    if val == 0: return "[dim]-[/dim]"
                    abs_val = abs(val)
                    if abs_val >= 1_000_000_000: s = f"{val/1_000_000_000:,.1f}B"
                    if abs_val >= 1_000_000: s = f"{val/1_000_000:,.1f}M"
                    elif abs_val >= 1000: s = f"{val/1000:,.0f}K"
                    else: s = f"{val:,}"
                    return f"[red]{s}[/]" if val > 0 else f"[blue]{s}[/]"
                
                inv_str = f"{_fmt_i(p)} {_fmt_i(f)} {_fmt_i(o)}"

        row_data = [
            date_str, 
            fmt_p(close), 
            diff_str, 
            fmt_p(row['open']), 
            fmt_p(row['high']), 
            fmt_p(row['low']), 
            fmt_ma(ma5_val, get_ma_color(ma5_val, 5)),
            fmt_ma(ma20_val, get_ma_color(ma20_val, 20)),
            fmt_ma(ma60_val, get_ma_color(ma60_val, 60)),
            fmt_ma(ma120_val, get_ma_color(ma120_val, 120)),
            obv_disp
        ]
        if not is_overseas and not is_domestic_index:
            row_data.append(foreign_rate_str)
            row_data.append(inv_str)

        table.add_row(*row_data)
        
        if (i + 1) % 5 == 0 and (i + 1) < len(recent_df):
            table.add_section()
    
    config.console.print(table)

def _print_period_price_30(code, is_overseas):
    """기간별 시세 30일치 출력"""
    _print_period_price_common(code, is_overseas, limit=30)
    config.console.print()
