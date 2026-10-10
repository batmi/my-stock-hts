"""6-5 캘린더 — 국내 배당 이력은 한 번에 받는다(2026-10-08).

[왜] yfinance 호출은 tz 캐시 경합 때문에 전역 락으로 한 줄로 선다. 종목마다 따로 물으면 8스레드라도
 30여 건이 하나씩 지나가 '예정 일정 조회'가 30초씩 걸렸다. 일괄 결과는 단건(.KS→.KQ)과 같아야 하고,
 일괄이 실패하거나 빠진 종목은 단건으로 다시 물어 '배당 없음'으로 굳히지 않는다.
"""
from concurrent.futures import Future
from unittest.mock import patch

import pandas as pd
import pytest

import api
from modules.manage import events as E


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(E.config, "JSON_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(E, "_kr_suffix_order",
                        lambda c: (".KQ", ".KS") if c.startswith("2") else (".KS", ".KQ"))
    return tmp_path


def _frame(divs, unpriced=()):
    """yf.download(actions=True, group_by='column') 모양: (항목, 티커) 2단 열. unpriced 는 시세가 없는 티커."""
    idx = pd.to_datetime(["2026-03-30", "2026-06-29", "2026-07-01"])
    tick = list(divs) + list(unpriced)
    cols = pd.MultiIndex.from_product([["Close", "Dividends"], tick])
    df = pd.DataFrame(0.0, index=idx, columns=cols)
    for t, vals in divs.items():
        df[("Close", t)] = 100.0
        df[("Dividends", t)] = vals
    for t in unpriced:
        df[("Close", t)] = float("nan")
        df[("Dividends", t)] = float("nan")
    return df


def test_batch_uses_market_suffix_and_marks_no_dividend():
    calls = []

    def fake(tickers, **k):
        calls.append((list(tickers), k.get("actions")))
        return _frame({"005930.KS": [372.0, 374.0, 0.0], "247540.KQ": [98.5, 0.0, 0.0],
                       "000020.KS": [0.0, 0.0, 0.0]})

    with patch.object(api, "fetch_yfinance_data", side_effect=fake):
        out = E._kr_yf_dividends_batch(["005930", "247540", "000020"])
    assert calls == [(["005930.KS", "247540.KQ", "000020.KS"], True)]   # 한 번에, 시장에 맞는 접미사로
    assert list(out["005930"].values) == [372.0, 374.0]                  # 0 은 배당이 아니다
    assert list(out["247540"].values) == [98.5]
    assert len(out["000020"]) == 0                                       # 시세는 있고 배당이 없다 = 확정된 '없음'


def test_unpriced_ticker_retried_with_other_suffix():
    calls = []

    def fake(tickers, **k):
        calls.append(list(tickers))
        if len(calls) == 1:
            return _frame({"005930.KS": [372.0, 0.0, 0.0]}, unpriced=["111110.KS"])
        return _frame({"111110.KQ": [50.0, 0.0, 0.0]})

    with patch.object(api, "fetch_yfinance_data", side_effect=fake):
        out = E._kr_yf_dividends_batch(["005930", "111110"])
    assert calls == [["005930.KS", "111110.KS"], ["111110.KQ"]]
    assert list(out["111110"].values) == [50.0]


def test_batch_result_is_cached():
    fake = lambda tickers, **k: _frame({"005930.KS": [372.0, 0.0, 0.0], "000020.KS": [0.0, 0.0, 0.0]})
    with patch.object(api, "fetch_yfinance_data", side_effect=fake):
        E._kr_yf_dividends_batch(["005930", "000020"])
    with patch.object(api, "fetch_yfinance_data", side_effect=AssertionError("캐시에서 끝나야 한다")):
        out = E._kr_yf_dividends_batch(["005930", "000020"])
    assert list(out["005930"].values) == [372.0] and len(out["000020"]) == 0


