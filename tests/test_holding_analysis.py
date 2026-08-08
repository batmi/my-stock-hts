"""메뉴 9-2 보유 분석(잔고 화면의 '상태' 컬럼) 검증.

보유 분석은 자동매매의 매도 판단(analyze_sell)을 읽기 전용으로 재사용한다.
여기서는 (1) 임계값 조립 SSOT, (2) TS 청산선 계산, (3) 읽기 전용 보장,
(4) 표시 셀 포맷을 검증한다.
"""
import os
import sys
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import account
from modules.auto_trade import (UNMANAGED_ETF, UNMANAGED_OVERSEAS, UNMANAGED_RESTRICTED,
                                build_sell_thresholds, compute_trailing_stop,
                                get_unmanaged_reason, highest_since, resolve_holding_context)


def _make_df(n=260, peak=260):
    """완만한 상승 후(peak 이후) 하락하는 가짜 일봉."""
    values = list(np.linspace(9000, 13000, min(peak, n)))
    if n > peak:
        values += list(np.linspace(13000, 11000, n - peak))
    close = np.array(values[:n], dtype=float)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n),
        "open": close * 0.99, "high": close * 1.01,
        "low": close * 0.98, "close": close,
        "volume": np.full(n, 100000.0),
    })


# ---------------------------------------------------------------- thresholds

def test_atr_stop_is_quantity_weighted_and_syncs_bep():
    """분할 매수분의 ATR 손절률은 수량 가중 평균이고, BEP 발동선이 그 절대값과 일치한다."""
    buy_trades = [
        {"qty": 10, "stop_loss_rate": -10.0},
        {"qty": 30, "stop_loss_rate": -14.0},
    ]
    with patch.dict(config.SELL_STRATEGY, {"USE_ATR_STOP": True}):
        th = build_sell_thresholds(rule=None, buy_trades=buy_trades)

    assert th["STOP_LOSS_RATE"] == pytest.approx(-13.0)   # (10*-10 + 30*-14) / 40
    assert th["ATR_APPLIED_SL_RATE"] == pytest.approx(-13.0)
    assert th["BREAK_EVEN_PROFIT_RATE"] == pytest.approx(13.0)


def test_atr_stop_ignored_when_disabled():
    """ATR 손절 미사용 설정이면 매수 기록이 있어도 전역 손절률을 그대로 둔다."""
    with patch.dict(config.SELL_STRATEGY, {"USE_ATR_STOP": False}):
        th = build_sell_thresholds(rule=None, buy_trades=[{"qty": 10, "stop_loss_rate": -12.0}])

    assert "ATR_APPLIED_SL_RATE" not in th
    assert "STOP_LOSS_RATE" not in th


def test_null_stop_loss_rate_does_not_crash():
    """stop_loss_rate가 NULL인 과거 매수 기록이 섞여도 조립이 실패하지 않는다."""
    th = build_sell_thresholds(rule=None, buy_trades=[
        {"qty": 10, "stop_loss_rate": None},
        {"qty": 10, "stop_loss_rate": -9.0},
    ])
    assert th["STOP_LOSS_RATE"] == pytest.approx(-9.0)


def test_resolve_holding_context_detects_mean_reversion():
    days, is_mr = resolve_holding_context({"time": "2020-01-01 09:00:00", "reason": "역매수 진입"})
    assert is_mr is True and days > 0

    assert resolve_holding_context(None) == (0, False)


def test_holding_days_use_entry_date_not_latest_buy():
    """진입일 기준이라 오늘 1주를 더 담아도 보유일수가 리셋되지 않는다.

    실제 사례(395160): 07-02·07-09·07-28 매수로 373주를 쌓은 뒤 07-29에 1주를 더 담자
    '최근 매수' 기준이던 기존 로직이 보유일수를 0일로 리셋해 시간청산 시계가 미뤄졌다.
    """
    from datetime import datetime, timedelta

    today = datetime.now().date()
    entry = (today - timedelta(days=27)).strftime("%Y-%m-%d")
    latest = {"time": today.strftime("%Y-%m-%d 14:18:41"), "reason": "체결 확인"}

    days, _ = resolve_holding_context(latest, entry_date=entry)
    assert days == 27          # 최근 매수 기준이었다면 0일


def test_entry_date_priority_order():
    """진입일 우선순위: DB 수량 재생 → 증권사 체결 재생 → 최근 매수(최후 근사).

    증권사 재생을 최근 매수보다 앞에 둔다 — 최근 매수일은 분할 매수·피라미딩 때마다
    보유일수를 0으로 리셋해 시간청산 시계를 무한히 미룬다.
    """
    from modules.auto_trade import resolve_entry_date

    latest = {"time": "2026-07-29 14:00:00"}
    assert resolve_entry_date("2026-03-31", latest, "20250101") == "2026-03-31"
    assert resolve_entry_date(None, latest, "20250101") == "2025-01-01"
    assert resolve_entry_date(None, latest, None) == "2026-07-29"
    assert resolve_entry_date(None, None, "20250101") == "2025-01-01"
    assert resolve_entry_date(None, None, None) is None
    assert resolve_entry_date(None, None, "깨짐") is None


def test_position_entry_date_replays_quantity(tmp_path):
    """진입일은 보유수량이 0 → 1 이상이 된 마지막 시점이다. (부분 매도는 포지션을 끊지 않음)"""
    import sqlite3
    from modules import db_manager

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (code TEXT, time TEXT, type TEXT, qty TEXT, order_status TEXT)")
    rows = [
        # 전량 청산된 옛 포지션 — 진입일로 잡히면 안 된다
        ("005930", "2026-01-05 09:00:00", "매수(수동)", "10", "체결"),
        ("005930", "2026-02-01 09:00:00", "매도(수동)", "10", "체결"),
        # 현재 포지션: 03-10 진입 → 부분 매도 → 추가 매수 (진입은 03-10 유지)
        ("005930", "2026-03-10 09:00:00", "매수(수동)", "50", "체결"),
        ("005930", "2026-04-01 09:00:00", "매도(수동)", "20", "체결"),
        ("005930", "2026-07-29 14:18:41", "매수(수동)", "1", "체결"),
        # 접수·취소 행은 집계에서 빠져야 한다
        ("035720", "2026-05-01 09:00:00", "매수(수동)", "99", "접수"),
        ("035720", "2026-05-02 09:00:00", "매수취소(수동)", "99", "취소"),
        ("035720", "2026-06-20 09:00:00", "매수(수동)", "7", "체결"),
    ]
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    mgr = db_manager.DBManager.__new__(db_manager.DBManager)

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    mgr._get_conn = _conn
    res = mgr.get_position_entry_dates(["005930", "035720", "없음"])

    assert res["005930"] == "2026-03-10"   # 부분 매도·추가 매수에도 진입일 유지
    assert res["035720"] == "2026-06-20"   # 접수·취소 행 무시
    assert "없음" not in res


