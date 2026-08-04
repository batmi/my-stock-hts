"""차트 추세선이 최근 흐름과 반대 방향으로 표기되지 않는가.

[증상] 최근 1.5개월 내리 하락한 구간인데 '상승 지지선 / 상승 저항선'이 그려졌다.
실측(KOSPI 2026-01~08, 최근 30봉 -24.6%)에서 두 선 모두 '상승'으로 나왔고,
현재가 6,600 부근인데 저항선은 11,400까지 치솟아 아무 정보가 없었다.

[원인 셋] indicators.get_trend_lines 주석 참조.
  ① 하락 구간에서는 스윙 포인트가 거의 생기지 않는다(order=5가 좌우 5봉 최극값을 요구).
  ② 최근 order봉은 구조적으로 스윙점이 될 수 없다.
  ③ TREND_PERIOD가 '기간'이라는 이름과 달리 개수만 정하고 탐색 범위를 자르지 않아,
     몇 달 전 점으로 만든 선을 현재까지 외삽했다.

[전제] 틀린 선보다 없는 선이 낫다. 방향을 잘못 표기하면 하락 추세를 상승으로 오독한다.
그래서 조건 미달이면 키를 아예 내보내지 않고, 이 파일도 '미표시'를 정상으로 본다.

추세선은 차트 표시 전용이며 매매 판단에는 쓰이지 않는다(호출부는 modules/chart.py 하나).
"""
import numpy as np
import pandas as pd
import pytest

import indicators
from indicators import TREND_MAX_ANCHOR_AGE, get_trend_lines


def _df(closes, wiggle=0.01):
    """종가 배열 → OHLC. 고가/저가를 종가 주변으로 벌려 스윙점이 생기게 한다."""
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({'close': c, 'high': c * (1 + wiggle), 'low': c * (1 - wiggle)})


def _zigzag(start, end, n, amp=0.03, step=6):
    """추세 + 톱니. 스윙 고점/저점이 step봉 주기로 생긴다."""
    base = np.linspace(start, end, n)
    saw = amp * base * np.sin(np.arange(n) * np.pi / (step / 2))
    return base + saw


# ---------------------------------------------------------------------------
# 방향 표기
# ---------------------------------------------------------------------------
def test_downtrend_is_labelled_down():
    """[핵심 회귀] 하락 구간에서 상승선이 나오면 안 된다."""
    df = _df(_zigzag(10_000, 6_000, 120))
    tl = get_trend_lines(df)
    assert tl, "하락 구간에서 추세선이 하나도 산출되지 않았다"
    for key, (slope, _ic, _xs) in tl.items():
        assert slope < 0, f"{key} 기울기가 {slope:+.2f} — 하락장인데 '상승'으로 표기된다"


def test_uptrend_is_labelled_up():
    """대조군 — 상승 구간은 그대로 상승이어야 한다."""
    df = _df(_zigzag(6_000, 10_000, 120))
    tl = get_trend_lines(df)
    assert tl
    for key, (slope, _ic, _xs) in tl.items():
        assert slope > 0, f"{key} 기울기가 {slope:+.2f} — 상승장인데 '하락'으로 표기된다"


def test_reversal_follows_the_recent_leg():
    """상승 후 하락으로 꺾이면, 최근 다리(하락)를 따라가야 한다.

    종전에는 상승 구간의 낡은 스윙점이 계속 쓰여 꺾인 뒤에도 상승선이 남았다.
    """
    df = _df(np.concatenate([_zigzag(6_000, 10_000, 80), _zigzag(10_000, 7_000, 45)]))
    tl = get_trend_lines(df)
    assert tl, "꺾인 뒤 추세선이 전부 사라졌다(과도한 억제)"
    for key, (slope, _ic, _xs) in tl.items():
        assert slope < 0, f"{key}가 꺾이기 전 상승 스윙점을 계속 쓰고 있다({slope:+.2f})"


# ---------------------------------------------------------------------------
# 기간 제한이 실제로 걸리는가
# ---------------------------------------------------------------------------
def test_period_window_is_actually_applied():
    """TREND_PERIOD 밖의 스윙점은 쓰지 않는다(이름과 동작을 일치시킨 부분).

    [중요] 스윙 간격이 촘촘하면 마지막 3점이 어차피 창 안에 들어와 기간 제한이 구속되지
    않는다(변이 검증에서 그대로 통과했다). 간격을 넓혀(step=40) 3점이 창 밖까지
    걸치도록 만들어야 이 테스트가 의미를 갖는다.
    """
    #  대칭 톱니에서는 고점과 저점이 반주기 어긋나 둘 다 '최신'일 수 없다. 앵커 나이
    #  상한에 먼저 걸리면 키가 사라져 루프가 비고, 테스트가 공허하게 통과한다
    #  (변이 검증에서 그렇게 새어나갔다). 두 조건을 동시에 만족하는 저점만 검사한다.
    n, period = 152, 60
    df = _df(_zigzag(6_000, 10_000, n, amp=0.05, step=40))
    _sh, sl = indicators.get_swing_points(df, indicators.TREND_SWING_ORDER)
    assert sl[-3][0] < n - period, "하네스 전제 붕괴: 저점 3점이 모두 창 안이다"
    assert (n - 1 - sl[-1][0]) <= TREND_MAX_ANCHOR_AGE, "하네스 전제 붕괴: 앵커가 이미 낡았다"

    tl = get_trend_lines(df, period=period)
    assert 'support' in tl, "지지선이 사라졌다 — 하네스 전제가 깨졌다"
    x_start = tl['support'][2]
    assert x_start >= n - period, (
        f"지지선이 기간({period}봉) 밖 인덱스 {x_start}에서 시작한다 — 기간 제한이 무시됐다")