def test_batch_failure_is_unknown_and_not_cached(cache_dir):
    with patch.object(api, "fetch_yfinance_data", side_effect=RuntimeError("yahoo down")):
        assert E._kr_yf_dividends_batch(["005930"]) == {}
    assert not (cache_dir / E._YF_CACHE_FILE).exists()


def test_cache_expires(monkeypatch):
    E._yf_cache_put("kr_div", {"005930": {"div": [["2026-03-30", 372.0]]}}, now=1000.0)
    assert E._yf_cache_get("kr_div", "005930", now=1000.0 + E._YF_CACHE_TTL_SEC - 1) is not None
    assert E._yf_cache_get("kr_div", "005930", now=1000.0 + E._YF_CACHE_TTL_SEC) is None


# ── 해외 예정일 캐시 ───────────────────────────────────────────────────────────────
class _Tk:
    def __init__(self, cal=None, info=None, cal_err=False):
        self._cal, self._info, self._err = cal, info, cal_err

    @property
    def calendar(self):
        if self._err:
            raise RuntimeError("yahoo 500")
        return self._cal

    @property
    def info(self):
        return self._info or {}


def test_us_dates_cached_when_clean():
    tk = _Tk(cal={"Ex-Dividend Date": "2026-11-05", "Earnings Date": ["2026-12-17"]})
    with patch.object(api, "yf_ticker_call", side_effect=lambda sym, fn: fn(tk)):
        first = E._collect_us("MU", "마이크론")
    with patch.object(api, "yf_ticker_call", side_effect=AssertionError("캐시에서 끝나야 한다")):
        second = E._collect_us("MU", "마이크론")
    assert first == second and {e["type"] for e in first} == {"배당락", "실적발표"}


def test_us_partial_failure_not_cached():
    tk = _Tk(cal_err=True, info={})
    with patch.object(api, "yf_ticker_call", side_effect=lambda sym, fn: fn(tk)):
        assert E._collect_us("MU", "마이크론") is None
    assert E._yf_cache_get("us", "MU") is None          # 실패를 '일정 없음'으로 굳히지 않는다


def _dart_ok():
    return patch.multiple(api,
                          get_dart_dividend=lambda c: {"year": "2025", "주당배당금": 1444.0, "시가배당률": 2.0},
                          get_dart_acc_month=lambda c: "12",
                          get_dart_dividend_decision=lambda c, days=200, rows=None: None)


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
    monkeypatch.setattr(E, "_collect_kr", lambda code, name, div_batch=None, filings=None: div_batch.result() and None)
    monkeypatch.setattr(E, "_collect_kr_earnings_est", lambda code, name, filings=None: None)
    E._collect_watchlist_events([("005930", "삼성전자"), ("000660", "SK하이닉스")], [])
    assert batches == [["005930", "000660"]]


# ── 예열(2026-10-09): 하루 첫 화면도 캐시로 ─────────────────────────────────────────
def test_prewarm_fills_yf_only_without_dart(monkeypatch):
    monkeypatch.setattr(E, "_gather_watchlist", lambda: ([("005930", "삼성전자")], [("MU", "마이크론"), ("NVDA", "엔비디아")]))
    batches, us = [], []
    monkeypatch.setattr(E, "_kr_yf_dividends_batch", lambda codes: batches.append(list(codes)) or {})
    monkeypatch.setattr(E, "_collect_us", lambda code, name: us.append(code))
    with patch.object(api, "call_dart", side_effect=AssertionError("예열은 DART 를 부르지 않는다")):
        E.prewarm_yf_cache()
    assert batches == [["005930"]] and us == ["MU", "NVDA"]


def test_prewarm_stops_between_us_tickers(monkeypatch):
    import threading
    stop = threading.Event()
    monkeypatch.setattr(E, "_gather_watchlist", lambda: ([], [("MU", "a"), ("NVDA", "b")]))
    seen = []
    monkeypatch.setattr(E, "_collect_us", lambda code, name: (seen.append(code), stop.set()))
    E.prewarm_yf_cache(stop=stop)
    assert seen == ["MU"]


