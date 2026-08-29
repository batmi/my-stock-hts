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
    """[폴백] ind 없이 부르면 추세(EMA20 vs EMA60) × 위치(현재가 vs EMA20) 4구간."""
    assert analysis.price_trend_color(110, 100, 90) == "[red]"      # 강세
    assert analysis.price_trend_color(95, 100, 90) == "[yellow]"    # 눌림목
    assert analysis.price_trend_color(105, 100, 110) == "[orange3]" # 반등 시도
    assert analysis.price_trend_color(95, 100, 110) == "[blue]"     # 약세


def _ind(ema5, slope_ref):
    """색상 판정에 필요한 지표만 담은 최소 dict.

    `ema_20_slope_ref` 는 EMA20_SLOPE_LOOKBACK(5)봉 전의 20일선 값이다 — 전일이 아니다.
    """
    return {'ema_5': ema5, 'ema_20_slope_ref': slope_ref}


def test_price_trend_color_with_ind_five_tiers():
    """[본 규칙] ind 를 주면 5일선·20일선 기울기까지 반영한 5단계로 판정한다."""
    # 과열 — 정배열 + 20일선 이격 110% 이상
    assert analysis.price_trend_color(118, 105, 95, ind=_ind(110, 104.0)) == "[magenta]"
    # 강세 — 완전 정배열 + 20일선 우상향
    assert analysis.price_trend_color(106, 103, 95, ind=_ind(104, 102.5)) == "[red]"
    # 눌림목 — 상승 구조지만 5일선이 20일선 아래 (난색: 추세는 살아 있다)
    assert analysis.price_trend_color(102, 103, 95, ind=_ind(101, 102.5)) == "[orange3]"
    # 반등 시도 — 하락 구조에서 20일선 돌파 + 20일선 턴
    assert analysis.price_trend_color(105, 100, 110, ind=_ind(103, 99.5)) == "[yellow]"
    # 약세 — 하락 구조 관망
    assert analysis.price_trend_color(95, 100, 110, ind=_ind(97, 100.5)) == "[blue]"


def test_structure_never_flips_to_white():
    """중장기 구조가 서 있으면 절대 '판단 보류(흰색)'로 떨어지지 않는다.

    [회귀 · 2026-08-29] 종전 구현은 조건을 평평하게 늘어놓고 아무 데도 걸리지 않으면
    white 로 떨어뜨렸다. 그 '아무 데도'가 넓어 아래 두 상태가 통째로 보류가 됐다.
    화면 색은 국면을 읽는 1차 수단이라 보류가 최빈값이 되면 정보가 죽는다.
    """
    # ① 장기 상승 추세 + 20일선 우상향 + 5일선을 막 회복한 눌림목 반등 초입
    #    (추세추종에서 가장 값진 진입 후보 구간 — 종전에는 white 였다)
    assert analysis.price_trend_color(102, 103, 95, ind=_ind(101, 102.5)) == "[orange3]"
    # ② 완전 정배열인데 20일선이 하루 눌린 상태 (종전에는 white — 하루짜리 휩소)
    assert analysis.price_trend_color(106, 103, 95, ind=_ind(104, 103.5)) == "[orange3]"
    # ③ 하락 구조에서 20일선 위에 있으나 20일선이 아직 하락 (종전에는 white)
    assert analysis.price_trend_color(105, 100, 110, ind=_ind(103, 100.5)) == "[blue]"


def test_white_only_when_ema20_equals_ema60():
    """흰색은 ema20 == ema60 (진짜 혼조) 에만 남는다 — ind 유무와 무관하게."""
    ind = _ind(100, 100)
    assert analysis.price_trend_color(100, 100, 100, ind=ind) == "[white]"
    assert analysis.price_trend_color(100, 100, 100) == "[white]"

    # 구조가 조금이라도 기울면 흰색이 아니다.
    import itertools
    for price, ema5, prev20 in itertools.product((90, 100, 110), (95, 100, 105), (99, 100, 101)):
        for ema20, ema60 in ((100, 99), (99, 100)):
            got = analysis.price_trend_color(price, ema20, ema60, ind=_ind(ema5, prev20))
            assert got != "[white]", (price, ema20, ema60, ema5, prev20, got)


def test_price_trend_color_falls_back_when_ind_lacks_keys():
    """ind 가 와도 ema_5·ema_20_slope_ref 가 없으면 폴백 규칙을 쓴다(예외 없이)."""
    assert analysis.price_trend_color(110, 100, 90, ind={}) == "[red]"
    assert analysis.price_trend_color(110, 100, 90, ind={'ema_5': 105}) == "[red]"
    assert analysis.price_trend_color(95, 100, 110, ind={'ema_20_slope_ref': 99}) == "[blue]"


def test_calculate_indicators_exposes_ema20_slope_ref():
    """색상 판정이 쓰는 기울기 기준점을 지표 계산이 실제로 내놓는지 고정한다."""
    from core import indicators as core_ind
    df = pd.DataFrame({'close': [100 + i for i in range(30)],
                       'high': [101 + i for i in range(30)],
                       'low': [99 + i for i in range(30)],
                       'volume': [1000] * 30})
    ind = core_ind.calculate_indicators(df)
    assert ind.get('ema_20') is not None
    assert ind.get('ema_20_slope_ref') is not None
    # 상승 시계열이므로 20일선은 우상향이어야 한다.
    assert ind['ema_20'] > ind['ema_20_slope_ref']

    # 기준점은 **전일이 아니라 5봉 전**이다.
    ema20_s = df['close'].ewm(span=20, adjust=False).mean()
    assert ind['ema_20_slope_ref'] == pytest.approx(ema20_s.iloc[-1 - core_ind.EMA20_SLOPE_LOOKBACK])
    assert ind['ema_20_slope_ref'] != pytest.approx(ema20_s.iloc[-2])


