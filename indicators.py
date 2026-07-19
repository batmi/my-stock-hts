# indicators.py
import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import config

def apply_realtime_price(df, price, market_date=None):
    """차트 마지막 봉(당일 미확정 캔들)의 종가를 실시간 현재가로 덮어쓰고 고가/저가를 보정한다.

    종목분석 메뉴와 시스템 트레이딩이 동일한 당일 시세로 지표를 계산하도록 통일하는 단일 진입점.
    (과거 이 로직이 여러 곳에 복제되며 메뉴 분석↔자동매매 점수 불일치를 유발해 단일화한다.)
    price<=0 이거나 df가 비어 있으면 아무 작업도 하지 않는다. df를 제자리에서 수정하고 반환한다.

    market_date(YYYYMMDD)가 주어지고 마지막 봉 날짜가 그보다 과거이면(깊은 프리마켓 등
    당일 일봉이 아직 캔들 소스에 없는 경우), 마지막 봉을 덮어쓰지 않고 '당일' 봉을 새로
    추가한다. 이렇게 해야 등락률 기준(iloc[-2])이 직전 거래일 종가로 유지되어, 프리마켓에서
    등락률이 하루 밀려(그제 대비) 과장되는 문제를 막는다. market_date=None이면 종전 동작.
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return df
    if p <= 0 or df is None or len(df) == 0:
        return df
    ci = df.columns.get_loc('close')
    hi = df.columns.get_loc('high')
    lo = df.columns.get_loc('low')

    # [프리마켓 보정] 마지막 봉이 '직전 거래일'뿐이면 당일 봉을 새로 추가(덮어쓰지 않음)
    if market_date is not None and 'date' in df.columns:
        last_val = df.iloc[-1]['date']
        # 날짜를 YYYYMMDD 문자열로 정규화해 비교(문자열·Timestamp 양쪽 안전)
        if hasattr(last_val, 'strftime'):
            last_str = last_val.strftime('%Y%m%d')
        else:
            last_str = str(last_val).replace('-', '').replace('/', '')[:8]
        md_str = str(market_date)
        if last_str < md_str:
            new_row = df.iloc[-1].copy()
            # 날짜 컬럼 dtype을 기존과 일치시켜(문자열↔Timestamp) 혼합타입 정렬오류 방지
            new_row['date'] = pd.Timestamp(md_str) if hasattr(last_val, 'strftime') else md_str
            for col in ('open', 'high', 'low', 'close'):
                if col in df.columns:
                    new_row[col] = p
            if 'volume' in df.columns:
                new_row['volume'] = 0
            df.loc[df.index.max() + 1] = new_row
            return df

    df.iloc[-1, ci] = p
    if p > float(df.iloc[-1, hi]): df.iloc[-1, hi] = p
    if p < float(df.iloc[-1, lo]): df.iloc[-1, lo] = p
    return df

def get_psar_full_series(df, af_start=None, af_step=None, af_max=None):
    if af_start is None: af_start = config.INDICATOR_PARAMS["SAR_AF_START"]
    if af_step is None: af_step = config.INDICATOR_PARAMS["SAR_AF_STEP"]
    if af_max is None: af_max = config.INDICATOR_PARAMS["SAR_AF_MAX"]

    length = len(df)
    if length == 0: return [0.0] * length
    
    high, low = df['high'].values, df['low'].values
    psar = [0.0] * length
    bull = True
    af, ep = af_start, high[0]
    psar[0] = low[0]
    
    for i in range(1, length):
        prev_psar = psar[i-1]
        curr_psar = prev_psar + af * (ep - prev_psar)
        
        if bull:
            if low[i] < curr_psar:
                bull, curr_psar, ep, af = False, ep, low[i], af_start
            else:
                if i >= 1: curr_psar = min(curr_psar, low[i-1])
                if i >= 2: curr_psar = min(curr_psar, low[i-2])
                if high[i] > ep: ep, af = high[i], min(af + af_step, af_max)
        else:
            if high[i] > curr_psar:
                bull, curr_psar, ep, af = True, ep, high[i], af_start
            else:
                if i >= 1: curr_psar = max(curr_psar, high[i-1])
                if i >= 2: curr_psar = max(curr_psar, high[i-2])
                if low[i] < ep: ep, af = low[i], min(af + af_step, af_max)
        psar[i] = curr_psar
    return psar

def _rolling_mad(tp, window):
    """롤링 평균절대편차(MAD)를 sliding_window_view로 벡터화 계산.
    (rolling.apply(lambda) 대비 결과는 비트 단위로 동일하며 100배 이상 빠름)"""
    tp_arr = tp.to_numpy(dtype=float)
    mad_arr = np.full(len(tp_arr), np.nan)
    if len(tp_arr) >= window:
        win = sliding_window_view(tp_arr, window)
        mad_arr[window - 1:] = np.abs(win - win.mean(axis=1, keepdims=True)).mean(axis=1)
    return pd.Series(mad_arr, index=tp.index)

def get_cci_full_series(df, window=None):
    if window is None: window = config.INDICATOR_PARAMS["CCI_WINDOW"]

    tp = (df['high'] + df['low'] + df['close']) / 3
    sma_tp = tp.rolling(window=window).mean()
    mad = _rolling_mad(tp, window)
    return (tp - sma_tp) / (0.015 * mad)

def get_rsi_full_series(df, period=None):
    if period is None: period = config.INDICATOR_PARAMS["RSI_PERIOD"]
    
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).ewm(com=period-1, adjust=False).mean()
    loss = -delta.where(delta < 0, 0).ewm(com=period-1, adjust=False).mean()
    return 100 - (100 / (1 + gain/loss))

def get_adx_full_series(df, n=None):
    if n is None: n = config.INDICATOR_PARAMS["ADX_PERIOD"]
    
    temp = df.copy()
    prev_close = temp['close'].shift(1)
    
    tr1 = temp['high'] - temp['low']
    tr2 = (temp['high'] - prev_close).abs()
    tr3 = (temp['low'] - prev_close).abs()
    temp['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    temp['up'] = temp['high'].diff()
    temp['down'] = temp['low'].shift(1) - temp['low']
    
    temp['+dm'] = np.where((temp['up'] > temp['down']) & (temp['up'] > 0), temp['up'], 0.0)
    temp['-dm'] = np.where((temp['down'] > temp['up']) & (temp['down'] > 0), temp['down'], 0.0)
    
    tr_s = temp['tr'].ewm(com=n-1, adjust=False).mean()
    dm_p_s = temp['+dm'].ewm(com=n-1, adjust=False).mean()
    dm_m_s = temp['-dm'].ewm(com=n-1, adjust=False).mean()
    
    di_p = 100 * (dm_p_s / tr_s)
    di_m = 100 * (dm_m_s / tr_s)
    
    dx = 100 * (di_p - di_m).abs() / (di_p + di_m)
    adx = dx.ewm(com=n-1, adjust=False).mean()
    return adx, di_p, di_m

def get_atr_full_series(df, period=None):
    """ATR (Average True Range) 계산"""
    if period is None: period = config.INDICATOR_PARAMS.get("ATR_PERIOD", 14)
    
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.ewm(alpha=1/period, adjust=False).mean()

def get_obv_full_series(df):
    obv = (np.sign(df['close'].diff()).fillna(0) * df['volume']).cumsum()
    return obv

def get_macd_full_series(df, fast=None, slow=None, signal=None):
    if fast is None: fast = config.INDICATOR_PARAMS["MACD_FAST"]
    if slow is None: slow = config.INDICATOR_PARAMS["MACD_SLOW"]
    if signal is None: signal = config.INDICATOR_PARAMS["MACD_SIGNAL"]
    
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist = macd_line - signal_line
    return macd_line, signal_line, macd_hist

def get_trend_quality(df, lookback=None):
    """[추세추종] 회귀 모멘텀 기반 추세 품질 지수 (연환산 기울기 × R², Clenow 모멘텀)

    최근 lookback 거래일의 로그 종가에 선형회귀를 적용해
      - 기울기(연환산 수익률 %): 추세의 '강도'
      - 결정계수 R²           : 추세의 '매끄러움' (지속성의 대리 지표)
    를 곱한 값을 반환한다. 급등락을 반복하다 우연히 정배열에 걸린 종목은 R²가
    낮아 값이 깎이고, 꾸준히 우상향한 주도주가 높은 값을 받는다.
    매수 게이트(점수)와 별개인 '동시 후보 간 우선순위(랭킹)' 전용 지표.

    데이터가 lookback에 못 미치면 None을 반환한다 (검증 이력 부족 → 랭킹 최하순위).
    """
    if lookback is None: lookback = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    try:
        closes = pd.to_numeric(df['close'], errors='coerce').dropna().tail(lookback)
        if len(closes) < lookback:
            return None
        arr = closes.to_numpy(dtype=float)
        if (arr <= 0).any():
            return None
        y = np.log(arr)
        if not np.isfinite(y).all():
            return None
        x = np.arange(len(y), dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        fitted = slope * x + intercept
        ss_res = float(np.sum((y - fitted) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 0.0 if ss_tot <= 0 else max(0.0, 1.0 - ss_res / ss_tot)
        annualized_pct = (np.exp(slope * 252) - 1) * 100
        return round(annualized_pct * r2, 2)
    except Exception:
        return None

def calculate_psar_series(df, af_start=None, af_step=None, af_max=None):
    if af_start is None: af_start = config.INDICATOR_PARAMS["SAR_AF_START"]
    if af_step is None: af_step = config.INDICATOR_PARAMS["SAR_AF_STEP"]
    if af_max is None: af_max = config.INDICATOR_PARAMS["SAR_AF_MAX"]
    
    psar = get_psar_full_series(df, af_start, af_step, af_max)
    return psar[-1] if psar else None

def calculate_indicators(df):
    indicators = {'ema_5': None, 'ema_20': None, 'ema_60': None, 'ema_120': None, 'rsi': None, 'obv': 0, 'cci': None, 'adx': None, 'plus_di': None, 'minus_di': None, 'atr': 0, 'psar': None, 'obv_trend': False, 'macd': None, 'macd_signal': None, 'macd_hist': None}
    if df is None or df.empty: return indicators

    if len(df) >= 5: indicators['ema_5'] = df['close'].ewm(span=5, adjust=False).mean().iloc[-1]
    if len(df) >= 20: indicators['ema_20'] = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
    if len(df) >= 60: indicators['ema_60'] = df['close'].ewm(span=60, adjust=False).mean().iloc[-1]
    if len(df) >= 120: indicators['ema_120'] = df['close'].ewm(span=120, adjust=False).mean().iloc[-1]

    if len(df) >= 15:
        # [일원화] 전체 시리즈 함수에 위임 (동일 수식). 전일 RSI(prev_rsi)도 함께 제공해
        # 호출부마다 반복되던 재계산(고정 com=13)을 제거하고 RSI_PERIOD 설정과 일치시킨다.
        rsi_series = get_rsi_full_series(df)
        indicators['rsi'] = rsi_series.iloc[-1]
        if len(rsi_series) > 1: indicators['prev_rsi'] = rsi_series.iloc[-2]

    if len(df) >= 2:
        obv_series = get_obv_full_series(df)
        indicators['obv'] = obv_series.iloc[-1]
        obv_period = config.INDICATOR_PARAMS["OBV_MA_PERIOD"]
        if len(df) >= obv_period:
            obv_ma = obv_series.ewm(span=obv_period, adjust=False).mean().iloc[-1]
            if indicators['obv'] > obv_ma: indicators['obv_trend'] = True

    if len(df) >= 20:
        window = config.INDICATOR_PARAMS["CCI_WINDOW"]
        tp = (df['high'] + df['low'] + df['close']) / 3
        sma_tp = tp.rolling(window=window).mean()
        mad = _rolling_mad(tp, window)  # [최적화] rolling.apply(lambda) 대비 100배 이상 빠름 (비트 단위 동일)
        cci = pd.Series(0.0, index=df.index)
        mask = mad != 0
        cci[mask] = (tp[mask] - sma_tp[mask]) / (0.015 * mad[mask])
        indicators['cci'] = cci.iloc[-1]
        if len(cci) > 1: indicators['prev_cci'] = cci.iloc[-2]

    if len(df) >= 28:
        adx_series, di_p_series, di_m_series = get_adx_full_series(df)
        indicators['adx'] = adx_series.iloc[-1]
        indicators['plus_di'] = di_p_series.iloc[-1]
        indicators['minus_di'] = di_m_series.iloc[-1]
        
    if len(df) >= 15:
        indicators['atr'] = get_atr_full_series(df).iloc[-1]
        
    if len(df) >= 5:
        indicators['psar'] = calculate_psar_series(df)

    if len(df) >= 26:
        macd, signal, hist = get_macd_full_series(df)
        indicators['macd'] = macd.iloc[-1]
        indicators['macd_signal'] = signal.iloc[-1]
        indicators['macd_hist'] = hist.iloc[-1]
        if len(hist) > 1: indicators['prev_macd_hist'] = hist.iloc[-2]

    return indicators

def get_swing_points(df, order=5):
    """스윙 고점/저점(피봇) 탐색.
    order봉 좌우를 비교해 국소 최고/최저를 찾는다.
    반환: (swing_highs, swing_lows) — 각각 [(index, price), ...]"""
    n = len(df)
    if n < (order * 2 + 1):
        return [], []
    highs = df['high'].values
    lows = df['low'].values
    swing_highs, swing_lows = [], []
    for i in range(order, n - order):
        window_h = highs[i - order:i + order + 1]
        if highs[i] == window_h.max():
            swing_highs.append((i, float(highs[i])))
        window_l = lows[i - order:i + order + 1]
        if lows[i] == window_l.min():
            swing_lows.append((i, float(lows[i])))
    return swing_highs, swing_lows

def detect_recent_box(df, window=None, value_area_pct=None):
    """
    지정 일수 기준 실제 '거래량(Volume)'이 가장 많이 몰려있는 핵심 매물대 구간을 박스로 산출합니다.
    Mode 1/2/3 모든 API 환경에서 동작할 수 있도록 방어 코드가 포함되어 있습니다.
    """
    if window is None: window = config.INDICATOR_PARAMS.get("BOX_PERIOD", 30)
    if value_area_pct is None: value_area_pct = config.INDICATOR_PARAMS.get("BOX_VALUE_AREA_PCT", 50.0) / 100.0

    n = len(df)
    end = n - 1  # 마지막 봉 제외
    if end < 10:
        return None
        
    w = min(window, end)
    s = end - w
    
    df_w = df.iloc[s:end]
    highs = df_w['high'].values
    lows = df_w['low'].values
    closes = df_w['close'].values
    
    # Mode 1/2/3 호환성 처리: volume 데이터가 누락되거나 NaN일 경우의 방어
    if 'volume' in df_w.columns:
        volumes = np.nan_to_num(df_w['volume'].values)
    else:
        volumes = np.ones_like(closes)
        
    min_val = np.min(lows)
    max_val = np.max(highs)
    if min_val == max_val:
        return None
        
    # 매물대(Volume Profile) 계산을 위해 가격을 20개 구간(Bin)으로 분할
    n_bins = 20
    bins = np.linspace(min_val, max_val, n_bins + 1)
    vol_profile = np.zeros(n_bins)
    
    # 각 캔들의 거래량을 해당 캔들 중심가가 속한 구간에 누적
    for h, l, c, v in zip(highs, lows, closes, volumes):
        typical_p = (h + l + c) / 3.0
        idx = np.searchsorted(bins, typical_p) - 1
        idx = max(0, min(idx, n_bins - 1))
        vol_profile[idx] += v
        
    total_vol = np.sum(vol_profile)
    if total_vol == 0:
        return None
        
    # 최대 거래량 구간(Point of Control)
    poc_idx = np.argmax(vol_profile)
    
    # 총 거래량의 value_area_pct(50%)가 집중된 영역(Value Area) 찾기
    target_vol = total_vol * value_area_pct
    current_vol = vol_profile[poc_idx]
    
    lower_idx = poc_idx
    upper_idx = poc_idx
    
    while current_vol < target_vol and (lower_idx > 0 or upper_idx < n_bins - 1):
        vol_down = vol_profile[lower_idx - 1] if lower_idx > 0 else -1
        vol_up = vol_profile[upper_idx + 1] if upper_idx < n_bins - 1 else -1
        
        if vol_up > vol_down:
            upper_idx += 1
            current_vol += vol_up
        else:
            lower_idx -= 1
            current_vol += vol_down
            
    box_low = bins[lower_idx]
    box_high = bins[upper_idx + 1]
    
    last = float(df['close'].iloc[-1])
    if last > box_high:
        status = '상단 돌파'
    elif last < box_low:
        status = '하단 이탈'
    else:
        status = '박스권 내'
        
    return {'high': box_high, 'low': box_low, 'start_idx': s, 'end_idx': end - 1,
            'last': last, 'status': status}

def get_trend_lines(df, order=5, period=None):
    """최근 스윙 저점들을 연결한 상승추세선, 고점들을 연결한 하락추세선을 회귀로 산출.
    반환: {'support': (slope, intercept, x_start), 'resistance': (...)} (없으면 키 생략).
    라인 y값은 slope * x + intercept (x는 df 인덱스), x_start부터 차트 끝까지 그리면 된다."""
    if period is None: period = config.INDICATOR_PARAMS.get("TREND_PERIOD", 60)
    
    # 60일 기준 3개(기본값)의 스윙 포인트를 사용하도록 비례식 적용 (20일당 1개 꼴)
    n_recent = max(2, period // 20)
    
    sh, sl = get_swing_points(df, order)
    result = {}
    for key, pts in (('support', sl), ('resistance', sh)):
        if len(pts) < 2:
            continue
        recent = pts[-n_recent:]
        xs = np.array([i for i, _ in recent], dtype=float)
        ys = np.array([p for _, p in recent], dtype=float)
        slope, intercept = np.polyfit(xs, ys, 1)
        result[key] = (float(slope), float(intercept), int(xs.min()))
    return result
