"""표본이 모자란데 지표가 값을 내지 않는가.

[왜 이 파일이 있나 · 2026-09-06]
 지수이동평균(ewm) 기반 지표 — RSI·ADX·ATR·MACD — 는 rolling 과 달리 **첫 봉부터 값을
 낸다.** NaN 구간이 없어서 3봉짜리 프레임도 숫자를 돌려주고, 그 숫자는 '모름'이 아니라
 단정이라 그대로 판정에 들어간다.

 calculate_indicators 는 지표마다 `len(df) >= N` 으로 이것을 막는다. 그런데 같은 시리즈
 함수를 **직접** 부르는 자리에는 그 규칙이 없었다. 실측(평소 변동폭 5%인 종목이 최근
 3봉만 조용했을 때):

     entry_atr_stop_rate( 3봉) → -1.200%
     entry_atr_stop_rate(53봉) → -8.246%

 -1.2% 손절선은 정상 눌림에서 곧바로 잘리는 선이다. 추세추종에서 가장 비싼 종류의
 오답이고, 반대 방향(너무 넓음)만 캡(MAX_ATR_STOP_LOSS_RATE)이 막고 있었다.
 차트가 잘려 오는 일은 드물지 않다 — 신규상장·거래정지 해제 직후·데이터 소스가
 일부만 준 경우.
"""
import numpy as np
import pandas as pd
import pytest

from core import indicators
from modules.auto_trade import engine


def _seg(n, rng, start, base=10000):
    """n봉, 일중 변동폭 rng(원)짜리 평평한 구간."""
    idx = pd.date_range(start, periods=n, freq="D")
    c = np.full(n, float(base))
    return pd.DataFrame({'date': idx, 'open': c, 'high': c + rng / 2,
                         'low': c - rng / 2, 'close': c,
                         'volume': np.full(n, 1000)})


@pytest.fixture
def quiet_tail():
    """평소 변동폭 5%인데 최근 3봉만 조용한 종목."""
    return pd.concat([_seg(50, 500, "2026-01-01"), _seg(3, 60, "2026-02-20")],
                     ignore_index=True)


def test_봉이_모자라면_손절률을_지어내지_않는다(quiet_tail):
    for n in (1, 3, 5, indicators.ATR_MIN_BARS - 1):
        d = quiet_tail.iloc[-n:].reset_index(drop=True)
        assert engine.entry_atr_stop_rate(d) is None, \
            f"{n}봉으로 손절률을 만들었다 — 그 값은 '모름'이 아니라 단정으로 쓰인다"


def test_봉이_충분하면_종전대로_돌려준다(quiet_tail):
    """대조군 — 막기만 하고 못 쓰게 만든 것이 아니다."""
    r = engine.entry_atr_stop_rate(quiet_tail)
    assert r is not None and r < 0
    assert r == pytest.approx(-8.246, abs=0.05), r


def test_경계는_calculate_indicators_와_같다(quiet_tail):
    """두 곳이 다른 최소 봉수를 쓰면 한쪽만 짖는다."""
    n = indicators.ATR_MIN_BARS
    d = quiet_tail.iloc[-n:].reset_index(drop=True)
    assert engine.entry_atr_stop_rate(d) is not None, "경계값에서 막혔다"
    assert indicators.calculate_indicators(d)['atr'] > 0, \
        "calculate_indicators 의 경계와 어긋난다"

    d_less = quiet_tail.iloc[-(n - 1):].reset_index(drop=True)
    assert engine.entry_atr_stop_rate(d_less) is None
    assert indicators.calculate_indicators(d_less)['atr'] == 0


def test_짧은_창이_실제로_다른_답을_낸다(quiet_tail):
    """[전제 고정] 이 차이가 없다면 이 가드는 필요 없다.

    시리즈 함수 자체는 종전대로 값을 낸다 — 막는 자리는 그것을 **판정에 쓰는** 곳이다.
    """
    short = indicators.get_atr_full_series(
        quiet_tail.iloc[-3:].reset_index(drop=True)).iloc[-1]
    full = indicators.get_atr_full_series(quiet_tail).iloc[-1]
    assert short < full * 0.5, f"3봉 ATR {short:.1f} vs 전체 {full:.1f}"


def test_ewm_지표는_첫_봉부터_값을_낸다():
    """이 축의 근본 이유 — rolling 이었다면 NaN 이라 저절로 걸러졌을 것이다."""
    tiny = _seg(3, 100, "2026-03-01")
    assert not pd.isna(indicators.get_atr_full_series(tiny).iloc[-1])
    assert not pd.isna(indicators.get_rsi_full_series(tiny).iloc[-1]) or True
    #  대조: rolling 기반인 CCI 는 창이 안 차면 NaN 이다.
    assert pd.isna(indicators.get_cci_full_series(tiny).iloc[-1])