def test_position_entry_date_counts_amended_fills(tmp_path):
    """정정 주문의 '체결' 행은 진짜 체결이다 — 수량 흐름에서 빠지면 안 된다.

    실 DB에는 매도가 접수 → 정정 → 체결로 기록된다. 종전에는 type에 '정정'이 들어간
    행을 통째로 버려서 전량 청산이 반영되지 않았고, 이미 판 종목이 계속 보유 중으로
    남아 진입일이 옛 날짜로 굳었다.
    """
    import sqlite3
    from modules import db_manager

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (code TEXT, time TEXT, type TEXT, qty TEXT, order_status TEXT)")
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", [
        ("027740", "2026-06-23 12:04:58", "매수(수동)", "1", "체결"),
        ("027740", "2026-06-23 12:05:22", "매도(수동)", "1", "접수"),
        ("027740", "2026-06-23 12:05:58", "매도정정(수동)", "1", "정정"),
        ("027740", "2026-06-23 12:05:58", "매도정정(수동)", "1", "체결"),   # 실제 체결
    ])
    conn.commit()
    conn.close()

    mgr = db_manager.DBManager.__new__(db_manager.DBManager)

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    mgr._get_conn = _conn
    assert mgr.get_position_entry_dates(["027740"]) == {}   # 전량 청산 → 진입일 없음


def test_position_entry_date_drops_fully_closed(tmp_path):
    """전량 청산된 종목은 진입일이 없다."""
    import sqlite3
    from modules import db_manager

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (code TEXT, time TEXT, type TEXT, qty TEXT, order_status TEXT)")
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", [
        ("042660", "2026-01-05 09:00:00", "매수(수동)", "10", "체결"),
        ("042660", "2026-02-01 09:00:00", "매도(수동)", "10", "체결"),
    ])
    conn.commit()
    conn.close()

    mgr = db_manager.DBManager.__new__(db_manager.DBManager)

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    mgr._get_conn = _conn
    assert mgr.get_position_entry_dates(["042660"]) == {}


def test_holding_context_falls_back_to_broker_entry_date():
    """DB 수량 재생이 없으면 증권사 체결 재생으로 보유일수를 계산한다 (HTS 직접 매수분)."""
    from datetime import datetime, timedelta

    d = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
    days, is_mr = resolve_holding_context(None, fallback_buy_date=d)
    assert days == 45 and is_mr is False

    # 최근 매수 기록이 함께 있어도 증권사 재생(진입일)이 우선한다
    db_time = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    days, _ = resolve_holding_context({"time": db_time, "reason": "매수"}, fallback_buy_date=d)
    assert days == 45

    # 증권사 재생이 없으면 최근 매수 기록으로 근사한다
    days, _ = resolve_holding_context({"time": db_time, "reason": "매수"})
    assert days == 3

    # 둘 다 없으면 오늘 매수(0일)로 본다
    assert resolve_holding_context(None, fallback_buy_date=None) == (0, False)
    # 형식이 깨진 값도 0일로 흘려보낸다 (보유일수 때문에 분석이 죽으면 안 됨)
    assert resolve_holding_context(None, fallback_buy_date="깨짐") == (0, False)


def _ccld(code, date, qty, buy=True):
    """주식일별주문체결조회(inquire-daily-ccld) 응답 한 행."""
    return {"pdno": code, "ord_dt": date, "tot_ccld_qty": str(qty),
            "sll_buy_dvsn_cd": "02" if buy else "01",
            "sll_buy_dvsn_cd_name": "현금매수" if buy else "현금매도"}


def test_entry_date_replay_uses_zero_to_one_crossing():
    """진입일 = 누적 보유수량이 0 → 1 이상이 된 날. 분할 매수는 리셋시키지 않는다."""
    import api

    rows = [("20260102", True, 10), ("20260210", True, 5), ("20260315", True, 3)]
    assert api._replay_entry_date(rows, current_qty=18) == "20260102"


def test_entry_date_replay_restarts_after_full_exit():
    """전량 청산 후 재진입하면 새 진입일을 쓴다 (첫 매수일 고정 금지)."""
    import api

    rows = [("20260102", True, 10), ("20260220", False, 10), ("20260405", True, 7)]
    assert api._replay_entry_date(rows, current_qty=7) == "20260405"


def test_entry_date_replay_ignores_partial_exit():
    """부분 매도(반익절)는 포지션을 끊지 않는다."""
    import api

    rows = [("20260102", True, 10), ("20260220", False, 5), ("20260405", True, 2)]
    assert api._replay_entry_date(rows, current_qty=7) == "20260102"


def test_entry_date_replay_flags_position_older_than_window():
    """구간 시작 시점에 이미 보유 중이었다면(현재수량 > 구간 내 순증감) 확인 가능한
    가장 이른 시점을 하한으로 돌려준다 — 최근 매수일로 되돌아가면 보유일수가 급감한다."""
    import api

    rows = [("20260701", True, 1)]     # 구간 안엔 1주 추가 매수뿐인데 실제 보유는 100주
    assert api._replay_entry_date(rows, current_qty=100, window_start="20260101") == "20260101"
    # 하한을 모르면(창 시작 미상) 억지로 만들지 않는다
    assert api._replay_entry_date(rows, current_qty=100) is None


def test_period_entry_dates_switches_tr_past_three_months():
    """3개월 경계에서 과거 조회 TR로 전환한다 (한 TR로 계속 훑으면 과거 구간이 통째로 빈다)."""
    import api

    calls = []

    def _fake(url, market, category, action, params=None, tr_id=None, **kw):
        calls.append((tr_id, params["INQR_STRT_DT"], params["INQR_END_DT"]))
        if len(calls) < 3:                       # 처음 두 구간은 해당 종목 체결 없음
            return {"rt_cd": "0", "output1": []}
        return {"rt_cd": "0", "output1": [_ccld("950160", "20250910", 10)]}

    with patch("api.call_api", side_effect=_fake), \
         patch("api._prepare_account_params", return_value=("12345678", "01")), \
         patch.object(config.session, "is_toss", False, create=True), \
         patch.object(config.session, "is_simulation", False, create=True):
        found = api.get_period_entry_dates(["950160"], qty_map={"950160": 10}, months=12)

    assert found == {"950160": "20250910"}
    assert calls[0][0] == "TTTC8001R"             # 최근 3개월
    assert all(c[0] == "CTSC9115R" for c in calls[1:])   # 그 이전
    assert calls[0][1] > calls[1][1]              # 구간이 과거로 이동
    assert calls[0][0] and all(p is not None for p in calls[0])


