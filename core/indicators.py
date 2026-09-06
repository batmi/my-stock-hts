# indicators.py
import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import config

# ==========================================================
# [52주 밴드] '52주'가 무엇인지 정하는 단 한 곳
# ==========================================================
#  250거래일(tail(250))은 실측상 373일치라 52주보다 8일 넓고, 그 경계 밖 극값이 밴드를
#  통째로 왜곡한다 — TIGER 조선TOP10 이 20.2% → 11.0% 로 바뀐 사고(2026-07-24).
#  그때 modules/analysis 에 _w52_band 를 만들었지만 **화면 경로만 옮겨졌고**, 매수·매도
#  판정을 비롯한 8곳은 옛 창을 그대로 들고 있었다(2026-09-04 전수 확인). 판정이 화면과
#  다른 52주를 보면, 화면에 보이는 근거와 실제로 내려진 결정이 갈린다.
#  api 계층도 부를 수 있도록 최하위(core)에 둔다.
W52_DAYS = 365
W52_MIN_BARS = 200   # 창이 52주를 못 채우면(신규상장·차트 절단) 좁아진 밴드를 그대로 쓰지 않는다

#  지수이동평균(ewm) 기반 지표는 **첫 봉부터 값을 낸다** — rolling 과 달리 NaN 구간이
#  없어서, 3봉짜리 프레임도 숫자를 돌려준다. 그 숫자는 '모름'이 아니라 단정이라 그대로
#  판정에 들어간다. calculate_indicators 는 지표마다 `len(df) >= N` 으로 이것을 막는데,
#  같은 시리즈 함수를 **직접** 부르는 자리에는 그 규칙이 없었다.
#  실측(평소 변동폭 5%인 종목이 최근 3봉만 조용했을 때):
#      3봉 → 손절률 -1.200%      53봉 → 손절률 -8.246%
#  -1.2% 는 정상 눌림에서 곧바로 잘리는 선이다. 추세추종에서 가장 비싼 종류의 오답이다.
ATR_MIN_BARS = 15    # calculate_indicators 의 ATR 가드와 같은 값 — 두 곳이 갈리면 안 된다


def w52_high_low(df, now=None):
    """'최근 365일'(=52주) 구간의 (고가, 저가). 창을 못 채우면 (None, None).

    창의 기준점은 '오늘'이다 — 과거 시점 프레임(백테스트)에는 쓰지 말 것.
    그쪽은 backtest.apply_w52_position 이 봉마다 창을 굴린다.
    """
    from datetime import datetime, timedelta
    try:
        if df is None or getattr(df, 'empty', True) or 'date' not in df.columns:
            return None, None
        base = now or datetime.now()
        cutoff = (base - timedelta(days=W52_DAYS)).strftime('%Y%m%d')
        dates = df['date'].astype(str).str.replace('-', '', regex=False).str[:8]
        win = df[dates >= cutoff]
        if len(win) < W52_MIN_BARS:
            return None, None
        h, l = float(win['high'].max()), float(win['low'].min())
        return (h, l) if h > l > 0 else (None, None)
    except Exception:
        return None, None


def w52_band(df, now=None):
    """52주 고/저 밴드 (h52, l52). 산출 불가 시 (0.0, 0.0).

    365일 창을 못 채우는 경우(신규상장·차트 수신 절단)만 보유 봉 전체로 폴백한다.
    """
    if df is None or getattr(df, 'empty', True):
        return 0.0, 0.0
    h, l = w52_high_low(df, now=now)
    if h is None:
        try:
            h, l = float(df['high'].max()), float(df['low'].min())
        except Exception:
            return 0.0, 0.0
    return h, l


