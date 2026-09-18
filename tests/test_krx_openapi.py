"""KRX Open API 적재·조회 — 네트워크 없이 _call 을 흉내 내어 규약을 고정한다.

[배경 · 2026-09-17] data.krx.co.kr 스크래핑(pykrx)이 약관 위반으로 IP 차단 → 공식 Open API 로
옮겼다. 조회 단위가 '기준일 하루 × 전 종목'이라 날짜별 스냅샷을 SQLite 에 누적한다.
"""
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

import config
from modules import krx_openapi as oa

NOW = datetime(2026, 9, 18, 9, 0)          # 금요일 09:00 → 최신 가능일 = 09-17(목)


def _stock_rows(dd, closes):
    return [{"BAS_DD": dd, "ISU_CD": code, "ISU_NM": f"N{code}", "MKT_NM": "KOSPI", "SECT_TP_NM": "",
             "TDD_CLSPRC": str(c), "CMPPREVDD_PRC": "0", "FLUC_RT": "0", "TDD_OPNPRC": str(c - 1),
             "TDD_HGPRC": str(c + 2), "TDD_LWPRC": str(c - 2), "ACC_TRDVOL": "100", "ACC_TRDVAL": "1",
             "MKTCAP": str(c * 1000), "LIST_SHRS": "1000"} for code, c in closes.items()]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KRX_OPENAPI_DB_PATH", str(tmp_path / "oa.db"), raising=False)
    monkeypatch.setattr(config, "KRX_OPENAPI_CALL_INTERVAL_SEC", 0.0, raising=False)
    monkeypatch.setattr(config, "KRX_OPENAPI_MAX_INLINE_CALLS", 60, raising=False)
    monkeypatch.setenv(oa.ENV_KEY, "TESTKEY")
    oa._DISABLED_UNTIL[0] = 0.0
    calls = []

    def fake_call(api_id, dd):
        calls.append((api_id, dd))
        if dd == "20260915":                       # 휴장일처럼 빈 응답
            return []
        if api_id in oa.STOCK_APIS:
            if api_id == "stk_bydd_trd":
                return _stock_rows(dd, {"005930": 100 + int(dd[-2:]), "000660": 150})
            if api_id == "ksq_bydd_trd":
                return _stock_rows(dd, {"247540": 500 + int(dd[-2:])})     # 코스닥은 다른 종목
            if api_id == "etf_bydd_trd":
                rows = _stock_rows(dd, {"0080G0": 9000})
                for r in rows:
                    r.pop("MKT_NM")                                    # ETF 응답엔 시장 칸이 없다
                return rows
            return []
        if api_id == "kospi_dd_trd":
            return [{"BAS_DD": dd, "IDX_CLSS": "KOSPI", "IDX_NM": "코스피 200", "CLSPRC_IDX": "1000.5",
                     "OPNPRC_IDX": "999", "HGPRC_IDX": "1001", "LWPRC_IDX": "998", "ACC_TRDVOL": "7"},
                    {"BAS_DD": dd, "IDX_CLSS": "KOSPI", "IDX_NM": "코스피", "CLSPRC_IDX": "6700",
                     "OPNPRC_IDX": "", "HGPRC_IDX": "", "LWPRC_IDX": "", "ACC_TRDVOL": ""}]
        if api_id == "fut_bydd_trd":
            return [{"BAS_DD": dd, "PROD_NM": "코스피200 선물", "MKT_NM": "정규", "ISU_CD": "A", "ISU_NM": "근월",
                     "TDD_CLSPRC": "250", "TDD_OPNPRC": "249", "TDD_HGPRC": "251", "TDD_LWPRC": "248",
                     "ACC_TRDVOL": "10", "ACC_OPNINT_QTY": "900"},
                    {"BAS_DD": dd, "PROD_NM": "코스피200 선물", "MKT_NM": "정규", "ISU_CD": "B", "ISU_NM": "차월",
                     "TDD_CLSPRC": "252", "TDD_OPNPRC": "251", "TDD_HGPRC": "253", "TDD_LWPRC": "250",
                     "ACC_TRDVOL": "1", "ACC_OPNINT_QTY": "100"},
                    {"BAS_DD": dd, "PROD_NM": "코스피200 선물", "MKT_NM": "야간", "ISU_CD": "A", "ISU_NM": "근월(야간)",
                     "TDD_CLSPRC": "251", "TDD_OPNPRC": "250", "TDD_HGPRC": "252", "TDD_LWPRC": "249",
                     "ACC_TRDVOL": "3", "ACC_OPNINT_QTY": ""}]
        if api_id == "gold_bydd_trd":
            return [{"BAS_DD": dd, "ISU_CD": oa.GOLD_ISU_CD, "ISU_NM": "금 99.99K", "TDD_CLSPRC": "190000",
                     "TDD_OPNPRC": "189000", "TDD_HGPRC": "191000", "TDD_LWPRC": "188000", "ACC_TRDVOL": "55"}]
        if api_id in oa.BASE_INFO_APIS:
            return [{"ISU_CD": "KR7005930003", "ISU_SRT_CD": "005930", "ISU_NM": "삼성전자보통주",
                     "ISU_ABBRV": "삼성전자", "MKT_TP_NM": "KOSPI", "SECUGRP_NM": "주권", "LIST_DD": "19750611",
                     "KIND_STKCERT_TP_NM": "보통주", "PARVAL": "100", "LIST_SHRS": "1000"}]
        return []

    with patch.object(oa, "_call", side_effect=fake_call):
        yield calls