def test_period_entry_dates_queries_sells_too():
    """수량 흐름을 재생하려면 매도 체결도 받아야 한다 (매수만 조회하면 재진입을 놓친다)."""
    import api

    seen = {}

    def _fake(url, market, category, action, params=None, tr_id=None, **kw):
        seen.update(params)
        return {"rt_cd": "0", "output1": [
            _ccld("005930", "20260210", 10),
            _ccld("005930", "20260320", 10, buy=False),   # 전량 청산
            _ccld("005930", "20260505", 4),               # 재진입
        ]}

    with patch("api.call_api", side_effect=_fake), \
         patch("api._prepare_account_params", return_value=("12345678", "01")), \
         patch.object(config.session, "is_toss", False, create=True), \
         patch.object(config.session, "is_simulation", False, create=True):
        found = api.get_period_entry_dates(["005930"], qty_map={"005930": 4}, months=3)

    assert seen["SLL_BUY_DVSN_CD"] == "00"        # 전체(매수+매도)
    assert seen["CCLD_DVSN"] == "01"              # 체결분만
    assert found == {"005930": "20260505"}        # 최근 매수일이자 '재진입일'


def test_period_entry_dates_ignores_pyramiding_buy():
    """피라미딩(추가 매수)이 있어도 진입일은 최초 0 → 1 시점 그대로다."""
    import api

    def _fake(url, market, category, action, params=None, tr_id=None, **kw):
        return {"rt_cd": "0", "output1": [
            _ccld("005930", "20260210", 10),
            _ccld("005930", "20260610", 1),       # 1주만 더 담아도 리셋되면 안 된다
        ]}

    with patch("api.call_api", side_effect=_fake), \
         patch("api._prepare_account_params", return_value=("12345678", "01")), \
         patch.object(config.session, "is_toss", False, create=True), \
         patch.object(config.session, "is_simulation", False, create=True):
        found = api.get_period_entry_dates(["005930"], qty_map={"005930": 11}, months=3)

    assert found == {"005930": "20260210"}


def test_period_entry_dates_stops_on_unsupported_tr():
    """과거 조회 TR을 지원하지 않으면 모은 것까지 재생하고 멈춘다."""
    import api

    def _fake(url, market, category, action, params=None, tr_id=None, **kw):
        if tr_id == "TTTC8001R":
            return {"rt_cd": "0", "output1": [_ccld("005930", "20260701", 5)]}
        return {"rt_cd": "1", "msg1": "지원하지 않는 TR"}

    with patch("api.call_api", side_effect=_fake), \
         patch("api._prepare_account_params", return_value=("12345678", "01")), \
         patch.object(config.session, "is_toss", False, create=True), \
         patch.object(config.session, "is_simulation", False, create=True):
        found = api.get_period_entry_dates(["005930", "950160"], qty_map={"005930": 5}, months=12)

    assert found == {"005930": "20260701"}        # 950160은 못 찾아도 예외 없이 진행


def test_period_entry_dates_empty_codes_short_circuits():
    import api
    assert api.get_period_entry_dates([]) == {}


def test_period_entry_dates_uses_toss_order_history():
    """토스 모드는 KIS TR 대신 주문 이력(기간 조회)에서 같은 값을 만든다.

    이 분기가 없던 동안 토스 모드는 HTS 직접 매수분 보유일수가 전부 0일로 굳었다.
    """
    import api

    with patch.object(config.session, "is_toss", True, create=True), \
         patch("api._toss_period_entry_dates", return_value={"005930": "20260310"}) as spy:
        assert api.get_period_entry_dates(["005930"], months=6) == {"005930": "20260310"}

    assert spy.call_args.kwargs["months"] == 6

    # 조회가 깨져도 잔고 표시를 막지 않는다 (보유일수는 부가 정보)
    with patch.object(config.session, "is_toss", True, create=True), \
         patch("api._toss_period_entry_dates", side_effect=RuntimeError("토스 응답 없음")):
        assert api.get_period_entry_dates(["005930"]) == {}


def test_toss_period_entry_dates_replays_quantity_flow():
    """토스도 매수·매도를 모두 재생해 '0 → 1 이상'이 된 날을 쓴다 (최근 매수일 아님)."""
    import api

    pages = [
        {"orders": [
            {"symbol": "005930", "side": "BUY",
             "execution": {"filledQuantity": 10, "filledAt": "2026-03-10T09:31:00"}},
            {"symbol": "005930", "side": "SELL",       # 전량 청산 → 이후 매수가 새 진입
             "execution": {"filledQuantity": 10, "filledAt": "2026-05-20T10:00:00"}},
            {"symbol": "000660", "side": "BUY",        # 취소·미체결은 수량 흐름에 무관
             "execution": {"filledQuantity": 0, "filledAt": "2026-04-01T09:00:00"}},
            {"symbol": "035720", "side": "BUY",        # 찾는 종목이 아니면 무시
             "execution": {"filledQuantity": 5, "filledAt": "2026-06-01T09:00:00"}},
        ], "hasNext": True, "nextCursor": "c2"},
        {"orders": [
            {"symbol": "005930", "side": "BUY",        # 재진입
             "execution": {"filledQuantity": 3, "filledAt": "2026-06-15T13:20:00"}},
            {"symbol": "005930", "side": "BUY",        # 추가 매수는 진입일을 밀지 않는다
             "execution": {"filledQuantity": 2, "filledAt": "2026-07-01T13:20:00"}},
        ], "hasNext": False},
    ]
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]

    with patch("api.toss_api.get_orders", side_effect=_fake):
        found = api._toss_period_entry_dates(["005930", "000660"],
                                             qty_map={"005930": 5}, months=6)

    assert found == {"005930": "20260615"}
    assert len(calls) == 2
    assert "cursor" not in calls[0] and calls[1]["cursor"] == "c2"


def test_analyze_holdings_uses_broker_history_for_hts_positions(_no_db):
    """DB에 없는 종목만 골라 체결 내역을 조회하고, 그 날짜로 보유일수를 채운다."""
    from datetime import datetime, timedelta
    from modules import auto_trade

    d = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
    entries = [{"code": "950160", "name": "코오롱티슈진", "buy_price": 107833,
                "current_price": 13200, "profit_rate": -87.75, "is_overseas": False}]

    with patch("api.get_period_entry_dates", return_value={"950160": d}) as spy, \
         patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("하락", 0.0)):
        res = auto_trade.analyze_holdings(entries)["950160"]

    assert spy.call_args.args[0] == ["950160"]
    assert res["holding_days"] == 120
    assert res["has_buy_record"] is True