def w52_position(df, price, now=None):
    """현재가의 52주 밴드 내 위치(0~100). 산출 불가하면 0.0.

    판정(점수·상태)과 화면이 **같은 값**을 보게 하는 진입점이다.
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.0
    h, l = w52_band(df, now=now)
    if not (h > l):
        return 0.0
    return (p - l) / (h - l) * 100


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

def get_regime_series(close, params=None):
    """[국면 판정] 이중 EMA 교차 + 추종 확인 규칙을 **시계열 전체**에 대해 산출한다.

    analysis.classify_regime_from_df 가 돌려주던 '마지막 시점 한 점'을 일반화한 것으로,
    각 시점의 값은 그 시점까지의 정보만 사용한다(인과적). 판정식이 두 벌로 갈라지지 않도록
    classify_regime_from_df 도 이 함수의 마지막 원소를 쓴다.

    Returns:
        dict: regime(object ndarray), moved_pct(float ndarray),
              whipsaw(float ndarray, 산출 불가 구간은 NaN), segments(int ndarray)
    """
    params = params or getattr(config, 'MARKET_REGIME_PARAMS', {}) or {}
    fast = int(params.get("REGIME_EMA_FAST", 9))
    slow = int(params.get("REGIME_EMA_SLOW", 41))
    confirm = float(params.get("REGIME_CONFIRM_PCT", 5.0))
    lookback = int(params.get("REGIME_WHIPSAW_LOOKBACK", 8))

    s = pd.Series(close, dtype='float64').reset_index(drop=True).dropna()
    n = len(s)
    if n == 0:
        return {'regime': np.array([], dtype=object), 'moved_pct': np.array([]),
                'whipsaw': np.array([]), 'segments': np.array([], dtype=int)}

    prices = s.values
    ema_f = s.ewm(span=fast, adjust=False).mean().values
    ema_s = s.ewm(span=slow, adjust=False).mean().values
    up = ema_f > ema_s

    # 교차 지점만 뽑아 구간 단위로 처리 — 각 시점의 '현재 구간 시작가' 대비 진행률이 판정 기준
    starts = np.concatenate(([0], np.flatnonzero(up[1:] != up[:-1]) + 1))
    seg_id = np.searchsorted(starts, np.arange(n), side='right') - 1
    p0 = prices[starts][seg_id]
    moved = np.where(p0 > 0, (prices - p0) / np.where(p0 > 0, p0, 1.0) * 100.0, 0.0)

    regime = np.where(up,
                      np.where(moved >= confirm, "Bull", "PendUp"),
                      np.where(moved <= -confirm, "Bear", "PendDown")).astype(object)

    # 완료된 구간의 확인 기준 달성 여부 → 그 시점까지의 휩소율
    done_seg, succ = [], []
    for i in range(len(starts) - 1):
        a, b = starts[i], starts[i + 1]
        pa = prices[a]
        if pa <= 0:
            continue
        seg = prices[a:b]
        ext = ((seg.max() - pa) if up[a] else (seg.min() - pa)) / pa * 100.0
        succ.append(ext >= confirm if up[a] else ext <= -confirm)
        done_seg.append(i)

    whipsaw = np.full(n, np.nan)
    segments = np.zeros(n, dtype=int)
    if done_seg:
        done_seg = np.asarray(done_seg)
        cum = np.concatenate(([0.0], np.cumsum(np.asarray(succ, dtype=float))))
        # 시점 t에서 '완료된' 구간 = 현재 구간(seg_id[t])보다 앞선 구간들
        k = np.searchsorted(done_seg, seg_id, side='left')
        segments = k
        ok = k >= lookback
        if ok.any():
            kk = k[ok]
            whipsaw[ok] = 1.0 - (cum[kk] - cum[kk - lookback]) / float(lookback)

    # 데이터가 EMA 기간에 못 미치는 구간은 판정 불가(호출부는 중립으로 취급)
    if n < slow:
        regime[:] = "Sideways"
    else:
        regime[:slow - 1] = "Sideways"

    return {'regime': regime, 'moved_pct': moved, 'whipsaw': whipsaw, 'segments': segments}


def get_market_filter_blocked(close, ma_period=None, band_pct=None, release_on_bear=None):
    """[시장 필터] 지수 종가 시계열 → 각 시점의 '신규 매수 차단' 여부 (bool Series).

    판정은 SMA 이탈 + 히스테리시스(밴드) 상태 기계다.
      · 종가 < SMA×(1-밴드)  → 차단(약세)으로 전환
      · 종가 > SMA×(1+밴드)  → 해제(정상)로 전환
      · 그 사이(밴드 안)     → 직전 상태 유지
    밴드 0%면 종전과 같은 단순 이탈 판정이 된다. SMA 워밍업 구간은 미차단(판단 유보가 아니라
    '필터 없음'과 동일 — 판단 불가 처리는 호출부의 데이터 부족 검사가 담당한다).

    [결측은 해제가 아니다 · 2026-09-05] 종전에는 SMA 가 NaN 인 봉을 `continue` 로 건너뛰어
      `blocked[i]` 가 초기값 False 로 남았다. 즉 **차단 중이던 상태가 결측 하나로 풀렸다.**
      SMA 는 창 안에 NaN 이 하나만 있어도 그 뒤 ma_period 봉이 통째로 NaN 이므로, 40봉 전
      결측 하나가 오늘의 차단을 해제한다(실측). 지수 최후 폴백(yfinance)이 최신 종가를
      결측으로 주는 일이 잦다는 것은 이 저장소에 이미 적혀 있다(trader.py 시장 필터 주석).
      워밍업 뒤의 결측 구간에서는 **직전 상태를 유지**한다 — 모르는 동안 문을 열지 않는다.
      다만 그것만으로는 '차단으로 넘어갔어야 할 전환'을 놓치는 경우를 못 막으므로,
      마지막 봉의 판정이 성립하는지는 `market_filter_ready` 로 호출부가 함께 본다.

    상태는 가격 이력만의 함수라 매 호출 전체 시계열에서 재계산해도 결과가 같다.
    → 재기동/캐시 만료로 상태가 유실되지 않고, 실매매(trader)와 백테스트가 같은 값을 본다.

    [검증 2026-08-03 / KOSPI·KOSDAQ 2005~2026 + 유니버스 59종목 포트폴리오 234경로]
      단순 이탈(밴드 0%)의 SMA60은 국면 판단이 아니라 '3일짜리 눌림에 매수 중단'으로 동작했다
      (KOSPI 차단 에피소드 연 7.6회·중앙 3.5일·59%가 5일 이하·82%가 지수 상승 중 헛경보).
      밴드 1%를 얹으면 같은 기간에서도 SMA60 대비 CAGR 중앙 +2.78%p·MDD +3.45%p,
      경로 승률 76.9%로 개선된다. 단 짧은 기간에 과한 확인은 역효과라(SMA60±2%는 무개선)
      기간 80일 + 밴드 1% 조합을 기본값으로 쓴다. 상세는 MARKET_FILTER_MA 주석 참조.

    [Bear 해제 / release_on_bear] MARKET_FILTER_RELEASE_ON_BEAR 이 켜져 있으면 국면(EMA9/41)이
      **확정 하락(Bear)** 인 동안에는 밴드 이탈에 따른 차단을 해제한다.
      **기본 OFF이며 2026-08-04 확정 기각됐다** — 지수 기준으로는 Bear 구간의 향후 20일
      수익이 +2.4%로 좋아 보였지만, 종목 단위 워크포워드(전 41종목·39창)에서는 정작 하락장
      창 10개 중 9개에서 지고 MDD가 -9%→-29%로 세 배 깊어졌다. 지수는 평균회귀해도 개별
      종목은 반등 전에 ATR 손절이 먼저 나가고, 그 포지션이 슬롯을 점유해 회복 진입까지 막는다.
      켜지 말 것. 상세·재현 방법은 config.MARKET_FILTER_RELEASE_ON_BEAR 주석 참조.
      Bull·Sideways(판정 불가)에서는 해제하지 않는다 — fail-closed.
    """
    if ma_period is None:
        ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
    if band_pct is None:
        band_pct = getattr(config, 'MARKET_FILTER_BAND', 1.0)
    if release_on_bear is None:
        release_on_bear = getattr(config, 'MARKET_FILTER_RELEASE_ON_BEAR', False)

    close = pd.Series(close, dtype='float64').reset_index(drop=True)
    ma = close.rolling(window=int(ma_period)).mean()
    band = max(0.0, float(band_pct)) / 100.0
    lower = ma * (1 - band)
    upper = ma * (1 + band)

    blocked = np.zeros(len(close), dtype=bool)
    state = False
    warmed = False
    for i in range(len(close)):
        if np.isnan(ma.iat[i]):
            #  워밍업 전이면 '필터 없음'(False), 뒤라면 직전 상태를 유지한다.
            blocked[i] = state if warmed else False
            continue
        warmed = True
        if close.iat[i] < lower.iat[i]:
            state = True
        elif close.iat[i] > upper.iat[i]:
            state = False
        blocked[i] = state

    if release_on_bear:
        regime = get_regime_series(close)['regime']
        if len(regime) == len(blocked):
            # 확정 Bear = 이미 -5% 하락을 소화한 반등 구간 → 밴드 이탈만으로 막지 않는다.
            blocked &= regime != "Bear"

    return pd.Series(blocked, index=range(len(close)))


def market_filter_ready(close, ma_period=None):
    """마지막 봉에서 시장 필터 판정이 **성립하는가**.

    `get_market_filter_blocked` 는 bool 만 돌려주므로 '차단 아님'과 '판정 불가'가
    같은 False 로 보인다. 신규 매수 게이트는 그 둘을 반드시 갈라야 한다 —
    모르면 보류(fail-closed)가 이 시스템의 규칙이다.

    길이만 세는 것으로는 부족하다. SMA 창 안에 결측이 하나만 있어도 그 시점 SMA 는
    NaN 이라, `len(df) >= ma_period` 를 통과하고도 판정은 성립하지 않는다.
    """
    if ma_period is None:
        ma_period = getattr(config, 'MARKET_FILTER_MA', 80)
    try:
        s = pd.Series(close, dtype='float64').reset_index(drop=True)
    except (TypeError, ValueError):
        return False
    n = int(ma_period)
    if len(s) < n or n <= 0:
        return False
    return bool(s.iloc[-n:].notna().all())


def vol_regime_ratio(close, window=None, ref_min=None):
    """[변동성 국면] 지수 실현변동성의 '장기 대비 배율' 시계열 (float Series).

    ATR 손절 캡을 국면에 맞춰 넓히기 위한 척도다. 배율 1.0이 평시,
    2.0이면 지수 변동성이 장기 중앙값의 두 배라는 뜻이다.

    [왜 지수인가 — 종목 ATR을 쓰면 안 되는 이유] 손절폭은 그 종목 ATR×배수다. 캡을 같은
    ATR에 비례시키면 캡은 아무 일도 하지 않는다 — 캡 배수가 손절 배수보다 크면 절대 안
    걸리고, 작으면 항상 걸린다. 장기 ATR로 척도를 늦춰도 마찬가지다(실측 2026-08-09:
    ATR120 기준 캡은 2026년 중앙 -15.1%로 고정값과 같아졌다 — 120봉 EMA가 5개월 전
    국면 전환을 절반밖에 못 따라온다). 지수 변동성은 종목 ATR과 **독립된 시계열**이라
    이 순환을 끊는다.

    [미래를 보지 않는다] 장기 기준은 확장(expanding) 중앙값에 shift(1)을 걸어 그 시점까지의
    정보만 쓴다. 워밍업 구간(ref_min 미만)은 1.0을 돌려준다 = 캡이 고정값 그대로 동작.

    실매매(trader)와 백테스트(backtest.prepare_vol_regime)가 이 함수 하나를 공유한다.
    """
    if window is None:
        window = int(config.SELL_STRATEGY.get("ATR_CAP_VOL_WINDOW", 60))
    if ref_min is None:
        ref_min = int(config.SELL_STRATEGY.get("ATR_CAP_VOL_REF_MIN", 250))

    s = pd.Series(close, dtype='float64').reset_index(drop=True)
    ret = s.pct_change()
    vol = ret.rolling(int(window)).std()
    ref = vol.expanding(min_periods=int(ref_min)).median().shift(1)
    lo = float(config.SELL_STRATEGY.get("ATR_CAP_RATIO_MIN", 0.4))
    hi = float(config.SELL_STRATEGY.get("ATR_CAP_RATIO_MAX", 3.0))
    ratio = (vol / ref).clip(lo, hi)
    return ratio.fillna(1.0)


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

def rolling_trend_quality(close, lookback=None):
    """get_trend_quality를 **전 구간 한 번에** 계산한다(연환산 기울기 × R², 반올림 없음).

    실매매는 후보를 고를 때마다 마지막 한 값만 구하면 되지만, 백테스트·감사는 종목마다
    수천 일치가 필요하다. 매일 polyfit을 부르면 수십만 번이 되므로, 기울기와 R²가
    Σy·Σy²·Σxy만으로 닫힌 형태로 나온다는 점을 이용해 고정 커널 합성곱으로 편다.

    [왜 여기 있나] 종전에는 이 산식이 tools/ 안에 두 벌(배열판·딕트판)로 흩어져 있었고,
     백테스트 엔진은 아예 갖고 있지 않아 순위 재현이 감사자의 손에 달려 있었다
     (그래서 기본 정렬이 실매매와 어긋난 채 오래 남았다). 실매매·백테스트·감사가
     같은 한 벌을 쓰도록 지표 계층으로 올린다. 대조는 verify_trend_quality_parity.

    Args:
        close: 종가 시퀀스(오름차순). Series·ndarray·list 모두 받는다.
        lookback: 회귀 구간. None이면 config의 TREND_QUALITY_LOOKBACK.
    Returns:
        len(close) 길이의 ndarray. 이력이 부족한 앞부분과 계산 불능(0 이하·결측)은 NaN.
    """
    if lookback is None:
        lookback = config.INDICATOR_PARAMS.get("TREND_QUALITY_LOOKBACK", 90)
    L = int(lookback)
    arr = pd.to_numeric(pd.Series(np.asarray(close).ravel()), errors="coerce").to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    if L < 2 or len(arr) < L:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        y = np.log(np.where(arr > 0, arr, np.nan))
    x = np.arange(L, dtype=float)
    Sx, Sxx = x.sum(), float((x * x).sum())
    ones = np.ones(L)
    # np.convolve는 커널을 뒤집으므로 x를 뒤집어 넣어야 Σ(x·y)가 된다.
    Sy = np.convolve(y, ones, mode="valid")
    Syy = np.convolve(y * y, ones, mode="valid")
    Sxy = np.convolve(y, x[::-1], mode="valid")
    den_x = L * Sxx - Sx * Sx
    num = L * Sxy - Sx * Sy
    den_y = L * Syy - Sy * Sy
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = num / den_x
        r2 = np.where(den_y > 0, num ** 2 / (den_x * den_y), 0.0)
    r2 = np.clip(r2, 0.0, 1.0)
    out[L - 1:] = (np.exp(slope * 252) - 1) * 100 * r2
    return out


def trend_quality_map(df, lookback=None):
    """{'YYYYMMDD': 추세품질 or None} — rolling_trend_quality를 날짜로 키한 형태.

    실매매(get_trend_quality)와 같은 소수 2자리 반올림을 적용한다. 동점 가름에 쓰는 값이라
    반올림 자리까지 같아야 '실매매 순위를 재현했다'가 참이 된다.
    """
    vals = rolling_trend_quality(df["close"], lookback)
    out = {}
    for d, v in zip((str(x) for x in df["date"]), vals):
        out[d] = None if not np.isfinite(v) else round(float(v), 2)
    return out


def verify_trend_quality_parity(dfs, lookback=None, sample=8, tol=0.05):
    """롤링판과 실매매판(get_trend_quality)의 마지막 시점 값을 대조해 불일치 수를 돌려준다.

    산식이 어긋나면 백테스트의 '실매매식 동점 가름'이 조용히 거짓이 된다 — 그 상태로 낸
    결론은 전부 계측기 결함이 된다(2026-08-18 기본 정렬 사건). 감사 도구는 시작할 때
    이 값이 0인지 찍고 들어갈 것.
    """
    bad = 0
    for code in list(dfs)[:sample]:
        df = dfs[code]
        ref = get_trend_quality(df, lookback=lookback)
        mine = trend_quality_map(df, lookback).get(str(df["date"].iloc[-1]))
        if ref is None and mine is None:
            continue
        if ref is None or mine is None or abs(ref - mine) > tol:
            bad += 1
    return bad


TREND_QUALITY_BANDS = (
    (0.0,   "하락"),    # 기울기가 음수 — 회귀선이 우하향
    (10.0,  "미검증"),  # 기울기가 미미하거나 R²가 낮음(횡보 끝 급등 포함) — 추세로 검증되지 않음
    (30.0,  "약함"),
    (60.0,  "양호"),
)


# 밴드별 표시색 — 도움말 표(main.show_help)와 종목 표 '분류' 컬럼이 이 하나를 공유한다.
#  두 곳에 따로 두면 한쪽만 고쳐져 같은 값이 다른 색으로 보인다.
TREND_QUALITY_COLORS = {
    "강함": "red", "양호": "green", "약함": "yellow",
    "미검증": "sky_blue3", "하락": "blue", "이력부족": "white",
}


def describe_trend_quality(tq):
    """추세 품질 값 → 운용자용 한 단어 해석.

    값만으로는 크기 감을 잡기 어려워(연환산 기울기 × R²의 곱이라 단위가 직관적이지 않다)
    로그·화면 표시에 곁들인다. 경계는 TREND_QUALITY_BANDS 참조.
    """
    if tq is None:
        return "이력부족"
    for upper, label in TREND_QUALITY_BANDS:
        if tq < upper:
            return label
    return "강함"


def calculate_psar_series(df, af_start=None, af_step=None, af_max=None):
    if af_start is None: af_start = config.INDICATOR_PARAMS["SAR_AF_START"]
    if af_step is None: af_step = config.INDICATOR_PARAMS["SAR_AF_STEP"]
    if af_max is None: af_max = config.INDICATOR_PARAMS["SAR_AF_MAX"]
    
    psar = get_psar_full_series(df, af_start, af_step, af_max)
    return psar[-1] if psar else None

# 20일선이 '우상향'인지 볼 때 몇 봉 전과 비교할 것인가.
#  [왜 1이 아닌가 · 2026-08-29] 전일 대비(1봉)로 보면 하루 등락에 기울기 부호가 뒤집혀,
#   현재가 색상이 **평균 5.8거래일마다** 바뀌었다(39종목 5년 43,792관측, 연 42.2회).
#   추세를 나타내는 라벨이 그 속도로 번복되면 라벨 자체가 정보를 잃는다.
#   5봉으로 늘리면 연 29.2회(8.4거래일마다)로 31% 줄면서 변별력은 그대로다 —
#   빨강 60일 승률 57.9%→58.1%, 주황 60.3% 유지, 60일 평균 수익도 사실상 불변.
#   10봉은 26.2회로 추가 감소가 작고 변별력 이득도 없어 5봉을 쓴다.
#  색상 표시 전용이며 매매 판정에는 들어가지 않는다.
EMA20_SLOPE_LOOKBACK = 5


def calculate_indicators(df):
    indicators = {'ema_5': None, 'ema_20': None, 'ema_60': None, 'ema_120': None, 'rsi': None, 'obv': 0, 'cci': None, 'adx': None, 'plus_di': None, 'minus_di': None, 'atr': 0, 'psar': None, 'obv_trend': False, 'macd': None, 'macd_signal': None, 'macd_hist': None}
    if df is None or df.empty: return indicators

    # [마지막 봉이 결측이면 아무 값도 지어내지 않는다 · 2026-09-05]
    #  아래 가드는 전부 `len(df) >= N`, 즉 **봉의 개수**만 센다. 마지막 봉의 OHLC 가
    #  결측이면 개수는 통과하는데 값이 극단으로 튄다 — 실측(200봉, 마지막 봉 결측):
    #  RSI 100.0 · ADX 100.0 · CCI NaN 이 나온다. 100 은 '모름'이 아니라 **최강 추세·
    #  극단 과매수**라는 단정이고, 그대로 점수·상태 판정에 들어간다.
    #  yfinance 프레임은 결측 행을 그대로 흘려보낸다(api.charts._fetch_yf_index_daily 에
    #  dropna 가 없고, 최신 종가를 결측으로 주는 일이 잦다는 것은 trader.py 시장 필터
    #  주석에 적혀 있다). 모르면 None 이다 — 호출부는 봉이 모자랄 때와 같은 길로 간다.
    try:
        last = df.iloc[-1]
        if any(pd.isna(last.get(c)) for c in ('high', 'low', 'close') if c in df.columns):
            return indicators
    except Exception:
        return indicators

    if len(df) >= 5: indicators['ema_5'] = df['close'].ewm(span=5, adjust=False).mean().iloc[-1]
    if len(df) >= 20:
        ema20_s = df['close'].ewm(span=20, adjust=False).mean()
        indicators['ema_20'] = ema20_s.iloc[-1]
        # 20일선 기울기 판정의 기준점 — EMA20_SLOPE_LOOKBACK 봉 전 값.
        #  창이 모자라면 있는 만큼 뒤로 간다(최소 1봉).
        if len(ema20_s) > 1:
            back = min(EMA20_SLOPE_LOOKBACK, len(ema20_s) - 1)
            indicators['ema_20_slope_ref'] = ema20_s.iloc[-1 - back]
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
        
    if len(df) >= ATR_MIN_BARS:
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

# [박스권] 하루치 거래량 상한(윈도 중앙값 대비). 선물 연결 시리즈는 월물 교체일 하루가
#  구간 거래량의 대부분을 차지한다(2026-09-03 실측: 금 GC=F 67%, 은 SI=F 73%. 주식·ETF 는
#  AAPL 9%·SPY 5%). 그러면 그 하루가 속한 가격 칸 하나로 밸류에어리어 50% 가 이미 채워져
#  확장 루프가 한 번도 돌지 않고, 박스가 그날 가격대에 한 칸으로 못박힌다.
#  중앙값의 몇 배로 잘라내면 주식·ETF 는 사실상 그대로이고 선물만 제자리를 찾는다.
BOX_VOLUME_CAP_MULT = 5.0
# [박스권] 밸류에어리어가 이 칸 수 미만이면 '구간'이 아니라 점이다 — 그리지 않는다.
BOX_MIN_BINS = 2
# [박스권] 현재가가 박스에서 (박스 높이 x 이 배수)보다 멀어졌으면 더는 박스로 설명되는
#  국면이 아니다(추세장). 낡은 구간을 지지·저항인 양 그려두는 편이 더 해롭다.
BOX_MAX_DISTANCE_MULT = 2.0


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
        volumes = np.nan_to_num(df_w['volume'].values).astype(float)
    else:
        volumes = np.ones_like(closes)

    # 하루치 이상 거래량을 잘라낸다(위 BOX_VOLUME_CAP_MULT 주석 참조).
    _positive = volumes[volumes > 0]
    if _positive.size:
        _cap = float(np.median(_positive)) * BOX_VOLUME_CAP_MULT
        if _cap > 0:
            volumes = np.minimum(volumes, _cap)
        
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

    # [무의미한 박스는 그리지 않는다] 한 칸짜리는 구간이 아니고, 현재가가 멀리 떠난
    #  박스는 지지·저항이 아니라 옛 흔적이다. 둘 다 차트에서 오해를 부른다.
    if (upper_idx - lower_idx + 1) < BOX_MIN_BINS:
        return None

    last = float(df['close'].iloc[-1])
    box_height = box_high - box_low
    if box_height > 0:
        distance = max(last - box_high, box_low - last, 0.0)
        if distance > box_height * BOX_MAX_DISTANCE_MULT:
            return None
    if last > box_high:
        status = '상단 돌파'
    elif last < box_low:
        status = '하단 이탈'
    else:
        status = '박스권 내'
        
    return {'high': box_high, 'low': box_low, 'start_idx': s, 'end_idx': end - 1,
            'last': last, 'status': status}

# [추세 채널] 레그가 이보다 짧으면 채널을 만들지 않는다. 몇 봉짜리 회귀는 기울기가
#  잡음에 좌우돼 방향 자체가 뒤집힌다.
# [추세 채널] 회귀에 쓸 최소 봉 수. 이보다 짧으면 기울기가 잡음에 좌우된다.
# [추세 채널] 회귀에 쓸 최소 봉 수. 이보다 짧으면 기울기가 잡음에 좌우된다.
TREND_MIN_LEG_BARS = 10
# [추세 채널] (기울기 × 레그 길이) / 채널 폭. 이 값 미만이면 '채널이 설명하는 움직임보다
#  채널이 더 넓다' = 방향성이 없다는 뜻이므로 추세선 대신 수평 박스를 그린다.
#  실측: 급등 후 재횡보한 종목에서 이 비율이 0.25까지 떨어지며, 그때 채널은 현재가에서
#  완전히 떨어진 선(상단 322,000 / 하단 117,000 vs 현재가 200,000)이 됐다.
TREND_MIN_MOVE_RATIO = 1.0
# [추세 채널] 채널 폭을 정할 때 위·아래로 잘라낼 종가 비율. 급등·급락 당일 종가 한두 개가
#  채널을 통째로 벌리는 것을 막는다(60봉 레그 기준 각 3봉). 실측 폭 20.0% → 14.5%,
#  종가 포함률 100% → 88.2%. 0으로 두면 최고·최저 종가에 그대로 접한다.
TREND_BAND_TRIM = 0.05


def _trend_anchor(hi, lo, start, n):
    """레그 시작점 — 구간 안 최고가·최저가 중 먼저 온 쪽.

    하락 레그면 최고가가 앞서고 상승 레그면 최저가가 앞선다. 방향을 따로 추정하지
    않아도 이 순서만으로 레그와 방향이 함께 정해진다.
    """
    i_hi = start + int(np.argmax(hi[start:]))
    i_lo = start + int(np.argmin(lo[start:]))
    anchor = min(i_hi, i_lo)
    return start if (n - anchor) < TREND_MIN_LEG_BARS else anchor


def _trend_fit(hi, lo, cl, anchor, n):
    """(기울기, 상단절편, 하단절편, 산포폭, 이동폭).

    기울기는 고가 회귀와 저가 회귀 기울기의 평균. 절편은 그 기울기를 고정한 채
    **종가 분포의 상·하 분위수에 접하도록 평행이동**한 값이다. 최고·최저 종가에 그대로
    접하면 급등 당일 종가 하나가 채널을 통째로 벌리므로 양끝 TREND_BAND_TRIM을 잘라낸다.

    넷째 값은 그린 두 선의 간격이 아니라 **잘라내기 전 종가 산포폭**이다. 방향성 판정은
    '추세가 흔들림보다 큰가'를 묻는 것이므로, 분모가 그리기 방식(절사율)에 따라 흔들리면
    안 된다. 분모를 그린 간격으로 두면 절사율만 올려도 채널·박스 판정이 바뀐다.
    """
    xs = np.arange(anchor, n, dtype=float)
    slope = float((np.polyfit(xs, hi[anchor:], 1)[0] + np.polyfit(xs, lo[anchor:], 1)[0]) / 2.0)
    detrended = cl[anchor:] - slope * xs
    up_b = float(np.quantile(detrended, 1.0 - TREND_BAND_TRIM))
    lo_b = float(np.quantile(detrended, TREND_BAND_TRIM))
    spread = float(np.max(detrended) - np.min(detrended))
    return slope, up_b, lo_b, spread, abs(slope) * (n - 1 - anchor)


def get_trend_lines(df, order=None, period=None):
    """최근 추세 레그의 **평행 회귀 채널**(저항선·지지선). 추세가 없으면 수평 박스.

    반환: {'support': (slope, intercept, x_start), 'resistance': (...)} (없으면 빈 dict).
    두 선은 기울기가 같고 절편만 다르다. y = slope * x + intercept (x는 df 인덱스).
    **기울기 0이면 '추세 없음(횡보)'을 뜻한다** — 호출부는 라벨을 그렇게 표기해야 한다.

    [산출]
      ① 레그 시작 = 창 안 최고가·최저가 중 먼저 온 쪽(_trend_anchor).
      ② 기울기 = 레그 구간 고가 회귀와 저가 회귀 기울기의 **평균**.
         고가만 쓰면 위꼬리에, 저가만 쓰면 아래꼬리에 끌린다. 포락선(모든 고가를 담는
         가장 완만한 선)은 극단 꼬리 하나에 끌려 둔해지고 신호가 늦는다
         (실측 KOSPI: 포락선 -85 vs 회귀 -100).
      ③ 절편 = 그 기울기를 고정한 채 **종가**의 상·하 분위수에 접하도록 평행이동.

    [왜 종가 기준인가] 고가·저가에 접하게 하면 장중 꼬리(헛신호) 하나가 채널을 통째로
    벌린다. 종가는 그날 시장 참여자의 합의 가격이라 노이즈가 걸러진다.

    [왜 최고·최저가 아니라 분위수인가] 꼬리를 걸러도 급등·급락 **당일 종가**는 남는다.
    최고·최저 종가에 그대로 접하면 그 하루가 폭을 지배해(실측 가격의 20.0%) 채널이
    '어디서 막히고 받치는가'를 말해 주지 못한다. 위·아래 5%를 잘라내면 폭 14.5%,
    종가 포함률 88.2% — 몇 개는 밖으로 나가되 선이 실제 등락에 붙는다.

    [평행이동 자체를 뺀 안은 기각] 회귀선 자리에 그대로 두면 폭이 4.4%로 좁아지지만
    종가의 34.0%만 담겨 경계선이 아니라 중심선이 된다.

    [레그 오인 방지 — 2단 탐색] 최저가가 창의 왼쪽 끝에 붙으면 레그가 창 전체가 되어
    급등·급락·재횡보가 한 직선에 담긴다(실측: 상단 322,000 / 하단 117,000 vs 현재가
    200,000). 그래서 채널이 방향성 검사(TREND_MIN_MOVE_RATIO)를 통과하지 못하면
    **창 후반부에서 앵커를 다시 찾아** 한 번 더 시도한다.

    [추세 없음 = 수평 박스] 두 번 다 실패하면 방향성이 없는 것이므로, 기울기 0의
    수평선 두 개(직전 절반 구간의 종가 최고·최저)를 돌려준다. 침묵하지 않되 '추세가
    있다'고 거짓말하지도 않는다.
    """
    if period is None: period = config.INDICATOR_PARAMS.get("TREND_PERIOD", 60)
    n = len(df)
    if n < TREND_MIN_LEG_BARS:
        return {}

    period = int(period)
    hi = np.asarray(df['high'].values, dtype=float)
    lo = np.asarray(df['low'].values, dtype=float)
    cl = np.asarray(df['close'].values, dtype=float)

    for win in (period, max(TREND_MIN_LEG_BARS, period // 2)):
        start = max(0, n - win)
        anchor = _trend_anchor(hi, lo, start, n)
        if (n - anchor) < 2:
            continue
        slope, up_b, lo_b, spread, move = _trend_fit(hi, lo, cl, anchor, n)
        if spread > 0 and move / spread >= TREND_MIN_MOVE_RATIO:
            return {'resistance': (slope, up_b, int(anchor)),
                    'support': (slope, lo_b, int(anchor))}

    # 추세 없음 — 직전 절반 구간의 종가 범위를 수평 박스로 보여 준다.
    box_start = max(0, n - max(TREND_MIN_LEG_BARS, period // 2))
    seg = cl[box_start:]
    return {'resistance': (0.0, float(np.max(seg)), int(box_start)),
            'support': (0.0, float(np.min(seg)), int(box_start))}