# ── 날짜 규약 ────────────────────────────────────────────
def test_latest_available_is_the_previous_weekday_after_publish_hour():
    assert oa.latest_available_dd(datetime(2026, 9, 18, 9, 0)) == "20260917"
    assert oa.latest_available_dd(datetime(2026, 9, 18, 7, 59)) == "20260916"   # 08:00 전엔 하루 더 전
    assert oa.latest_available_dd(datetime(2026, 9, 21, 10, 0)) == "20260918"   # 월요일 → 금요일


def test_weekends_are_never_asked(store):
    oa.ensure(["kospi_dd_trd"], "20260912", "20260917", now=NOW)
    asked = {dd for _, dd in store}
    assert "20260912" not in asked and "20260913" not in asked
    assert asked == {"20260914", "20260915", "20260916", "20260917"}


# ── 적재·재사용 ──────────────────────────────────────────
def test_stock_daily_is_cut_from_the_daily_snapshots(store):
    df = oa.stock_daily("005930", 4, now=NOW)
    assert list(df["date"]) == ["20260914", "20260916", "20260917"]   # 09-15 는 휴장(빈 응답)
    assert df.attrs["source"] == "OPENAPI"
    assert float(df["close"].iloc[-1]) == 117.0
    n_first = len(store)
    oa.stock_daily("000660", 4, now=NOW)
    assert len(store) == n_first, "같은 날짜 스냅샷을 다시 받았다 — 종목이 달라도 호출은 0이어야 한다"


def test_an_empty_day_is_remembered_as_a_holiday_but_recent_ones_are_retried(store, monkeypatch):
    oa.ensure(oa.STOCK_APIS, "20260914", "20260917", now=NOW)
    n = len(store)
    oa.ensure(oa.STOCK_APIS, "20260914", "20260917", now=NOW)
    assert len(store) == n                                    # TTL 안에서는 다시 묻지 않는다
    with oa._DB_LOCK, oa._connect() as conn:
        conn.execute("UPDATE fetched SET ts = ts - 7200 WHERE bas_dd='20260915'")
    oa.ensure(oa.STOCK_APIS, "20260914", "20260917", now=NOW)
    assert [dd for _, dd in store[n:]] == ["20260915"] * len(oa.STOCK_APIS)  # 최근 빈 날만 TTL 뒤 재확인


def test_a_gap_larger_than_the_inline_cap_returns_none_not_a_partial_history(store, monkeypatch, caplog):
    monkeypatch.setattr(config, "KRX_OPENAPI_MAX_INLINE_CALLS", 2, raising=False)
    assert oa.stock_daily("005930", 10, now=NOW) is None
    assert any("krx_openapi_backfill" in r.getMessage() for r in caplog.records)


def test_unauthorized_disables_the_client_for_a_while(tmp_path, monkeypatch):
    """미승인 서비스의 401 은 10분 쿨다운 — 매 요청마다 KRX 를 두드리지 않는다."""
    import requests

    class _Resp:
        status_code = 401
        text = '{"respMsg":"Unauthorized API Call","respCode":"401"}'

        def json(self):
            return {"respMsg": "Unauthorized API Call", "respCode": "401"}

    monkeypatch.setattr(config, "KRX_OPENAPI_DB_PATH", str(tmp_path / "oa.db"), raising=False)
    monkeypatch.setenv(oa.ENV_KEY, "TESTKEY")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(oa, "_throttle", lambda: None)
    oa._DISABLED_UNTIL[0] = 0.0
    with pytest.raises(oa.OpenAPIError) as ei:
        oa._call("kospi_dd_trd", "20260917")
    assert ei.value.status == 401
    assert not oa.is_available()
    assert oa.index_daily("KOSPI", 4, now=NOW) is None     # 쿨다운 중엔 묻지도 않는다
    oa._DISABLED_UNTIL[0] = 0.0