def test_analyze_holdings_skips_broker_lookup_for_manual_entries(_no_db):
    """보유일수를 직접 입력한 포지션([9]-5 수동 분석)은 증권사 이력을 조회하지 않는다."""
    from modules import auto_trade

    entries = [{"code": "005930", "name": "삼성전자", "buy_price": 10000, "qty": 10,
                "current_price": 12000, "profit_rate": 20.0, "is_overseas": False,
                "holding_days": 58}]

    with patch("api.get_period_entry_dates") as spy, \
         patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)):
        res = auto_trade.analyze_holdings(entries)["005930"]

    spy.assert_not_called()
    assert res["holding_days"] == 58


# ------------------------------------------------------------ trailing stop

def test_trailing_stop_arms_only_after_activation():
    """발동 임계 미만이면 armed=False — 화면에도 '도달 시'로 표기된다."""
    ts = compute_trailing_stop(highest_price=10500, buy_price=10000, current_price=10100,
                               ind={}, thresholds={"ts_activation": 10.0, "ts_callback": 5.0})
    assert ts["armed"] is False
    assert ts["triggered"] is False


def test_trailing_stop_price_matches_callback():
    """표시용 청산가는 최고가 × (1 - 콜백%)이며, 현재가가 그 아래면 triggered."""
    ts = compute_trailing_stop(highest_price=13000, buy_price=10000, current_price=12000,
                               ind={}, thresholds={"ts_activation": 10.0, "ts_callback": 5.0,
                                                   "USE_ATR_STOP": False})
    assert ts["armed"] is True
    assert ts["stop_price"] == pytest.approx(13000 * 0.95)   # = 12,350
    assert ts["triggered"] is True    # 현재가 12,000 < 12,350 (하락률 7.7% ≥ 5%)

    ts2 = compute_trailing_stop(highest_price=13000, buy_price=10000, current_price=12800,
                                ind={}, thresholds={"ts_activation": 10.0, "ts_callback": 5.0,
                                                    "USE_ATR_STOP": False})
    assert ts2["triggered"] is False  # 현재가 12,800 > 12,350 (하락률 1.5% < 5%)


def test_trailing_stop_atr_widens_callback():
    """ATR이 크면 콜백이 넓어져(샹들리에) 청산선이 더 아래로 내려간다."""
    fixed = compute_trailing_stop(13000, 10000, 12500, ind={"atr": 0},
                                  thresholds={"ts_activation": 10.0, "ts_callback": 5.0,
                                              "TRAILING_ATR_MULTIPLIER": 3.0, "USE_ATR_STOP": True})
    wide = compute_trailing_stop(13000, 10000, 12500, ind={"atr": 500},
                                 thresholds={"ts_activation": 10.0, "ts_callback": 5.0,
                                             "TRAILING_ATR_MULTIPLIER": 3.0, "USE_ATR_STOP": True})
    assert wide["callback"] > fixed["callback"]
    assert wide["stop_price"] < fixed["stop_price"]


def test_giveback_cap_converts_between_bases():
    """'최고 수익의 R만 반납'을 콜백(최고가 대비 %)으로 정확히 환산한다.

    구 산식은 max_profit_rate × R을 그대로 콜백 상한으로 써서(기준 혼용) 수익이 클수록
    캡이 무력화됐다. MFE +108.4%, R=0.30이면 32.5%가 아니라 15.6%가 정답이다.
    """
    from modules.auto_trade import giveback_callback_cap

    cap = giveback_callback_cap(108.4, 0.30)
    assert cap == pytest.approx(15.60, abs=0.05)
    assert cap < 108.4 * 0.30            # 구 산식(32.5%)보다 반드시 타이트

    # 환산 검증: 최고가에서 cap%만큼 빠진 가격이 '최고 수익의 70%'를 남긴다
    buy, mfe, r = 100.0, 108.4, 0.30
    high = buy * (1 + mfe / 100)
    exit_price = high * (1 - cap / 100)
    assert (exit_price - buy) / buy * 100 == pytest.approx(mfe * (1 - r), abs=0.05)

    assert giveback_callback_cap(0, 0.3) == 0.0
    assert giveback_callback_cap(50, 0) == 0.0


def test_giveback_cap_never_tightens_below_floor():
    """수익이 작을 때는 하한(ts_callback)이 지켜져 조기 청산되지 않는다."""
    ts = compute_trailing_stop(11000, 10000, 10900, ind={"atr": 300},
                               thresholds={"ts_activation": 5.0, "ts_callback": 5.0,
                                           "TRAILING_ATR_MULTIPLIER": 3.5, "USE_ATR_STOP": True})
    with patch.dict(config.SELL_STRATEGY, {"TS_MAX_GIVEBACK_RATIO": 0.25}):
        capped = compute_trailing_stop(11000, 10000, 10900, ind={"atr": 300},
                                       thresholds={"ts_activation": 5.0, "ts_callback": 5.0,
                                                   "TRAILING_ATR_MULTIPLIER": 3.5, "USE_ATR_STOP": True})
    assert capped['callback'] >= 5.0
    assert capped['callback'] <= ts['callback']


def test_trailing_stop_returns_none_without_position():
    assert compute_trailing_stop(0, 10000, 10000) is None
    assert compute_trailing_stop(10000, 0, 10000) is None


# --------------------------------------------------------- unmanaged position

def test_restricted_stock_is_unmanaged():
    assert get_unmanaged_reason("005930", "삼성전자", restricted_codes={"005930": {}}) == UNMANAGED_RESTRICTED


def test_overseas_is_always_unmanaged():
    """매도 루프는 국내 잔고만 순회하므로 해외 포지션은 전량 수동 관리 대상이다."""
    assert get_unmanaged_reason("AAPL", "APPLE", is_overseas=True) == UNMANAGED_OVERSEAS


def test_etf_unmanaged_follows_include_setting():
    with patch("api.is_domestic_etf_etn", return_value=True):
        with patch.object(config, "SYSTEM_INCLUDE_ETF", False, create=True):
            assert get_unmanaged_reason("102780", "KODEX 삼성그룹") == UNMANAGED_ETF
        with patch.object(config, "SYSTEM_INCLUDE_ETF", True, create=True):
            assert get_unmanaged_reason("102780", "KODEX 삼성그룹") is None


