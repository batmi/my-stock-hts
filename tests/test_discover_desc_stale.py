"""관심 종목 탐색(7-4)이 오래된 업종 목록(KRX-DESC)으로도 동작하고, 그 사실을 밝히는가.

[왜 · 2026-10-03] 캐시 저장소의 업종 파일이 2026-09-17 에서 멈춰, 기본 10일 창으로는 못 찾아
'KRX 상장목록·업종 조회 실패' 로 메뉴가 통째로 죽었다. 업종 분류는 거의 안 바뀌므로 오래된 파일을
쓰되, 업종을 모르는 종목이 방어주·지주회사 규칙을 조용히 통과하지 않게 표시한다.
"""
from datetime import date

import pandas as pd

import config
from modules import krx_daily
from modules.manage import discover


def _krx():
    return pd.DataFrame([
        {"Code": "000010", "Name": "가전자", "Market": "KOSPI", "Marcap": 9e12, "Dept": ""},
        {"Code": "000020", "Name": "나통신", "Market": "KOSPI", "Marcap": 8e12, "Dept": ""},
        {"Code": "000030", "Name": "다신규", "Market": "KOSDAQ", "Marcap": 7e12, "Dept": ""},
    ])


def _desc():
    return pd.DataFrame([
        {"Code": "000010", "Industry": "반도체 제조업", "Products": "메모리"},
        {"Code": "000020", "Industry": "전기 통신업", "Products": "통신"},
    ])


def test_old_industry_file_is_used_and_disclosed(monkeypatch):
    def fake_listing(kind, lookback=None, on=None):
        return _krx() if kind == "KRX" else _desc()

    monkeypatch.setattr(krx_daily, "fdr_listing", fake_listing)
    monkeypatch.setattr(krx_daily, "last_listing_date",
                        lambda kind: "2026-09-17" if kind == "KRX-DESC" else None)
    monkeypatch.setattr(config.session, "stock_data", {"stocks_kr": [], "etfs_kr": []}, raising=False)
    printed = []
    monkeypatch.setattr(config.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    cands, steps, defs, n0, n_kept = discover._fetch_candidates(10, 500, True, seed=1)

    assert n0 == 3
    assert {c["code"] for c in cands} == {"000010", "000030"}      # 통신은 방어주로 제외
    assert next(c for c in cands if c["code"] == "000030")["industry"] == "업종 미상"
    assert any("업종 미상" in p and "2026-09-17" in p for p in printed), printed


def test_staleness_note():
    today = date(2026, 10, 3)
    assert discover._desc_staleness_note("2026-10-01", 0, today) is None
    assert discover._desc_staleness_note(None, 0, today) is None              # 정상 경로(날짜 모름·최신)
    assert "16일 전" in discover._desc_staleness_note("2026-09-17", 0, today)
    assert "3종목" in discover._desc_staleness_note("2026-10-01", 3, today)


def test_missing_industry_file_fails_loudly(monkeypatch):
    monkeypatch.setattr(krx_daily, "fdr_listing",
                        lambda kind, lookback=None, on=None: _krx() if kind == "KRX" else None)
    monkeypatch.setattr(krx_daily, "last_listing_date", lambda kind: None)
    monkeypatch.setattr(config.session, "stock_data", {"stocks_kr": [], "etfs_kr": []}, raising=False)
    try:
        discover._fetch_candidates(10, 500, True)
    except RuntimeError as e:
        assert "업종 목록" in str(e)
    else:
        raise AssertionError("업종 목록이 없으면 조용히 빈 규칙으로 진행하면 안 된다")


def test_frozen_listings_look_back_far_enough(monkeypatch):
    """업종·폐지 목록은 기본 10일보다 넓게 거슬러 찾는다 — 캐시 저장소가 09-17 에서 멈췄다."""
    tried = []

    def fake_read_csv(url, **k):
        tried.append(url)
        raise OSError("404")

    monkeypatch.setattr(krx_daily, "_lazy_import", lambda: None)
    monkeypatch.setattr(krx_daily.pd, "read_csv", fake_read_csv)
    assert krx_daily.fdr_listing("KRX-DESC", on="2026-10-03") is None
    assert len(tried) >= 60
    tried.clear()
    krx_daily.fdr_listing("KRX", on="2026-10-03")
    assert len(tried) == 10                  # 매일 갱신되는 목록은 종전 창 그대로
