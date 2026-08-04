"""차트 추세선(평행 채널)이 최근 흐름을 제대로 그리는가.

[왜 채널인가] 종전에는 스윙 피봇 3점을 최소자승으로 이어 지지·저항을 **각각 독립으로**
구했다. 그 결과 (a) 두 선의 기울기가 따로 놀고(실측 KOSPI 저항 -57 / 지지 -125),
(b) 선이 피봇들 '사이'를 지나 캔들에 닿지 않았으며, (c) 회귀 시작점이 직전 레그까지
거슬러 올라가 하락장에 '상승 저항선'이 그려졌다.

현재 구현은 ① 추세가 시작된 극값에서 출발해 ② 고가·저가 회귀 기울기의 **평균**을 쓰고
③ 위아래가 평행한 회귀 채널이다.

[검증 근거] tools/audit_trend_lines.py · 22종목 400일 · 지평 10/20/30봉의 '오도율'
(표기 방향이 최근 실제 방향과 반대인 비율): 45.9/43.7/38.1% → 40.1/30.8/20.7%.

[선의 의미] 절편은 **종가** 분포의 상·하 5%(TREND_BAND_TRIM)에 접하도록 평행이동한다.
고가·저가에 접하면 장중 꼬리 하나가, 최고·최저 종가에 접하면 급등 당일 종가 하나가
채널을 통째로 벌린다. 실측 폭 20.0% → 14.5%, 종가 포함률 100% → 88.2%.
평행이동을 아예 빼는 안(회귀선 자리)은 폭 4.4%로 좁지만 종가의 34.0%만 담겨 기각했다.

[항상 표시] 조건 미달로 침묵하지 않는다(운영자가 늘 추세선을 보고 판단하겠다는 요구).
그 대가로 횡보 구간의 오도율이 올라간다 — 위 수치가 그 비용이다.

추세선은 차트 표시 전용이며 매매 판단에는 쓰이지 않는다(호출부는 modules/chart.py 하나).
"""
import numpy as np
import pandas as pd
import pytest

import indicators
from indicators import TREND_MIN_LEG_BARS, get_trend_lines


def _df(closes, wiggle=0.01):
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({'close': c, 'high': c * (1 + wiggle), 'low': c * (1 - wiggle)})


def _leg(start, end, n, amp=0.02, step=8):
    """추세 + 톱니(고점·저점이 생기도록)."""
    base = np.linspace(start, end, n)
    return base + amp * base * np.sin(np.arange(n) * np.pi / (step / 2))


# ---------------------------------------------------------------------------
# 1. 방향
# ---------------------------------------------------------------------------
def test_downtrend_both_lines_point_down():
    """[핵심 회귀] 하락 레그에서 상승선이 나오면 안 된다."""
    tl = get_trend_lines(_df(_leg(10_000, 6_000, 60)))
    assert tl, "하락 레그인데 채널이 나오지 않았다"
    for key, (slope, _b, _x) in tl.items():
        assert slope < 0, f"{key} 기울기 {slope:+.1f} — 하락장인데 '상승'으로 표기된다"


def test_uptrend_both_lines_point_up():
    tl = get_trend_lines(_df(_leg(6_000, 10_000, 60)))
    assert tl
    for key, (slope, _b, _x) in tl.items():
        assert slope > 0, f"{key} 기울기 {slope:+.1f} — 상승장인데 '하락'으로 표기된다"


def test_reversal_follows_the_recent_leg():
    """상승 후 꺾이면 최근 다리(하락)를 따라간다 — 종전 구현이 실패하던 지점."""
    tl = get_trend_lines(_df(np.concatenate([_leg(6_000, 10_000, 70), _leg(10_000, 7_000, 40)])))
    assert tl, "꺾인 뒤 채널이 사라졌다(과도한 억제)"
    for key, (slope, _b, _x) in tl.items():
        assert slope < 0, f"{key}가 꺾이기 전 상승 구간을 따르고 있다({slope:+.1f})"


# ---------------------------------------------------------------------------
# 2. 채널 형태 — 평행하고, 기울기·절편이 규정대로인가
# ---------------------------------------------------------------------------
def test_lines_are_parallel():
    """채널의 정의. 종전에는 두 기울기가 2배 넘게 벌어졌다."""
    tl = get_trend_lines(_df(_leg(10_000, 6_000, 60)))
    assert tl['support'][0] == pytest.approx(tl['resistance'][0]), "두 선이 평행하지 않다"





