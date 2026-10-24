"""화면(개별 종목 진단·텔레그램 종목 분석)의 매수·보유 판단은 실매매와 같은 함수로 낸다.

[2026-10-24] 두 화면이 판정을 각자 옮겨 적고 있었다(텔레그램 주석은 '자동매매와 동일한 기준').
  · 체결강도를 못 구하면 화면은 '매수 가능' — 실매매(analyze_buy)는 2026-07-27 부터 보류(fail-closed)
  · 토스에서 체결강도가 없으면 텔레그램은 매도잔량비 검사까지 건너뜀 — 실매매 토스는 그것만으로 판정
  · 개별 룰의 체결강도 0(게이트 끔)을 진단 화면은 무시하고 전역 100% 로 판정
  · 판정 불가('-') 상태를 진단 화면은 '매도(추세이탈)'로 보임 — 실매매는 2026-09-06 부터 점수매도 없음
  · 시장 필터로 신규 매수가 막혀 있어도 화면은 '매수 가능'
 운용자가 '왜 안 샀나'를 보는 창구가 시스템과 다른 규칙을 쓰고 있었다. 판정은 engine.flow_gate ·
 engine.market_filter_status · exit_rules.trend_exit_fires 하나씩이고 실매매·화면이 같이 부른다.
"""
import inspect
import itertools
from unittest.mock import patch

import numpy as np
import pandas as pd

import config
from modules import analysis, exit_rules
from modules.auto_trade import engine
from modules.telegram_bot import TelegramCommander


def _legacy_gate(state, vol_strength, ask_bid_ratio, thresholds, is_overseas, is_toss):
    """2026-10-24 이전 analyze_buy 인라인 블록 그대로 — flow_gate 가 같은 답을 내는지 대조하는 신탁."""
    at = config.ANALYSIS_THRESHOLDS
    if state == "역매수":
        min_vol = thresholds.get("MR_VOL_STRENGTH", at.get("MR_VOL_STRENGTH", 120.0)) if thresholds else at.get("MR_VOL_STRENGTH", 120.0)
    else:
        min_vol = thresholds.get("BUY_VOL_STRENGTH", at.get("BUY_VOL_STRENGTH", 100.0)) if thresholds else at.get("BUY_VOL_STRENGTH", 100.0)
    is_vol_ok, reason = True, ""
    min_ab = thresholds.get("BUY_ASK_BID_RATIO", at.get("BUY_ASK_BID_RATIO", 1.0)) if thresholds else at.get("BUY_ASK_BID_RATIO", 1.0)
    auto = thresholds.get("AUTO_ADJUST_ASK_BID_RATIO", at.get("AUTO_ADJUST_ASK_BID_RATIO", True)) if thresholds else at.get("AUTO_ADJUST_ASK_BID_RATIO", True)
    if auto and min_ab > 0 and min_vol > 0:
        min_ab = round(min_ab * (min_vol / 100.0), 2)
    if vol_strength is not None:
        if vol_strength < min_vol:
            is_vol_ok, reason = False, f"체결:{vol_strength:.1f}%<{min_vol}%"
        elif ask_bid_ratio is not None and min_ab > 0:
            if ask_bid_ratio < min_ab:
                is_vol_ok, reason = False, f"매도비:{ask_bid_ratio:.2f}<{min_ab}"
    elif is_toss:
        if ask_bid_ratio is not None and min_ab > 0 and ask_bid_ratio < min_ab:
            is_vol_ok, reason = False, f"매도비:{ask_bid_ratio:.2f}<{min_ab}"
    elif min_vol > 0 and not is_overseas:
        is_vol_ok, reason = False, "체결강도 미확인(보류)"
    return is_vol_ok, reason, min_vol, min_ab


def test_flow_gate_는_종전_실매매_판정과_비트_동일하다():
    grid = itertools.product(
        ("매수", "강매수", "역매수"),
        (None, 50.0, 99.9, 100.0, 150.0),
        (None, 0.5, 1.0, 1.5),
        (None, {"BUY_VOL_STRENGTH": 0}, {"BUY_VOL_STRENGTH": 120.0, "BUY_ASK_BID_RATIO": 1.2,
                                          "AUTO_ADJUST_ASK_BID_RATIO": False},
         {"MR_VOL_STRENGTH": 150.0, "BUY_ASK_BID_RATIO": 0}),
        (False, True), (False, True))
    n = 0
    for state, vol, ab, thr, overseas, toss in grid:
        got = engine.flow_gate(state, vol, ab, thr, is_overseas=overseas, is_toss=toss)
        assert got == _legacy_gate(state, vol, ab, thr, overseas, toss), (state, vol, ab, thr, overseas, toss)
        n += 1
    assert n == 960


def test_analyze_buy_는_flow_gate_를_부른다():
    assert "flow_gate(" in inspect.getsource(engine.DefaultStrategy.analyze_buy)


# ── 시장 필터 ────────────────────────────────────────────────────────────────────────────────
def _index(trend, n=200):
    closes = 2500 * np.cumprod(np.full(n, 1 + trend))
    dates = pd.date_range("2026-01-01", periods=n, freq="B").strftime("%Y%m%d")
    return pd.DataFrame({"date": dates, "close": closes})


def test_시장_필터_판정은_모르면_보류다():
    assert engine.market_filter_status("KOSPI", df=pd.DataFrame())["unknown"] is True
    short = _index(0.001, n=30)
    st = engine.market_filter_status("KOSPI", df=short)
    assert st["unknown"] is True and st["is_healthy"] is False


def test_시장_필터_판정은_상승_허용_하락_차단():
    up = engine.market_filter_status("KOSPI", df=_index(0.002))
    down = engine.market_filter_status("KOSPI", df=_index(-0.002))
    assert up["unknown"] is False and up["is_healthy"] is True and "filter" in up
    assert down["unknown"] is False and down["is_healthy"] is False


