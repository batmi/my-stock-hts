"""관심 종목 탐색(7-4) — 상장목록은 KRX Open API, 업종은 DART(modules/industry.py).

[왜 · 2026-10-07] 업종 원천이던 KIND 상장법인목록(FDR 캐시 KRX-DESC)이 2026-09-17 에서 멈췄다.
 업종을 모르는 종목이 방어주·지주회사 규칙을 조용히 통과하지 않게 '업종 미상'과 그 수를 밝히고,
 하나도 모르면 메뉴를 실패로 알린다.
"""
import pandas as pd

import config
from modules import industry, krx_daily
from modules.manage import discover


def _krx():
    return pd.DataFrame([
        {"Code": "000010", "Name": "가전자", "Market": "KOSPI", "Marcap": 9e12, "Dept": ""},
        {"Code": "000020", "Name": "나통신", "Market": "KOSPI", "Marcap": 8e12, "Dept": ""},
        {"Code": "000030", "Name": "다신규", "Market": "KOSDAQ", "Marcap": 7e12, "Dept": ""},
    ])


def _openapi_listing(monkeypatch, frame=None):
    """상장목록은 Open API 하나다(2026-10-05 FDR 폴백 제거) — 그 자리를 frame 으로 채운다."""
    from modules import krx_openapi
    frame = _krx() if frame is None else frame
    monkeypatch.setattr(krx_openapi, "is_configured", lambda: True)
    monkeypatch.setattr(krx_openapi, "listing_map", lambda *a, **k: {
        r["Code"]: {"name": r["Name"], "market": r["Market"], "marcap": r["Marcap"],
                    "dept": r["Dept"], "kind": "보통주"} for _, r in frame.iterrows()})


def _industry(monkeypatch, known, why=None):
    asked = []

    def fake(codes, progress=None, today=None):
        asked.append(list(codes))
        return {c: known[c] for c in codes if c in known}, why

    monkeypatch.setattr(industry, "lookup", fake)
    return asked