def test_anchor_is_the_leg_origin_on_a_downtrend():
    """하락 레그의 시작은 최고가다(최저가가 아니라)."""
    df = _df(np.concatenate([_leg(6_000, 10_000, 70), _leg(10_000, 7_000, 40)]))
    x0 = get_trend_lines(df)['resistance'][2]
    win = max(0, len(df) - 60)
    assert x0 == win + int(np.argmax(df['high'].values[win:])), "앵커가 최고가가 아니다"


def test_anchor_is_the_leg_origin_on_an_uptrend():
    df = _df(np.concatenate([_leg(10_000, 6_000, 70), _leg(6_000, 9_000, 40)]))
    x0 = get_trend_lines(df)['support'][2]
    win = max(0, len(df) - 60)
    assert x0 == win + int(np.argmin(df['low'].values[win:])), "앵커가 최저가가 아니다"


def test_anchor_stays_inside_the_period_window():
    """창(TREND_PERIOD) 밖의 극값을 앵커로 잡으면 안 된다."""
    df = _df(np.concatenate([_leg(20_000, 19_000, 60), _leg(9_000, 6_000, 40)]))
    n, period = len(df), 30
    for _k, (_s, _b, x0) in get_trend_lines(df, period=period).items():
        assert x0 >= n - period, f"창({period}봉) 밖 인덱스 {x0}을 앵커로 잡았다"


def test_slope_is_the_mean_of_high_and_low_regressions():
    """기울기는 (d) 고가 회귀와 저가 회귀의 평균이다.

    고가만 쓰면 위꼬리에, 저가만 쓰면 아래꼬리에 끌린다. 어느 한쪽으로 바뀌면
    비대칭이 생기므로 산식을 직접 못박는다.
    """
    df = _df(_leg(10_000, 6_000, 60))
    slope, _b, x0 = get_trend_lines(df)['resistance']
    xs = np.arange(x0, len(df), dtype=float)
    s_hi = np.polyfit(xs, df['high'].values[x0:], 1)[0]
    s_lo = np.polyfit(xs, df['low'].values[x0:], 1)[0]
    assert slope == pytest.approx((s_hi + s_lo) / 2)
    assert slope != pytest.approx(s_hi), "고가 회귀만 쓰고 있다"
    assert slope != pytest.approx(s_lo), "저가 회귀만 쓰고 있다"


def _band(df):
    """(상단선, 하단선, 종가, 앵커) — 채널을 실제 좌표로 펼친다."""
    tl = get_trend_lines(df)
    s, ub, x0 = tl['resistance']
    xs = np.arange(x0, len(df), dtype=float)
    return s * xs + ub, s * xs + tl['support'][1], df['close'].values[x0:], x0


def test_channel_is_shifted_out_to_the_closes():
    """절편은 종가 분포까지 **평행이동**한다 — 회귀선 자리에 두면 중심선이 된다.

    밀지 않는 안은 폭이 4.4%로 좁지만 종가의 34.0%만 담겨, 경계가 아니라 통계적
    중심선이 된다(기각). 밀었다면 대다수 종가가 안에 들어와야 한다.
    """
    # 꼬리를 작게 둔다: 평행이동을 뺀 안의 간격은 곧 평균 고저폭이라, 꼬리가 크면
    # 두 안의 간격이 우연히 같아져 구분이 되지 않는다.
    df = _df(_leg(10_000, 6_000, 60), wiggle=0.005)
    up, dn, cl, x0 = _band(df)
    inside = float(np.mean((cl <= up + 1e-6) & (cl >= dn - 1e-6)))
    assert inside > 0.8, f"종가 포함률 {inside:.0%} — 평행이동이 빠졌다(회귀 중심선)"

    s = up[1] - up[0]
    xs = np.arange(x0, len(df), dtype=float)
    center_gap = float(np.mean(df['high'].values[x0:] - s * xs)
                       - np.mean(df['low'].values[x0:] - s * xs))
    assert (up[0] - dn[0]) > center_gap * 1.5, (
        f"간격 {up[0] - dn[0]:,.0f} vs 회귀 중심선 {center_gap:,.0f} — 밀지 않았다")