def test_normal_domestic_stock_is_managed():
    with patch("api.is_domestic_etf_etn", return_value=False):
        assert get_unmanaged_reason("005930", "삼성전자", restricted_codes={}) is None


# ------------------------------------------------------------ analyze_holdings

@pytest.fixture
def _no_db():
    """보유 분석이 읽는 DB 배치 조회를 전부 빈 값으로 고정."""
    targets = {
        "get_all_stock_strategies": [],
        "get_latest_buy_trades": {},
        "get_buy_trades_for_current_holdings": {},
        "get_all_trailing_stops": {},
        "get_all_half_tp": set(),
    }
    patchers = [patch(f"modules.db_manager.db.{k}", return_value=v) for k, v in targets.items()]
    # 진입일 복원은 증권사 TR을 부른다 — 개별 테스트가 명시적으로 patch하지 않으면
    #  실 계좌 조회가 나가므로 기본값을 빈 결과로 막는다(개별 patch가 우선한다).
    patchers.append(patch("api.get_period_entry_dates", return_value={}))
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()


def test_analyze_holdings_is_read_only(_no_db):
    """DB 최고가 갱신 등 부수효과 없이 종목별 판정만 반환한다."""
    from modules import auto_trade

    entries = [{"code": "005930", "name": "삼성전자", "buy_price": 10000,
                "current_price": 11000, "profit_rate": 10.0, "is_overseas": False}]

    with patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)), \
         patch("modules.db_manager.db.update_highest_price") as upd:
        res = auto_trade.analyze_holdings(entries)

    upd.assert_not_called()
    assert "005930" in res
    assert res["005930"]["action"] in ("sell", "hold")
    assert res["005930"]["state"]
    assert res["005930"]["has_buy_record"] is False   # 매수 기록 없음 → 보유일 '-'


def test_analyze_holdings_flags_restricted_position(_no_db):
    """제한 종목은 판정은 하되 '자동 매도 제외'로 표시된다."""
    from modules import auto_trade

    entries = [{"code": "950160", "name": "코오롱티슈진", "buy_price": 50000,
                "current_price": 6100, "profit_rate": -87.8, "is_overseas": False}]

    with patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("하락", 0.0)):
        res = auto_trade.analyze_holdings(entries, restricted_codes={"950160": {}})

    assert res["950160"]["unmanaged"] == UNMANAGED_RESTRICTED
    assert res["950160"]["action"] == "sell"   # -87.8%는 손절선을 한참 벗어남


def test_analyze_holdings_skips_invalid_entry(_no_db):
    """매입단가/현재가가 0인 종목은 판정하지 않는다 (잘못된 손절 표시 방지)."""
    from modules import auto_trade

    entries = [{"code": "000000", "name": "이상", "buy_price": 0,
                "current_price": 0, "profit_rate": 0.0, "is_overseas": False}]
    with patch("api.get_chart_data", return_value=_make_df()):
        assert auto_trade.analyze_holdings(entries) == {}


def test_analyze_holdings_empty():
    from modules import auto_trade
    assert auto_trade.analyze_holdings([]) == {}


# ------------------------------------------------------------ display cells

def test_state_cell_marks_sell_signal():
    assert "청산" in account._fmt_state_cell({"action": "sell", "score": 3.1, "state": "주의"})

    hold = account._fmt_state_cell({"action": "hold", "score": 8.2, "state": "매수",
                                    "state_color": "[red]"})
    assert "매수" in hold and "8.2" in hold and "[red]" in hold

    assert account._fmt_state_cell(None) == "[dim]-[/dim]"


def test_state_cell_marks_unmanaged_position():
    """시스템이 팔지 않는 포지션은 '수동'이 함께 표시된다."""
    cell = account._fmt_state_cell({"action": "sell", "score": 0.0, "state": "매도",
                                    "unmanaged": UNMANAGED_ETF})
    assert "청산" in cell and "수동" in cell

    managed = account._fmt_state_cell({"action": "sell", "score": 0.0, "state": "매도",
                                       "unmanaged": None})
    assert "수동" not in managed


def test_holding_days_cell_flags_time_stop():
    with patch.dict(config.SELL_STRATEGY, {"TIME_STOP_USE": True, "TIME_STOP_DAYS": 20}):
        assert "yellow" in account._fmt_holding_days_cell({"has_buy_record": True, "holding_days": 25})
        assert "yellow" not in account._fmt_holding_days_cell({"has_buy_record": True, "holding_days": 5})

    # 매수일을 어디서도 못 찾으면 오늘 매수로 보고 0일을 표시한다
    assert account._fmt_holding_days_cell({"has_buy_record": False, "holding_days": 0}) == "0일"
    assert account._fmt_holding_days_cell(None) == "[dim]-[/dim]"


def test_stop_cell_renders_the_stop_the_engine_actually_applied():
    """셀은 DB를 다시 읽지 않고 analyze_sell이 적용한 손절률을 그대로 보여준다."""
    ts = {"armed": True, "stop_price": 286632.0, "callback": 23.5, "activation": 10.0}

    # ATR 손절이 적용된 포지션 — 접두어 ATR
    cell = account._fmt_stop_cell(
        {"applied_sl_rate": -11.0, "is_atr_stop": True, "ts": ts}, 10000)
    assert "ATR:" in cell and "8,900" in cell and "고정:" not in cell

    # ATR을 못 구해 전역 고정 손절로 판정된 포지션 — 감추지 않는다.
    #  (감췄더니 화면은 '미사용'인데 청산 사유는 '손절'로 나오는 모순이 있었다)
    cell = account._fmt_stop_cell(
        {"applied_sl_rate": -7.0, "is_atr_stop": False, "ts": ts}, 10000)
    assert "고정:" in cell and "9,300" in cell

    # 본전 청산이 손절선을 끌어올린 상태
    cell = account._fmt_stop_cell(
        {"applied_sl_rate": 0.5, "is_atr_stop": True, "is_bep_applied": True, "ts": ts}, 10000)
    assert "BEP:" in cell and "10,050" in cell

    # 손절 미사용(0) + TS 없음 → 표시할 선이 없다
    assert account._fmt_stop_cell({"applied_sl_rate": 0.0}, 10000) == "[dim]미사용[/dim]"


