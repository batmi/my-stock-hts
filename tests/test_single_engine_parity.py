"""단일 종목 백테스트 엔진(backtest.simulate_strategy)도 실매매·포트폴리오 엔진과 같은 판정 계층을 쓴다.

[2026-10-24] 이 엔진은 메뉴 4 의 단일 종목 리포트·파라미터 최적화 5종·워크포워드·몬테카를로가 쓴다.
2026-10-16 단계 C 가 청산 체인을 modules/exit_rules.evaluate_exit 하나로 모을 때 이 세 번째 사본이
빠졌고, 다른 두 엔진에서 이미 고친 결함 셋을 그대로 들고 있었다.
  ① 증액할 때 시간청산 시계를 리셋했다 — 실매매는 2026-07-29 부터 진입일 기준(engine.resolve_entry_date),
     포트폴리오 엔진은 2026-08-16 에 고쳤다(pyr_reset_time_stop 기본 False).
  ② 시간청산 '유예'가 elif 체인을 소비해 그날 트레일링 스탑 판정을 건너뛰었다(2026-09-01, 두 곳만 고침).
  ③ 증액 매수 수수료를 잔고에서 빼지 않았다(신규 매수·포트폴리오 엔진은 뺀다).
 그리고 이익보호선(PROFIT_LOCK_USE)을 아예 모델링하지 않아 켜도 조용히 무시했다.
"""
import inspect
from unittest.mock import patch

import pandas as pd
import pytest

import config
from core import trading_cost
from modules import backtest


def _df(closes, highs=None, start="2023-01-02", step_days=1):
    n = len(closes)
    dates = [(pd.Timestamp(start) + pd.Timedelta(days=step_days * i)).strftime("%Y%m%d") for i in range(n)]
    highs = highs if highs is not None else [c + 100 for c in closes]
    return pd.DataFrame({
        "date": dates, "close": closes, "open": closes, "high": highs,
        "low": [c - 100 for c in closes],
        "RSI": [55.0] * n, "ADX": [25.0] * n, "CCI": [100] * n,
        "OBV": [1000] * n, "OBV_MA": [900] * n, "ATR": [1000] * n,
        "SAR": [c - 1000 for c in closes], "EMA60": [1.0] * n,     # 60일선 아래로는 안 떨어진다
    })


_TF_SELL = {"TAKE_PROFIT_RATE": 0.0, "HALF_TAKE_PROFIT_USE": False, "DEFENSIVE_HALF_SELL_USE": False,
            "TAKE_PROFIT_RSI": 0.0, "SELL_SCORE": 4.0, "STOP_LOSS_RATE": -30.0, "USE_ATR_STOP": False,
            "USE_BREAK_EVEN_STOP": False, "PROFIT_LOCK_USE": False, "TIME_STOP_USE": True,
            "TIME_STOP_MIN_PROFIT_RATE": 0.0}
_PYR_ON = {"PYRAMIDING_USE": True, "PYRAMIDING_PROFIT_TRIGGER": 10.0, "PYRAMIDING_RATIO": 0.5,
           "PYRAMIDING_MAX_COUNT": 1, "PYRAMIDING_REQUIRE_HEALTHY_MARKET": False}
_PYR_OFF = {"PYRAMIDING_USE": False}


def _run(df, statuses, sell=None, pyr=None, **kw):
    with patch("modules.backtest.calculate_daily_status", side_effect=statuses), \
            patch.dict(config.ANALYSIS_THRESHOLDS, pyr or _PYR_OFF), \
            patch.dict(config.SELL_STRATEGY, {**_TF_SELL, **(sell or {})}):
        return backtest.simulate_strategy(sim_df=df, prev_row_init=None, initial_capital=10_000_000,
                                          buy_score_limit=8.0, buy_rsi_limit=70.0, is_overseas=False, **kw)


def _types(res):
    return [(t["date"], t["type"]) for t in res["trades"]]


# ------------------------------------------------------------------------------------------------
# ① 증액해도 시간청산 시계는 진입일부터 잰다
# ------------------------------------------------------------------------------------------------
def _pyramid_then_stall():
    #  0일 매수 → 1일 +12%(증액, 평단 ↑) → 이후 평단 아래로 정체(손실) → 시간청산 대상
    closes = [50000, 56000] + [52000] * 12
    statuses = [(9.0, 9.0, True, "매수", "강세"), (9.0, 9.0, True, "매수", "강세")] + \
               [(7.0, 7.0, False, "주의", "")] * 12        # 유예 상태가 아니다(주의)
    return _df(closes), statuses


#  트레일링 스탑이 먼저 무장하지 않게 발동선을 +50%(고정 모드)로 올린다 — 시간청산만 본다.
_TIME_ONLY = {"TIME_STOP_DAYS": 10, "TS_ACTIVATION_MODE": "fixed", "TRAILING_STOP_ACTIVATION_RATE": 50.0}


def test_증액해도_시간청산은_진입일부터_잰다():
    df, statuses = _pyramid_then_stall()
    res = _run(df, statuses, sell=_TIME_ONLY, pyr=_PYR_ON)
    trades = _types(res)
    assert ("20230103", "매수(피라미딩 1차)") in trades
    sells = [(d, t) for d, t in trades if t.startswith("매도")]
    #  진입 20230102 + 10일 = 20230112 에 시간청산(증액일 20230103 + 10일 = 20230113 이 아니다)
    assert sells == [("20230112", "매도(시간청산)")], trades