def test_one_spike_close_does_not_blow_up_the_channel():
    """[핵심 회귀] 급등 종가 하나가 채널 폭을 지배하면 안 된다.

    꼬리는 종가 기준이라 이미 걸러지지만 급등 **당일 종가**는 남는다. 최고·최저 종가에
    그대로 접하면 그 하루가 폭을 통째로 벌린다 — 같은 입력에서 절사 0은 2.3배, 절사
    5%는 1.06배. 아래 두 단언이 그 대비를 함께 못박는다(뒤엣것이 없으면 '원래 안 벌어진
    입력'을 쓴 것인지 절사가 막은 것인지 구분되지 않는다).
    """
    base = _leg(10_000, 6_000, 60)
    spiked = base.copy()
    spiked[45] = base[45] * 1.12         # 종가 +12% 한 봉(레그 최고가는 아니다)

    def width(closes):
        up, dn, _cl, _x0 = _band(_df(closes))
        return float(up[0] - dn[0])

    assert width(spiked) < width(base) * 1.3, (
        f"급등 종가 하나로 폭이 {width(base):,.0f} → {width(spiked):,.0f}로 벌어졌다")

    old = indicators.TREND_BAND_TRIM
    indicators.TREND_BAND_TRIM = 0.0
    try:
        assert width(spiked) > width(base) * 2.0, (
            "절사를 꺼도 폭이 안 벌어진다 — 이 입력은 절사를 검증하지 못한다")
    finally:
        indicators.TREND_BAND_TRIM = old


def test_extreme_closes_are_allowed_outside_the_channel():
    """절사한 만큼은 밖으로 나간다 — 폭을 좁게 유지하기 위한 의도된 대가다.

    전부 담기게 하면(절사 0) 폭이 14.5% → 20.0%로 되돌아간다.
    """
    df = _df(_leg(10_000, 6_000, 60), wiggle=0.02)
    up, dn, cl, _x0 = _band(df)
    outside = np.sum(cl > up + 1e-6) + np.sum(cl < dn - 1e-6)
    assert outside > 0, "종가가 하나도 밖에 없다 — 최고·최저 접선으로 되돌아갔다"
    assert outside < len(cl) * 0.3, f"{outside}/{len(cl)}봉이 밖 — 너무 많이 잘라냈다"


def test_trend_or_box_verdict_ignores_the_trim_setting():
    """절사율은 **그리는 폭**만 바꾼다 — 채널이냐 박스냐는 바뀌면 안 된다.

    방향성 판정 분모가 그린 간격이면 절사율을 올리는 것만으로 횡보가 추세로 둔갑한다.
    """
    cases = [_df(_leg(10_000, 6_000, 60)),                     # 추세
             _df(np.full(60, 10_000.0) + _leg(0, 0, 60, amp=0))]  # 횡보
    verdicts = {}
    for trim in (0.0, 0.05, 0.25):
        old = indicators.TREND_BAND_TRIM
        indicators.TREND_BAND_TRIM = trim
        try:
            verdicts[trim] = [get_trend_lines(d)['resistance'][0] != 0.0 for d in cases]
        finally:
            indicators.TREND_BAND_TRIM = old
    assert verdicts[0.0] == [True, False], f"기준 판정부터 틀렸다: {verdicts[0.0]}"
    assert verdicts[0.05] == verdicts[0.0] == verdicts[0.25], (
        f"절사율에 따라 채널/박스 판정이 바뀐다: {verdicts}")


# ---------------------------------------------------------------------------
# 4. 추세 없음 — 침묵하지 않되 '추세가 있다'고 거짓말하지도 않는다
# ---------------------------------------------------------------------------
def _spike_then_range():
    """급등 후 재횡보 — 레그가 창 전체로 잡혀 채널이 가격에서 떨어지던 실제 케이스.

    실측 차트에서 이동폭/채널폭이 0.25까지 떨어지며 상단 322,000 / 하단 117,000
    (현재가 200,000)이 그려졌다.
    """
    rng = np.random.default_rng(5)
    cl = np.concatenate([np.linspace(178_000, 168_000, 70),
                         np.linspace(175_000, 310_000, 6),
                         np.linspace(300_000, 205_000, 12),
                         197_000 + rng.normal(0, 7_000, 45)])
    hi, lo = cl * 1.02, cl * 0.98
    hi[73] = 390_000
    lo[76] = 150_000
    return pd.DataFrame({'close': cl, 'high': hi, 'low': lo})


def test_sideways_falls_back_to_a_flat_box():
    """방향성이 없으면 기울기 0의 수평 박스를 돌려준다(침묵하지 않는다)."""
    rng = np.random.default_rng(3)
    tl = get_trend_lines(_df(9_000 + rng.normal(0, 120, 80)))
    assert tl, "횡보에서 아무것도 나오지 않았다 — '항상 표시' 사양 위반"
    assert tl['resistance'][0] == 0.0 and tl['support'][0] == 0.0, (
        "방향성이 없는데 기울기가 붙었다 — 없는 추세를 있다고 표기한다")
    assert tl['resistance'][1] > tl['support'][1]


