# indicators.py
import pandas as pd
import numpy as np
import config

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

def get_cci_full_series(df, window=None):
    if window is None: window = config.INDICATOR_PARAMS["CCI_WINDOW"]
    
    tp = (df['high'] + df['low'] + df['close']) / 3
    sma_tp = tp.rolling(window=window).mean()
    mad = tp.rolling(window=window).apply(lambda x: np.abs(x - x.mean()).mean(), raw=False)
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
    return dx.ewm(com=n-1, adjust=False).mean()

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

def calculate_psar_series(df, af_start=None, af_step=None, af_max=None):
    if af_start is None: af_start = config.INDICATOR_PARAMS["SAR_AF_START"]
    if af_step is None: af_step = config.INDICATOR_PARAMS["SAR_AF_STEP"]
    if af_max is None: af_max = config.INDICATOR_PARAMS["SAR_AF_MAX"]
    
    psar = get_psar_full_series(df, af_start, af_step, af_max)
    return psar[-1] if psar else None

def calculate_indicators(df):
    indicators = {'ema_5': None, 'ema_20': None, 'ema_60': None, 'ema_120': None, 'rsi': None, 'obv': 0, 'cci': None, 'adx': None, 'atr': 0, 'psar': None, 'obv_trend': False, 'macd': None, 'macd_signal': None, 'macd_hist': None}
    if df is None or df.empty: return indicators
    
    if len(df) >= 5: indicators['ema_5'] = df['close'].ewm(span=5, adjust=False).mean().iloc[-1]
    if len(df) >= 20: indicators['ema_20'] = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
    if len(df) >= 60: indicators['ema_60'] = df['close'].ewm(span=60, adjust=False).mean().iloc[-1]
    if len(df) >= 120: indicators['ema_120'] = df['close'].ewm(span=120, adjust=False).mean().iloc[-1]

    if len(df) >= 15: 
        rsi_period = config.INDICATOR_PARAMS["RSI_PERIOD"]
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).ewm(com=rsi_period-1, adjust=False).mean()
        loss = -delta.where(delta < 0, 0).ewm(com=rsi_period-1, adjust=False).mean()
        rs = gain / loss
        indicators['rsi'] = 100 - (100 / (1 + rs)).iloc[-1]

    if len(df) >= 2:
        df['obv_change'] = 0
        df.loc[df['close'] > df['close'].shift(1), 'obv_change'] = df['volume']
        df.loc[df['close'] < df['close'].shift(1), 'obv_change'] = -df['volume']
        obv_series = df['obv_change'].cumsum()
        indicators['obv'] = obv_series.iloc[-1]
        obv_period = config.INDICATOR_PARAMS["OBV_MA_PERIOD"]
        if len(df) >= obv_period:
            obv_ma = obv_series.rolling(window=obv_period).mean().iloc[-1]
            if indicators['obv'] > obv_ma: indicators['obv_trend'] = True

    if len(df) >= 20:
        window = config.INDICATOR_PARAMS["CCI_WINDOW"]
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3
        df['sma_tp'] = df['tp'].rolling(window=window).mean()
        df['mad'] = df['tp'].rolling(window=window).apply(lambda x: np.abs(x - x.mean()).mean(), raw=False)
        df['cci'] = 0.0
        mask = df['mad'] != 0
        df.loc[mask, 'cci'] = (df.loc[mask, 'tp'] - df.loc[mask, 'sma_tp']) / (0.015 * df.loc[mask, 'mad'])
        indicators['cci'] = df['cci'].iloc[-1]

    if len(df) >= 28:
        indicators['adx'] = get_adx_full_series(df).iloc[-1]
        
    if len(df) >= 15:
        indicators['atr'] = get_atr_full_series(df).iloc[-1]
        
    if len(df) >= 5:
        indicators['psar'] = calculate_psar_series(df)

    if len(df) >= 26:
        macd, signal, hist = get_macd_full_series(df)
        indicators['macd'] = macd.iloc[-1]
        indicators['macd_signal'] = signal.iloc[-1]
        indicators['macd_hist'] = hist.iloc[-1]

    return indicators
