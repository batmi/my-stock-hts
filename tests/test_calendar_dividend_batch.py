"""6-5 캘린더 — 국내 배당 이력은 한 번에 받는다(2026-10-08).

[왜] yfinance 호출은 tz 캐시 경합 때문에 전역 락으로 한 줄로 선다. 종목마다 따로 물으면 8스레드라도
 30여 건이 하나씩 지나가 '예정 일정 조회'가 30초씩 걸렸다. 일괄 결과는 단건(.KS→.KQ)과 같아야 하고,
 일괄이 실패하거나 빠진 종목은 단건으로 다시 물어 '배당 없음'으로 굳히지 않는다.
"""
from concurrent.futures import Future
from unittest.mock import patch

import pandas as pd

import api
from modules.manage import events as E


def _frame(divs):
    """yf.download(actions=True, group_by='column') 모양: (항목, 티커) 2단 열."""
    idx = pd.to_datetime(["2026-03-30", "2026-06-29", "2026-07-01"])
    cols = pd.MultiIndex.from_product([["Close", "Dividends"], list(divs)])
    df = pd.DataFrame(0.0, index=idx, columns=cols)
    for t, vals in divs.items():
        df[("Dividends", t)] = vals
    return df


def test_batch_ks_then_kq_for_missing():
    calls = []

    def fake(tickers, **k):
        calls.append((list(tickers), k.get("actions")))
        if tickers[0].endswith(".KS"):
            return _frame({"005930.KS": [372.0, 374.0, 0.0], "247540.KS": [0.0, 0.0, 0.0]})
        return _frame({"247540.KQ": [98.5, 0.0, 0.0]})

    with patch.object(api, "fetch_yfinance_data", side_effect=fake):
        out = E._kr_yf_dividends_batch(["005930", "247540"])
    assert calls == [(["005930.KS", "247540.KS"], True), (["247540.KQ"], True)]   # .KQ 는 빠진 종목만
    assert list(out["005930"].values) == [372.0, 374.0]                            # 0 은 배당이 아니다
    assert list(out["247540"].values) == [98.5]


def test_batch_failure_is_empty_not_no_dividend():
    with patch.object(api, "fetch_yfinance_data", side_effect=RuntimeError("yahoo down")):
        assert E._kr_yf_dividends_batch(["005930"]) == {}


def _dart_ok():
    return patch.multiple(api,
                          get_dart_dividend=lambda c: {"year": "2025", "주당배당금": 1444.0, "시가배당률": 2.0},
                          get_dart_acc_month=lambda c: "12",
                          get_dart_dividend_decision=lambda c, days=200: None)


def test_collect_kr_uses_batch_without_single_call():
    fut = Future()
    fut.set_result({"005930": pd.Series([372.0], index=pd.to_datetime(["2026-03-30"]))})
    with _dart_ok(), patch.object(E, "_kr_yf_dividends", side_effect=AssertionError("단건으로 묻지 않는다")):
        row = E._collect_kr("005930", "삼성전자", fut)
    assert row and row["code"] == "005930"


def test_collect_kr_falls_back_to_single_when_batch_missing():
    fut = Future()
    fut.set_result({})                                  # 일괄 실패·빠짐
    asked = []
    with _dart_ok(), patch.object(E, "_kr_yf_dividends", side_effect=lambda c: asked.append(c)):
        E._collect_kr("005930", "삼성전자", fut)
    assert asked == ["005930"]


def test_watchlist_events_make_one_batch_call(monkeypatch):
    monkeypatch.setattr(E.config, "DART_API_KEY", "dummy", raising=False)
    batches = []
    monkeypatch.setattr(E, "_kr_yf_dividends_batch", lambda codes: batches.append(list(codes)) or {})
    monkeypatch.setattr(E, "_collect_kr", lambda code, name, div_batch=None: div_batch.result() and None)
    monkeypatch.setattr(E, "_collect_kr_earnings_est", lambda code, name: None)
    E._collect_watchlist_events([("005930", "삼성전자"), ("000660", "SK하이닉스")], [])
    assert batches == [["005930", "000660"]]