# ── 추세 이탈(점수매도) ─────────────────────────────────────────────────────────────────────
def test_판정_불가_상태는_실매매_규약에서_점수매도를_내지_않는다():
    assert exit_rules.trend_exit_fires("-", 1.0, None, 10000, 4.0) == (False, exit_rules.TREND_EXIT)
    #  백테스트 규약(require_judged=False)은 종전대로 — 그쪽 EMA60 은 0번 봉부터 있어 사실상 닿지 않는다
    assert exit_rules.trend_exit_fires("-", 1.0, None, 10000, 4.0, require_judged=False)[0] is True


def test_매도_상태와_추세_이탈():
    assert exit_rules.trend_exit_fires("매도", 6.0, 9000, 10000, 4.0) == (True, exit_rules.STATE_SELL)
    assert exit_rules.trend_exit_fires("매도", 6.0, 9000, 10000, 4.0, state_sell_always=False)[0] is False
    assert exit_rules.trend_exit_fires("관망", 3.0, 11000, 10000, 4.0)[0] is True      # 60일선 아래
    assert exit_rules.trend_exit_fires("관망", 3.0, 9000, 10000, 4.0)[0] is False      # 60일선 위 — TS 에 위임


# ── 화면이 같은 함수를 부른다 ────────────────────────────────────────────────────────────────
def test_진단_화면과_텔레그램은_공통_판정을_부른다():
    diag = inspect.getsource(analysis.diagnose_stock)
    tg = inspect.getsource(TelegramCommander._analyze_stock)
    for name, src in (("diagnose_stock", diag), ("telegram._analyze_stock", tg)):
        assert "flow_gate(" in src, f"{name}: 수급 게이트를 다시 쓰고 있다"
        assert "market_filter_status(" in src, f"{name}: 시장 필터를 보지 않는다"
        assert "is_buy_vol = True" not in src and "is_vol_ok = True" not in src, f"{name}: 모르면 통과(fail-open) 사본"
    assert "trend_exit_fires(" in diag, "진단 화면의 보유 판단이 청산 체인과 다른 식을 쓴다"


def _diagnose_rows(state, vol_strength, market_healthy=True):
    """진단 화면을 돌려 '시스템 트레이딩 판단 결과' 표의 {항목: (결과, 사유)} 를 돌려준다."""
    n = 100
    df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n).strftime("%Y%m%d"),
                       "close": [10000.0] * n, "high": [10100.0] * n, "low": [9900.0] * n,
                       "open": [10000.0] * n, "volume": [1000] * n})
    ind = {"ema_5": 10000, "ema_20": 9800, "ema_60": None if state == "-" else 9500, "ema_120": 9000,
           "rsi": 60, "adx": 30, "cci": 100, "obv": 1000, "obv_trend": True, "psar": 9000,
           "macd": 50, "macd_signal": 40, "macd_hist": 10, "atr": 100}
    tables = []
    mstat = {"is_healthy": market_healthy, "unknown": False, "current": 2500, "source": None, "filter": {}}
    with patch("modules.analysis.api.get_chart_data", return_value=df), \
            patch("modules.analysis.api.get_realtime_vol_strength", return_value=vol_strength), \
            patch("modules.analysis.api.get_ask_bid_ratio", return_value=2.0), \
            patch("modules.analysis.indicators.calculate_indicators", return_value=ind), \
            patch("modules.analysis.calculate_score", return_value=(8.0 if state != "-" else 1.0, [])), \
            patch("modules.analysis.classify_stock_state", return_value=(state, "[red]", "테스트")), \
            patch("modules.auto_trade.resolve_market_type", return_value="KOSPI"), \
            patch.object(engine, "market_filter_status", return_value=mstat), \
            patch("modules.analysis.db_manager.db.get_stock_strategy", return_value=None), \
            patch.object(config.session, "is_toss", False), \
            patch("rich.prompt.Prompt.ask", return_value="n"), \
            patch("modules.analysis.Progress"), \
            patch("config.console.print", side_effect=lambda *a, **k: tables.extend(a)):
        analysis.diagnose_stock("005930", "삼성전자", False)
    for t in tables:
        if getattr(t, "title", None) == "시스템 트레이딩 판단 결과":
            keys, vals, why = (list(c._cells) for c in t.columns)
            return {k: (str(v), str(w)) for k, v, w in zip(keys, vals, why)}
    raise AssertionError("판단 결과 표가 나오지 않았다")


def test_진단_화면은_체결강도를_모르면_매수_불가로_보인다():
    rows = _diagnose_rows("매수", vol_strength=None)
    assert "매수 불가" in rows["매수 판단"][0], rows["매수 판단"]
    assert "체결강도 미확인" in rows["매수 판단"][1]


def test_진단_화면은_시장_필터가_막으면_매수_불가로_보인다():
    rows = _diagnose_rows("매수", vol_strength=150.0, market_healthy=False)
    assert "매수 불가" in rows["매수 판단"][0]
    assert "시장 필터" in rows["매수 판단"][1]
    assert "보류" in rows["시장 필터"][0]


def test_진단_화면은_조건이_다_맞으면_매수_가능이다():
    rows = _diagnose_rows("매수", vol_strength=150.0)
    assert "매수 가능" in rows["매수 판단"][0], rows["매수 판단"]


def test_진단_화면은_판정_불가_상태를_추세이탈로_보이지_않는다():
    rows = _diagnose_rows("-", vol_strength=150.0)
    assert "매도" not in rows["보유 판단"][0], rows["보유 판단"]
    assert "판정 불가" in rows["보유 판단"][1]