#  [중요] 꼬리 길이를 TREND_MAX_ANCHOR_AGE 로 계산하면 안 된다 — 검사 대상 상수로
#   입력을 만들면 상수를 키우는 변이에서 입력도 같이 커져 자기참조가 되고, 변이가
#   검출되지 않는다(실제로 그렇게 새어나갔다). 고정 길이를 쓴다.
_STALE_TAIL = 30


def _stale_anchor_frame():
    """앞: 상승 톱니(스윙점 생성) → 뒤: 단조 급락(스윙점이 전혀 생기지 않음).

    꼬리를 '평탄'하게 두면 값이 모두 같아 lows[i] == min(window) 이 항상 참이 되고,
    모든 봉이 스윙점이 되어 앵커가 낡지 않는다. 단조 감소여야 앞뒤 비교가 전부 실패한다.
    """
    tail = np.linspace(8_000, 5_600, _STALE_TAIL)
    return _df(np.concatenate([_zigzag(6_000, 8_000, 80), tail]), wiggle=0.0005)


def test_stale_anchor_is_not_extrapolated():
    """마지막 스윙점이 낡았으면 그리지 않는다.

    낡은 앵커에서 현재까지 외삽하면 현재가와 동떨어진 선이 나온다
    (실제로 현재가 6,600 종목에 11,400짜리 '상승 저항선'이 그려졌다).
    """
    df = _stale_anchor_frame()
    n = len(df)
    sh, sl = indicators.get_swing_points(df, indicators.TREND_SWING_ORDER)
    # 전제 확인 — 앵커가 실제로 낡았는가(아니면 이 테스트가 아무것도 검증하지 못한다)
    for pts, lab in ((sl, 'support'), (sh, 'resistance')):
        assert pts and (n - 1 - pts[-1][0]) > TREND_MAX_ANCHOR_AGE, (
            f"하네스 전제 붕괴: {lab} 앵커가 낡지 않았다")

    assert get_trend_lines(df) == {}, (
        "낡은 앵커에서 현재까지 외삽한 선이 그려졌다 — 현재가와 동떨어진 선이 된다")


# ---------------------------------------------------------------------------
# 미표시가 정상인 경우
# ---------------------------------------------------------------------------
def test_no_swing_points_draws_nothing():
    """스윙점이 아예 없으면 키 자체를 내보내지 않는다."""
    df = _df(np.linspace(5_000, 5_010, 40), wiggle=0.0001)   # 거의 직선
    assert get_trend_lines(df) == {}


def test_single_swing_point_draws_nothing():
    """[중요] 점 1개로 직선을 적합하면 기울기가 임의로 정해진다 — 그리면 안 된다.

    스윙점 0개짜리 입력만으로는 '2개 미만' 경계를 검증하지 못한다(변이 검증에서
    1개 허용이 그대로 통과했다). V자 반등처럼 저점이 정확히 하나인 형태로 확인한다.
    """
    df = _df(np.concatenate([np.linspace(10_000, 6_000, 40),
                             np.linspace(6_050, 6_800, 8)]), wiggle=0.005)
    _sh, sl = indicators.get_swing_points(df, indicators.TREND_SWING_ORDER)
    assert len(sl) == 1, f"하네스 전제 붕괴: 저점이 {len(sl)}개다"
    assert (len(df) - 1 - sl[0][0]) <= TREND_MAX_ANCHOR_AGE, "하네스 전제 붕괴: 앵커가 낡았다"

    assert get_trend_lines(df) == {}, "스윙점 1개로 추세선을 그렸다"


def test_short_frame_is_safe():
    """데이터가 짧아도 예외 없이 빈 결과를 준다."""
    assert get_trend_lines(_df([100, 101, 102])) == {}


def test_result_shape_is_unchanged():
    """차트 호출부 계약((slope, intercept, x_start))을 깨지 않는다."""
    df = _df(_zigzag(6_000, 10_000, 120))
    for key, val in get_trend_lines(df).items():
        assert key in ('support', 'resistance')
        assert len(val) == 3
        slope, intercept, x_start = val
        assert isinstance(slope, float) and isinstance(intercept, float)
        assert isinstance(x_start, int) and 0 <= x_start < len(df)