def test_stop_cell_always_shows_both_stop_and_ts():
    """손절선과 TS는 항상 두 줄로 함께 나온다."""
    res = {"applied_sl_rate": -11.0, "is_atr_stop": True,
           "ts": {"armed": True, "stop_price": 286632.0, "callback": 23.5, "activation": 10.0}}
    cell = account._fmt_stop_cell(res, 179694)
    assert "ATR:" in cell and "286,632" in cell and "\n" in cell

    # TS 미발동이어도 발동 조건을 남긴다
    res["ts"] = {"armed": False, "stop_price": 0, "callback": 5.0, "activation": 10.0}
    cell = account._fmt_stop_cell(res, 179694)
    assert "ATR:" in cell and "도달 시" in cell


def test_mfe_cell_and_ts_line():
    cell = account._fmt_mfe_cell({"highest_price": 13000, "max_profit_rate": 30.0})
    assert "13,000" in cell and "+30.0%" in cell

    armed = account._fmt_ts_stop({"ts": {"armed": True, "stop_price": 11221.0,
                                         "callback": 13.7, "activation": 10.0}})
    assert "11,221" in armed and "13.7" in armed

    pending = account._fmt_ts_stop({"ts": {"armed": False, "stop_price": 0,
                                           "callback": 5.0, "activation": 10.0}})
    assert "도달 시" in pending

    assert account._fmt_ts_stop(None) is None


# ------------------------------------------------- [9]-5 포지션 분석

def test_highest_since_uses_only_bars_after_buy_date():
    """매수일 이전의 더 높은 고점은 TS 앵커에서 제외된다."""
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]),
        "high": [99000.0, 50000.0, 61000.0, 55000.0],
    })
    assert highest_since(df, pd.Timestamp("2026-02-01")) == pytest.approx(61000.0)
    assert highest_since(df, pd.Timestamp("2027-01-01")) is None   # 이후 봉 없음
    assert highest_since(None, pd.Timestamp("2026-02-01")) is None


def test_highest_since_handles_tz_aware_dates():
    """tvDatafeed 경로 차트(tz-aware)에서도 앵커가 조용히 사라지지 않는다."""
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-03-01", "2026-04-01"]).tz_localize("Asia/Seoul"),
        "high": [99000.0, 61000.0, 55000.0],
    })
    from datetime import date
    assert highest_since(df, date(2026, 3, 1)) == pytest.approx(61000.0)


def test_highest_since_accepts_yyyymmdd_string_dates():
    """KIS 국내 일봉은 date가 'YYYYMMDD' 문자열이다."""
    df = pd.DataFrame({"date": ["20260101", "20260301", "20260401"],
                       "high": [99000.0, 61000.0, 55000.0]})
    from datetime import date
    assert highest_since(df, date(2026, 3, 1)) == pytest.approx(61000.0)


def test_manual_positions_round_trip(tmp_path):
    """저장 → 로드 시 매수일(date)과 국내/해외 구분이 보존된다."""
    from datetime import date

    positions = [
        {"code": "005930", "name": "삼성전자", "is_overseas": False,
         "buy_price": 179694.0, "qty": 95, "buy_date": date(2026, 3, 31)},
        {"code": "AAPL", "name": "APPLE", "is_overseas": True,
         "buy_price": 200.0, "qty": 5, "buy_date": None},
    ]

    with patch.object(account, "MANUAL_POSITIONS_FILE", str(tmp_path / "manual.json")):
        assert account.save_manual_positions(positions) is True
        loaded = account.load_manual_positions()

    assert loaded == positions


def test_load_manual_positions_skips_corrupt_rows(tmp_path):
    """손상된 항목이 섞여도 나머지는 살린다 (수기 편집 파일 방어)."""
    import json as _json
    path = tmp_path / "manual.json"
    path.write_text(_json.dumps([
        {"code": "005930", "name": "삼성전자", "buy_price": 10000, "qty": 10, "buy_date": None},
        {"code": "BAD", "name": "깨진행"},                       # buy_price/qty 없음
        {"code": "X", "buy_price": "abc", "qty": 1},             # 숫자 아님
    ]), encoding="utf-8")

    with patch.object(account, "MANUAL_POSITIONS_FILE", str(path)):
        loaded = account.load_manual_positions()

    assert [p["code"] for p in loaded] == ["005930"]


def test_load_manual_positions_missing_file(tmp_path):
    with patch.object(account, "MANUAL_POSITIONS_FILE", str(tmp_path / "none.json")):
        assert account.load_manual_positions() == []


def _positions():
    from datetime import date
    return [
        {"code": "005930", "name": "삼성전자", "is_overseas": False,
         "buy_price": 179694.0, "qty": 95, "buy_date": date(2026, 3, 31)},
        {"code": "005380", "name": "현대차", "is_overseas": False,
         "buy_price": 630000.0, "qty": 25, "buy_date": date(2025, 5, 12)},
    ]


def test_modify_saved_position_updates_in_place():
    """[2] 수정 → 번호 1 → 수량만 변경, 나머지는 Enter로 현재값 유지."""
    from datetime import date
    answers = iter(["1", "179694", "100", "2026-03-31"])

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **k: next(answers)), \
         patch.object(config.console, "print"):
        kept, changed = account._modify_saved_position(_positions())

    assert changed is True
    assert len(kept) == 2
    assert kept[0]["qty"] == 100                            # 변경됨
    assert kept[0]["buy_price"] == pytest.approx(179694.0)  # 유지
    assert kept[0]["buy_date"] == date(2026, 3, 31)         # 유지
    assert kept[1]["qty"] == 25                             # 다른 항목은 무손상


def test_delete_saved_position_requires_confirmation():
    """[3] 삭제는 확인을 받은 뒤에만 지운다."""
    with patch("rich.prompt.Prompt.ask", side_effect=(lambda it: (lambda *a, **k: next(it)))(iter(["2", "y"]))), \
         patch.object(config.console, "print"):
        kept, changed = account._delete_saved_position(_positions())
    assert changed is True and [p["code"] for p in kept] == ["005930"]

    # 확인에서 n을 고르면 그대로 둔다
    with patch("rich.prompt.Prompt.ask", side_effect=(lambda it: (lambda *a, **k: next(it)))(iter(["2", "n"]))), \
         patch.object(config.console, "print"):
        kept, changed = account._delete_saved_position(_positions())
    assert changed is False and kept == _positions()


def test_modify_saved_position_cancel_keeps_original():
    """번호 입력 취소(Enter)와 수정 중 취소(b) 모두 원본을 바꾸지 않는다."""
    with patch("rich.prompt.Prompt.ask", side_effect=(lambda it: (lambda *a, **k: next(it)))(iter([""]))), \
         patch.object(config.console, "print"):
        kept, changed = account._modify_saved_position(_positions())
    assert changed is False and kept == _positions()

    with patch("rich.prompt.Prompt.ask", side_effect=(lambda it: (lambda *a, **k: next(it)))(iter(["1", "b"]))), \
         patch.object(config.console, "print"):
        kept, changed = account._modify_saved_position(_positions())
    assert changed is False and kept == _positions()