def test_slope_lookback_survives_short_history():
    """봉이 5개도 안 되게 짧아도 예외 없이 있는 만큼 뒤로 간다."""
    from core import indicators as core_ind
    df = pd.DataFrame({'close': [100 + i for i in range(22)],
                       'high': [101 + i for i in range(22)],
                       'low': [99 + i for i in range(22)],
                       'volume': [1000] * 22})
    ind = core_ind.calculate_indicators(df)
    assert ind.get('ema_20_slope_ref') is not None


def test_one_day_wiggle_no_longer_flips_the_color():
    """[회귀] 하루 등락으로 색이 뒤집히던 번복.

    실측(39종목 5년): 전일 대비 기울기면 색이 평균 5.8거래일마다 바뀌었다(연 42.2회).
    5봉 차분으로 연 29.2회(8.4거래일마다)로 줄면서 변별력은 그대로였다.
    """
    # 20일선이 5봉에 걸쳐 오르는 중인데 어제 하루만 살짝 눌린 상황.
    #  전일 대비였다면 '우상향 아님'이 되어 빨강 → 주황으로 뒤집혔다.
    assert analysis.price_trend_color(106, 103, 95, ind=_ind(104, 101.0)) == "[red]"
    # 5봉 전보다도 낮으면 그때는 진짜로 꺾인 것이다.
    assert analysis.price_trend_color(106, 103, 95, ind=_ind(104, 104.0)) == "[orange3]"


def test_price_trend_color_unavailable_is_dim():
    """산출 불가는 눌림목(노란색)과 구분되는 dim이어야 한다."""
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


def test_uptrend_uses_warm_color_downtrend_uses_yellow():
    """색 배정 — 상승 구조의 조정은 주황(빨강 인접 난색), 하락 구조의 반등은 노랑.

    [2026-08-29] 두 색을 맞바꿨다. 상승 구조가 강세(빨강)에 인접한 난색을 쓰는 편이
    '추세는 살아 있고 잠시 쉬는 중'과 '아직 하락 추세인데 되돌리는 중'을 색만으로
    가르기 쉽다. 판정 조건 자체는 그대로다.
    """
    ind = _ind(101, 102.5)
    # 상승 구조(ema20 > ema60) 의 미달분은 전부 주황이다.
    assert analysis.price_trend_color(102, 103, 95, ind=ind) == "[orange3]"
    assert analysis.price_trend_color(99, 103, 95, ind=ind) == "[orange3]"
    # 하락 구조(ema20 < ema60) 의 반등 시도는 노랑이다.
    assert analysis.price_trend_color(105, 100, 110, ind=_ind(103, 99.5)) == "[yellow]"

    # 상승 구조에서 노랑이, 하락 구조에서 주황이 나오면 안 된다.
    import itertools
    for price, ema5, prev20 in itertools.product((90, 100, 110), (95, 100, 105), (99, 101)):
        up = analysis.price_trend_color(price, 100, 95, ind=_ind(ema5, prev20))
        down = analysis.price_trend_color(price, 100, 110, ind=_ind(ema5, prev20))
        assert up != "[yellow]", (price, ema5, prev20, up)
        assert down != "[orange3]", (price, ema5, prev20, down)


def test_disparity_threshold_comes_from_config():
    """[단일 소스] 과열 임계값을 색상 룰이 따로 박아 두면 표와 색이 갈린다."""
    import config as cfg
    ind = _ind(110, 104.0)
    # 상한을 크게 올리면 같은 입력이 더 이상 과열이 아니어야 한다.
    original = dict(cfg.ANALYSIS_THRESHOLDS)
    try:
        cfg.ANALYSIS_THRESHOLDS["DISPARITY_UPPER"] = 999
        assert analysis.price_trend_color(118, 105, 95, ind=ind) != "[magenta]"
        cfg.ANALYSIS_THRESHOLDS["DISPARITY_UPPER"] = 101
        assert analysis.price_trend_color(118, 105, 95, ind=ind) == "[magenta]"
    finally:
        cfg.ANALYSIS_THRESHOLDS.clear()
        cfg.ANALYSIS_THRESHOLDS.update(original)


def test_overheat_does_not_advise_taking_profit():
    """[정책] 과열은 '신규 진입 자제'까지다 — 익절 권유는 추세추종 정책과 반대다.

    실측(2026-08-29 · 39종목 5년 43,792관측): 보라 구간의 60일 전방 수익은 평균
    +14.21%, 승률 58.3% 로 전 색 중 최고다. 고정 익절은 기본 OFF 이고 주청산은
    샹들리에 TS 이므로, 화면이 익절을 권하면 엔진과 다른 말을 하게 된다.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    help_src = (root / "main.py").read_text(encoding="utf-8")
    rule_src = (root / "modules" / "analysis.py").read_text(encoding="utf-8")
    # 도움말 표에서 과열 행에 '익절'이 다시 들어오면 잡는다.
    overheat_rows = [ln for ln in help_src.splitlines()
                     if "보라색" in ln and "과열" in ln]
    assert overheat_rows, "과열 안내 행을 찾지 못했다"
    for ln in overheat_rows:
        assert "익절" not in ln, f"과열 행이 익절을 권한다: {ln.strip()}"
    assert "익절 고려" not in rule_src
