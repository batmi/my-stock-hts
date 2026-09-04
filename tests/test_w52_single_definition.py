"""'52주'가 무엇인지 정하는 곳이 하나인가.

[배경] 2026-07-24 에 52주 창을 `tail(250)`(250거래일 = 실측 373일)에서 365 달력일로
바꾸고 `analysis._w52_band` 를 "단일 진입점"이라 선언했다. 경계 밖 8일의 극값이 밴드를
통째로 왜곡하기 때문이다(TIGER 조선TOP10 20.2% → 11.0%).

그런데 2026-09-04 전수 확인 결과 **화면 경로만 옮겨져 있었다.** 매수·매도 판정
(engine.analyze_buy / analyze_sell), 결론 감시, 자동매매 메뉴, 텔레그램 2곳, 테마 분석,
예약 감시, 액면분할 보정(api) — 여덟 곳이 옛 창을 각자 들고 있었다.

판정이 화면과 다른 52주를 보면 **화면에 보이는 근거와 실제로 내려진 결정이 갈린다.**
w52_pos 는 점수 항목이자 상태 분류 입력이다.
"""
import datetime as dt
import os
import re

import numpy as np
import pandas as pd
import pytest

from core import indicators as ind


NOW = dt.datetime(2026, 7, 10)


def _frame(days=400, spike_at=None, spike=1000.0):
    idx = pd.bdate_range(NOW - dt.timedelta(days=int(days * 1.45)), periods=days)
    c = np.linspace(100.0, 200.0, days)
    if spike_at is not None:
        c[spike_at] = spike
    return pd.DataFrame({"date": idx.strftime("%Y%m%d"),
                         "high": c * 1.01, "low": c * 0.99, "close": c})


def test_a_peak_outside_52_weeks_is_excluded():
    df = _frame(spike_at=0)
    h, _ = ind.w52_band(df, now=NOW)
    assert h < 300, f"52주 밖 극값이 밴드에 들어왔다: {h}"


def test_a_peak_inside_52_weeks_is_included():
    df = _frame(spike_at=-30)
    h, _ = ind.w52_band(df, now=NOW)
    assert h > 900


def test_a_short_history_falls_back_to_everything_held():
    """신규상장·차트 절단 — 좁아진 밴드를 그대로 쓰면 위치가 부풀려진다."""
    df = _frame(days=60)
    h, l = ind.w52_band(df, now=NOW)
    assert h == pytest.approx(float(df["high"].max()))
    assert l == pytest.approx(float(df["low"].min()))


def test_the_position_is_bounded():
    df = _frame()
    h, l = ind.w52_band(df, now=NOW)
    assert ind.w52_position(df, l, now=NOW) == pytest.approx(0.0)
    assert ind.w52_position(df, h, now=NOW) == pytest.approx(100.0)


@pytest.mark.parametrize("bad", [None, "", "없음"])
def test_an_unreadable_price_gives_zero(bad):
    """숫자로 못 읽는 값은 0.0 — 예외를 올리면 판정 경로가 통째로 끊긴다."""
    assert ind.w52_position(_frame(), bad, now=NOW) == 0.0


def test_a_zero_price_keeps_the_previous_semantics():
    """시세 조회 실패(0원)의 처리는 **이번 변경 범위가 아니다.**

    옛 사본들도 `(0 - l52) / (h52 - l52)` 를 그대로 계산해 음수를 냈다. 창 정의만 모으는
    변경이므로 값 규약은 건드리지 않는다 — 다만 예외 없이 유한한 값이어야 한다.
    """
    v = ind.w52_position(_frame(), 0, now=NOW)
    assert np.isfinite(v) and v < 0


def test_an_empty_frame_gives_a_defined_answer():
    empty = pd.DataFrame(columns=["date", "high", "low", "close"])
    assert ind.w52_band(empty) == (0.0, 0.0)
    assert ind.w52_position(empty, 100) == 0.0


def test_analysis_delegates_to_the_core_definition():
    """화면 경로가 자기 사본으로 되돌아가면 다시 갈라진다."""
    from modules import analysis as an

    df = _frame(spike_at=0)

    class _Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    orig = an.datetime
    an.datetime = _Fixed
    try:
        assert an._w52_band(df) == ind.w52_band(df, now=NOW)
        assert an._w52_high_low(df) == ind.w52_high_low(df, now=NOW)
    finally:
        an.datetime = orig


def test_no_one_computes_the_band_by_hand():
    """`tail(250)` 로 고/저를 뽑는 사본이 다시 생기면 여기서 걸린다."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pat = re.compile(r"tail\(250\)")
    band = re.compile(r"\b(h52|l52|real_h52|real_l52)\b")
    hits = []
    for base in ("modules", "core", "api"):
        for dirpath, _, files in os.walk(os.path.join(root, base)):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                lines = open(path, encoding='utf-8').read().splitlines()
                for n, line in enumerate(lines):
                    if line.strip().startswith("#") or not pat.search(line):
                        continue
                    # tail(250) 자체는 차트를 250봉으로 자르는 정상 용법이다.
                    # 그 앞뒤에서 52주 고/저를 뽑고 있으면 사본이다.
                    window = "\n".join(lines[max(0, n - 1):n + 4])
                    if band.search(window):
                        hits.append((os.path.relpath(path, root), n + 1, line.strip()))
    assert not hits, f"52주 밴드를 직접 계산한다 — indicators.w52_band 를 쓸 것: {hits}"
