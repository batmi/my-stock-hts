"""채점 경계 관측(지시서 5-4) — 판단은 그대로, 경계가 진입을 갈랐는지만 남긴다.

시너지 두 항목(각 1점)은 'MACD 확산 유지'(macd_hist >= prev×0.8) 조건을 공유한다. 경계가
BUY_SCORE 를 가른 경우를 매수 상태가 된 쪽(admitted)과 **후보에서 빠진 쪽(excluded)** 모두
세야 경계의 실제 영향을 잴 수 있다. signal_ledger 는 매수 상태 주기만 세므로 따로 둔다.
"""
import os
import sys
import threading

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from modules import analysis, score_model  # noqa: E402
from modules.auto_trade.engine import DefaultStrategy  # noqa: E402
from modules.db_manager import DBManager  # noqa: E402

BASE = dict(price=120, ema20=100, ema60=95, ema120=90, ema_5=110, sar=80,
            rsi=65, adx=30, cci=-50, prev_cci=-50, obv_trend=True,
            smart_money=False, macd=2, macd_signal=1.2, macd_hist=0.8,
            prev_macd_hist=1, plus_di=20, minus_di=20,
            vol_spike=False, vol_trend=False, w52_pos=95, mom_ret=40,
            mom_ret_1m=5, mom_ret_3m=15, trend_persist=None)
BELOW = dict(BASE, macd_hist=0.7999, macd_signal=1.2001)


@pytest.fixture
def db(tmp_path):
    original = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(tmp_path / "boundary.sqlite")
    manager = DBManager()
    yield manager
    if getattr(getattr(manager, "local", None), "conn", None):
        manager.local.conn.close()
    config.DB_FILE_PATH = original


def _diag(inputs, state):
    _, details = analysis.calculate_score(**inputs)
    kw = {k: v for k, v in inputs.items()}
    return DefaultStrategy._score_boundary_diag(details, None, state, dict(kw, prev_rsi=None, thresholds=None))


def test_boundary_admitted_and_excluded_with_counterfactual_state():
    up = _diag(BASE, "?")
    down = _diag(BELOW, "?")
    assert (up["cross"], up["cf_score"]) == ("admitted", 5.5)
    assert (down["cross"], down["cf_score"]) == ("excluded", 7.5)
    # 반사실 상태는 채점 없이 상태 분류만 다시 돌린 결과다 — 실제 상태 분류와 같은 함수.
    assert up["cf_state"] == analysis.classify_stock_state(score=5.5, **BASE)[0]
    assert down["cf_state"] == analysis.classify_stock_state(score=7.5, **BELOW)[0]
    assert up["near"] and down["near"]


def test_no_cross_when_boundary_does_not_decide_threshold():
    far = dict(BASE, macd_hist=1.5, prev_macd_hist=1.0)   # 비율 1.5 — 경계에서 멀다
    d = _diag(far, "매수")
    #  확산 조건은 여전히 문턱을 가르지만(cross) 경계 효과가 아니므로 반사실 상태는 계산하지 않는다
    assert d["near"] is False and d["cross"] == "admitted" and d["cf_state"] is None
    weak = dict(BELOW, rsi=45, adx=10)                   # 확산이 아니어도 시너지 다른 조건이 없다
    assert _diag(weak, "관망")["cross"] is None


def test_last_breakdown_requires_same_call():
    _, details = analysis.calculate_score(**BASE)
    assert analysis.last_breakdown_for(details) is not None
    analysis.calculate_score(**BELOW)                     # 다른 호출이 끼어들면
    assert analysis.last_breakdown_for(details) is None   # 그 결과를 내주지 않는다
    assert analysis.last_breakdown_for(["가짜"]) is None   # 목(mock)이 돌려준 설명이면 None


def test_last_breakdown_is_per_thread():
    _, details = analysis.calculate_score(**BASE)
    seen = {}

    def other():
        seen["res"] = analysis.last_breakdown_for(details)
    t = threading.Thread(target=other)
    t.start(); t.join()
    assert seen["res"] is None
    assert analysis.last_breakdown_for(details) is not None


def test_diag_failure_never_raises(monkeypatch):
    monkeypatch.setattr(score_model, "boundary_event", lambda *a, **k: 1 / 0)
    _, details = analysis.calculate_score(**BASE)
    assert DefaultStrategy._score_boundary_diag(details, None, "매수", dict(BASE)) is None


def test_ledger_accumulates_per_day_and_stock(db):
    up, down = _diag(BASE, "매수"), _diag(BELOW, "관망")
    db.record_score_boundary("20261016", [{"code": "005930", "name": "삼성전자", "diag": up, "flip": False}])
    db.record_score_boundary("20261016", [{"code": "005930", "name": "삼성전자", "diag": down, "flip": True}])
    rows = db.get_score_boundary()
    assert len(rows) == 1
    r = rows[0]
    assert r["cycles"] == 2 and r["near_cycles"] == 2 and r["flips"] == 1
    assert r["admitted_cycles"] == 1 and r["excluded_cycles"] == 1
    assert r["min_ratio"] == pytest.approx(0.7999) and r["max_ratio"] == pytest.approx(0.8)
    assert r["last_state"] == "관망" and r["rules_version"] == score_model.SCORING_RULES_VERSION
    assert r["max_score"] == pytest.approx(7.5)


def test_ledger_read_failure_is_none_not_empty(db):
    db._get_conn().execute("DROP TABLE score_boundary_ledger")
    assert db.get_score_boundary() is None


def test_trader_collects_only_eventful_stocks_and_detects_flip():
    from modules.auto_trade.trader import AutoTrader
    t = AutoTrader.__new__(AutoTrader)
    t.__dict__["_boundary_rows"] = []
    quiet = {"near": False, "cross": "admitted", "fail_open": False, "expanding": True}
    t._note_score_boundary("A", "a", {"score_diag": quiet})
    assert t.__dict__["_boundary_rows"] == []                 # 아무 일 없으면 쓰지 않는다
    t._note_score_boundary("A", "a", {"score_diag": dict(quiet, expanding=False)})
    rows = t._drain_boundary_rows()
    assert len(rows) == 1 and rows[0]["flip"] is True         # 확산이 뒤집힌 주기만 남는다
    t._note_score_boundary("A", "a", {"score_diag": dict(quiet, near=True)})   # 주기 밖 호출
    assert t._drain_boundary_rows() == []                     # 수집기가 없으면 버린다
    t._note_score_boundary("B", "b", {"score_diag": None})    # 계측 실패(None)도 조용히 지나간다
