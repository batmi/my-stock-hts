"""지수/종목 색상 규칙 테스트.

- 이름 색상: 방향성 자산은 국면 룰, 절대 밴드 자산은 밴드 룰 (두 축 분리)
- 값 색상: price_trend_color 단일 소스 + 역방향 자산 반전 + 판정 불가 dim
"""
import re

import pandas as pd
import pytest

import config
from modules import analysis, market


# ==========================================================
# 1. 값 색상 — price_trend_color
# ==========================================================
def test_price_trend_color_basic():
    """정방향 자산: 추세(EMA20 vs EMA60) × 위치(현재가 vs EMA20) 4구간."""
    assert analysis.price_trend_color(110, 100, 90) == "[red]"      # 강세
    assert analysis.price_trend_color(95, 100, 90) == "[white]"     # 눌림목
    assert analysis.price_trend_color(105, 100, 110) == "[orange3]" # 반등 시도
    assert analysis.price_trend_color(95, 100, 110) == "[blue]"     # 약세


def test_price_trend_color_unavailable_is_dim():
    """산출 불가는 눌림목(흰색)과 구분되는 dim이어야 한다."""
    assert analysis.price_trend_color(None, 100, 90) == "[dim]"
    assert analysis.price_trend_color(100, None, 90) == "[dim]"
    assert analysis.price_trend_color(100, 100, None) == "[dim]"
    # 혼조(EMA 동일)는 판정 보류 — 흰색 유지
    assert analysis.price_trend_color(100, 100, 100) == "[white]"


def test_price_trend_color_invert_swaps_axes():
    """역방향 자산: 강세축(red/white) ↔ 약세축(blue/orange3) 반전."""
    assert analysis.price_trend_color(110, 100, 90, invert=True) == "[blue]"
    assert analysis.price_trend_color(95, 100, 90, invert=True) == "[orange3]"
    assert analysis.price_trend_color(105, 100, 110, invert=True) == "[white]"
    assert analysis.price_trend_color(95, 100, 110, invert=True) == "[red]"
    # 산출 불가는 반전해도 dim
    assert analysis.price_trend_color(None, None, None, invert=True) == "[dim]"


def test_inverse_value_indices_membership():
    """반전 대상은 '값이 오를수록 시장에 불리한' 자산에 한정된다."""
    for name in ("VIX (변동성)", "V코스피200", "달러인덱스", "달러환율",
                 "미국채 2년물 금리", "미국채 30년물 금리"):
        assert name in config.INVERSE_VALUE_INDICES
    for name in ("코스피", "나스닥", "SOX (반도체)", "금", "비트코인", "WTI 원유"):
        assert name not in config.INVERSE_VALUE_INDICES


# ==========================================================
# 2. 이름 색상 — 국면 룰 통합 범위
# ==========================================================
def _adaptive_targets():
    """market._process_index_worker 내부 리터럴에서 국면 룰 대상 목록을 추출."""
    src = open("modules/market.py", encoding="utf-8").read()
    block = src.split("adaptive_targets = [", 1)[1].split("]", 1)[0]
    return set(re.findall(r'"([^"]+)"', block))


def test_direction_assets_use_regime_rule():
    """섹터 지수·금/은/구리·암호화폐는 낙폭 룰이 아니라 국면 룰로 색을 입힌다."""
    targets = _adaptive_targets()
    for name in ("SOX (반도체)", "NBI (바이오)", "BKX (은행)", "DJU (유틸/전력)",
                 "DRG (제약)", "DJT (운송)", "XAL (항공)", "XOI (에너지)", "HUI (금광)",
                 "금", "은", "구리", "비트코인", "이더리움", "솔라나", "리플",
                 "UK - FTSE 100", "Europe - STOXX 50"):
        assert name in targets, f"{name}은 국면 룰 대상이어야 한다"


def test_band_assets_excluded_from_regime_rule():
    """수준 자체가 매크로 의미인 자산은 절대 밴드를 유지한다(국면 룰 제외)."""
    targets = _adaptive_targets()
    for name in ("VIX (변동성)", "V코스피200", "달러인덱스", "달러환율",
                 "WTI 원유", "브랜트유", "천연가스", "밀", "미국채 10년물 금리"):
        assert name not in targets, f"{name}은 밴드 룰을 유지해야 한다"


def test_no_drawdown_name_color_branches_left():
    """52주 낙폭으로 지수명 색을 정하던 분기는 모두 제거되었다."""
    src = open("modules/market.py", encoding="utf-8").read()
    body = src.split("adaptive_targets = [", 1)[1]
    assert "high_52_rate >= -" not in body, "낙폭 기반 지수명 색상 분기가 남아 있다"


def test_regime_rule_covers_all_direction_assets():
    """국면 룰 대상은 실제 지수 목록에 존재하는 이름이어야 한다(오타 방지)."""
    unknown = _adaptive_targets() - set(market.INDICES_MAP)
    assert not unknown, f"지수 목록에 없는 이름: {unknown}"


# ==========================================================
# 3. 국면 판정 자체는 자산군과 무관하게 동작
# ==========================================================
@pytest.mark.parametrize("prices,expected", [
    ([100 + i for i in range(120)], "Bull"),      # 꾸준한 상승 → 확정 상승추세
    ([300 - i for i in range(120)], "Bear"),      # 꾸준한 하락 → 확정 하락추세
])
def test_classify_regime_direction(prices, expected):
    df = pd.DataFrame({"close": prices})
    assert analysis.classify_regime_from_df(df)["regime"] == expected


def test_classify_regime_insufficient_data():
    df = pd.DataFrame({"close": [100, 101, 102]})
    assert analysis.classify_regime_from_df(df)["regime"] == "Sideways"