def test_옛_동작은_명시해야만_재현된다():
    """이전 단일 종목 수치를 재현할 때만 쓴다(포트폴리오 엔진의 pyr_reset_time_stop 과 같은 이름)."""
    df, statuses = _pyramid_then_stall()
    res = _run(df, statuses, sell=_TIME_ONLY, pyr=_PYR_ON, pyr_reset_time_stop=True)
    sells = [(d, t) for d, t in _types(res) if t.startswith("매도")]
    assert sells == [("20230113", "매도(시간청산)")]


# ------------------------------------------------------------------------------------------------
# ② 시간청산이 유예돼도 그날 트레일링 스탑은 판정한다
# ------------------------------------------------------------------------------------------------
def test_시간청산_유예가_트레일링_스탑을_삼키지_않는다():
    #  고정 발동 모드: +10% 무장, 콜백 5%. 15일째 손실(시간청산 조건) + 매수 상태 + 최근 5일 고점 = 10일 고점
    #  (유예 조건)인데, 그날 고점 대비 -12% 라 TS 가 발동해야 한다.
    n = 16
    closes = [50000] + [50000] * 13 + [55500, 49000]
    highs = [50100] * 14 + [56000, 56000]
    statuses = [(9.0, 9.0, True, "매수", "강세")] + [(7.0, 7.0, False, "매수", "")] * (n - 1)
    res = _run(_df(closes, highs=highs), statuses,
               sell={"TIME_STOP_DAYS": 14, "TS_ACTIVATION_MODE": "fixed",
                     "TRAILING_STOP_ACTIVATION_RATE": 10.0, "TRAILING_STOP_CALLBACK_RATE": 5.0})
    sells = [t for _d, t in _types(res) if t.startswith("매도")]
    assert sells == ["매도(트레일링스탑)"], _types(res)


# ------------------------------------------------------------------------------------------------
# ③ 증액 매수 수수료도 잔고에서 뺀다
# ------------------------------------------------------------------------------------------------
def test_증액_매수_수수료를_잔고에서_뺀다():
    df = _df([50000, 56000, 56000])
    statuses = [(9.0, 9.0, True, "매수", "강세"), (9.0, 9.0, True, "매수", "강세"), (7.0, 7.0, False, "상승", "")]
    res = _run(df, statuses, pyr=_PYR_ON)
    buy, pyr = res["trades"][0], res["trades"][1]
    assert pyr["type"] == "매수(피라미딩 1차)"
    cost = pyr["qty"] * pyr["price"]
    assert pyr["balance"] == pytest.approx(buy["balance"] - cost - trading_cost.buy_fee(cost, False))


# ------------------------------------------------------------------------------------------------
# 이익보호선 — 실매매와 같은 체인이라 켜면 그대로 재현된다
# ------------------------------------------------------------------------------------------------
def test_이익보호선을_켜면_재현된다():
    #  +30%(MFE ≥ 25%) 뒤 +14% 로 되돌림 → 보호선 = 매수가 + MFE×0.5 = +15% 아래 → 이익보호 청산
    closes = [50000, 65000, 57000]
    statuses = [(9.0, 9.0, True, "매수", "강세")] + [(7.0, 7.0, False, "상승", "")] * 2
    on = _run(_df(closes, highs=[50100, 65000, 65000]), statuses,
              sell={"PROFIT_LOCK_USE": True, "PROFIT_LOCK_MIN_MFE": 25.0, "PROFIT_LOCK_GIVEBACK": 0.5,
                    "TS_ACTIVATION_MODE": "fixed", "TRAILING_STOP_ACTIVATION_RATE": 50.0})
    assert [t for _d, t in _types(on) if t.startswith("매도")] == ["매도(이익보호)"]
    statuses = [(9.0, 9.0, True, "매수", "강세")] + [(7.0, 7.0, False, "상승", "")] * 2
    off = _run(_df(closes, highs=[50100, 65000, 65000]), statuses,
               sell={"PROFIT_LOCK_USE": False, "TS_ACTIVATION_MODE": "fixed", "TRAILING_STOP_ACTIVATION_RATE": 50.0})
    assert not [t for _d, t in _types(off) if t.startswith("매도")]


# ------------------------------------------------------------------------------------------------
# 구조 — 사본이 다시 생기지 않게
# ------------------------------------------------------------------------------------------------
def test_청산과_증액_판정은_공통_계층을_부른다():
    src = inspect.getsource(backtest.simulate_strategy)
    assert "evaluate_exit(" in src, "청산 판정이 exit_rules.evaluate_exit 를 거치지 않는다"
    assert "pyramid_gate_ok(" in src, "증액 판정이 engine.pyramid_gate_ok 를 거치지 않는다"
    #  옛 인라인 체인의 흔적 — 사유 문자열을 직접 정하면 체인을 다시 쓴 것이다.
    for literal in ('reason = "트레일링스탑"', 'reason = "시간청산"', 'reason = "본전청산"'):
        assert literal not in src, f"인라인 청산 체인이 남아 있다: {literal}"
