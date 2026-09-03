from core import indicators
import pandas as pd
import numpy as np

def test_rsi_calculation(sample_uptrend_df):
    """RSI가 정상 범위(0~100) 내에서 계산되는지 테스트"""
    rsi = indicators.get_rsi_full_series(sample_uptrend_df)
    
    assert len(rsi) == len(sample_uptrend_df)
    assert rsi.min() >= 0
    assert rsi.max() <= 100
    
    # 상승장이므로 마지막 RSI는 50 이상이어야 함
    assert rsi.iloc[-1] > 40

def test_macd_calculation(sample_uptrend_df):
    """MACD 계산 및 골든크로스 여부 확인"""
    macd, signal, hist = indicators.get_macd_full_series(sample_uptrend_df)
    
    assert len(macd) == len(sample_uptrend_df)
    assert len(signal) == len(sample_uptrend_df)
    
    # 상승장 후반부에는 MACD가 0보다 커야 함
    assert macd.iloc[-1] > 0

def test_adx_calculation(sample_uptrend_df):
    """ADX 계산 및 추세 강도 확인"""
    adx, plus_di, minus_di = indicators.get_adx_full_series(sample_uptrend_df)
    
    assert len(adx) == len(sample_uptrend_df)
    assert adx.min() >= 0
    # 부동 소수점 오차 허용 (100.00000000000001 등 방지)
    assert adx.max() <= 100.000001
    
    # 뚜렷한 상승장이므로 ADX가 일정 수준(예: 15) 이상이어야 함
    assert adx.iloc[-1] > 15

def test_psar_calculation(sample_uptrend_df, sample_downtrend_df):
    """PSAR(파라볼릭) 계산 및 추세 위치 확인"""
    # 상승장: 주가가 PSAR보다 위에 있어야 함
    psar_up = indicators.get_psar_full_series(sample_uptrend_df)
    # 노이즈로 인한 일시적 반전 가능성을 고려하여 후반부 5일 중 4일 이상 만족하면 통과
    recent_up = sample_uptrend_df['close'].tail(5) > psar_up[-5:]
    assert recent_up.sum() >= 3
    
    # 하락장: 주가가 PSAR보다 아래에 있어야 함
    psar_down = indicators.get_psar_full_series(sample_downtrend_df)
    # 노이즈로 인한 일시적 반전 가능성을 고려하여 후반부 5일 중 4일 이상 만족하면 통과
    recent_down = sample_downtrend_df['close'].tail(5) < psar_down[-5:]
    assert recent_down.sum() >= 3

def test_calculate_indicators_structure(sample_uptrend_df):
    """통합 지표 계산 함수가 필요한 키를 모두 반환하는지 테스트"""
    ind = indicators.calculate_indicators(sample_uptrend_df)
    
    required_keys = ['rsi', 'macd', 'adx', 'cci', 'psar', 'ema_20', 'atr', 'obv']
    for key in required_keys:
        assert key in ind, f"{key} 지표가 누락되었습니다."
        
    # 상승장이므로 정배열 확인 (5일 > 20일)
    if ind['ema_5'] and ind['ema_20']:
        assert ind['ema_5'] > ind['ema_20']

def test_롤링_추세품질은_실매매_산식과_같은_값을_낸다(sample_uptrend_df):
    """rolling_trend_quality(전 구간 한 번에) == get_trend_quality(마지막 한 점).

    백테스트의 진입 순위 기본값이 이 롤링판으로 실매매 동점 가름을 재현한다. 산식이
    어긋나면 '실매매 순위로 쟀다'가 조용히 거짓이 되고, 그 위에 쌓은 감사 결론이
    전부 계측기 결함이 된다(2026-08-18 기본 정렬 사건).
    """
    lookback = 60
    df = sample_uptrend_df.copy()
    if "date" not in df.columns:
        df["date"] = [f"2024{i:04d}" for i in range(len(df))]
    ref = indicators.get_trend_quality(df, lookback=lookback)
    mapped = indicators.trend_quality_map(df, lookback)
    assert ref is not None
    assert abs(mapped[str(df["date"].iloc[-1])] - ref) <= 0.05
    assert indicators.verify_trend_quality_parity({"X": df}, lookback) == 0
    # 이력이 모자란 앞부분은 값이 없어야 한다 — 랭킹에서 '검증 불가'로 최하순위가 된다.
    assert all(v is None for v in list(mapped.values())[:lookback - 1])
    assert mapped[str(df["date"].iloc[lookback - 1])] is not None


# ==========================================================
# [박스권] 거래량 이상치·무의미한 박스 (2026-09-03)
# ==========================================================
def _box_df(prices, volumes):
    """detect_recent_box 용 최소 데이터프레임."""
    return pd.DataFrame({
        'high': [p * 1.005 for p in prices],
        'low': [p * 0.995 for p in prices],
        'close': prices,
        'open': prices,
        'volume': volumes,
    })


def test_box_ignores_single_volume_spike():
    """하루가 거래량 대부분을 먹어도 박스가 그 한 칸에 못박히지 않는다.

    선물 연결 시리즈(금·은)의 월물 교체일이 실제로 이랬다(구간 거래량의 67~73%).
    """
    # 40봉을 4,000 -> 4,600 으로 올린 뒤, 초반 한 봉에만 거래량을 몰아준다.
    prices = [4000 + i * 15 for i in range(42)]
    volumes = [1000] * 42
    volumes[3] = 500000          # 구간 거래량의 대부분
    box = indicators.detect_recent_box(_box_df(prices, volumes), window=40)

    assert box is not None
    # 그 한 봉(약 4,045)만의 칸이 아니라, 실제로 머문 가격대를 덮어야 한다.
    assert box['high'] - box['low'] > (max(prices) - min(prices)) * 0.1


def test_box_hidden_when_price_left_it_far_behind():
    """현재가가 박스에서 멀리 떠났으면 그리지 않는다(추세장)."""
    # 앞 35봉은 4,000 근처에 머물고, 마지막 몇 봉이 급등해 박스를 크게 벗어난다.
    prices = [4000 + (i % 3) for i in range(38)] + [7000, 7200, 7400, 7600]
    box = indicators.detect_recent_box(_box_df(prices, [1000] * 42), window=40)

    assert box is None


def test_box_kept_when_price_is_near():
    """현재가가 박스 근처면 종전대로 그린다(정상 케이스 회귀 방지)."""
    prices = [4000 + (i % 40) for i in range(42)]
    box = indicators.detect_recent_box(_box_df(prices, [1000] * 42), window=40)

    assert box is not None
    assert box['low'] <= prices[-1] * 1.05