def test_calendar_warmer_runs_then_stops(monkeypatch):
    import threading
    from api import chart_cache as cc
    ran = threading.Event()
    monkeypatch.setattr(cc, "CALENDAR_WARM_DELAY_SEC", 0)
    monkeypatch.setattr(E, "prewarm_yf_cache", lambda stop=None: ran.set())
    cc._WARM_STOP.clear()
    t = cc.start_calendar_warmer()
    assert ran.wait(2), "기동 뒤 예열이 돌아야 한다"
    cc.stop_background_warmers(timeout=2)
    assert not t.is_alive(), "종료 신호에 내려가야 한다(6시간 대기 중에도)"


# ── 거래소공시 목록 1회 공유(2026-10-09) ───────────────────────────────────────────
def test_filings_fetched_once_per_stock_and_exchange_only(monkeypatch):
    monkeypatch.setattr(E.config, "DART_API_KEY", "dummy", raising=False)
    asked = []

    def disc(code, days=30, pblntf_ty=None, page_count=100):
        asked.append((code, days, pblntf_ty))
        return []

    monkeypatch.setattr(api, "get_dart_disclosures", disc)
    monkeypatch.setattr(api, "get_dart_dividend", lambda c: {"year": "2025", "주당배당금": 1.0, "시가배당률": 1.0})
    monkeypatch.setattr(api, "get_dart_acc_month", lambda c: "12")
    monkeypatch.setattr(E, "_kr_yf_dividends_batch", lambda codes: {c: pd.Series(dtype=float) for c in codes})
    E._collect_watchlist_events([("005930", "삼성전자"), ("000660", "SK하이닉스")], [])
    assert sorted(asked) == [("000660", 400, "I"), ("005930", 400, "I")]   # 배당결정·실적예상이 한 목록을 같이 쓴다


def test_dividend_decision_filters_shared_rows_to_its_window(monkeypatch):
    from datetime import datetime, timedelta
    from modules import dart_api
    recent = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    old = (datetime.now() - timedelta(days=300)).strftime("%Y%m%d")
    rows = [{"rcept_no": "A", "report_nm": "현금ㆍ현물배당결정", "rcept_dt": old}]
    monkeypatch.setattr(api, "get_dart_document_text", lambda r: (_ for _ in ()).throw(AssertionError("200일 밖")))
    assert dart_api.get_dart_dividend_decision("005930", days=200, rows=rows) is None
    rows = [{"rcept_no": "B", "report_nm": "현금ㆍ현물배당결정", "rcept_dt": recent}]
    monkeypatch.setattr(api, "get_dart_document_text", lambda r: "1주당 배당금\n361\n배당기준일\n2026-12-31")
    assert dart_api.get_dart_dividend_decision("005930", days=200, rows=rows)["rcept_no"] == "B"


def test_once_per_key_shares_failure():
    import threading
    once, calls = E._OncePerKey(), []

    def boom():
        calls.append(1)
        raise RuntimeError("DART 020")

    errs = []

    def ask():
        try:
            once.get("005930", boom)
        except RuntimeError as e:
            errs.append(str(e))

    ts = [threading.Thread(target=ask) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls == [1] and errs == ["DART 020"] * 4     # 실패도 한 번, 모두에게 같은 실패


def test_batch_is_chunked_so_yf_lock_is_released_between(monkeypatch):
    calls = []
    codes = [f"{i:06d}" for i in range(1, 26)]
    monkeypatch.setattr(E, "_kr_suffix_order", lambda c: (".KS", ".KQ"))

    def fake(tickers, **k):
        calls.append(len(tickers))
        return _frame({t: [1.0, 0.0, 0.0] for t in tickers})

    with patch.object(api, "fetch_yfinance_data", side_effect=fake):
        out = E._kr_yf_dividends_batch(codes)
    assert calls == [10, 10, 5] and len(out) == 25