# ── 지수·선물·금·목록 ──────────────────────────────────────
def test_index_name_matching_ignores_spaces(store):
    df = oa.index_daily("KOSPI200", 4, now=NOW)
    assert float(df["close"].iloc[-1]) == 1000.5
    flat = oa.index_daily("KOSPI", 4, now=NOW)         # 시·고·저 빈값 → 종가로 평탄화
    assert float(flat["open"].iloc[-1]) == 6700.0


def test_front_month_is_the_contract_with_the_largest_open_interest(store):
    day = oa.k200_futures_daily("F", 4, now=NOW)
    night = oa.k200_futures_daily("CM", 4, now=NOW)
    assert float(day["close"].iloc[-1]) == 250.0        # OI 900 인 A, 252(B) 아님
    assert float(night["close"].iloc[-1]) == 251.0      # 같은 계약의 야간 봉


def test_gold_and_listing(store):
    g = oa.gold_daily(4, now=NOW)
    assert float(g["high"].iloc[-1]) == 191000.0 and float(g["volume"].iloc[-1]) == 55.0
    lst = oa.listing_map(now=NOW)
    assert lst["005930"]["name"] == "삼성전자" and lst["005930"]["market"] == "KOSPI"
    assert lst["005930"]["marcap"] == 117.0 * 1000
    assert lst["0080G0"]["market"] == "ETF" and lst["0080G0"]["name"] == "N0080G0"   # ETF 도 이름·시장이 나온다
    etf = oa.stock_daily("0080G0", 4, now=NOW)
    assert float(etf["close"].iloc[-1]) == 9000.0


def test_a_trading_halt_bar_with_zero_ohlc_is_dropped_but_empty_ohlc_is_flattened(store):
    oa.stock_daily("005930", 4, now=NOW)               # 먼저 적재해 두고 한 봉을 거래정지 모양으로 바꾼다
    with oa._DB_LOCK, oa._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("20260916", "005930", "KOSPI", "n", 0.0, 0.0, 0.0, 116.0, 0.0, 0.0, 0.0, 1000.0))
    df = oa.stock_daily("005930", 4, now=NOW)
    assert "20260916" not in list(df["date"])          # 거래정지(0원 봉)는 버린다
    flat = oa.index_daily("KOSPI", 4, now=NOW)         # 빈 시·고·저(None)는 종가로 평탄화
    assert float(flat["low"].iloc[-1]) == 6700.0


# ── 수정주가 ─────────────────────────────────────────────
def test_a_split_rescales_prior_bars_but_a_rights_issue_does_not():
    rows = [("20180502", 50000, 51000, 49000, 50000, 100, 1000), ("20180503", 50500, 51500, 49500, 50500, 100, 1000),
            ("20180504", 1010, 1030, 990, 1000, 5000, 50000)]
    out = oa.adjust_splits(rows)
    assert out[0][4] == pytest.approx(1000.0) and out[0][5] == pytest.approx(5000.0)
    assert out[2][4] == 1000
    rights = [("20200101", 100, 110, 90, 100, 10, 1000), ("20200102", 100, 110, 90, 100, 10, 2000)]
    assert oa.adjust_splits(rights)[0][4] == 100


# ── 호출부 통합 ──────────────────────────────────────────
def test_krx_daily_prefers_openapi_and_tops_up_today_from_fdr(store, monkeypatch):
    from modules import krx_daily
    krx_daily.clear_cache()
    today = datetime.now().strftime("%Y%m%d")
    tail = pd.DataFrame({"date": [today], "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [9.0]})
    monkeypatch.setattr(krx_daily, "_fetch_fdr", lambda code, s, e: tail)
    monkeypatch.setattr(oa, "latest_available_dd", lambda now=None: "20260917")
    df = krx_daily.get_daily("005930", lookback_days=4, use_cache=False)
    assert df.attrs["source"] == "OPENAPI+FDR"
    assert list(df["date"])[-2:] == ["20260917", today]


def test_pykrx_scraping_is_off_by_default(monkeypatch):
    from modules import krx_daily, krx_data
    assert config.KRX_WEB_SCRAPING_ALLOWED is False
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")
    assert krx_data.is_available() is False
    calls = []
    monkeypatch.setattr(krx_daily, "_fetch_pykrx", lambda *a: calls.append(a))
    monkeypatch.setattr(krx_daily, "_fetch_openapi", lambda *a: None)
    monkeypatch.setattr(krx_daily, "_fetch_fdr", lambda *a: None)
    krx_daily.clear_cache()
    krx_daily.get_daily("005930", lookback_days=4, use_cache=False)
    assert calls == [], "스크래핑이 꺼져 있는데 pykrx 를 불렀다"
