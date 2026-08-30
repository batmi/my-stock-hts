"""장중(분봉) 백테스트 경로 — 계측기가 실현 불가능한 체결로 결과를 부풀리지 않는가.

[왜 이 파일인가] 이 프로젝트의 장중 관련 결론(장중 TS가 실매매를 깎는다 · 60m→30m
견고성 · 장중 다이얼 8종 · 피라미딩 체결 시점)은 전부 `run_portfolio` 의 분봉 리플레이와
장중 청산 모사에서 나왔다. 그런데 그 두 경로는 전체 스위트에서 한 번도 실행되지 않았다
(2026-08-30 커버리지 실측: `_intraday_stop_level` 3%). 계측기 결함은 코드 한 줄이 아니라
**그 도구로 내린 판정 전체**를 되돌린다 — 이 프로젝트는 이미 그 대가를 여러 번 치렀다
(동점가름 정렬·make_scale_fn 오염·생존편향 수치 폐기).

[무엇을 고정하나] 수익률 숫자가 아니라 **체결이 실현 가능한가**다.
  ① 청산 체결가는 그날 봉이 실제로 지나간 가격대 안에 있어야 한다.
  ② 장중 모사의 체결가는 그날 시가를 넘지 못한다(갭 하락이면 시가가 곧 체결가다).
  ③ 분봉 리플레이는 '선 위'가 아니라 그 봉의 종가로 체결한다.
  ④ 익일 시가 이연은 그날이 아니라 다음 거래일 시가로 나간다.
  ⑤ 청산선 산식 자기검증(intraday_mismatch)이 0이고, 실제로 작동한다.

[합성 데이터] 네트워크 없이 돈다. 분봉은 일봉 OHLC 를 지나가는 결정적 경로
(시가 → 저가 → 고가 → 종가)로 만든다.
"""
import numpy as np
import pandas as pd
import pytest

import config
from modules import backtest, portfolio_backtest as pbt

SLIP = getattr(config, "SLIPPAGE_RATE", 0.002)
PRICE_EXITS = ("손절", "ATR손절", "트레일링스탑", "본전청산", "이익보호")


