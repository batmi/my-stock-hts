"""지수/종목 색상 규칙 테스트.

- 이름 색상: 방향성 자산은 국면 룰, 절대 밴드 자산은 밴드 룰 (두 축 분리)
- 값 색상: price_trend_color 단일 소스(자산 무관 동일 문법) + 판정 불가 dim
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
    """추세(EMA20 vs EMA60) × 위치(현재가 vs EMA20) 4구간 — 자산 종류와 무관하게 동일."""
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


def test_value_color_not_inverted_for_any_asset():
    """값 색상 반전은 폐기됐다 — VIX·금리·달러도 값 자체의 방향만 색으로 나타낸다.

    (반전 시절엔 같은 줄의 등락률·52주 고점대비와 색이 엇갈려 읽기 어려웠다)
    """
    assert not hasattr(config, "INVERSE_VALUE_INDICES")
    src = open("modules/market.py", encoding="utf-8").read()
    assert "invert=" not in src, "지수 값 색상에 반전 인자가 다시 들어왔다"


def test_index_table_uses_config_thresholds():
    """지수 표의 RSI·CCI 임계값은 도움말·종목 표와 같은 config 단일 소스여야 한다.

    상수로 두면 사용자가 RSI_UPPER 등을 바꿔도 지수 표만 옛 기준으로 남아 색이 어긋난다.
    """
    src = open("modules/market.py", encoding="utf-8").read()
    for key in ("RSI_UPPER", "RSI_LOWER", "CCI_UPPER", "CCI_LOWER"):
        assert f'INDICATOR_PARAMS["{key}"]' in src, f"{key}가 config에서 오지 않는다"


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