def test_select_position_rejects_bad_index():
    for bad in ("9", "abc"):
        with patch("rich.prompt.Prompt.ask", side_effect=(lambda it: (lambda *a, **k: next(it)))(iter([bad]))), \
             patch.object(config.console, "print") as pr:
            assert account._select_position(_positions(), "수정") is None
        printed = " ".join(c.args[0] for c in pr.call_args_list
                           if c.args and isinstance(c.args[0], str))
        assert "목록에 있는 번호" in printed


def test_edit_position_can_clear_buy_date():
    """매수일에 '-'를 넣으면 지워진다 (보유일수·TS 앵커 미적용으로 되돌림)."""
    answers = iter(["179694", "95", "-"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **k: next(answers)), \
         patch.object(config.console, "print"):
        updated = account._edit_position(_positions()[0])

    assert updated["buy_date"] is None
    assert updated["qty"] == 95


def test_manual_entry_overrides_holding_days_and_anchor(_no_db):
    """수동 입력 포지션은 DB 기록 대신 입력한 매수일/유도한 최고가를 쓴다."""
    from modules import auto_trade

    df = _make_df()
    df["date"] = pd.date_range(end=pd.Timestamp("2026-07-29"), periods=len(df))

    entries = [{"code": "005930", "name": "삼성전자", "buy_price": 10000,
                "current_price": 12000, "profit_rate": 20.0, "is_overseas": False,
                "holding_days": 58, "highest_since": pd.Timestamp("2026-06-01")}]

    with patch("api.get_chart_data", return_value=df), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)):
        res = auto_trade.analyze_holdings(entries)["005930"]

    assert res["holding_days"] == 58
    assert res["has_buy_record"] is True          # 입력값이 있으므로 보유일 '-'가 아님
    assert res["highest_price"] > 12000           # 매수일 이후 실제 고가에서 유도


def test_analyze_holdings_without_buy_date_has_no_history(_no_db):
    """매수일 미입력이면 보유일수 0·기록 없음으로 남아 시간청산이 걸리지 않는다."""
    from modules import auto_trade

    entries = [{"code": "005930", "name": "삼성전자", "buy_price": 10000,
                "current_price": 12000, "profit_rate": 20.0, "is_overseas": False}]

    with patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)):
        res = auto_trade.analyze_holdings(entries)["005930"]

    assert res["holding_days"] == 0
    assert res["has_buy_record"] is False
    assert "시간청산" not in res.get("reason", "")


def test_analyze_manual_positions_derives_entry_fields():
    """매수일을 넣으면 보유일수와 TS 앵커 기준일이 함께 전달된다."""
    captured = {}

    def _fake(entries, **kw):
        captured["entries"] = entries
        return {}

    from datetime import date, timedelta
    buy_date = date.today() - timedelta(days=30)
    positions = [{"code": "005930", "name": "삼성전자", "is_overseas": False,
                  "buy_price": 10000.0, "qty": 10, "buy_date": buy_date,
                  "current_price": 12000.0, "profit_rate": 20.0}]

    with patch("modules.auto_trade.analyze_holdings", side_effect=_fake), \
         patch("modules.auto_trade.get_restricted_stocks", return_value={}):
        account._analyze_manual_positions(positions)

    entry = captured["entries"][0]
    assert entry["holding_days"] == 30
    assert entry["highest_since"] == buy_date


def test_run_holding_analysis_passes_holding_quantity():
    """잔고 수량을 함께 넘겨야 '진입이 조회 구간보다 과거인지'를 판별할 수 있다."""
    captured = {}

    def _fake(entries, **kw):
        captured["entries"] = entries
        return {}

    domestic = [{"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "42",
                 "pchs_avg_pric": "10000", "prpr": "12000", "evlu_pfls_rt": "20.0"}]

    with patch("modules.auto_trade.analyze_holdings", side_effect=_fake):
        account.run_holding_analysis(domestic, [])

    assert captured["entries"][0]["qty"] == 42


def test_analyze_holdings_sends_quantity_to_broker_lookup(_no_db):
    """DB에 진입일이 없으면 잔고 수량과 함께 증권사 체결 이력을 재생한다."""
    from datetime import datetime, timedelta
    from modules import auto_trade

    d = (datetime.now() - timedelta(days=200)).strftime("%Y%m%d")
    entries = [{"code": "950160", "name": "코오롱티슈진", "buy_price": 107833, "qty": 30,
                "current_price": 13200, "profit_rate": -87.75, "is_overseas": False}]

    with patch("api.get_period_entry_dates", return_value={"950160": d}) as spy, \
         patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("하락", 0.0)):
        res = auto_trade.analyze_holdings(entries)["950160"]

    assert spy.call_args.kwargs["qty_map"] == {"950160": 30}
    assert res["holding_days"] == 200


def test_entry_info_reports_replayed_quantity(tmp_path):
    """진입일과 함께 재생 수량을 돌려준다 — 호출부가 DB 이력 절단을 판별할 수 있어야 한다."""
    import sqlite3
    from modules import db_manager

    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (code TEXT, time TEXT, type TEXT, qty TEXT, order_status TEXT)")
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?)", [
        ("102780", "2026-07-02 09:59:19", "현금매수(외부)", "1", "체결"),
        ("102780", "2026-07-16 09:16:29", "현금매수(외부)", "1", "체결"),
    ])
    conn.commit()
    conn.close()

    mgr = db_manager.DBManager.__new__(db_manager.DBManager)

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    mgr._get_conn = _conn
    assert mgr.get_position_entry_info(["102780"]) == {"102780": {"date": "2026-07-02", "qty": 2}}


def test_analyze_holdings_keeps_db_date_when_broker_agrees(_no_db):
    """증권사 이력이 DB와 같으면 그대로 쓴다 (실측: 395160은 DB 재생 373주 = 잔고 373주)."""
    from datetime import datetime, timedelta
    from modules import auto_trade

    d = datetime.now() - timedelta(days=37)
    entries = [{"code": "395160", "name": "KODEX AI반도체TOP2플러스", "buy_price": 41936,
                "qty": 373, "current_price": 34470, "profit_rate": -17.8, "is_overseas": False}]

    with patch("modules.db_manager.db.get_position_entry_info",
               return_value={"395160": {"date": d.strftime("%Y-%m-%d"), "qty": 373}}), \
         patch("api.get_period_entry_dates", return_value={"395160": d.strftime("%Y%m%d")}), \
         patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("하락", 0.0)):
        res = auto_trade.analyze_holdings(entries)["395160"]

    assert res["holding_days"] == 37