def _make_df(seed, n=320, start=10_000.0, drift=0.004):
    """상승 추세 + 큰 노이즈. 노이즈가 커야 장중 청산이 실제로 발생한다."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n).strftime("%Y%m%d").tolist()
    price = start * np.cumprod(1 + drift + rng.normal(0, 0.02, n))
    op = price * (1 + rng.normal(0, 0.002, n))
    hi = price * (1 + np.abs(rng.normal(0, 0.01, n)))
    lo = price * (1 - np.abs(rng.normal(0, 0.01, n)))
    #  [OHLC 정합] 고가는 시가·종가보다 낮을 수 없다. 독립 난수로 만들면 실제로 어긋나
    #  (실측 320일 중 19일에서 시가 > 고가), 체결가 불변식이 계측기가 아니라 **데이터**
    #  때문에 깨진다. 합성 봉도 실재할 수 있는 모양이어야 한다.
    hi = np.maximum.reduce([hi, op, price])
    lo = np.minimum.reduce([lo, op, price])
    df = pd.DataFrame({
        "date": dates, "open": op, "high": hi, "low": lo, "close": price,
        "volume": rng.integers(50_000, 200_000, n).astype(float),
    })
    df = backtest.compute_price_indicators(df)
    df["roll_high_5"] = df["high"].rolling(5, min_periods=1).max()
    df["roll_high_10"] = df["high"].rolling(10, min_periods=1).max()
    return df


def _bars_and_status(dfs):
    """일봉을 지나가는 결정적 장중 경로(시가→저가→고가→종가)와 그 시점 지표."""
    bars, status = {}, {}
    for code, df in dfs.items():
        b, s = {}, {}
        for r in df.to_dict("records"):
            day = str(r["date"])
            o, h, l, c = r["open"], r["high"], r["low"], r["close"]
            path = [("0930", o, max(o, l * 1.001), l, l * 1.002),
                    ("1100", l, h, l, h * 0.999),
                    ("1400", h * 0.999, h, min(h * 0.999, c), c)]
            b[day] = [(t, bo, bh, bl, bc, 1000.0) for t, bo, bh, bl, bc in path]
            atr = float(r.get("ATR", 0) or 0)
            #  (score, sell_check, can_buy, state, state_reason, rsi, w52, atr, close, high)
            s[day] = {t: (0.0, 0.0, False, "보유", "", 50.0, 50.0, atr, bc, bh)
                      for t, _bo, bh, _bl, bc, _bv in b[day]}
        bars[code], status[code] = b, s
    return bars, status


@pytest.fixture(scope="module")
def world():
    dfs = {f"9{i:05d}": _make_df(seed=300 + i) for i in range(8)}
    thr = {"BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
           "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
           "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
           "WEIGHTS": config.SCORING_WEIGHTS}
    status = pbt.precompute_status(dfs, thr)
    dates = sorted({str(d) for df in dfs.values() for d in df["date"]})
    bars, istatus = _bars_and_status(dfs)
    rows = {c: {str(r["date"]): r for r in df.to_dict("records")} for c, df in dfs.items()}
    return {"dfs": dfs, "status": status, "dates": dates, "bars": bars,
            "istatus": istatus, "rows": rows}


@pytest.fixture(scope="module")
def daily_intraday(world):
    """일봉 장중 청산 모사(분봉 없음)."""
    return pbt.run_portfolio(world["dfs"], world["status"], world["dates"],
                             exit_intraday=True)


@pytest.fixture(scope="module")
def bar_replay(world):
    """분봉 리플레이."""
    return pbt.run_portfolio(world["dfs"], world["status"], world["dates"],
                             intraday_bars=world["bars"], intraday_status=world["istatus"])


def _exits(res):
    return [t for t in res["trades"]
            if any(t["reason"].startswith(p) for p in PRICE_EXITS)]


# ───────────────────── 전제: 두 경로가 실제로 돈다 ─────────────────────

def test_both_intraday_paths_actually_execute(daily_intraday, bar_replay):
    """[공허한 통과 방지] 장중 청산이 한 건도 없으면 아래 불변식은 아무것도 재지 않는다."""
    assert daily_intraday["intraday_exits"] > 10
    assert bar_replay["intraday_exits"] > 10


# ───────────────────── ① 실현 가능한 체결가인가 ─────────────────────

@pytest.mark.parametrize("which", ["daily", "bars"])
def test_every_exit_fill_is_inside_the_days_range(world, daily_intraday, bar_replay, which):
    """[핵심] 그날 봉이 지나가지도 않은 가격에 팔면 그 수익률은 허구다."""
    res = daily_intraday if which == "daily" else bar_replay
    for t in _exits(res):
        row = world["rows"][t["code"]].get(t["date"])
        if not row or not t.get("fill"):
            continue
        lo = row["low"] * (1 - SLIP) * 0.999
        assert lo <= t["fill"] <= row["high"] * 1.001, (
            f"{which}: {t['code']} {t['date']} 체결 {t['fill']:.0f} 이 "
            f"봉 범위({row['low']:.0f}~{row['high']:.0f}) 밖이다")


def test_a_gap_down_fills_at_the_open_not_at_the_line(world, daily_intraday):
    """[핵심] 시가가 이미 청산선 아래면 그 시가가 체결가다.

    선에서 체결됐다고 가정하면 갭 하락마다 실매매보다 유리해져 손실이 과소평가된다.
    (선이 시가 아래면 선에서 체결되므로, 어느 쪽이든 '체결가 ≤ 시가'가 성립한다)
    """
    checked = 0
    for t in _exits(daily_intraday):
        row = world["rows"][t["code"]].get(t["date"])
        if not row or not t.get("fill"):
            continue
        assert t["fill"] <= row["open"] * 1.001, (
            f"{t['code']} {t['date']} 체결 {t['fill']:.0f} > 시가 {row['open']:.0f}")
        checked += 1
    assert checked > 10


def test_bar_replay_fills_at_a_bar_close(world, bar_replay):
    """분봉 경로는 '선 위'가 아니라 그 봉의 종가로 체결한다(보수적)."""
    checked = 0
    for t in _exits(bar_replay):
        closes = [b[4] for b in world["bars"][t["code"]].get(t["date"], [])]
        if not closes or not t.get("fill"):
            continue
        assert any(abs(t["fill"] - c * (1 - SLIP)) / c < 0.01 for c in closes), (
            f"{t['code']} {t['date']} 체결 {t['fill']:.0f} 이 어느 봉 종가와도 맞지 않는다")
        checked += 1
    assert checked > 10


# ───────────────────── ② 익일 시가 이연 ─────────────────────

def test_deferred_trailing_exit_leaves_on_the_next_open(world):
    """TS 이연(next_open)은 그날 종가가 아니라 **다음 거래일 시가**로 나간다."""
    res = pbt.run_portfolio(world["dfs"], world["status"], world["dates"],
                            intraday_bars=world["bars"], intraday_status=world["istatus"],
                            bar_ts_defer="next_open")
    ts_exits = [t for t in res["trades"] if t["reason"] == "트레일링스탑"]
    assert ts_exits, "이연 실행에서 TS 청산이 한 건도 없다"
    for t in ts_exits:
        row = world["rows"][t["code"]].get(t["date"])
        if not row or not t.get("fill"):
            continue
        assert abs(t["fill"] - row["open"] * (1 - SLIP)) / row["open"] < 0.01, (
            f"{t['code']} {t['date']} 이연 체결이 시가가 아니다: {t['fill']:.0f}")


# ───────────────────── ③ 청산선 자기검증 ─────────────────────

def test_the_self_check_reports_no_mismatch(daily_intraday, bar_replay):
    """[핵심] 장중 청산선(_intraday_stop_level)과 decide_sell 이 같은 판정을 해야 한다.

    두 산식이 갈라져도 수익률은 그럴듯하게 나오므로 사람 눈으로는 못 잡는다.
    """
    assert daily_intraday["intraday_mismatch"] == 0
    assert bar_replay["intraday_mismatch"] == 0


@pytest.mark.parametrize("mode", ["daily", "bars"])
def test_the_self_check_has_teeth(world, monkeypatch, mode):
    """자기검증이 살아 있는가 — decide_sell 이 '안 판다'고 답하면 불일치가 세어져야 한다.

    (분봉 경로에는 종전에 대조 자체가 없었다. 장중 결론 대부분이 그 경로에서 나오는데도.)
    """
    monkeypatch.setattr(pbt, "decide_sell", lambda **kw: (False, ""))
    kw = ({"exit_intraday": True} if mode == "daily"
          else {"intraday_bars": world["bars"], "intraday_status": world["istatus"]})
    res = pbt.run_portfolio(world["dfs"], world["status"], world["dates"], **kw)
    assert res["intraday_mismatch"] > 0, "청산선이 틀려도 아무 신호가 없다"


# ───────────────────── ④ 재현성 ─────────────────────

def test_the_same_inputs_give_the_same_result(world, bar_replay):
    """계측기는 재현돼야 한다 — 같은 입력에 다른 답이 나오면 비교 자체가 성립하지 않는다."""
    again = pbt.run_portfolio(world["dfs"], world["status"], world["dates"],
                              intraday_bars=world["bars"], intraday_status=world["istatus"])
    assert again["total_return"] == pytest.approx(bar_replay["total_return"])
    assert len(again["trades"]) == len(bar_replay["trades"])
    assert again["intraday_exits"] == bar_replay["intraday_exits"]