def test_spike_then_range_does_not_draw_a_runaway_channel():
    """[핵심 회귀] 급등 후 재횡보에 직선을 씌우면 채널이 가격에서 떨어져 나간다."""
    df = _spike_then_range()
    tl = get_trend_lines(df)
    n, last = len(df), df['close'].values[-1]
    s, up_b, x0 = tl['resistance']
    _s, lo_b, _x = tl['support']
    up_end, dn_end = s * (n - 1) + up_b, s * (n - 1) + lo_b
    assert dn_end <= last <= up_end, (
        f"현재가 {last:,.0f}가 채널({dn_end:,.0f}~{up_end:,.0f}) 밖이다")
    assert (up_end - dn_end) < last, "채널 폭이 가격만큼 넓다 — 정보가 없는 선이다"


def test_flat_box_uses_recent_closes_only():
    """수평 박스는 **직전 절반(30봉)** 종가 범위다 — 급등 시절 가격을 끌어오면 안 된다.

    [중요] 반환된 x0으로 구간을 다시 계산해 비교하면 어떤 구간을 쓰든 항상 일치해
    아무것도 검증하지 못한다(변이 검증에서 그대로 통과했다). 기대 구간을 고정해서 쓴다.
    """
    df = _spike_then_range()
    tl = get_trend_lines(df)
    assert tl['resistance'][0] == 0.0, "하네스 전제 붕괴: 횡보로 판정되지 않았다"

    n = len(df)
    recent = df['close'].values[n - 30:]          # period(60)의 절반
    assert tl['resistance'][1] == pytest.approx(recent.max())
    assert tl['support'][1] == pytest.approx(recent.min())
    # 급등 구간(종가 최대 ~310,000)이 섞이면 박스 상단이 크게 부풀어 오른다
    assert tl['resistance'][1] < df['close'].values.max() * 0.8, (
        "박스가 급등 시절 가격까지 끌어왔다")


def test_second_pass_can_rescue_a_real_trend():
    """1차(창 전체)에서 레그를 잘못 잡아도, 후반부 재탐색으로 진짜 추세를 살린다.

    앞쪽에 큰 급등이 있어 창 전체로는 방향성 검사를 통과하지 못하지만, 후반부는
    깨끗한 하락이라 채널이 나와야 한다(수평 박스로 떨어지면 정보를 잃는다).
    """
    df = _df(np.concatenate([_leg(9_000, 30_000, 40, amp=0.01),
                             _leg(30_000, 9_000, 8, amp=0.01),
                             _leg(9_200, 6_000, 40)]))
    tl = get_trend_lines(df)
    assert tl['resistance'][0] < 0, "후반부의 명확한 하락 추세를 놓쳤다"
    assert tl['resistance'][2] >= len(df) - 40, "앵커가 급등 구간까지 거슬러 올라갔다"


def test_short_leg_never_regresses_on_a_handful_of_bars():
    """레그가 짧게 잡히면(조용한 횡보 뒤 급변) 3~4봉 회귀로 기울기를 만들지 않는다.

    미표시로 숨지도 않는다 — 채널이든 수평 박스든 무언가는 나오되, 기울기가 붙은
    선이라면 반드시 최소 레그 길이를 넘겨야 한다.
    """
    rng = np.random.default_rng(11)
    quiet = 9_000 + rng.normal(0, 8, 55)
    spike = np.array([8_600, 9_400, 8_500, 9_600, 10_200])
    df = _df(np.concatenate([quiet, spike]), wiggle=0.002)

    n = len(df)
    win = max(0, n - 60)
    raw_anchor = min(win + int(np.argmax(df['high'].values[win:])),
                     win + int(np.argmin(df['low'].values[win:])))
    assert n - raw_anchor < TREND_MIN_LEG_BARS, "하네스 전제 붕괴: 레그가 이미 충분히 길다"

    tl = get_trend_lines(df)
    assert tl, "짧은 레그에서 미표시로 빠졌다"
    for key, (slope, _b, x0) in tl.items():
        if slope != 0.0:
            assert n - x0 >= TREND_MIN_LEG_BARS, f"{key}가 {n - x0}봉으로 회귀했다"


def test_short_frame_draws_nothing():
    assert get_trend_lines(_df(_leg(10_000, 6_000, TREND_MIN_LEG_BARS - 1))) == {}



def test_result_shape_is_unchanged():
    """차트 호출부 계약((slope, intercept, x_start))을 깨지 않는다."""
    df = _df(_leg(10_000, 6_000, 60))
    for key, val in get_trend_lines(df).items():
        assert key in ('support', 'resistance')
        slope, intercept, x_start = val
        assert isinstance(slope, float) and isinstance(intercept, float)
        assert isinstance(x_start, int) and 0 <= x_start < len(df)


def test_empty_frame_is_safe():
    assert get_trend_lines(pd.DataFrame({'close': [], 'high': [], 'low': []})) == {}