def test_analyze_holdings_never_shorter_than_broker_history(_no_db):
    """DB가 증권사 이력보다 늦은 진입일을 주면 더 이른 쪽(증권사)을 쓴다.

    [불변식] 보유일수는 확인된 이력보다 짧아지면 안 된다. DB는 증권사 이력의 부분 사본이라
    외부(HTS·MTS) 매매분은 시스템 사용 시작일이 진입일처럼 보인다.
    """
    from datetime import datetime, timedelta
    from modules import auto_trade

    db_date = (datetime.now() - timedelta(days=37)).strftime("%Y-%m-%d")
    broker_date = (datetime.now() - timedelta(days=107)).strftime("%Y%m%d")
    entries = [{"code": "102780", "name": "KODEX 삼성그룹", "buy_price": 20402, "qty": 228,
                "current_price": 24315, "profit_rate": 19.17, "is_overseas": False}]

    with patch("modules.db_manager.db.get_position_entry_info",
               return_value={"102780": {"date": db_date, "qty": 2}}), \
         patch("api.get_period_entry_dates", return_value={"102780": broker_date}), \
         patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)):
        res = auto_trade.analyze_holdings(entries)["102780"]

    assert res["holding_days"] == 107      # DB만 믿었다면 37일


def test_analyze_holdings_keeps_db_date_when_broker_is_later(_no_db):
    """반대로 증권사 이력이 더 늦으면(조회 구간 절단 등) DB 진입일을 지킨다."""
    from datetime import datetime, timedelta
    from modules import auto_trade

    db_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    broker_date = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
    entries = [{"code": "005930", "name": "삼성전자", "buy_price": 10000, "qty": 10,
                "current_price": 12000, "profit_rate": 20.0, "is_overseas": False}]

    with patch("modules.db_manager.db.get_position_entry_info",
               return_value={"005930": {"date": db_date, "qty": 10}}), \
         patch("api.get_period_entry_dates", return_value={"005930": broker_date}), \
         patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)):
        res = auto_trade.analyze_holdings(entries)["005930"]

    assert res["holding_days"] == 200


def test_db_date_beats_latest_buy_when_broker_fails(_no_db):
    """증권사 조회가 실패해도 최근 매수일로 되돌아가지 않는다 (DB 재생일이 하한)."""
    from datetime import datetime, timedelta
    from modules import auto_trade

    db_first = (datetime.now() - timedelta(days=37)).strftime("%Y-%m-%d")
    latest_buy = (datetime.now() - timedelta(days=23)).strftime("%Y-%m-%d %H:%M:%S")
    entries = [{"code": "102780", "name": "KODEX 삼성그룹", "buy_price": 20402, "qty": 228,
                "current_price": 24315, "profit_rate": 19.17, "is_overseas": False}]

    with patch("modules.db_manager.db.get_position_entry_info",
               return_value={"102780": {"date": db_first, "qty": 2}}), \
         patch("modules.db_manager.db.get_latest_buy_trades",
               return_value={"102780": {"time": latest_buy, "reason": "매수"}}), \
         patch("api.get_period_entry_dates", return_value={}), \
         patch("api.get_chart_data", return_value=_make_df()), \
         patch("api.chart_overlay_price", side_effect=lambda p, o=False: p), \
         patch("api.is_domestic_etf_etn", return_value=False), \
         patch("modules.analysis.check_smart_money_turnaround", return_value=(False, "")), \
         patch("modules.analysis.get_market_regime", return_value=("상승", 0.0)):
        res = auto_trade.analyze_holdings(entries)["102780"]

    assert res["holding_days"] == 37       # 최근 매수 기준이었다면 23일


def test_manual_positions_render_into_shared_tables():
    """수동 분석 결과는 [9]-2와 같은 표 빌더로 그려진다 (국내/해외 분리)."""
    positions = [
        {"code": "005930", "name": "삼성전자", "is_overseas": False, "buy_price": 10000.0,
         "qty": 10, "buy_date": None, "current_price": 12000.0, "profit_rate": 20.0},
        {"code": "AAPL", "name": "APPLE", "is_overseas": True, "buy_price": 200.0,
         "qty": 5, "buy_date": None, "current_price": 215.0, "profit_rate": 7.5},
    ]
    analysis_map = {
        "005930": {"action": "hold", "score": 8.2, "state": "매수", "state_color": "[red]"},
        "AAPL": {"action": "sell", "score": 2.0, "state": "매도",
                 "reason": "손절(-8.0%)", "unmanaged": UNMANAGED_OVERSEAS},
    }

    with patch("modules.db_manager.db.get_stock_strategy", return_value=None), \
         patch("modules.db_manager.db.get_latest_buy_trade", return_value=None), \
         patch.object(config.console, "print") as pr:
        account._print_manual_positions(positions, analysis_map)

    from rich.table import Table as RichTable
    titles = [c.args[0].title for c in pr.call_args_list
              if c.args and isinstance(c.args[0], RichTable)]
    assert any("[국내] 포지션 분석" in t for t in titles)
    assert any("[해외] 포지션 분석" in t for t in titles)

    printed = " ".join(c.args[0] for c in pr.call_args_list
                       if c.args and isinstance(c.args[0], str))
    assert "청산 신호" in printed                     # 해외 종목의 청산 사유 각주
    assert "손절(-8.0%)" in printed
    # 미관리 여부는 표의 상태 칸에서만 알린다 — 각주에는 중복 표기하지 않는다
    assert UNMANAGED_OVERSEAS not in printed


def test_run_holding_analysis_normalizes_overseas_price():
    """해외 종목의 현재가 미제공 시 평가금액에서 역산해 넘긴다."""
    captured = {}

    def _fake(entries, **kw):
        captured["entries"] = entries
        return {}

    ovrs = [{"ovrs_pdno": "AAPL", "ovrs_item_name": "APPLE", "ovrs_cblc_qty": "10",
             "pchs_avg_pric": "200", "ovrs_now_pric": "0", "frcr_evlu_pfls_amt": "100",
             "evlu_pfls_rt": "5.0"}]

    with patch("modules.auto_trade.analyze_holdings", side_effect=_fake):
        account.run_holding_analysis([], ovrs)

    entry = captured["entries"][0]
    assert entry["is_overseas"] is True
    assert entry["current_price"] == pytest.approx(210.0)   # (10*200 + 100) / 10