def _quiet(monkeypatch):
    monkeypatch.setattr(config.session, "stock_data", {"stocks_kr": [], "etfs_kr": []}, raising=False)
    printed = []
    monkeypatch.setattr(config.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    return printed


def test_dart_industry_drives_rules_and_unknown_is_disclosed(monkeypatch):
    _openapi_listing(monkeypatch)
    _industry(monkeypatch, {"000010": "264", "000020": "61220"}, why="1건 조회 실패")
    printed = _quiet(monkeypatch)

    cands, steps, defs, n0, n_kept = discover._fetch_candidates(10, 500, True, seed=1)

    assert n0 == 3
    assert {c["code"] for c in cands} == {"000010", "000030"}      # 통신은 방어주로 제외
    by = {c["code"]: c for c in cands}
    assert by["000010"]["industry"] == "통신 및 방송 장비 제조업"
    assert by["000030"]["industry"] == "업종 미상"
    assert defs == [("나통신", "통신")]
    assert any("업종 미상" in p and "1종목" in p and "1건 조회 실패" in p for p in printed), printed


def test_known_empty_industry_is_also_unknown_to_rules(monkeypatch):
    """DART 가 업종 칸을 비운 종목('')도 규칙이 판단할 수 없다 — '업종 미상'으로 밝힌다."""
    _openapi_listing(monkeypatch)
    _industry(monkeypatch, {"000010": "264", "000020": "", "000030": "261"})
    printed = _quiet(monkeypatch)
    cands, *_ = discover._fetch_candidates(10, 500, True, seed=1)
    assert {c["code"]: c["industry"] for c in cands}["000020"] == "업종 미상"
    assert any("1종목" in p for p in printed)


def test_no_industry_at_all_fails_loudly(monkeypatch):
    _openapi_listing(monkeypatch)
    _industry(monkeypatch, {}, why="DART 호출 중단 중(연결이 3번 연달아 끊김)")
    _quiet(monkeypatch)
    try:
        discover._fetch_candidates(10, 500, True)
    except RuntimeError as e:
        assert "DART 업종 조회 실패" in str(e) and "연달아" in str(e)
    else:
        raise AssertionError("업종을 하나도 모르면 조용히 빈 규칙으로 진행하면 안 된다")


def test_industry_note():
    assert discover._industry_note(0, None) is None
    assert discover._industry_note(0, "중단") is None
    assert "3종목" in discover._industry_note(3, None)
    assert "다시 실행하면" in discover._industry_note(3, "중단")


def test_frozen_listings_look_back_far_enough(monkeypatch):
    """(krx_daily) 업종·폐지 목록은 기본 10일보다 넓게 거슬러 찾는다 — 감사 도구가 아직 쓴다."""
    tried = []

    def fake_get(url):
        tried.append(url)
        raise OSError("HTTP 404")

    monkeypatch.setattr(krx_daily, "_http_get_text", fake_get)
    assert krx_daily.fdr_listing("KRX-DESC", on="2026-10-03") is None
    assert len(tried) >= 60
    tried.clear()
    krx_daily.fdr_listing("KRX", on="2026-10-03")
    assert len(tried) == 10                  # 매일 갱신되는 목록은 종전 창 그대로


# ── 상장목록은 KRX Open API (2026-10-04) ───────────────────────────────────────────
def _oa_listing():
    return {
        "000010": {"name": "가전자", "market": "KOSPI", "marcap": 9e12, "dept": "", "kind": "보통주"},
        "000015": {"name": "가전자우", "market": "KOSPI", "marcap": 8.5e12, "dept": "", "kind": "구형우선주"},
        "00001K": {"name": "가전자2우B", "market": "KOSPI", "marcap": 8.4e12, "dept": "", "kind": "신형우선주"},
        "000040": {"name": "라관리", "market": "KOSDAQ", "marcap": 8e12, "dept": "관리종목(소속부없음)", "kind": "보통주"},
        "0080G0": {"name": "마신규", "market": "KOSDAQ", "marcap": 7e12, "dept": "우량기업부", "kind": "보통주"},
        "000050": {"name": "바폐지", "market": "KOSPI", "marcap": 0.0, "dept": "", "kind": "보통주"},
    }


def test_listing_from_openapi_and_industry_only_for_survivors(monkeypatch):
    from modules import krx_openapi
    monkeypatch.setattr(krx_openapi, "is_configured", lambda: True)
    monkeypatch.setattr(krx_openapi, "listing_map", lambda *a, **k: _oa_listing())
    asked = _industry(monkeypatch, {"000010": "264", "0080G0": "261"})
    monkeypatch.setattr(krx_daily, "fdr_listing",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("FDR 에 묻지 않는다")))
    _quiet(monkeypatch)

    cands, steps, _defs, n0, _n = discover._fetch_candidates(10, 500, True, seed=1)

    assert n0 == 5                                         # 시총 0(폐지 잔여 행)은 목록에서 빠진다
    assert {c["code"] for c in cands} == {"000010", "0080G0"}
    assert asked == [["000010", "0080G0"]]                 # DART 는 앞 규칙을 통과한 종목만 묻는다
    s = dict(steps)
    assert s["관리종목·투자주의환기"] == 1
    assert s["우선주·스팩·리츠"] == 2                       # 구형·신형 우선주 모두 '주식 종류'로 걸린다


def test_listing_does_not_fall_back_to_frozen_fdr(monkeypatch):
    """[2026-10-05] 인증키가 없으면 FDR(09-17 에서 멈춘 캐시) 시총 순위로 후보를 고르지 않는다 — 실패로 알린다."""
    from modules import krx_openapi
    monkeypatch.setattr(krx_openapi, "is_configured", lambda: False)
    asked = _industry(monkeypatch, {})
    _quiet(monkeypatch)
    try:
        discover._fetch_candidates(10, 500, True)
    except RuntimeError as e:
        assert "KRX_OPENAPI_KEY" in str(e)
    else:
        raise AssertionError("상장목록이 없는데 후보를 만들었다")
    assert asked == []
