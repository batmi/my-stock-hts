# modules/backtest.py
import pandas as pd
import numpy as np
import math
import random
import time
from rich.table import Table
from rich.prompt import Prompt
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from datetime import datetime, timedelta
import config
import context
import api
import utils
import indicators
from modules import analysis
from modules import chart
import logging
from modules import market # [추가] 통합 지수 리스트 참조용
from modules import db_manager # [추가]
import json

logger = logging.getLogger(__name__)

def calculate_daily_status(row, prev_row, thresholds=None):
    """
    analysis.py의 로직을 기반으로 일별 상태 및 점수 계산
    반환값: (raw_score, sell_check_score, can_buy_state, state, reason)
    """
    price = row['close']
    ema20 = row['EMA20']
    ema60 = row['EMA60']
    ema120 = row['EMA120']
    sar = row['SAR']
    rsi = row['RSI']
    adx = row['ADX']
    cci = row['CCI']
    
    macd = row.get('MACD')
    macd_signal = row.get('MACD_Signal')
    # OBV Trend
    obv = row['OBV']
    obv_ma = row['OBV_MA']
    obv_trend = (obv > obv_ma)

    # Previous RSI for divergence check
    prev_rsi = prev_row['RSI'] if prev_row is not None else None
    w52_pos = row.get('w52_pos', 0.0)
    smart_money = row.get('smart_money', False) # [추가] 사전 병합된 스마트머니 시그널 확인
    
    # [추가] 점수 산정에 필요한 세부 지표들
    ema_5 = row.get('EMA5')
    prev_cci = prev_row.get('CCI') if prev_row is not None else None
    vol_spike = row.get('VOL_SPIKE', False)
    vol_trend = row.get('VOL_TREND', False)
    macd_hist = row.get('MACD_Hist')
    prev_macd_hist = prev_row.get('MACD_Hist') if prev_row is not None else None

    # [수정] analysis 모듈을 사용하여 로직 동기화
    is_yangbong_flag = (row['close'] > row['open'])
    # 1. 상태 분류 (위험/주의/관망/상승/매수)
    state, _, reason = analysis.classify_stock_state(
        price, ema20, ema60, ema120, sar, rsi, prev_rsi, adx, cci, obv_trend, macd, macd_signal,
        thresholds=thresholds, w52_pos=w52_pos, smart_money=smart_money,
        plus_di=row.get('PLUS_DI'), minus_di=row.get('MINUS_DI'),
        ema_5=ema_5, macd_hist=macd_hist, prev_macd_hist=prev_macd_hist, prev_cci=prev_cci, vol_spike=vol_spike, vol_trend=vol_trend,
        is_yangbong=is_yangbong_flag
    )
    
    # 2. 점수 계산
    weights = thresholds.get("WEIGHTS") if thresholds else None
    raw_score, _ = analysis.calculate_score(
        price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd, macd_signal,
        ema_5=ema_5, prev_cci=prev_cci, vol_spike=vol_spike, vol_trend=vol_trend,
        weights=weights, smart_money=smart_money, plus_di=row.get('PLUS_DI'), minus_di=row.get('MINUS_DI'),
        macd_hist=macd_hist, prev_macd_hist=prev_macd_hist,
        w52_pos=w52_pos, mom_ret=row.get('MOM_RET')  # [추가] 가격 모멘텀 팩터 (라이브와 동기화)
    )
    raw_score = round(raw_score, 1) # [Fix] 부동소수점 오차 제거 (예: 6.9999 -> 7.0)
    
    # 3. 백테스팅용 플래그 변환
    can_buy_state = (state not in ["매도", "주의"]) # 매도/주의가 아니면 매수 후보 (역매수 포함)
    sell_check_score = 0 if state == "매도" else raw_score # 매도 상태면 점수 0점 처리 (매도 유도)
    
    return raw_score, sell_check_score, can_buy_state, state, reason

def get_backtest_data(code, is_overseas, days):
    # 1. yfinance 시도 (장기간 데이터 확보 유리)
    try:
        start_dt = datetime.now() - timedelta(days=days + 400) # 지표 계산용 여유 기간 포함 (52주 윈도우 충족 위해 약 1년 워밍업)
        start_str = start_dt.strftime("%Y-%m-%d")
        
        tickers = []
        if is_overseas:
            tickers.append(code)
        else:
            tickers.append(f"{code}.KS") # 코스피
            tickers.append(f"{code}.KQ") # 코스닥
            
        for t in tickers:
            if config.SCREEN_DEBUG_LEVEL == "TRACE":
                config.console.print(f"[dim cyan][TRACE] REQ (yfinance) | Ticker: {t} | Start: {start_str}[/dim cyan]")
            
            df = api.fetch_yfinance_data(t, start=start_str)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    try: df = df.xs(t, axis=1, level=1)
                    except: pass
                
                df.columns = [c.lower() for c in df.columns]
                df = df.reset_index()
                df.rename(columns={'Date': 'date', 'Close': 'close'}, inplace=True) # 컬럼명 통일
                
                # 날짜 포맷 통일 (YYYYMMDD 문자열)
                df['date'] = df['date'].apply(lambda x: x.strftime('%Y%m%d') if isinstance(x, datetime) else str(x).replace('-', '')[:8])
                
                return df
    except Exception as e:
        pass

    # 2. KIS API 시도 (Fallback)
    return api.get_chart_data(code, is_overseas)

def _append_smart_money_signal(df, code, is_overseas):
    """과거 수급 데이터를 API로 조회하여 DataFrame에 병합하고 스마트머니 시그널을 사전 계산 (Vectorized)"""
    df['smart_money'] = False
    if is_overseas:
        return df
        
    try:
        inv_list = api.get_investor_trend(code)
        if not inv_list: 
            config.console.print("[dim yellow]※ 안내: 과거 수급 데이터를 불러올 수 없어 '스마트머니' 시그널이 시뮬레이션에 반영되지 않습니다.[/dim yellow]")
            return df
            
        inv_df = pd.DataFrame(inv_list)
        if 'stck_bsop_date' not in inv_df.columns: 
            config.console.print("[dim yellow]※ 안내: 유효한 수급 데이터가 없어 '스마트머니' 시그널이 시뮬레이션에 반영되지 않습니다.[/dim yellow]")
            return df
            
        inv_df = inv_df[['stck_bsop_date', 'frgn_ntby_qty', 'orgn_ntby_qty']].copy()
        inv_df.rename(columns={'stck_bsop_date': 'date', 'frgn_ntby_qty': 'f_net', 'orgn_ntby_qty': 'o_net'}, inplace=True)
        
        inv_df['f_net'] = pd.to_numeric(inv_df['f_net'], errors='coerce').fillna(0)
        inv_df['o_net'] = pd.to_numeric(inv_df['o_net'], errors='coerce').fillna(0)
        inv_df['date'] = inv_df['date'].astype(str)
        df['date'] = df['date'].astype(str)
        
        merged = pd.merge(df, inv_df, on='date', how='left')
        merged['f_net'] = merged['f_net'].fillna(0)
        merged['o_net'] = merged['o_net'].fillna(0)
        
        f_net = merged['f_net']
        o_net = merged['o_net']
        
        # 벡터화 연산 (df는 과거->최신 순서이므로 shift(1)은 전일 데이터)
        c1_today = (f_net > 0) & (o_net > 0)
        c1_yest = (f_net.shift(1) > 0) & (o_net.shift(1) > 0)
        
        c2_today = (f_net > 0) & (f_net.shift(1) < 0) & (f_net.shift(2) < 0)
        c2_yest = (f_net.shift(1) > 0) & (f_net.shift(2) < 0) & (f_net.shift(3) < 0)
        
        c3_today = (o_net > 0) & (o_net.shift(1) < 0) & (o_net.shift(2) < 0)
        c3_yest = (o_net.shift(1) > 0) & (o_net.shift(2) < 0) & (o_net.shift(3) < 0)
        
        merged['smart_money'] = c1_today | c1_yest | c2_today | c2_yest | c3_today | c3_yest
        merged.drop(columns=['f_net', 'o_net'], inplace=True)
        
        return merged
    except Exception as e:
        logger.error(f"스마트머니 데이터 병합 오류: {e}")
        config.console.print("[dim yellow]※ 안내: 수급 데이터 병합 중 오류가 발생하여 '스마트머니' 시그널이 시뮬레이션에 반영되지 않습니다.[/dim yellow]")
        df['smart_money'] = False
        return df

def compute_price_indicators(df):
    """가격(OHLCV) 기반 보조지표를 일괄 계산하여 df에 채운다.
    ※ smart_money 등 가격과 무관한 사전 병합 컬럼은 건드리지 않는다.
    ※ 단일 백테스트와 Monte Carlo(노이즈 주입 후 재계산)가 동일 로직을 쓰도록 공유한다.
    """
    df['EMA5'] = df['close'].ewm(span=5, adjust=False).mean()
    df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA60'] = df['close'].ewm(span=60, adjust=False).mean()
    df['EMA120'] = df['close'].ewm(span=120, adjust=False).mean()
    df['SAR'] = indicators.get_psar_full_series(df)
    df['ADX'], df['PLUS_DI'], df['MINUS_DI'] = indicators.get_adx_full_series(df)
    df['RSI'] = indicators.get_rsi_full_series(df)
    df['CCI'] = indicators.get_cci_full_series(df)
    df['OBV'] = indicators.get_obv_full_series(df)
    df['OBV_MA'] = df['OBV'].ewm(span=config.INDICATOR_PARAMS["OBV_MA_PERIOD"], adjust=False).mean()
    df['ATR'] = indicators.get_atr_full_series(df)

    macd_res = indicators.get_macd_full_series(df)
    if len(macd_res) == 3:
        df['MACD'], df['MACD_Signal'], df['MACD_Hist'] = macd_res
    else:
        df['MACD'], df['MACD_Signal'] = macd_res
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']

    df['VOL_MA20'] = df['volume'].rolling(window=20, min_periods=1).mean()
    df['VOL_MA5'] = df['volume'].rolling(window=5, min_periods=1).mean()
    df['VOL_TREND'] = df['VOL_MA5'] > df['VOL_MA20']
    df['VOL_SPIKE'] = (df['volume'] > df['VOL_MA20'] * 2.0) & (df['close'] > df['open'])

    # 52주 위치는 워밍업 구간을 포함한 전체 df 기준으로 계산
    df['roll_high_250'] = df['high'].rolling(250, min_periods=1).max()
    df['roll_low_250'] = df['low'].rolling(250, min_periods=1).min()
    df['w52_pos'] = ((df['close'] - df['roll_low_250']) / (df['roll_high_250'] - df['roll_low_250']) * 100).fillna(0)

    # [추가] 가격 모멘텀(절대 모멘텀): 라이브(calculate_score 내부 df 계산)와 동일 정의로 사전계산
    mom_lb = config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK', 126)
    df['MOM_RET'] = df['close'].pct_change(periods=mom_lb, fill_method=None) * 100
    return df

def simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score_limit, buy_rsi_limit, is_overseas,
                      stop_loss_rate=None, take_profit_rate=None, 
                      take_profit_rsi=None, sell_score=None, 
                      ts_activation_rate=None, ts_callback_rate=None, 
                      time_stop_days_limit=None,
                      use_atr_stop_limit=None, atr_stop_multiplier_limit=None, half_tp_use_limit=None,
                      weights=None,
                      execution_noise=False):
    """주어진 설정으로 백테스팅 시뮬레이션을 수행하고 결과를 반환"""
    
    # 시뮬레이션 변수
    balance = initial_capital
    # [Fix: Point 4] 분할 매수 지원을 위해 포지션 상태를 딕셔너리로 관리
    position = {'qty': 0, 'avg_price': 0, 'buy_trades': []}
    trades = []
    buy_date = None
    
    # 통계 변수
    max_score_observed = 0
    score_8_count = 0
    
    # [추가] 매수 보류 카운트
    missed_caution_count = 0
    missed_danger_count = 0
    missed_trades = [] # [추가] 매수 보류 상세 내역
    
    # 매도 설정값 로드 (Config 참조)
    stop_loss_limit = stop_loss_rate if stop_loss_rate is not None else config.SELL_STRATEGY["STOP_LOSS_RATE"]
    take_profit_limit = take_profit_rate if take_profit_rate is not None else config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    take_profit_rsi_limit = take_profit_rsi if take_profit_rsi is not None else config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    sell_score_limit = sell_score if sell_score is not None else config.SELL_STRATEGY["SELL_SCORE"]
    
    ts_activation = ts_activation_rate if ts_activation_rate is not None else config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0)
    ts_callback = ts_callback_rate if ts_callback_rate is not None else config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0)

    # [추가] 리스크 관리 설정 로드
    risk_per_trade = getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0)
    
    # [추가] ATR 기반 손절 설정 로드
    use_atr_stop = use_atr_stop_limit if use_atr_stop_limit is not None else config.SELL_STRATEGY.get("USE_ATR_STOP", True)
    atr_mult = atr_stop_multiplier_limit if atr_stop_multiplier_limit is not None else config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    use_vol_target = getattr(config, 'USE_VOLATILITY_TARGETING', True)

    peak_asset = initial_capital
    mdd = 0.0
    win_trades = 0
    loss_trades = 0
    gross_profit = 0
    gross_loss = 0
    cum_profit = 0
    daily_assets = []
    buy_reason_str = "" # [추가] 역추세/일반 매수 구분 추적용
    
    ts_highest_price = 0
    half_tp_executed = False # [추가] 백테스트용 반익절 추적 변수
    last_valid_price = 0 # [추가] 마지막 유효 가격 추적용
    
    # [추가] 52주 고점/저점 및 위치 사전 계산 (벡터화 처리)
    # [Fix] 호출부(run_backtest)에서 워밍업 구간 포함 전체 df로 w52_pos를 미리 계산해 두면 그대로 사용한다.
    #       (sim_df만으로 rolling(250)을 하면 분석 시작 시점의 52주 윈도우가 비어 w52_pos가 왜곡됨)
    if 'w52_pos' not in sim_df.columns:
        sim_df['roll_high_250'] = sim_df['high'].rolling(250, min_periods=1).max()
        sim_df['roll_low_250'] = sim_df['low'].rolling(250, min_periods=1).min()
        sim_df['w52_pos'] = (sim_df['close'] - sim_df['roll_low_250']) / (sim_df['roll_high_250'] - sim_df['roll_low_250']) * 100
        sim_df['w52_pos'] = sim_df['w52_pos'].fillna(0)

    # [추가] 가격 모멘텀 컬럼 사전계산 (전체 df 사전계산을 거치지 않은 경로 대비 안전장치)
    if 'MOM_RET' not in sim_df.columns:
        mom_lb = config.INDICATOR_PARAMS.get('MOMENTUM_LOOKBACK', 126)
        sim_df['MOM_RET'] = sim_df['close'].pct_change(periods=mom_lb, fill_method=None) * 100
    
    # [추가] 시뮬레이션용 임계값 설정 (상태 분류 동기화)
    # ※ 백테스팅은 사용자가 설정한 기준 점수 검증이 목적이므로, 
    #    적응형 임계값(시장 국면 보정)은 적용하지 않고 입력된 값을 그대로 사용합니다.
    current_thresholds = {
        "BUY_SCORE": buy_score_limit,
        "BUY_RSI_MAX": buy_rsi_limit,
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"], # 기본값 유지
        "WEIGHTS": weights # [추가] 가중치 전달
    }

    # [최적화 1] 날짜 문자열 파싱 오버헤드 제거 (루프 전 벡터화 사전 변환)
    parsed_dates = pd.to_datetime(sim_df['date'], format='%Y%m%d', errors='coerce').tolist()
    buy_date_dt = None

    # [최적화 2] Pandas DataFrame 순회(iloc)의 막대한 오버헤드를 제거하기 위해
    # 데이터를 List of Dicts로 변환하여 순수 Python 딕셔너리로 초고속 순회합니다.
    sim_records = sim_df.to_dict('records')
    prev_row = prev_row_init.to_dict() if prev_row_init is not None and isinstance(prev_row_init, pd.Series) else prev_row_init

    for i in range(len(sim_records)):
        row = sim_records[i]
        date = row['date']
        current_date_dt = parsed_dates[i]
        price = row['close']
        high_price = row['high']
        
        # [추가] 결측치(NaN) 또는 유효하지 않은 가격 데이터 방어 로직
        if price is None or math.isnan(price) or price <= 0:
            prev_row = row
            continue

        last_valid_price = price # [추가] 정상적인 가격일 경우에만 업데이트

        # [추가] 체결 노이즈: 1% 확률로 매매 기회 놓침 (체결 누락/지연 시뮬레이션)
        if execution_noise and random.random() < 0.01:
            prev_row = row
            continue

        current_asset = balance + (position['qty'] * price)
        daily_assets.append(current_asset)
        if current_asset > peak_asset: peak_asset = current_asset
        if peak_asset > 0:
            dd = (current_asset - peak_asset) / peak_asset * 100
            if dd < mdd: mdd = dd
        
        # 상태 및 점수 계산
        raw_score, sell_check_score, can_buy_state, state, state_reason = calculate_daily_status(row, prev_row, thresholds=current_thresholds)
        
        if raw_score > max_score_observed: max_score_observed = raw_score
        if raw_score >= buy_score_limit: score_8_count += 1

        # [Fix] 슈퍼 모멘텀 판정을 매도 로직 이전으로 이동
        #  (매도 측 'RSI과열' 기준과 매수 측 'RSI 상한 완화'가 동일 변수를 참조하므로 루프 상단에서 1회 계산)
        use_super = current_thresholds.get("SUPER_MOMENTUM_USE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_USE", True))
        super_score = current_thresholds.get("SUPER_MOMENTUM_SCORE", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_SCORE", 8.5))
        super_w52 = current_thresholds.get("SUPER_MOMENTUM_W52_POS", config.ANALYSIS_THRESHOLDS.get("SUPER_MOMENTUM_W52_POS", 90.0))
        is_super = use_super and raw_score >= super_score and row.get('w52_pos', 0) >= super_w52

        # 매매 로직 (매도 우선)
        if position['qty'] > 0:
            # [Fix: Point 4] 가중 평균 ATR 손절률 계산
            sl_rate_to_use = stop_loss_limit
            if use_atr_stop:
                total_qty_trade = 0
                weighted_sl_sum = 0
                for trade in position['buy_trades']:
                    qty_trade = trade.get('qty', 0)
                    sl_rate_trade = trade.get('atr_sl_rate', 0.0)
                    if qty_trade > 0 and sl_rate_trade != 0.0:
                        total_qty_trade += qty_trade
                        weighted_sl_sum += qty_trade * sl_rate_trade
                
                if total_qty_trade > 0:
                    avg_sl_rate = weighted_sl_sum / total_qty_trade
                    if avg_sl_rate != 0.0:
                        sl_rate_to_use = avg_sl_rate
            
            loss_rate = (price - position['avg_price']) / position['avg_price'] * 100
            if high_price > ts_highest_price: ts_highest_price = high_price
            
            max_profit_rate = ((ts_highest_price - position['avg_price']) / position['avg_price']) * 100 if position['avg_price'] > 0 else 0
            
            # [추가] 본전 청산(BEP) 로직 적용
            bep_activation = config.SELL_STRATEGY.get("BREAK_EVEN_PROFIT_RATE", 7.0)
            bep_stop = config.SELL_STRATEGY.get("BREAK_EVEN_STOP_RATE", 0.5)
            is_bep_applied = False
            if max_profit_rate >= bep_activation:
                if sl_rate_to_use < bep_stop:
                    sl_rate_to_use = bep_stop
                    is_bep_applied = True
                    
            # [추가] 현재 보유 기간(일수) 계산
            current_holding_days = (current_date_dt - buy_date_dt).days if buy_date_dt and pd.notna(current_date_dt) else 0

            sell_signal = False
            reason = ""
            sell_ratio = 1.0 # 기본 전량 매도
            
            use_half_tp = half_tp_use_limit if half_tp_use_limit is not None else config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", False)
            half_tp_limit = config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_RATE", take_profit_limit / 2.0)
            
            use_time_stop = config.SELL_STRATEGY.get("TIME_STOP_USE", True)
            time_stop_days = time_stop_days_limit if time_stop_days_limit is not None else config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
            time_stop_min_profit = config.SELL_STRATEGY.get("TIME_STOP_MIN_PROFIT_RATE", 3.0)
            
            if time_stop_days <= 0:
                use_time_stop = False

            mr_grace_loss_limit = config.SELL_STRATEGY.get("MR_GRACE_LOSS_RATE", -7.0)

            if take_profit_limit > 0 and loss_rate >= take_profit_limit: sell_signal = True; reason = "익절"
            elif use_half_tp and take_profit_limit > 0 and not half_tp_executed and loss_rate >= half_tp_limit: # [수정] half_tp_limit 사용
                sell_signal = True; reason = "반익절"; sell_ratio = 0.5
            elif sl_rate_to_use != 0 and loss_rate <= sl_rate_to_use: # [수정] 가중 평균 손절률 사용
                sell_signal = True
                if is_bep_applied:
                    reason = "본전청산"
                elif use_atr_stop and sl_rate_to_use != stop_loss_limit:
                    reason = "ATR손절" # [수정] ATR 손절 사유
                else:
                    reason = "손절"
            elif use_time_stop and current_holding_days >= time_stop_days and loss_rate < time_stop_min_profit:
                if state in ["매수", "강매수", "역매수", "상승"]:
                    pass # 상승 또는 매수 신호가 유지 중이면 시간 청산 유예
                else:
                    sell_signal = True; reason = "시간청산"
            elif ts_highest_price > 0:
                if max_profit_rate >= ts_activation:
                    drop_rate = ((ts_highest_price - price) / ts_highest_price) * 100
                    
                    actual_ts_callback = ts_callback
                    atr_val = row.get('ATR', 0)
                    if use_atr_stop and atr_val > 0:
                        dynamic_callback = (atr_val * atr_mult / ts_highest_price) * 100
                        # [추가] 트레일링 스탑 하/상한선 방어 로직 동기화
                        max_allowed_callback = max(ts_callback, max_profit_rate * 0.5)
                        actual_ts_callback = min(max(ts_callback, dynamic_callback), max_allowed_callback)
                        
                    if drop_rate >= actual_ts_callback: sell_signal = True; reason = "트레일링스탑"
                    
            # [추가] 방어적 반매도 로직 동기화
            defensive_half_tp = config.SELL_STRATEGY.get("DEFENSIVE_HALF_SELL_USE", True)
            if not sell_signal and defensive_half_tp and not half_tp_executed:
                psar_val = row.get('SAR')
                ema5_val = row.get('EMA5')
                if psar_val is not None and ema5_val is not None:
                    if loss_rate >= time_stop_min_profit and price < psar_val and price < ema5_val:
                        sell_signal = True; reason = "방어적 반매도"; sell_ratio = 0.5

            # [추가] 슈퍼 모멘텀 기반 동적 RSI 매도 로직 반영
            actual_tp_rsi = take_profit_rsi_limit
            if is_super:
                actual_tp_rsi = config.SELL_STRATEGY.get("SUPER_TAKE_PROFIT_RSI", 85.0)
            
            if not sell_signal and row['RSI'] > actual_tp_rsi: sell_signal = True; reason = "RSI과열"
            if not sell_signal and sell_check_score < sell_score_limit:
                # [추가] 역추세 매수 종목은 지정된 유예 기간(TIME_STOP_DAYS)간 점수 하락으로 팔지 않고 기회를 줌
                if buy_reason_str == "역매수" and current_holding_days <= time_stop_days and loss_rate > mr_grace_loss_limit:
                    pass
                else:
                    sell_signal = True; reason = "점수하락"
            
            if sell_signal:
                # [수정] 슬리피지 비율 적용 및 호가 정렬 (노이즈 포함)
                slippage_mult = 1.0
                if execution_noise:
                    # 슬리피지가 0.5배 ~ 1.5배 사이로 변동
                    slippage_mult = random.uniform(0.5, 1.5)

                raw_sell_price = price * (1 - (config.SLIPPAGE_RATE * slippage_mult))
                sell_price = utils.adjust_to_tick(raw_sell_price, is_overseas)
                if sell_price <= 0: sell_price = utils.adjust_to_tick(price, is_overseas)

                # [수정] 포지션 기반으로 수량 및 수익 계산
                sell_qty = max(1, int(position['qty'] * sell_ratio)) if sell_ratio < 1.0 else position['qty']
                sell_amt = sell_qty * sell_price
                fee = sell_amt * 0.0023
                if not is_overseas: fee = int(fee)
                sell_amt -= fee

                profit = sell_amt - (sell_qty * position['avg_price'])
                profit_rate = (profit / (sell_qty * position['avg_price'])) * 100 if (sell_qty * position['avg_price']) != 0 else 0
                
                if profit > 0: 
                    win_trades += 1
                    gross_profit += profit
                else: 
                    loss_trades += 1
                    gross_loss += abs(profit)
                
                cum_profit += profit
                
                holding_days = (current_date_dt - buy_date_dt).days if buy_date_dt and pd.notna(current_date_dt) else 0
                
                balance += sell_amt
                sold_qty = sell_qty
                position['qty'] -= sell_qty
                
                if position['qty'] == 0:
                    position = {'qty': 0, 'avg_price': 0, 'buy_trades': []} # 포지션 초기화
                    buy_date = None
                    buy_date_dt = None
                    ts_highest_price = 0
                    half_tp_executed = False
                    buy_reason_str = ""
                else:
                    half_tp_executed = True
                
                if reason == "점수하락" and sell_check_score == 0 and raw_score > 0: reason = state_reason
                    
                trades.append({
                    "date": date, "type": f"매도({reason})", "price": sell_price, "qty": sold_qty, "balance": balance, 
                    "profit": profit_rate, "profit_amt": profit, "days": holding_days, 
                    "score": sell_check_score, "rsi": row['RSI'], "adx": row['ADX'], "cci": row['CCI'], "plus_di": row.get('PLUS_DI'), "minus_di": row.get('MINUS_DI'), "obv": row['OBV'], "obv_trend": (row['OBV'] > row['OBV_MA']),
                    "cum_profit": cum_profit
                })

        # [매수]
        # [수정] 매수 조건 체크 (역추세 허용)
        is_score_ok = raw_score >= buy_score_limit
        is_mr_buy = (state == "역매수")
        
        # 슈퍼 모멘텀 시 RSI 상한 완화 (use_super/is_super는 매도 로직 이전에 계산됨)
        actual_buy_rsi = current_thresholds.get("SUPER_BUY_RSI_MAX", config.ANALYSIS_THRESHOLDS.get("SUPER_BUY_RSI_MAX", 75.0)) if is_super else buy_rsi_limit
        is_rsi_ok = row['RSI'] < actual_buy_rsi
        
        # [수정] 실제 자동매매 시스템과 동일하게 이미 보유 중인 경우 추가 매수 금지
        if position['qty'] == 0 and (is_score_ok or is_mr_buy) and is_rsi_ok and can_buy_state:
            # [수정] 슬리피지 비율 적용 및 호가 정렬 (노이즈 포함)
            slippage_mult = 1.0
            if execution_noise:
                slippage_mult = random.uniform(0.5, 1.5)

            raw_buy_price = price * (1 + (config.SLIPPAGE_RATE * slippage_mult))
            buy_price = utils.adjust_to_tick(raw_buy_price, is_overseas)

            # [Fix: Point 4] ATR 기반 동적 손절률 계산
            atr_sl_rate = 0.0
            atr_val = row.get('ATR', 0)
            if use_atr_stop and atr_val > 0:
                stop_distance = atr_val * atr_mult
                atr_sl_rate = -((stop_distance / buy_price) * 100)
                
                # [추가] ATR 손절 최대 한도 적용
                max_atr_sl = config.SELL_STRATEGY.get("MAX_ATR_STOP_LOSS_RATE", -15.0)
                if max_atr_sl != 0 and atr_sl_rate < max_atr_sl:
                    atr_sl_rate = max_atr_sl
            
            # [수정] 리스크 기반 포지션 사이징 적용 (백테스팅)
            invest_amt = balance
            sl_for_risk_calc = atr_sl_rate if use_atr_stop else stop_loss_limit
            if risk_per_trade > 0 and sl_for_risk_calc and abs(sl_for_risk_calc) > 0:
                max_loss_amt = balance * (risk_per_trade / 100.0)
                sl_ratio = abs(sl_for_risk_calc) / 100.0
                risk_based_amt = int(max_loss_amt / sl_ratio)
                invest_amt = min(balance, risk_based_amt)
                
            # [추가] 변동성 타겟팅 스케일링 (간이 구현)
            if use_vol_target and atr_val > 0:
                daily_vol = atr_val / buy_price
                annual_vol = daily_vol * np.sqrt(252)
                
                target_vol = getattr(config, 'TARGET_VOLATILITY', 0.30)
                scale_max = getattr(config, 'VOLATILITY_SCALING_MAX', 2.0)
                scale_min = getattr(config, 'VOLATILITY_SCALING_MIN', 0.5)
                
                if annual_vol > 0:
                    scale = target_vol / annual_vol
                    scale = max(scale_min, min(scale_max, scale))
                    invest_amt = int(invest_amt * scale)

            buy_qty = int(invest_amt / buy_price)
            
            if buy_qty > 0:
                cost = buy_qty * buy_price
                balance -= cost
                
                # [Fix: Point 4] 포지션 정보 업데이트
                if position['qty'] == 0: # 신규 진입
                    buy_date = date
                    buy_date_dt = current_date_dt
                    ts_highest_price = buy_price
                    buy_reason_str = "역매수" if is_mr_buy else "일반"
                
                total_cost = (position['qty'] * position['avg_price']) + cost
                position['qty'] += buy_qty
                position['avg_price'] = total_cost / position['qty']
                position['buy_trades'].append({'qty': buy_qty, 'price': buy_price, 'atr_sl_rate': atr_sl_rate})

                trades.append({
                    "date": date, "type": f"매수({buy_reason_str})", "price": buy_price, "qty": buy_qty, "balance": balance, 
                    "profit": 0, "profit_amt": 0, "days": 0, 
                    "score": raw_score, "rsi": row['RSI'], "adx": row['ADX'], "cci": row['CCI'], "plus_di": row.get('PLUS_DI'), "minus_di": row.get('MINUS_DI'), "obv": row['OBV'], "obv_trend": (row['OBV'] > row['OBV_MA']),
                    "cum_profit": cum_profit
                })
        elif position['qty'] == 0: # 매수 조건 미충족 시 (이미 보유 중인 상태는 누락으로 기록하지 않음)
            if raw_score >= buy_score_limit: # 점수는 충족했으나 다른 조건(RSI 등) 미충족
                if state == "주의": missed_caution_count += 1
                elif state == "매도": missed_danger_count += 1
                
                missed_reason = state_reason
                if not can_buy_state: missed_reason = f"{state}: {state_reason}"
                elif not is_rsi_ok: missed_reason = f"RSI 과열 ({row['RSI']:.1f} >= {actual_buy_rsi})"

                missed_trades.append({
                    "date": date, "score": raw_score, "state": state, "reason": missed_reason,
                    "rsi": row['RSI'], "adx": row['ADX'], "cci": row['CCI'], "plus_di": row.get('PLUS_DI'), "minus_di": row.get('MINUS_DI'), "price": price
                })
        
        prev_row = row

    # [수정] 마지막 행이 NaN일 수 있으므로 기록해둔 마지막 유효 가격(last_valid_price) 사용
    final_asset = balance + (position['qty'] * last_valid_price)
    total_return = (final_asset - initial_capital) / initial_capital * 100 if initial_capital > 0 else 0.0
    
    return {
        "trades": trades,
        "final_asset": final_asset,
        "total_return": total_return,
        "mdd": mdd,
        "win_trades": win_trades,
        "loss_trades": loss_trades,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "daily_assets": daily_assets,
        "max_score_observed": max_score_observed,
        "score_8_count": score_8_count,
        "missed_caution_count": missed_caution_count,
        "missed_danger_count": missed_danger_count,
        "missed_trades": missed_trades
    }

def run_monte_carlo_simulation(full_df, start_idx, initial_capital, buy_score, buy_rsi, is_overseas,
                               stop_loss, take_profit, take_profit_rsi, sell_score, ts_activation, ts_callback, time_stop_days,
                               use_atr_stop, atr_mult, half_tp_use,
                               weights=None, name="Unknown", code="Unknown", days=0):
    """Monte Carlo 시뮬레이션 실행 (1,000회 반복)

    각 시행마다 (워밍업 포함) 전체 가격 시계열에 노이즈를 주입한 뒤 보조지표를 재계산하므로,
    체결 노이즈뿐 아니라 매매 시그널(점수/상태) 자체의 견고성까지 검증한다.
    """
    config.console.print("\n[bold magenta]━━━ Monte Carlo Simulation (1,000 runs) ━━━[/]")
    config.console.print("[dim]가격 데이터 노이즈(±1%) 주입 후 지표를 재계산하고, 체결 노이즈(슬리피지 변동, 체결 누락)를 적용하여 전략 시그널의 견고성을 검증합니다.[/dim]\n")

    # 날짜/표시는 노이즈와 무관하므로 깨끗한 원본 슬라이스를 참조용으로 보관
    sim_df = full_df.iloc[start_idx:]
    
    # 결과 저장용 리스트
    returns = []
    mdds = []
    final_assets = []
    win_rates = []
    profit_factors = []
    sharpe_ratios = []
    trade_counts = []
    
    # [추가] 상세 통계 수집용
    avg_trade_profits = []
    avg_trade_losses = []
    avg_holding_days = []
    
    # [추가] 추가 정보 수집
    win_counts = []
    loss_counts = []
    gross_profits = []
    gross_losses = []
    missed_cautions = []
    missed_dangers = []
    max_scores = []
    score_8_counts = []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task("[cyan]시뮬레이션 진행 중...[/cyan]", total=1000)
        
        for _ in range(1000):
            # 1. (워밍업 포함) 전체 시계열에 노이즈 주입 후 지표 재계산
            noisy_full = full_df.copy()
            # 정규분포 노이즈 (평균 0, 표준편차 1%) — 동일 봉의 OHLC는 같은 비율로 이동시켜 봉 구조 보존
            noise = np.random.normal(0, 0.01, len(noisy_full))
            for col in ['close', 'open', 'high', 'low']:
                if col in noisy_full.columns:
                    noisy_full[col] = noisy_full[col] * (1 + noise)

            # 노이즈가 반영된 가격으로 보조지표 재계산 후 분석 구간만 슬라이스
            noisy_full = compute_price_indicators(noisy_full)
            noisy_df = noisy_full.iloc[start_idx:].copy()
            prev_row_init = noisy_full.iloc[start_idx - 1] if start_idx > 0 else None

            # 2. 시뮬레이션 실행 (체결 노이즈 ON)
            res = simulate_strategy(noisy_df, prev_row_init, initial_capital, buy_score, buy_rsi, is_overseas,
                                    stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                                    take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                    ts_activation_rate=ts_activation, ts_callback_rate=ts_callback,
                                    time_stop_days_limit=time_stop_days,
                                    use_atr_stop_limit=use_atr_stop, atr_stop_multiplier_limit=atr_mult, half_tp_use_limit=half_tp_use,
                                    execution_noise=True,
                                    weights=weights)
            
            # 결과 수집
            returns.append(res['total_return'])
            mdds.append(res['mdd'])
            final_assets.append(res['final_asset'])
            
            total_trades = len(res['trades'])
            trade_counts.append(total_trades)
            
            # [추가] 추가 정보 수집
            win_counts.append(res['win_trades'])
            loss_counts.append(res['loss_trades'])
            gross_profits.append(res['gross_profit'])
            gross_losses.append(res['gross_loss'])
            missed_cautions.append(res.get('missed_caution_count', 0))
            missed_dangers.append(res.get('missed_danger_count', 0))
            max_scores.append(res.get('max_score_observed', 0))
            score_8_counts.append(res.get('score_8_count', 0))
            
            # [추가] 상세 통계 수집
            trades = res['trades']
            actual_sell_trades = [t for t in trades if t['type'].startswith("매도")]
            if actual_sell_trades:
                profits = [t['profit'] for t in actual_sell_trades if t['profit'] > 0]
                losses = [t['profit'] for t in actual_sell_trades if t['profit'] <= 0]
                if profits: avg_trade_profits.append(sum(profits) / len(profits))
                if losses: avg_trade_losses.append(sum(losses) / len(losses))
                holding_days = [t['days'] for t in actual_sell_trades]
                if holding_days: avg_holding_days.append(sum(holding_days) / len(holding_days))
            
            sell_trades = res['win_trades'] + res['loss_trades']
            wr = (res['win_trades'] / sell_trades * 100) if sell_trades > 0 else 0.0
            win_rates.append(wr)
            
            pf = (res['gross_profit'] / res['gross_loss']) if res['gross_loss'] > 0 else (99.9 if res['gross_profit'] > 0 else 0.0)
            profit_factors.append(pf)
            
            # Sharpe Ratio 계산
            daily_assets = res['daily_assets']
            sr = 0.0
            if len(daily_assets) > 1:
                s = pd.Series(daily_assets).pct_change().dropna()
                if not s.empty and s.std() > 0:
                    sr = (s.mean() / s.std()) * np.sqrt(252)
            sharpe_ratios.append(sr)
            
            progress.advance(task)
            
    # 통계 계산 (평균)
    avg_return = np.nanmean(returns) if returns else 0.0
    avg_mdd = np.nanmean(mdds) if mdds else 0.0
    avg_asset = np.nanmean(final_assets) if final_assets else 0.0
    avg_wr = np.nanmean(win_rates) if win_rates else 0.0
    avg_pf = np.nanmean(profit_factors) if profit_factors else 0.0
    avg_sr = np.nanmean(sharpe_ratios) if sharpe_ratios else 0.0
    avg_trades = np.nanmean(trade_counts) if trade_counts else 0.0
    
    # [추가] 상세 통계 평균
    avg_trade_profit_val = np.nanmean(avg_trade_profits) if avg_trade_profits else 0.0
    avg_trade_loss_val = np.nanmean(avg_trade_losses) if avg_trade_losses else 0.0
    avg_holding_val = np.nanmean(avg_holding_days) if avg_holding_days else 0.0
    
    # [추가] 추가 통계 평균
    avg_win_count = np.nanmean(win_counts) if win_counts else 0.0
    avg_loss_count = np.nanmean(loss_counts) if loss_counts else 0.0
    avg_gross_profit = np.nanmean(gross_profits) if gross_profits else 0.0
    avg_gross_loss = np.nanmean(gross_losses) if gross_losses else 0.0
    avg_missed_caution = np.nanmean(missed_cautions) if missed_cautions else 0.0
    avg_missed_danger = np.nanmean(missed_dangers) if missed_dangers else 0.0
    avg_max_score = np.nanmean(max_scores) if max_scores else 0.0
    avg_score_8_count = np.nanmean(score_8_counts) if score_8_counts else 0.0
    
    # 포맷팅 헬퍼
    def fmt_money(val):
        try:
            if pd.isna(val) or math.isnan(float(val)): return "-"
            if is_overseas: return f"${float(val):,.2f}"
            return f"{int(float(val)):,}원"
        except:
            return "-"

    # [추가] 테이블 위 공백 라인
    config.console.print()

    # 결과 리포트 테이블 출력 (단일 백테스팅과 유사한 형식)
    summary_table = Table(title=f"Monte Carlo 시뮬레이션 결과: {name}", box=box.HORIZONTALS, show_header=False, border_style="dim")
    summary_table.add_column("항목", style="cyan", justify="left")
    summary_table.add_column("값 (평균)", justify="left")
    
    start_date_str = str(sim_df.iloc[0]['date'])
    end_date_str = str(sim_df.iloc[-1]['date'])
    if len(start_date_str) == 8: start_date_str = f"{start_date_str[:4]}-{start_date_str[4:6]}-{start_date_str[6:]}"
    if len(end_date_str) == 8: end_date_str = f"{end_date_str[:4]}-{end_date_str[4:6]}-{end_date_str[6:]}"
    
    summary_table.add_row("기간", f"{days}일간 ({start_date_str} ~ {end_date_str})")
    summary_table.add_row("초기 자본금", fmt_money(initial_capital))
    summary_table.add_row("최종 평가액 (평균)", fmt_money(avg_asset))
    
    color = "red" if avg_return > 0 else "blue"
    summary_table.add_row("누적 수익률 (평균)", f"[{color}]{avg_return:+.2f}%[/]")
    
    summary_table.add_section()
    
    # [수정] 매매 횟수 상세 표시
    summary_table.add_row("총 매매 횟수 (평균)", f"{avg_trades:.1f}건 (익절 {avg_win_count:.1f} / 손절 {avg_loss_count:.1f})")
    
    # [추가] 매수 보류 표시
    if avg_missed_caution > 0 or avg_missed_danger > 0:
        summary_table.add_row("매수 보류 (상태)", f"[yellow]주의 {avg_missed_caution:.1f}회[/] / [blue]매도 {avg_missed_danger:.1f}회[/] (점수 충족했으나 진입 불가)")

    summary_table.add_row("승률 (Win Rate)", f"{avg_wr:.1f}% ({avg_win_count:.1f}승 {avg_loss_count:.1f}패)")
    
    # [추가] 상세 분석 정보 출력
    summary_table.add_row("평균 수익률 (건당)", f"[red]{avg_trade_profit_val:+.2f}%[/]")
    summary_table.add_row("평균 손실률 (건당)", f"[blue]{avg_trade_loss_val:+.2f}%[/]")
    
    structure_msg = "-"
    if avg_trade_loss_val == 0:
        structure_msg = "[green]무손실 (완벽한 방어)[/]"
    else:
        pl_ratio = abs(avg_trade_profit_val / avg_trade_loss_val)
        be_win_rate = (1 / (pl_ratio + 1)) * 100
        if pl_ratio >= 2.0:
            structure_msg = f"[green]매우 우수 (승률 {be_win_rate:.0f}%만 넘으면 수익)[/]"
        elif pl_ratio >= 1.5:
            structure_msg = f"[green]양호 (승률 {be_win_rate:.0f}%만 넘으면 수익)[/]"
        elif pl_ratio >= 1.0:
            structure_msg = f"[yellow]보통 (승률 {be_win_rate:.0f}% 이상 필요)[/]"
        else:
            structure_msg = f"[red]불리함 (승률 {be_win_rate:.0f}% 이상 필요)[/]"
            
    summary_table.add_row("손익 구조 분석", structure_msg)
    summary_table.add_row("보유 기간", f"평균 {avg_holding_val:.1f}일")

    # 손익비 (Profit Factor) 상세 표시
    pf_str = "Inf"
    pf_desc = ""
    if avg_pf >= 2.0: pf_color = "red"; pf_desc = " (매우 훌륭)"
    elif avg_pf >= 1.5: pf_color = "orange3"; pf_desc = " (우수)"
    elif avg_pf >= 1.0: pf_color = "green"; pf_desc = " (평범)"
    else: pf_color = "blue"; pf_desc = " (손실)"
    pf_str = f"[{pf_color}]{avg_pf:.2f}[/]"
    
    summary_table.add_row("손익비 (Profit Factor)", f"{pf_str}{pf_desc} (총 이익 [red]+{fmt_money(avg_gross_profit)}[/] / 총 손실 [blue]-{fmt_money(avg_gross_loss)}[/])")
    
    # 샤프 지수 상세 표시
    sharpe_desc = ""
    if avg_sr >= 1.0: sr_color = "red"; sharpe_desc = " (매우 우수)"
    elif avg_sr >= 0.5: sr_color = "green"; sharpe_desc = " (양호)"
    else: sr_color = "blue"; sharpe_desc = " (미흡)"
    summary_table.add_row("위험 대비 수익 (샤프지수)", f"[{sr_color}]{avg_sr:.2f}[/]{sharpe_desc}")
    
    # MDD 상세 표시
    mdd_color = "red"; mdd_desc = " (위험)"
    if avg_mdd >= -10: mdd_color = "green"; mdd_desc = " (안정)"
    elif avg_mdd >= -20: mdd_color = "yellow"; mdd_desc = " (보통)"
    elif avg_mdd >= -30: mdd_color = "orange3"; mdd_desc = " (주의)"
    summary_table.add_row("평균 최대 낙폭 (Average MDD)", f"[{mdd_color}]{avg_mdd:.2f}%[/]{mdd_desc}")
    
    config.console.print(summary_table)
    
    # [추가] 매매가 없을 경우 진단 정보 출력
    if avg_trades == 0:
        config.console.print("\n[yellow]※ 매매가 발생하지 않았습니다. (조건 미충족)[/yellow]")
        config.console.print(f"  - 기간 내 최고 점수(평균): {avg_max_score:.1f}점 (매수 기준: {buy_score}점)")
        config.console.print(f"  - {buy_score}점 이상 도달 횟수(평균): {avg_score_8_count:.1f}회")
        config.console.print(f"  [안내] 현재 설정된 매수 조건({buy_score}점 이상 & RSI<{buy_rsi})이 엄격하여 진입 기회가 없었습니다.")
        config.console.print("  [Tip] 매수 조건을 완화하거나 분석 기간을 늘려보세요.")

    # [추가] 공백 라인
    config.console.print()

    # 추가 통계 (분포 정보)
    # [수정] 테이블 폭을 80으로 고정하여 상단 요약 테이블과 시각적 균형을 맞추고 깔끔하게 출력
    risk_table = Table(title="리스크 분석 (분포)", box=box.HORIZONTALS, header_style="dim", border_style="dim", width=80)
    risk_table.add_column("항목", style="cyan", justify="left")
    risk_table.add_column("값", justify="right")
    
    min_ret = np.nanmin(returns) if returns else 0.0
    min_color = "[red]" if min_ret > 0 else ("[blue]" if min_ret < 0 else "[white]")
    var_95 = np.nanpercentile(returns, 5) if returns else 0.0
    var_color = "[red]" if var_95 > 0 else ("[blue]" if var_95 < 0 else "[white]")

    # [추가] 고급 리스크 지표 계산
    prob_profit = len([r for r in returns if r >= 0]) / len(returns) * 100 if returns else 0.0
    median_ret = np.nanmedian(returns) if returns else 0.0
    median_color = "[red]" if median_ret > 0 else ("[blue]" if median_ret < 0 else "[white]")
    
    tail_returns = [r for r in returns if r <= var_95]
    cvar_95 = np.nanmean(tail_returns) if tail_returns else 0.0
    cvar_color = "[red]" if cvar_95 > 0 else ("[blue]" if cvar_95 < 0 else "[white]")
    
    worst_mdd = np.nanmin(mdds) if mdds else 0.0
    worst_mdd_color = "red"
    if worst_mdd >= -10: worst_mdd_color = "green"
    elif worst_mdd >= -20: worst_mdd_color = "yellow"
    elif worst_mdd >= -30: worst_mdd_color = "orange3"

    risk_table.add_row("수익 발생 확률", f"{prob_profit:.1f}%")
    risk_table.add_row("가장 흔한 수익률 (중앙값)", f"{median_color}{median_ret:+.2f}%[/]")
    risk_table.add_row("수익률 변동폭 (표준편차)", f"{np.std(returns):.2f}%")
    risk_table.add_row("가장 운이 나쁠 때 (최저)", f"{min_color}{min_ret:+.2f}%[/]")
    risk_table.add_row("하위 5% 수익률 마지노선", f"[dim](VaR 95%)[/dim] {var_color}{var_95:+.2f}%[/]")
    risk_table.add_row("최악의 하위 5% 평균 수익", f"[dim](CVaR)[/dim] {cvar_color}{cvar_95:+.2f}%[/]")
    risk_table.add_row("최악의 낙폭 (Worst MDD)", f"[{worst_mdd_color}]{worst_mdd:.2f}%[/]")
    config.console.print(risk_table)
    
    # [추가] 텍스트 히스토그램 및 이미지 차트 생성
    hist, bin_edges = np.histogram(returns, bins=10)
    max_count = max(hist)
    
    config.console.print("\n[bold]수익률 분포 (Text Histogram)[/bold]")
    for i in range(len(hist)):
        count = hist[i]
        if count > 0:
            bar_len = int((count / max_count) * 50)
            bar = "█" * bar_len
            range_str = f"{bin_edges[i]:>6.1f}% ~ {bin_edges[i+1]:>6.1f}%"
            config.console.print(f"{range_str} : [cyan]{bar}[/] ({count})")
            
    # [추가] AI 백테스팅 진단
    config.console.print()
    if Prompt.ask("🤖 AI 백테스팅 성과 진단을 수행하시겠습니까?", choices=["y", "n"], default="n") == 'y':
        from modules import theme_analysis
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.padding import Padding
        
        param_info = f"매수 {buy_score}점/RSI{buy_rsi}, 매도 {sell_score}점, 익절 +{take_profit}%/RSI{take_profit_rsi}, 손절 {stop_loss}%, 트레일링 +{ts_activation}%/-{ts_callback}%, 시간청산 {time_stop_days}일"
        if use_atr_stop: param_info += f", ATR손절 x{atr_mult}"
        
        backtest_info = f"""
        [시뮬레이션 요약]
        - 종목: {name} ({code})
        - 기간: {days}일 ({start_date_str} ~ {end_date_str})
        - 적용 파라미터: {param_info}
        - 누적 수익률 (평균): {avg_return:+.2f}%
        - 승률: {avg_wr:.1f}%
        - 평균 수익률 (건당): {avg_trade_profit_val:+.2f}%
        - 평균 손실률 (건당): {avg_trade_loss_val:+.2f}%
        - 손익비 (Profit Factor): {avg_pf:.2f}
        - 위험 대비 수익 (샤프지수): {avg_sr:.2f}
        - 평균 최대 낙폭 (Average MDD): {avg_mdd:.2f}%
        - 수익 발생 확률: {prob_profit:.1f}%
        - 최악의 낙폭 (Worst MDD): {worst_mdd:.2f}%
        - 최악의 하위 5% 평균 수익 (CVaR): {cvar_95:+.2f}%
        """
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task(f"[cyan]Google Gemini가 몬테카를로 백테스팅 결과를 심층 분석 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
            answer = theme_analysis.evaluate_backtest_with_gemini(code, name, backtest_info, mode='monte_carlo')
            
        if answer:
            if answer.startswith("⚠️"):
                config.console.print(f"\n{answer}")
            else:
                md = Markdown(answer)
                panel = Panel(md, title=f"🤖 AI 몬테카를로 백테스팅 성과 진단: {name}({code})", border_style="cyan", padding=(1, 2), width=120)
                config.console.print()
                config.console.print(Padding(panel, (0, 4)))
        else:
            config.console.print("[red]진단 결과를 생성하지 못했습니다.[/red]")


def run_walk_forward(full_df, start_idx, initial_capital, is_overseas, base_params,
                     name="", code="", days=365, n_splits=4):
    """Walk-Forward 검증 (과최적화 진단)

    전체 분석 구간을 n_splits개의 연속된 OOS(Out-of-Sample) 폴드로 나눈다.
    각 폴드 직전까지의 데이터를 IS(In-Sample)로 삼아 후보 가중치 세트 중
    'IS에서 가장 성과가 좋은' 세트를 고른 뒤, 그 세트를 한 번도 학습에 쓰지 않은
    OOS 구간에 그대로 적용해 평가한다. IS 대비 OOS 성과 저하율로 과최적화(curve fitting)를 진단한다.

    ※ 매도/리스크 파라미터는 base_params(사용자 설정)로 고정하고, 가중치(스코어링 팩터 배분)만
       IS에서 선택하여 검증한다. (자유도가 큰 가중치의 과최적화 여부를 집중 점검)
    """
    n = len(full_df)
    analysis_len = n - start_idx
    # 워밍업/최소 길이 가드: 초기 IS 40% + 폴드별 최소 약 30거래일 필요
    if analysis_len < 150 or analysis_len < n_splits * 30 + 60:
        config.console.print(f"[yellow]Walk-Forward 검증을 위한 데이터가 부족합니다. (분석 구간 {analysis_len}행) 더 긴 기간으로 시도하세요.[/yellow]")
        return

    # 후보 가중치 세트 (총점 10점 유지)
    candidate_weights = [
        {"DESC": "기본(추세추종)",   "TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0},
        {"DESC": "추세 중시",        "TREND": 5.0, "MOMENTUM": 2.0, "STRENGTH": 1.0, "SYNERGY": 2.0},
        {"DESC": "모멘텀 중시",      "TREND": 3.5, "MOMENTUM": 3.0, "STRENGTH": 1.5, "SYNERGY": 2.0},
        {"DESC": "수급/강도 중시",   "TREND": 3.5, "MOMENTUM": 2.0, "STRENGTH": 2.5, "SYNERGY": 2.0},
    ]

    def _run(seg_start, seg_end, w):
        seg_df = full_df.iloc[seg_start:seg_end].copy()
        prev_row = full_df.iloc[seg_start - 1] if seg_start > 0 else None
        return simulate_strategy(
            seg_df, prev_row, initial_capital, base_params['buy_score'], base_params['buy_rsi'], is_overseas,
            stop_loss_rate=base_params['stop_loss'], take_profit_rate=base_params['take_profit'],
            take_profit_rsi=base_params['take_profit_rsi'], sell_score=base_params['sell_score'],
            ts_activation_rate=base_params['ts_activation'], ts_callback_rate=base_params['ts_callback'],
            time_stop_days_limit=base_params['time_stop_days'],
            use_atr_stop_limit=base_params['use_atr_stop'], atr_stop_multiplier_limit=base_params['atr_mult'],
            half_tp_use_limit=base_params['half_tp_use'], weights={k: v for k, v in w.items() if k != "DESC"}
        )

    def _sharpe(res):
        da = res.get('daily_assets', [])
        if len(da) > 1:
            r = pd.Series(da).pct_change().dropna()
            if len(r) > 0 and r.std() > 0:
                return (r.mean() / r.std()) * np.sqrt(252)
        return 0.0

    def _date_str(idx):
        try:
            d = str(full_df.iloc[idx]['date'])
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        except Exception:
            return "-"

    # 초기 40%는 학습 전용, 이후 구간을 n_splits개의 OOS 폴드로 분할 (Anchored/Expanding IS)
    oos_region_start = start_idx + int(analysis_len * 0.4)
    fold_size = (n - oos_region_start) // n_splits

    config.console.print(f"\n[bold magenta]━━━ Walk-Forward 검증 ({name}) ━━━[/]")
    config.console.print(f"[dim]분석 {analysis_len}행 | 초기 학습 40% | OOS {n_splits}폴드 | 후보 가중치 {len(candidate_weights)}종 (매도/리스크 파라미터는 입력값 고정)[/dim]\n")

    table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("폴드", justify="center")
    table.add_column("OOS 기간", justify="center")
    table.add_column("선택 가중치(IS 최적)", justify="left")
    table.add_column("IS 수익률", justify="right")
    table.add_column("OOS 수익률", justify="right")
    table.add_column("OOS 승률", justify="right")
    table.add_column("OOS MDD", justify="right")

    oos_equity = 1.0       # OOS 폴드 수익률 복리 누적
    is_returns, oos_returns, oos_winrates = [], [], []

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=config.console, transient=True) as progress:
        progress.add_task("[cyan]Walk-Forward 시뮬레이션 진행 중...[/cyan]", total=None)

        for i in range(n_splits):
            oos_start = oos_region_start + i * fold_size
            oos_end = n if i == n_splits - 1 else oos_start + fold_size
            if oos_end - oos_start < 20:
                continue

            # 1) IS(expanding): 분석 시작 ~ OOS 직전. IS에서 최적 가중치 선택
            best_w, best_is_ret, best_is_res = None, -1e9, None
            for w in candidate_weights:
                is_res = _run(start_idx, oos_start, w)
                if is_res['total_return'] > best_is_ret:
                    best_is_ret = is_res['total_return']
                    best_w = w
                    best_is_res = is_res

            # 2) OOS: 선택된 가중치를 미학습 구간에 적용
            oos_res = _run(oos_start, oos_end, best_w)
            oos_ret = oos_res['total_return']
            sells = oos_res['win_trades'] + oos_res['loss_trades']
            oos_wr = (oos_res['win_trades'] / sells * 100) if sells > 0 else 0.0

            is_returns.append(best_is_ret)
            oos_returns.append(oos_ret)
            oos_winrates.append(oos_wr)
            oos_equity *= (1 + oos_ret / 100.0)

            oc = "[red]" if oos_ret >= 0 else "[blue]"
            table.add_row(
                f"{i+1}",
                f"{_date_str(oos_start)}~{_date_str(oos_end-1)}",
                best_w["DESC"],
                f"{best_is_ret:+.1f}%",
                f"{oc}{oos_ret:+.1f}%[/]",
                f"{oos_wr:.0f}%",
                f"{oos_res['mdd']:.1f}%",
            )

    config.console.print(table)

    if not oos_returns:
        config.console.print("[yellow]유효한 OOS 폴드가 없어 검증을 종료합니다.[/yellow]")
        return

    avg_is = sum(is_returns) / len(is_returns)
    avg_oos = sum(oos_returns) / len(oos_returns)
    total_oos_ret = (oos_equity - 1) * 100.0
    avg_oos_wr = sum(oos_winrates) / len(oos_winrates)
    pos_folds = sum(1 for r in oos_returns if r > 0)

    # 과최적화 진단: OOS가 IS 대비 얼마나 무너지는가
    if avg_is != 0:
        retention = (avg_oos / avg_is) * 100 if avg_is > 0 else (0.0 if avg_oos <= 0 else 100.0)
    else:
        retention = 0.0

    summary = Table(title=f"\nWalk-Forward 종합 ({name})", box=box.HORIZONTALS, show_header=False, border_style="dim")
    summary.add_column("항목", style="dim")
    summary.add_column("값", justify="right")
    summary.add_row("OOS 누적 수익률 (복리)", f"{'[red]' if total_oos_ret>=0 else '[blue]'}{total_oos_ret:+.2f}%[/]")
    summary.add_row("폴드 평균 IS 수익률", f"{avg_is:+.2f}%")
    summary.add_row("폴드 평균 OOS 수익률", f"{avg_oos:+.2f}%")
    summary.add_row("OOS 성과 유지율 (OOS/IS)", f"{retention:.0f}%")
    summary.add_row("수익 폴드 비율", f"{pos_folds}/{len(oos_returns)}")
    summary.add_row("OOS 평균 승률", f"{avg_oos_wr:.1f}%")
    config.console.print(summary)

    # 진단 메시지
    if avg_oos <= 0:
        verdict = "[bold blue]⚠ 과최적화 의심[/]: OOS 평균 수익이 음수입니다. IS 성과가 미래로 이어지지 않습니다."
    elif retention < 40:
        verdict = "[bold yellow]주의[/]: OOS 유지율이 낮습니다(<40%). 파라미터가 과거에 과적합되었을 가능성이 있습니다."
    elif retention < 70:
        verdict = "[green]양호[/]: 일부 성과 저하가 있으나 OOS에서도 수익 추세를 유지합니다."
    else:
        verdict = "[bold green]견고[/]: OOS 성과가 IS와 유사하게 유지됩니다. 강건한(robust) 파라미터로 판단됩니다."
    config.console.print(f"\n{verdict}")
    config.console.print("[dim]※ 단일 종목·단일 구간 검증은 표본이 작습니다. 여러 종목·기간으로 반복해 일관성을 확인하세요.[/dim]")

    # [추가] AI 백테스팅 진단 (Walk-Forward)
    config.console.print()
    if Prompt.ask("🤖 AI 백테스팅 성과 진단을 수행하시겠습니까?", choices=["y", "n"], default="n") == 'y':
        from modules import theme_analysis
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.padding import Padding
        
        param_info = f"매수 {base_params['buy_score']}점/RSI{base_params['buy_rsi']}, 매도 {base_params['sell_score']}점, 익절 +{base_params['take_profit']}%/RSI{base_params['take_profit_rsi']}, 손절 {base_params['stop_loss']}%, 트레일링 +{base_params['ts_activation']}%/-{base_params['ts_callback']}%"
        
        clean_verdict = verdict.replace('[bold blue]', '').replace('[/]', '').replace('[bold yellow]', '').replace('[green]', '').replace('[bold green]', '')
        
        backtest_info = f"""
        [시뮬레이션 요약]
        - 종목: {name} ({code})
        - 적용 파라미터: {param_info}
        - OOS 누적 수익률 (복리): {total_oos_ret:+.2f}%
        - 폴드 평균 IS 수익률: {avg_is:+.2f}%
        - 폴드 평균 OOS 수익률: {avg_oos:+.2f}%
        - OOS 성과 유지율: {retention:.0f}%
        - 수익 폴드 비율: {pos_folds}/{len(oos_returns)}
        - OOS 평균 승률: {avg_oos_wr:.1f}%
        - 1차 시스템 진단: {clean_verdict}
        
        [폴드별 상세 내역]
"""
        for idx, (is_r, oos_r, oos_w) in enumerate(zip(is_returns, oos_returns, oos_winrates)):
            backtest_info += f"        - 폴드 {idx+1}: IS {is_r:+.1f}% / OOS {oos_r:+.1f}% / 승률 {oos_w:.0f}%\n"

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task(f"[cyan]Google Gemini가 Walk-Forward 백테스팅 결과를 심층 분석 중...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
            answer = theme_analysis.evaluate_backtest_with_gemini(code, name, backtest_info, mode='walk_forward')
            
        if answer:
            if answer.startswith("⚠️"):
                config.console.print(f"\n{answer}")
            else:
                md = Markdown(answer)
                panel = Panel(md, title=f"🤖 AI Walk-Forward 성과 진단: {name}({code})", border_style="cyan", padding=(1, 2), width=120)
                config.console.print()
                config.console.print(Padding(panel, (0, 4)))
        else:
            config.console.print("[red]진단 결과를 생성하지 못했습니다.[/red]")


def run_backtest():
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "1"
    while True:
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        menu_items = [
            ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"),
            ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"),
            ("5", "시장 지수", "Market Indices"), ("6", "직접 입력", "Direct Input")
        ]
        sub_choice = utils.show_menu("전략 백테스팅 (Backtest)", menu_items, default_choice=last_choice)
        if sub_choice.lower() in ['b', 'q']: return False
        if sub_choice.lower() == 'h':
            if getattr(utils, 'show_help', None):
                utils.show_help()
                utils.pause()
            continue
        
        menu_map_dict = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{sub_choice}] {menu_map_dict[sub_choice]}")

        code, name, is_overseas = None, None, False

        if sub_choice == '6':
            utils.print_breadcrumb()
            raw_input = Prompt.ask("종목코드(6자리/티커) 입력 [dim](이전: b, 메인: q)[/dim]")
            config.console.print()
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
                    
                if not utils.validate_and_confirm_stock(code, name, is_overseas, "이 종목으로 백테스팅을 진행하시겠습니까?"):
                    continue
        elif sub_choice == '5':
            # [수정] 통합 지수 리스트 사용 (백테스팅용)
            indices_list = market.ALL_INDICES
            dict_list = [{'name': n, 'code': c} for n, c in indices_list]
            idx, item = utils.search_stock_in_list(dict_list, title="시장 지수 목록")
            if item:
                name, code = item['name'], item['code']
                is_overseas = True
                context.USER_ACTION_BREADCRUMB.append(f"[지수선택] {name}")
        elif sub_choice in ["1", "2", "3", "4"]:
            key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
            s_list = config.session.stock_data.get(key_map[sub_choice], [])
            if s_list:
                idx, item = utils.search_stock_in_list(s_list, title=f"{menu_map_dict[sub_choice]} 목록")
                if item:
                    code, name = item['code'], item['name']
                    is_overseas = (sub_choice in ["3", "4"])
                    context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {name}")
            else:
                config.console.print("[yellow]목록이 비어있습니다.[/yellow]")
                utils.pause()
                continue

        if not code: continue

        # 2. 설정 입력
        apply_preset = Prompt.ask("시장 상황 프리셋을 적용하여 시뮬레이션을 진행하시겠습니까? [dim](이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="n")
        config.console.print()
        if apply_preset in ['b', 'q']: continue

        change_settings = "n"
        preset_choice = None
        msg_preset = ""
        if apply_preset == "y":
            preset_items = [
                ("1", "강세장  (Bull) - 수익 극대화 & 추세 추종", "Bull"),
                ("2", "약세장  (Bear) - 생존 우선 & 낙폭과대 스윙", "Bear"),
                ("3", "횡보장  (Sideways) - 박스권 단기 스윙", "Sideways"),
                ("0", "기본설정 (Default) - 시스템 권장 설정", "Default")
            ]
            preset_choice = utils.show_menu("시장 상황 프리셋 선택", preset_items, default_choice="1")
            if preset_choice.lower() in ['b', 'q']: continue
        else:
            change_settings = Prompt.ask("시뮬레이션 조건을 변경하시겠습니까? [dim](이전: b, 메인: q)[/dim]", choices=["y", "n", "b", "q"], default="n")
            config.console.print()
            if change_settings in ['b', 'q']: continue
        
        # [추가] 개별 룰 로드
        custom_rule = db_manager.db.get_stock_strategy(code)
        
        # 기본값 설정 (개별 룰 우선 적용)
        days = 365
        buy_score = custom_rule['buy_score'] if custom_rule else config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        buy_rsi = custom_rule['buy_rsi'] if custom_rule else config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        sell_score = custom_rule['sell_score'] if custom_rule else config.SELL_STRATEGY["SELL_SCORE"]
        stop_loss = custom_rule['stop_loss'] if custom_rule else config.SELL_STRATEGY["STOP_LOSS_RATE"]
        take_profit = custom_rule['take_profit'] if custom_rule else config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        take_profit_rsi = custom_rule['take_profit_rsi'] if custom_rule else config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        ts_activation = custom_rule['ts_activation'] if custom_rule else config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 15.0)
        ts_callback = custom_rule['ts_callback'] if custom_rule else config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 4.0)
        time_stop_days = custom_rule['time_stop_days'] if custom_rule and custom_rule.get('time_stop_days') is not None else config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
        use_atr_stop = bool(custom_rule['use_atr_stop']) if custom_rule and custom_rule.get('use_atr_stop') is not None else config.SELL_STRATEGY.get("USE_ATR_STOP", True)
        atr_mult = custom_rule['atr_stop_multiplier'] if custom_rule and custom_rule.get('atr_stop_multiplier') is not None else config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
        half_tp_use = bool(custom_rule['half_take_profit_use']) if custom_rule and custom_rule.get('half_take_profit_use') is not None else config.SELL_STRATEGY.get("HALF_TAKE_PROFIT_USE", True)
        
        weights = config.SCORING_WEIGHTS
        if custom_rule and custom_rule.get('weights'):
            try:
                w_data = custom_rule['weights']
                if isinstance(w_data, str): weights = json.loads(w_data)
                elif isinstance(w_data, dict): weights = w_data
            except: pass

        # 프리셋 적용 시 기본값 덮어쓰기
        if preset_choice == "1":
            buy_score = 7.0
            buy_rsi = 70
            sell_score = 5.0
            take_profit = 40.0
            take_profit_rsi = 80.0
            stop_loss = -7.0
            ts_activation = 20.0
            ts_callback = 5.0
            time_stop_days = 10
            atr_mult = 2.0
            weights = {"TREND": 5.0, "MOMENTUM": 2.0, "STRENGTH": 1.0, "SYNERGY": 2.0}
            msg_preset = "강세장 (수익 극대화 & 추세 추종)"
        elif preset_choice == "2":
            buy_score = 9.0
            buy_rsi = 65
            sell_score = 6.0
            take_profit = 7.0
            stop_loss = -3.0
            ts_activation = 4.0
            ts_callback = 2.0
            time_stop_days = 3
            atr_mult = 1.5
            weights = {"TREND": 1.0, "MOMENTUM": 4.0, "STRENGTH": 3.0, "SYNERGY": 2.0}
            msg_preset = "약세장 (생존 우선 & 낙폭과대 스윙)"
        elif preset_choice == "3":
            buy_score = 7.5
            buy_rsi = 50
            sell_score = 5.0
            take_profit = 15.0
            stop_loss = -5.0
            ts_activation = 7.0
            ts_callback = 3.0
            time_stop_days = 5
            atr_mult = 1.8
            weights = {"TREND": 2.5, "MOMENTUM": 3.5, "STRENGTH": 2.0, "SYNERGY": 2.0}
            msg_preset = "횡보장 (박스권 단기 스윙)"
        elif preset_choice == "0":
            buy_score = 7.5
            buy_rsi = 70
            sell_score = 5.0
            take_profit = 50.0
            take_profit_rsi = 85.0
            stop_loss = -7.0
            ts_activation = 10.0
            ts_callback = 4.0
            time_stop_days = 20
            atr_mult = 2.0
            weights = {"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0}
            msg_preset = "기본설정 (시스템 권장: 추세추종 + 트레일링 주청산)"

        if change_settings == "y":
            config.console.print()
            config.console.print("[bold]1. 시뮬레이션 기본 설정[/bold]")
            days_input = Prompt.ask("분석 기간 (일 단위)\n[dim]과거 며칠간의 데이터를 시뮬레이션할지 설정[/dim]", default="365")
            if days_input.lower() in ['b', 'q']: continue
            try:
                days = int(days_input)
            except:
                days = 365
            
            config.console.print("\n[bold]2. 기본 매수 타점 설정[/bold]")
            def_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
            val = Prompt.ask(f"매수 기준 점수 (기본: {def_buy_score}점)\n[dim]이 점수 이상일 때 매수 진입 (지표 종합 점수)[/dim]", default=str(def_buy_score))
            if val.lower() in ['b', 'q']: continue
            try: buy_score = float(val)
            except: buy_score = float(def_buy_score)
            
            def_buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
            val = Prompt.ask(f"매수 허용 RSI 상한 (기본: {def_buy_rsi})\n[dim]RSI가 이 값보다 낮아야 매수 (과열 방지)[/dim]", default=str(def_buy_rsi))
            if val.lower() in ['b', 'q']: continue
            buy_rsi = float(val)
            
            config.console.print("\n[bold]3. 기본 청산 타점 설정[/bold]")
            def_tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
            val = Prompt.ask(f"익절 수익률(%) (기본: {def_tp}%)\n[dim]수익이 이 비율에 도달하면 이익 실현 (0: 미사용)[/dim]", default=str(def_tp))
            if val.lower() in ['b', 'q']: continue
            take_profit = float(val)
            
            curr_half_tp = "y" if half_tp_use else "n"
            val = Prompt.ask(f"반익절 사용 (y: 사용 / n: 미사용) (기본: {curr_half_tp})\n[dim]목표 익절 수익률의 절반 도달 시 50% 선매도[/dim]", choices=["y", "n", "b", "q"], default=curr_half_tp)
            if val.lower() in ['b', 'q']: continue
            half_tp_use = (val.lower() == 'y')
            
            def_tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
            val = Prompt.ask(f"익절 RSI 기준 (기본: {def_tp_rsi})\n[dim]RSI가 이 값을 초과하면 과열로 판단하여 매도[/dim]", default=str(def_tp_rsi))
            if val.lower() in ['b', 'q']: continue
            take_profit_rsi = float(val)
            
            def_sell_score = config.SELL_STRATEGY["SELL_SCORE"]
            val = Prompt.ask(f"매도(추세이탈) 기준 점수 (기본: {def_sell_score}점)\n[dim]점수가 이 값 미만으로 떨어지면 매도[/dim]", default=str(def_sell_score))
            if val.lower() in ['b', 'q']: continue
            try: sell_score = float(val)
            except: sell_score = float(def_sell_score)
            
            def_ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
            val = Prompt.ask(f"트레일링 스탑 발동 수익률(%) (기본: {def_ts_act}%)\n[dim]수익률이 이 값 이상일 때 트레일링 스탑 감시 시작[/dim]", default=str(def_ts_act))
            if val.lower() in ['b', 'q']: continue
            ts_activation = float(val)
            
            def_ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
            val = Prompt.ask(f"트레일링 스탑 하락 감지율(%) (기본: {def_ts_call}%)\n[dim]최고가 대비 이 비율만큼 하락 시 매도[/dim]", default=str(def_ts_call))
            if val.lower() in ['b', 'q']: continue
            ts_callback = float(val)
            
            def_time_stop = config.SELL_STRATEGY.get("TIME_STOP_DAYS", 10)
            val = Prompt.ask(f"시간 청산 기한(일) (기본: {def_time_stop}일)\n[dim]매수 후 목표 기간 내 수익 미달 시 강제 청산 (0: 미사용)[/dim]", default=str(def_time_stop))
            if val.lower() in ['b', 'q']: continue
            time_stop_days = int(val)
            
            config.console.print("\n[bold]4. 리스크 관리 설정[/bold]")
            curr_use_atr = "y" if use_atr_stop else "n"
            val = Prompt.ask(f"손절 방식 (y: ATR 동적 손절 / n: 고정 손절률) (기본: {curr_use_atr})\n[dim]종목의 변동성에 비례하여 손절폭 자동 계산 여부[/dim]", choices=["y", "n", "b", "q"], default=curr_use_atr)
            if val.lower() in ['b', 'q']: continue
            use_atr_stop = (val.lower() == 'y')
            
            if use_atr_stop:
                val = Prompt.ask(f"ATR 손절 배수 (기본: {atr_mult})\n[dim]ATR 값의 몇 배를 손절폭으로 할지 설정 (0: 미사용)[/dim]", default=str(atr_mult))
                if val.lower() in ['b', 'q']: continue
                atr_mult = float(val)
            else:
                def_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
                val = Prompt.ask(f"손절 수익률(%) (기본: {def_sl}%)\n[dim]손실이 이 비율에 도달하면 손절매 (0: 미사용)[/dim]", default=str(def_sl))
                if val.lower() in ['b', 'q']: continue
                stop_loss = float(val)
            
            config.console.print("\n[bold]5. 스코어링 가중치 설정[/bold]")
            if Prompt.ask("가중치를 변경하시겠습니까?", choices=["y", "n"], default="n") == "y":
                curr_weights = weights.copy() if weights else config.SCORING_WEIGHTS.copy()
                while True:
                    config.console.print()
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
                        
                        weights = {"TREND": w_trend, "MOMENTUM": w_mom, "STRENGTH": w_str, "SYNERGY": w_syn}
                        break
                    except ValueError as e:
                        if str(e) == "quit": break
                        config.console.print("[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")
                        continue

        if change_settings == "y":
            header_msg = "⚪ [개별 설정] 사용자 지정 시뮬레이션 조건으로 진행합니다."
        else:
            if preset_choice:
                header_msg = f"⚪ [프리셋 적용] {msg_preset} 설정으로 진행합니다."
            else:
                header_msg = "⚪ [기본 설정] 시스템 권장 설정 (또는 개별 룰)으로 진행합니다."

        msg = f"\n{header_msg}\n"
        msg += "[dim]" + "─" * 75 + "[/dim]\n"
        msg += f"   [cyan]시뮬레이션 기간[/cyan]          {days}일\n"
        msg += f"   [cyan]매수 허들 (점수/RSI)[/cyan]     {buy_score}점 이상 / RSI {buy_rsi} 미만\n"
        msg += f"   [cyan]매도 허들 (점수/RSI)[/cyan]     점수 {sell_score} 미만 / RSI {take_profit_rsi} 초과\n"
        msg += f"   [cyan]익절 / 손절[/cyan]              +{take_profit}% (반익절: {'ON' if half_tp_use else 'OFF'}) / {f'{stop_loss}% (ATR x{atr_mult})' if use_atr_stop else f'{stop_loss}%'}\n"
        msg += f"   [cyan]트레일링 스탑[/cyan]            +{ts_activation}% 발동 후 -{ts_callback}%\n"
        msg += f"   [cyan]시간 청산[/cyan]                {time_stop_days}일 경과 시 강제 매도\n"
        if weights:
            msg += f"   [cyan]스코어링 가중치[/cyan]          추세 {weights.get('TREND', 4.0)} / 모멘텀 {weights.get('MOMENTUM', 2.5)} / 강도 {weights.get('STRENGTH', 1.5)} / 시너지 {weights.get('SYNERGY', 2.0)}\n"
        msg += "[dim]" + "─" * 75 + "[/dim]\n"
        
        # [수정] 초기 자본금 및 환율 설정
        initial_capital_krw = 10_000_000
        exchange_rate = 1.0
        
        if is_overseas:
            exchange_rate = config.DEFAULT_EXCHANGE_RATE
            initial_capital = initial_capital_krw / exchange_rate
        else:
            initial_capital = initial_capital_krw

        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

        # [이동] 실행 모드 선택 (데이터 준비 전으로 이동)
        mode_items = [("1", "단일 백테스팅", "Single Run"), ("2", "Monte Carlo 시뮬레이션", "Monte Carlo Sim"), ("3", "Walk-Forward 검증", "Walk-Forward Validation")]
        mode_choice = utils.show_menu("실행 모드를 선택하세요", mode_items, default_choice="1", text_before=msg)

        if mode_choice.lower() in ['b', 'q']: continue
        mode_map_dict = dict((k, v) for k, v, _ in mode_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{mode_choice}] {mode_map_dict.get(mode_choice, '')}")

        # 3. 데이터 준비
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task(f"[cyan]{name} ({code}) 데이터 분석 및 시뮬레이션 준비 중...[/cyan]", total=None)
            # KIS API 사용 시를 대비해 설정 변경 (yfinance 실패 시 동작)
            original_lookback = config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"]
            needed_days = days + 400 # 52주 윈도우 충족 위해 약 1년 워밍업 확보
            if needed_days > original_lookback:
                config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] = needed_days
                
            try:
                # [수정] yfinance 우선 조회 함수 사용
                df = get_backtest_data(code, is_overseas, days)
            finally:
                config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] = original_lookback

            if df is None or df.empty:
                config.console.print("[red]데이터를 불러올 수 없습니다.[/red]")
                utils.pause()
                continue
                
            # [추가] 스마트머니(수급) 시그널 사전 병합
            df = _append_smart_money_signal(df, code, is_overseas)

            # 지표 계산 (워밍업 포함 전체 df 기준)
            df = compute_price_indicators(df)

            # 분석 기간 필터링
            # [수정] 행 개수 기준이 아닌 날짜 기준으로 필터링
            cutoff_dt = datetime.now() - timedelta(days=days)
            cutoff_str = cutoff_dt.strftime("%Y%m%d")
            
            start_idx = 0
            mask = df['date'] >= cutoff_str
            if mask.any():
                start_idx = mask.idxmax()
            
            sim_df = df.iloc[start_idx:].copy()
            prev_row_init = df.iloc[start_idx-1] if start_idx > 0 else None
            
            # [수정] 경고 로직: 실제 기간(일수)과 요청 기간 비교
            if not sim_df.empty:
                actual_days = (datetime.strptime(str(sim_df.iloc[-1]['date']), "%Y%m%d") - datetime.strptime(str(sim_df.iloc[0]['date']), "%Y%m%d")).days
                if actual_days < days * 0.9: # 90% 미만일 때만 경고
                    config.console.print(f"[dim yellow]주의: 요청 기간({days}일)보다 실제 분석 기간({actual_days}일)이 짧습니다.[/dim yellow]")

        if mode_choice == "2":
            run_monte_carlo_simulation(df, start_idx, initial_capital, buy_score, buy_rsi, is_overseas,
                                       stop_loss, take_profit, take_profit_rsi, sell_score, ts_activation, ts_callback,
                                       time_stop_days, use_atr_stop, atr_mult, half_tp_use,
                                       weights=weights, name=name, code=code, days=days)
            last_choice = sub_choice
            utils.pause()
            continue

        if mode_choice == "3":
            base_params = {
                "buy_score": buy_score, "buy_rsi": buy_rsi, "sell_score": sell_score,
                "stop_loss": stop_loss, "take_profit": take_profit, "take_profit_rsi": take_profit_rsi,
                "ts_activation": ts_activation, "ts_callback": ts_callback, "time_stop_days": time_stop_days,
                "use_atr_stop": use_atr_stop, "atr_mult": atr_mult, "half_tp_use": half_tp_use,
            }
            run_walk_forward(df, start_idx, initial_capital, is_overseas, base_params,
                             name=name, code=code, days=days)
            last_choice = sub_choice
            utils.pause()
            continue

        # === 단일 실행 모드 (기존 로직) ===
        res = simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score, buy_rsi, is_overseas, 
                                stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                                take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                ts_activation_rate=ts_activation, ts_callback_rate=ts_callback,
                                time_stop_days_limit=time_stop_days,
                                use_atr_stop_limit=use_atr_stop, atr_stop_multiplier_limit=atr_mult, half_tp_use_limit=half_tp_use,
                                weights=weights) # [수정] 가중치 전달
        
        # 결과 변수 매핑 (기존 출력 로직 호환)
        final_asset = res['final_asset']
        total_return = res['total_return']
        trades = res['trades']
        win_trades = res['win_trades']
        loss_trades = res['loss_trades']
        gross_profit = res['gross_profit']
        gross_loss = res['gross_loss']
        mdd = res['mdd']
        daily_assets = res['daily_assets']
        max_score_observed = res['max_score_observed']
        score_8_count = res['score_8_count']
        missed_caution = res.get('missed_caution_count', 0)
        missed_danger = res.get('missed_danger_count', 0)
        missed_trades = res.get('missed_trades', [])

        # 4. 결과 출력
        # [추가] 샤프 지수 계산 (연율화: 252일 기준)
        sharpe_ratio = 0.0
        if len(daily_assets) > 1:
            returns = pd.Series(daily_assets).pct_change().dropna()
            if not returns.empty and returns.std() > 0:
                sharpe_ratio = (returns.mean() / returns.std()) * np.sqrt(252)

        # [추가] 포맷팅 헬퍼 함수
        def fmt_money(val):
            try:
                if pd.isna(val) or math.isnan(float(val)): return "-"
                if is_overseas: return f"${float(val):,.2f}"
                return f"{int(float(val)):,}원"
            except:
                return "-"

        # [수정] 결과 리포트를 테이블로 출력
        config.console.print()
        summary_table = Table(title=f"백테스팅 결과 리포트: {name}", box=box.HORIZONTALS, show_header=False, border_style="dim")
        summary_table.add_column("항목", style="cyan", justify="left")
        summary_table.add_column("값", justify="left")
        
        start_date_str = str(sim_df.iloc[0]['date'])
        end_date_str = str(sim_df.iloc[-1]['date'])
        if len(start_date_str) == 8 and start_date_str.isdigit(): start_date_str = f"{start_date_str[:4]}-{start_date_str[4:6]}-{start_date_str[6:]}"
        if len(end_date_str) == 8 and end_date_str.isdigit(): end_date_str = f"{end_date_str[:4]}-{end_date_str[4:6]}-{end_date_str[6:]}"
        
        summary_table.add_row("기간", f"{days}일간 ({start_date_str} ~ {end_date_str})")
        summary_table.add_row("초기 자본금", fmt_money(initial_capital))
        summary_table.add_row("최종 평가액", fmt_money(final_asset))
        
        color = "red" if total_return > 0 else "blue"
        summary_table.add_row("누적 수익률", f"[{color}]{total_return:+.2f}%[/]")
        
        summary_table.add_section()
        
        # [수정] 매매 횟수 상세 및 승률/MDD 출력
        sell_count = win_trades + loss_trades
        sell_trades_list = [t for t in trades if t['type'].startswith("매도")]
        win_rate = (win_trades / sell_count * 100) if sell_count > 0 else 0.0
        summary_table.add_row("총 매매 횟수", f"{len(trades)}건 (진입 {len(trades)-sell_count} / 청산 {sell_count})")
        
        # [추가] 매수 보류 통계 출력
        if missed_caution > 0 or missed_danger > 0:
            summary_table.add_row("매수 보류 (상태)", f"[yellow]주의 {missed_caution}회[/] / [blue]매도 {missed_danger}회[/] (점수 충족했으나 진입 불가)")
        
        if sell_count > 0:
            summary_table.add_row("승률 (Win Rate)", f"{win_rate:.1f}% ({win_trades}승 {loss_trades}패)")
            
            # [순서 변경] 평균 수익률 먼저 출력
            profits = [t['profit'] for t in sell_trades_list if t['profit'] > 0]
            losses = [t['profit'] for t in sell_trades_list if t['profit'] <= 0]
            
            avg_profit = sum(profits) / len(profits) if profits else 0.0
            avg_loss = sum(losses) / len(losses) if losses else 0.0
            
            summary_table.add_row("평균 수익률", f"[red]{avg_profit:+.2f}%[/]")
            summary_table.add_row("평균 손실률", f"[blue]{avg_loss:+.2f}%[/]")
            
            # [추가] 손익 구조 분석 (평균 손익비 기반)
            structure_msg = "-"
            if avg_loss == 0:
                structure_msg = "[green]무손실 (완벽한 방어)[/]"
            else:
                # 손익비 = 평균수익 / |평균손실|
                pl_ratio = abs(avg_profit / avg_loss)
                # 손익분기 승률 = 1 / (손익비 + 1)
                be_win_rate = (1 / (pl_ratio + 1)) * 100
                
                if pl_ratio >= 2.0:
                    structure_msg = f"[green]매우 우수 (승률 {be_win_rate:.0f}%만 넘으면 수익)[/]"
                elif pl_ratio >= 1.5:
                    structure_msg = f"[green]양호 (승률 {be_win_rate:.0f}%만 넘으면 수익)[/]"
                elif pl_ratio >= 1.0:
                    structure_msg = f"[yellow]보통 (승률 {be_win_rate:.0f}% 이상 필요)[/]"
                else:
                    structure_msg = f"[red]불리함 (승률 {be_win_rate:.0f}% 이상 필요)[/]"
            
            summary_table.add_row("손익 구조 분석", structure_msg)

            # [추가] 보유 기간 통계
            holding_days_list = [t['days'] for t in sell_trades_list]
            if holding_days_list:
                max_days = max(holding_days_list)
                min_days = min(holding_days_list)
                avg_days = sum(holding_days_list) / len(holding_days_list)
                summary_table.add_row("보유 기간", f"평균 {avg_days:.1f}일 (최대 {max_days}일 / 최소 {min_days}일)")
            
            # [수정] 손익비(Profit Factor) 출력 (설명 추가)
            pf_str = "Inf"
            pf_desc = ""
            if gross_loss > 0:
                pf = gross_profit / gross_loss
                if pf >= 2.0: pf_color = "red"; pf_desc = " (매우 훌륭)"
                elif pf >= 1.5: pf_color = "orange3"; pf_desc = " (우수)"
                elif pf >= 1.0: pf_color = "green"; pf_desc = " (평범)"
                else: pf_color = "blue"; pf_desc = " (손실)"
                pf_str = f"[{pf_color}]{pf:.2f}[/]"
            elif gross_profit > 0: 
                pf_str = "[dim]0.00[/]"
                pf_desc = " (손실 없음)"
            else: pf_str = "[dim]-[/]"
            
            summary_table.add_row("손익비 (Profit Factor)", f"{pf_str}{pf_desc} (총 이익 [red]+{fmt_money(gross_profit)}[/] / 총 손실 [blue]-{fmt_money(gross_loss)}[/])")
            
            # [수정] 샤프 지수 출력 (설명 추가)
            sharpe_desc = ""
            if sharpe_ratio >= 1.0: sharpe_color = "red"; sharpe_desc = " (매우 우수)"
            elif sharpe_ratio >= 0.5: sharpe_color = "green"; sharpe_desc = " (양호)"
            else: sharpe_color = "blue"; sharpe_desc = " (미흡)"
            summary_table.add_row("위험 대비 수익 (샤프지수)", f"[{sharpe_color}]{sharpe_ratio:.2f}[/]{sharpe_desc}")
        
        mdd_color = "red"
        mdd_desc = " (위험)"
        if mdd >= -10: mdd_color = "green"; mdd_desc = " (안정)"
        elif mdd >= -20: mdd_color = "yellow"; mdd_desc = " (보통)"
        elif mdd >= -30: mdd_color = "orange3"; mdd_desc = " (주의)"
        
        summary_table.add_row("최대 낙폭 (MDD)", f"[{mdd_color}]{mdd:.2f}%[/]{mdd_desc}")
        
        config.console.print(summary_table)
        
        # [추가] 매매가 없을 경우 진단 정보 출력
        if len(trades) == 0:
            config.console.print("\n[yellow]※ 매매가 발생하지 않았습니다. (조건 미충족)[/yellow]")
            config.console.print(f"  - 기간 내 최고 점수: {max_score_observed:.1f}점 (매수 기준: {buy_score}점)")
            config.console.print(f"  - {buy_score}점 이상 도달 횟수: {score_8_count}회")
            config.console.print(f"  [안내] 현재 설정된 매수 조건({buy_score}점 이상 & RSI<{buy_rsi})이 엄격하여 진입 기회가 없었습니다.")
            config.console.print("  [Tip] 매수 조건을 완화하거나 분석 기간을 늘려보세요.")
        
        if trades:
            config.console.print()
            t_table = Table(title="상세 매매 일지", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            t_table.add_column("일자", justify="center")
            t_table.add_column("구분", justify="center")
            t_table.add_column("점수", justify="center")
            t_table.add_column("RSI", justify="right")
            t_table.add_column("CCI", justify="right")
            t_table.add_column("ADX", justify="right")
            t_table.add_column("DMI", justify="right")
            t_table.add_column("OBV", justify="right")
            t_table.add_column("수량", justify="right")
            t_table.add_column("단가", justify="right")
            t_table.add_column("수익금", justify="right")
            t_table.add_column("수익률", justify="right")
            t_table.add_column("누적손익", justify="right")
            t_table.add_column("보유기간", justify="right")

            for t in trades:
                p_str = f"{t['profit']:+.2f}%" if t['type'].startswith("매도") else "-"
                p_color = "[red]" if t['profit'] > 0 else ("[blue]" if t['profit'] < 0 else "")
                profit_display = f"{p_color}{p_str}[/]" if p_color else p_str
                type_color = "[red]" if "매수" in t['type'] else "[blue]"
                date_str = str(t['date'])
                if len(date_str) == 8 and date_str.isdigit(): date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                
                # 지표 색상 적용
                # RSI
                rsi_val = t.get('rsi', 0)
                if rsi_val is None or np.isnan(rsi_val):
                    rsi_str = "-"
                    rsi_c = "dim"
                else:
                    rsi_str = f"{rsi_val:.1f}"
                    rsi_c = "blue"
                    if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_c = "magenta"
                    elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_c = "red"
                    elif 45 <= rsi_val < 55: rsi_c = "orange3"
                    elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: rsi_c = "yellow"
                
                # ADX
                adx_val = t.get('adx', 0)
                if adx_val is None or np.isnan(adx_val):
                    adx_str = "-"
                    adx_c = "dim"
                else:
                    adx_str = f"{adx_val:.1f}"
                    adx_c = "white"
                    if adx_val >= 40: adx_c = "magenta"
                    elif adx_val >= 30: adx_c = "red"
                    elif adx_val >= 20: adx_c = "orange3"
                    elif adx_val >= 15: adx_c = "yellow"
                
                # CCI
                cci_val = t.get('cci', 0)
                if cci_val is None or np.isnan(cci_val):
                    cci_str = "-"
                    cci_c = "dim"
                else:
                    cci_str = f"{cci_val:.1f}"
                    cci_c = "blue"
                    if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_c = "red"
                    elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_c = "orange3"
                    elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: cci_c = "yellow"
                
                # DMI
                plus_di = t.get('plus_di')
                minus_di = t.get('minus_di')
                if plus_di is None or minus_di is None or np.isnan(plus_di) or np.isnan(minus_di):
                    dmi_str = "-"
                else:
                    if plus_di > minus_di:
                        dmi_str = f"[red]{plus_di:.1f}[/]/[dim]{minus_di:.1f}[/]"
                    elif minus_di > plus_di:
                        dmi_str = f"[dim]{plus_di:.1f}[/]/[blue]{minus_di:.1f}[/]"
                    else:
                        dmi_str = f"{plus_di:.1f}/{minus_di:.1f}"
                
                # OBV
                obv_val = t.get('obv') or 0
                obv_c = "red" if t.get('obv_trend') else "blue"
                
                qty_str = f"{t['qty']:,}"
                
                # [수정] 금액 포맷팅 (해외/국내 분기)
                price_str = f"{t['price']:,.2f}" if is_overseas else f"{t['price']:,.0f}"
                
                amt_val = t.get('profit_amt', 0)
                amt_str = "-"
                if t['type'].startswith("매도"):
                    s_val = fmt_money(abs(amt_val)).replace('$', '').replace('원', '')
                    prefix = "$" if is_overseas else ""
                    suffix = "" if is_overseas else "원"
                    if amt_val > 0: amt_str = f"[red]+{prefix}{s_val}{suffix}[/]"
                    elif amt_val < 0: amt_str = f"[blue]-{prefix}{s_val}{suffix}[/]"
                    else: amt_str = f"{prefix}{s_val}{suffix}"
                
                cum_val = t.get('cum_profit', 0)
                s_cum = fmt_money(abs(cum_val)).replace('$', '').replace('원', '')
                prefix = "$" if is_overseas else ""
                suffix = "" if is_overseas else "원"
                cum_p_str = f"{prefix}{s_cum}{suffix}"
                if cum_val > 0: cum_p_str = f"[red]+{cum_p_str}[/]"
                elif cum_val < 0: cum_p_str = f"[blue]-{cum_p_str}[/]"
                
                days_str = "-"
                if t['type'].startswith("매도"):
                    days_str = f"{t.get('days', 0)}일"

                t_table.add_row(
                    date_str[:10], 
                    f"{type_color}{t['type']}[/]", 
                    f"{t.get('score', 0):.1f}",
                    f"[{rsi_c}]{rsi_str}[/]",
                    f"[{cci_c}]{cci_str}[/]",
                    f"[{adx_c}]{adx_str}[/]",
                    dmi_str,
                    f"[{obv_c}]{int(obv_val/1000):,}K[/]",
                    qty_str, 
                    price_str, 
                    amt_str, 
                    profit_display, 
                    cum_p_str,
                    days_str
                )
            config.console.print(t_table)

        # [추가] 매매 사유별 통계 분석 (상세 매매 일지 다음에 출력)
        if sell_trades_list:
            reason_stats = {}
            for t in sell_trades_list:
                # 사유 추출 (예: "매도(익절)" -> "익절")
                raw_type = t['type']
                reason = raw_type.replace("매도(", "").replace(")", "")
                
                if reason == "RSI과열":
                    reason = f"RSI과열({take_profit_rsi})"
                elif reason == "점수하락":
                    reason = f"점수하락({sell_score})"

                if reason not in reason_stats:
                    reason_stats[reason] = {
                        'count': 0, 
                        'profit_rate_sum': 0.0, 
                        'profit_amt_sum': 0, 
                        'win_count': 0, 
                        'days_sum': 0,
                        'max_profit': -9999.0,
                        'min_profit': 9999.0
                    }
                
                reason_stats[reason]['count'] += 1
                reason_stats[reason]['profit_rate_sum'] += t['profit']
                reason_stats[reason]['profit_amt_sum'] += t.get('profit_amt', 0)
                reason_stats[reason]['days_sum'] += t.get('days', 0)
                if t['profit'] > 0:
                    reason_stats[reason]['win_count'] += 1
                
                if t['profit'] > reason_stats[reason]['max_profit']:
                    reason_stats[reason]['max_profit'] = t['profit']
                
                if t['profit'] < reason_stats[reason]['min_profit']:
                    reason_stats[reason]['min_profit'] = t['profit']
                
            reason_table = Table(title="매매 사유별 성과 분석", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            reason_table.add_column("사유", justify="left", style="cyan")
            reason_table.add_column("횟수", justify="right")
            reason_table.add_column("비중", justify="right")
            reason_table.add_column("승률", justify="right")
            reason_table.add_column("평균 수익률", justify="right")
            reason_table.add_column("최고 수익률", justify="right")
            reason_table.add_column("최저 수익률", justify="right")
            reason_table.add_column("총 손익", justify="right")
            reason_table.add_column("평균 보유", justify="right")
            
            total_sells = len(sell_trades_list)
            
            # 수익률 높은 순으로 정렬
            sorted_reasons = sorted(reason_stats.items(), key=lambda x: x[1]['profit_rate_sum'] / x[1]['count'], reverse=True)
            
            for reason, stat in sorted_reasons:
                cnt = stat['count']
                avg_p = stat['profit_rate_sum'] / cnt
                ratio = (cnt / total_sells) * 100
                win_rate = (stat['win_count'] / cnt) * 100
                total_amt = stat['profit_amt_sum']
                avg_days = stat['days_sum'] / cnt
                max_p = stat['max_profit']
                min_p = stat['min_profit']
                
                p_color = "[red]" if avg_p > 0 else ("[blue]" if avg_p < 0 else "[white]")
                max_p_color = "[red]" if max_p > 0 else ("[blue]" if max_p < 0 else "[white]")
                min_p_color = "[red]" if min_p > 0 else ("[blue]" if min_p < 0 else "[white]")
                amt_color = "[red]" if total_amt > 0 else ("[blue]" if total_amt < 0 else "[white]")
                
                amt_str = fmt_money(total_amt)
                if total_amt > 0: amt_str = f"+{amt_str}"
                
                reason_table.add_row(
                    reason, 
                    f"{cnt}회", 
                    f"{ratio:.1f}%", 
                    f"{win_rate:.1f}%",
                    f"{p_color}{avg_p:+.2f}%[/]",
                    f"{max_p_color}{max_p:+.2f}%[/]",
                    f"{min_p_color}{min_p:+.2f}%[/]",
                    f"{amt_color}{amt_str}[/]",
                    f"{avg_days:.1f}일"
                )
                
            config.console.print()
            config.console.print(reason_table)

        # [이동] 매수 보류 상세 내역 테이블 출력 (매매 사유별 분석 아래로 이동)
        if missed_trades:
            config.console.print()
            m_table = Table(title=f"매수 보류 상세 내역 (기준 점수 {buy_score}점)", box=box.HORIZONTALS, header_style="dim", border_style="dim")
            m_table.add_column("일자", justify="center")
            m_table.add_column("점수", justify="center")
            m_table.add_column("상태", justify="center")
            m_table.add_column("당시 주가", justify="right")
            m_table.add_column("RSI", justify="right")
            m_table.add_column("CCI", justify="right")
            m_table.add_column("ADX", justify="right")
            m_table.add_column("DMI", justify="right")
            m_table.add_column("사유", justify="left")
            
            for m in missed_trades:
                date_str = str(m['date'])
                if len(date_str) == 8: date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                
                state = m['state']
                state_color = "white"
                if state in ["매수", "강매수"]: state_color = "red"
                elif state == "상승": state_color = "orange3"
                elif state == "관심": state_color = "green"
                elif state == "관망": state_color = "white"
                elif state == "주의": state_color = "yellow"
                elif state == "매도": state_color = "blue"
                adx_str = f"{m.get('adx', 0):.1f}"
                cci_str = f"{m.get('cci', 0):.1f}"
                
                plus_di = m.get('plus_di')
                minus_di = m.get('minus_di')
                if plus_di is None or minus_di is None or np.isnan(plus_di) or np.isnan(minus_di):
                    dmi_str = "-"
                else:
                    if plus_di > minus_di:
                        dmi_str = f"[red]{plus_di:.1f}[/]/[dim]{minus_di:.1f}[/]"
                    elif minus_di > plus_di:
                        dmi_str = f"[dim]{plus_di:.1f}[/]/[blue]{minus_di:.1f}[/]"
                    else:
                        dmi_str = f"{plus_di:.1f}/{minus_di:.1f}"
                
                price_str = fmt_money(m.get('price', 0))
                m_table.add_row(date_str, f"{m['score']:.1f}", f"[{state_color}]{state}[/]", price_str, f"{m['rsi']:.1f}", cci_str, adx_str, dmi_str, m['reason'])
                
            config.console.print(m_table)

        # === 최적화 모드 (매수 점수) ===
        table = Table(title=f"\n매수 점수 최적화 분석 ({name})", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("매수 점수", justify="center")
        table.add_column("수익률", justify="right")
        table.add_column("승률", justify="right")
        table.add_column("MDD", justify="right")
        table.add_column("매매 횟수", justify="right")
        table.add_column("손익비", justify="right")
        
        best_return_score = 0
        best_return = -999.0
        
        best_mdd_score = 0
        best_mdd = -999.0
        
        best_win_score = 0
        best_win_rate = -1.0
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            # [수정] 0.5점 단위로 시뮬레이션 (4.0 ~ 9.5)
            scores = [x * 0.5 for x in range(8, 20)]
            task = progress.add_task("[cyan]점수별 시뮬레이션 진행 중...[/cyan]", total=len(scores))
            for score in scores:
                res = simulate_strategy(sim_df, prev_row_init, initial_capital, score, buy_rsi, is_overseas,
                                        stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                                        take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                        ts_activation_rate=ts_activation, ts_callback_rate=ts_callback,
                                        time_stop_days_limit=time_stop_days,
                                        use_atr_stop_limit=use_atr_stop, atr_stop_multiplier_limit=atr_mult, half_tp_use_limit=half_tp_use,
                                        weights=weights)
                
                # 결과 계산
                total_trades = len(res['trades'])
                sell_trades_inner = res['win_trades'] + res['loss_trades']
                win_rate = (res['win_trades'] / sell_trades_inner * 100) if sell_trades_inner > 0 else 0.0
                pf = (res['gross_profit'] / res['gross_loss']) if res['gross_loss'] > 0 else (99.9 if res['gross_profit'] > 0 else 0.0)
                
                # 1. 수익률 기준 갱신
                if res['total_return'] > best_return:
                    best_return = res['total_return']
                    best_return_score = score
                
                # 2. MDD 기준 갱신 (값이 클수록 좋음, 예: -5 > -20)
                if res['mdd'] > best_mdd:
                    best_mdd = res['mdd']
                    best_mdd_score = score
                
                # 3. 승률 기준 갱신
                if win_rate > best_win_rate:
                    best_win_rate = win_rate
                    best_win_score = score
                
                # 테이블 행 추가
                r_color = "[red]" if res['total_return'] > 0 else "[blue]"
                table.add_row(
                    f"{score}점",
                    f"{r_color}{res['total_return']:+.2f}%[/]",
                    f"{win_rate:.1f}%",
                    f"{res['mdd']:.2f}%",
                    f"{total_trades}건",
                    f"{pf:.2f}"
                )
                progress.advance(task)
        
        config.console.print(table)
        config.console.print(f"\n[green]추천 (수익률):[/] {best_return_score}점 (수익률 {best_return:+.2f}%)")
        config.console.print(f"[cyan]추천 (안정성):[/] {best_mdd_score}점 (MDD {best_mdd:.2f}%)")
        config.console.print(f"[magenta]추천 (승률):[/]   {best_win_score}점 (승률 {best_win_rate:.1f}%)")

        # === RSI 최적화 모드 ===
        table = Table(title=f"\nRSI 최적화 분석 ({name}) / 점수 {buy_score}점 / RSI {buy_rsi}", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("RSI 기준", justify="center")
        table.add_column("수익률", justify="right")
        table.add_column("승률", justify="right")
        table.add_column("MDD", justify="right")
        table.add_column("매매 횟수", justify="right")
        table.add_column("손익비", justify="right")
        
        best_return_rsi = 0
        best_return = -999.0
        
        rsi_candidates = [45, 50, 55, 60, 65, 70, 75, 80]
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task("[cyan]RSI 기준별 시뮬레이션 진행 중...[/cyan]", total=len(rsi_candidates))
            for rsi_limit in rsi_candidates:
                res = simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score, rsi_limit, is_overseas,
                                        stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                                        take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                        ts_activation_rate=ts_activation, ts_callback_rate=ts_callback,
                                        time_stop_days_limit=time_stop_days,
                                        use_atr_stop_limit=use_atr_stop, atr_stop_multiplier_limit=atr_mult, half_tp_use_limit=half_tp_use,
                                        weights=weights)
                
                total_trades = len(res['trades'])
                sell_trades_inner = res['win_trades'] + res['loss_trades']
                win_rate = (res['win_trades'] / sell_trades_inner * 100) if sell_trades_inner > 0 else 0.0
                pf = (res['gross_profit'] / res['gross_loss']) if res['gross_loss'] > 0 else (99.9 if res['gross_profit'] > 0 else 0.0)
                
                if res['total_return'] > best_return:
                    best_return = res['total_return']
                    best_return_rsi = rsi_limit
                
                r_color = "[red]" if res['total_return'] > 0 else "[blue]"
                table.add_row(
                    f"RSI < {rsi_limit}",
                    f"{r_color}{res['total_return']:+.2f}%[/]",
                    f"{win_rate:.1f}%",
                    f"{res['mdd']:.2f}%",
                    f"{total_trades}건",
                    f"{pf:.2f}"
                )
                progress.advance(task)
        
        config.console.print(table)
        config.console.print(f"\n[green]추천 (수익률):[/] RSI < {best_return_rsi} (수익률 {best_return:+.2f}%)")

        # === 익절/손절 최적화 모드 ===
        table = Table(title=f"\n익절/손절 비율 최적화 분석 ({name}) / 기준 매수 점수 ({buy_score}점)", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("익절/손절", justify="center")
        table.add_column("수익률", justify="right")
        table.add_column("승률", justify="right")
        table.add_column("MDD", justify="right")
        table.add_column("매매 횟수", justify="right")
        table.add_column("손익비", justify="right")
        
        best_return_set = None
        best_return = -999.0
        
        best_mdd_set = None
        best_mdd = -999.0
        
        # 테스트할 범위 설정
        tp_candidates = [10.0, 15.0, 20.0, 30.0, 40.0, 50.0]
        sl_candidates = [-3.0, -5.0, -7.0, -10.0, -15.0]

        row_count = 0
        total_combinations = len(tp_candidates) * len(sl_candidates)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]다양한 익절/손절 조합 시뮬레이션 중...[/cyan]", total=None)
            for tp in tp_candidates:
                for sl in sl_candidates:
                    res = simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score, buy_rsi, is_overseas, 
                                            stop_loss_rate=sl, take_profit_rate=tp,
                                            take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                            ts_activation_rate=ts_activation, ts_callback_rate=ts_callback,
                                            time_stop_days_limit=time_stop_days,
                                            use_atr_stop_limit=use_atr_stop, atr_stop_multiplier_limit=atr_mult, half_tp_use_limit=half_tp_use,
                                            weights=weights)
                    
                    total_trades = len(res['trades'])
                    sell_trades_inner = res['win_trades'] + res['loss_trades']
                    win_rate = (res['win_trades'] / sell_trades_inner * 100) if sell_trades_inner > 0 else 0.0
                    pf = (res['gross_profit'] / res['gross_loss']) if res['gross_loss'] > 0 else (99.9 if res['gross_profit'] > 0 else 0.0)
                    
                    if res['total_return'] > best_return:
                        best_return = res['total_return']
                        best_return_set = (tp, sl)
                    
                    if res['mdd'] > best_mdd:
                        best_mdd = res['mdd']
                        best_mdd_set = (tp, sl)
                    
                    label = f"익절 +{tp}% / 손절 {sl}%"
                    r_color = "[red]" if res['total_return'] > 0 else "[blue]"
                    table.add_row(label, f"{r_color}{res['total_return']:+.2f}%[/]", f"{win_rate:.1f}%", f"{res['mdd']:.2f}%", f"{total_trades}건", f"{pf:.2f}")
                    
                    row_count += 1
                    if row_count % 5 == 0 and row_count < total_combinations:
                        table.add_section()
        
        config.console.print(table)
        
        if best_return_set:
            config.console.print(f"\n[green]추천 (수익률):[/] 익절 +{best_return_set[0]}% / 손절 {best_return_set[1]}% (수익률 {best_return:+.2f}%)")
        if best_mdd_set:
            config.console.print(f"[cyan]추천 (안정성):[/] 익절 +{best_mdd_set[0]}% / 손절 {best_mdd_set[1]}% (MDD {best_mdd:.2f}%)")
            
        # [추가] 가중치 최적화 로직 통합 (1번 실행 시 자동 수행)
        # [수정] 타이틀/프롬프트 제거 및 자동 실행 (랜덤 조합 포함)
        use_random = True
        
        # 테스트할 가중치 조합 생성 (Grid Search)
        scenarios = [
            {"TREND": 4.0, "MOMENTUM": 2.5, "STRENGTH": 1.5, "SYNERGY": 2.0, "DESC": "기본값(추세추종)"},
            {"TREND": 5.0, "MOMENTUM": 2.0, "STRENGTH": 1.0, "SYNERGY": 2.0, "DESC": "추세 중시"},
            {"TREND": 3.5, "MOMENTUM": 3.0, "STRENGTH": 1.5, "SYNERGY": 2.0, "DESC": "모멘텀 중시"},
            {"TREND": 3.5, "MOMENTUM": 2.0, "STRENGTH": 2.5, "SYNERGY": 2.0, "DESC": "수급/강도 중시"},
        ]

        if use_random:
            for i in range(10):
                t = random.uniform(1.0, 5.0)
                m = random.uniform(1.0, 4.0)
                s = random.uniform(0.5, 3.0)
                syn = random.uniform(0.5, 3.0)

                total = t + m + s + syn
                factor = 10.0 / total

                t = round(t * factor, 1)
                m = round(m * factor, 1)
                s = round(s * factor, 1)
                syn = round(10.0 - (t + m + s), 1)

                scenarios.append({
                    "TREND": t, "MOMENTUM": m, "STRENGTH": s, "SYNERGY": syn,
                    "DESC": f"랜덤 조합 {i+1}"
                })
        
        opt_table = Table(title=f"\n가중치 최적화 결과 ({name})", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        opt_table.add_column("시나리오", justify="left")
        opt_table.add_column("수익률", justify="right")
        opt_table.add_column("MDD", justify="right")
        opt_table.add_column("승률", justify="right")
        opt_table.add_column("매매횟수", justify="right")
        opt_table.add_column("가중치 (T/M/S/Syn)", justify="right", style="dim")
        
        best_opt_return = -999.0
        best_opt_scenario = None

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]가중치 최적화 시뮬레이션 진행 중...[/cyan]", total=None)
            for sc in scenarios:
                weights_opt = {k: v for k, v in sc.items() if k != "DESC"}
                res_opt = simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score, buy_rsi, is_overseas, 
                                        stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                                        take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                        ts_activation_rate=ts_activation, ts_callback_rate=ts_callback,
                                        time_stop_days_limit=time_stop_days,
                                        use_atr_stop_limit=use_atr_stop, atr_stop_multiplier_limit=atr_mult, half_tp_use_limit=half_tp_use,
                                        weights=weights_opt)
                
                total_trades_opt = len(res_opt['trades'])
                sell_trades_opt = res_opt['win_trades'] + res_opt['loss_trades']
                win_rate_opt = (res_opt['win_trades'] / sell_trades_opt * 100) if sell_trades_opt > 0 else 0.0
                
                # [추가] 최고 수익률 갱신
                if res_opt['total_return'] > best_opt_return:
                    best_opt_return = res_opt['total_return']
                    best_opt_scenario = sc

                r_color = "[red]" if res_opt['total_return'] > 0 else "[blue]"
                w_str = f"{weights_opt['TREND']:.1f}/{weights_opt['MOMENTUM']:.1f}/{weights_opt['STRENGTH']:.1f}/{weights_opt['SYNERGY']:.1f}"
                
                opt_table.add_row(
                    sc["DESC"],
                    f"{r_color}{res_opt['total_return']:+.2f}%[/]",
                    f"{res_opt['mdd']:.2f}%",
                    f"{win_rate_opt:.1f}%",
                    f"{total_trades_opt}건",
                    w_str
                )
        
        config.console.print(opt_table)

        # [추가] 추천 가중치 출력
        if best_opt_scenario:
            config.console.print(f"\n[green]추천 (수익률):[/] {best_opt_scenario['DESC']} (수익률 {best_opt_return:+.2f}%)")

        # [추가] 단일 백테스팅 AI 진단 연동
        config.console.print()
        if Prompt.ask("🤖 AI 백테스팅 성과 진단 및 최적 파라미터 조언을 받으시겠습니까?", choices=["y", "n"], default="n") == 'y':
            from modules import theme_analysis
            from rich.markdown import Markdown
            from rich.panel import Panel
            from rich.padding import Padding
            
            param_info = f"매수 {buy_score}점/RSI{buy_rsi}, 매도 {sell_score}점, 익절 +{take_profit}%/RSI{take_profit_rsi}, 손절 {stop_loss}%, 트레일링 +{ts_activation}%/-{ts_callback}%, 시간청산 {time_stop_days}일"
            if use_atr_stop: param_info += f", ATR손절 x{atr_mult}"
            
            tp_opt = best_return_set[0] if best_return_set else take_profit
            sl_opt = best_return_set[1] if best_return_set else stop_loss
            w_opt = best_opt_scenario['DESC'] if best_opt_scenario else "기본값"
            
            backtest_info = f"""
            [시뮬레이션 요약 (단일 실행)]
            - 종목: {name} ({code})
            - 기간: {days}일 ({start_date_str} ~ {end_date_str})
            - 현재 적용 파라미터: {param_info}
            - 누적 수익률: {total_return:+.2f}%
            - 승률: {win_rate:.1f}% ({win_trades}승 {loss_trades}패)
            - 평균 수익률 (건당): {avg_profit:+.2f}%
            - 평균 손실률 (건당): {avg_loss:+.2f}%
            - 손익비 (Profit Factor): {pf:.2f}
            - 샤프지수 (Sharpe Ratio): {sharpe_ratio:.2f}
            - 최대 낙폭 (MDD): {mdd:.2f}%
            - [시스템 산출 최적화 추천값]: 매수 {best_return_score}점, RSI < {best_return_rsi}, 익절 +{tp_opt}% / 손절 {sl_opt}%, 가중치: {w_opt}
            """
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=config.console,
                transient=True
            ) as progress:
                progress.add_task(f"[cyan]Google Gemini가 최적 파라미터를 계산하고 분석 중입니다...[/cyan]\n[dim]  (모델: {config.GEMINI_MODEL})[/dim]", total=None)
                answer = theme_analysis.evaluate_backtest_with_gemini(code, name, backtest_info)
                
            if answer:
                if answer.startswith("⚠️"):
                    config.console.print(f"\n{answer}")
                else:
                    md = Markdown(answer)
                    panel = Panel(md, title=f"🤖 AI 단일 백테스팅 성과 진단: {name}({code})", border_style="cyan", padding=(1, 2), width=120)
                    config.console.print()
                    config.console.print(Padding(panel, (0, 4)))
            else:
                config.console.print("[red]진단 결과를 생성하지 못했습니다.[/red]")

        # [이동] 경고 문구 출력 (가장 마지막)
        config.console.print("\n[bold red]경고: 과거의 성과가 미래의 수익을 보장하지는 않습니다.[/bold red]")
        last_choice = sub_choice
        utils.pause()