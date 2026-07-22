"""DART 주요사항보고서 '결정' 계열의 접수일자 복원 테스트.

DART API는 계열에 따라 날짜 필드 제공 여부가 다르다(실측 2026-07-22, 삼성전자):
  - 공시목록 계열(list/elestock/majorstock): rcept_dt 제공
  - 주요사항보고서 '결정' 계열(자기주식·메자닌·무상증자·감자): rcept_dt **미제공**
    → 응답 키에 아예 없고, 접수번호(14자리) 앞 8자리가 접수일자다.

복원이 없으면 메뉴 6-7(수급·물량 신호)의 '일자'/'발행결정일' 칸이 공백이 되고,
rcept_dt 기준 정렬이 전부 빈 문자열 비교가 되어 최신순 정렬도 무효화된다.
"""
from unittest.mock import patch

import pytest

from modules import dart_api


# ==========================================================
# _rcept_date — 접수번호에서 접수일자 복원
# ==========================================================

def test_rcept_date_prefers_existing_field():
    """rcept_dt가 정상 제공되면 그대로 쓴다 (공시목록 계열)."""
    assert dart_api._rcept_date({"rcept_dt": "20260713", "rcept_no": "20991231000001"}) == "20260713"


def test_rcept_date_normalizes_dashes():
    assert dart_api._rcept_date({"rcept_dt": "2026-07-13"}) == "20260713"


def test_rcept_date_falls_back_to_rcept_no():
    """결정 계열: rcept_dt가 없으면 접수번호 앞 8자리로 복원한다."""
    assert dart_api._rcept_date({"rcept_no": "20260713000395"}) == "20260713"


@pytest.mark.parametrize("row", [
    {},                                      # 둘 다 없음
    {"rcept_no": ""},                        # 빈 접수번호
    {"rcept_no": "abcd1234000001"},          # 숫자가 아님
    {"rcept_no": "2026071"},                 # 8자리 미만
    {"rcept_dt": None, "rcept_no": None},    # None
])
def test_rcept_date_returns_blank_when_unrecoverable(row):
    """복원 불가 시 빈 문자열 — 화면에서 '-'로 처리되며 예외를 던지지 않는다."""
    assert dart_api._rcept_date(row) == ""


def test_fill_rcept_dt_injects_into_rows():
    rows = [{"rcept_no": "20260713000395"}, {"rcept_no": "20260129000003"}]
    out = dart_api._fill_rcept_dt(rows)
    assert [r["rcept_dt"] for r in out] == ["20260713", "20260129"]


def test_fill_rcept_dt_handles_non_list():
    assert dart_api._fill_rcept_dt(None) == []
    assert dart_api._fill_rcept_dt({"rcept_no": "x"}) == []


# ==========================================================
# 자기주식 결정 — 일자 복원 + 최신순 정렬
# ==========================================================

def _fake_call_dart(endpoint, params):
    """rcept_dt가 없는 실제 DART 결정 계열 응답을 모사."""
    if endpoint == "tsstkAqDecsn.json":
        return [{"rcept_no": "20260107000715", "aqpln_stk_ostk": "1,000",
                 "aqpln_prc_ostk": "100,000,000", "aqexpd_bgd": "2026년 01월",
                 "aqexpd_edd": "2026년 04월", "aq_pp": "주주가치 제고"}]
    if endpoint == "tsstkDpDecsn.json":
        return [{"rcept_no": "20260713000395", "dppln_stk_ostk": "1,132,477",
                 "dppln_prc_ostk": "322,800,000,000", "dpprpd_bgd": "2026년 07월",
                 "dpprpd_edd": "2026년 07월", "dp_pp": "임원 등 성과급의 자기주식 지급"}]
    if endpoint == "tsstkAqTrctrCnsDecsn.json":
        return [{"rcept_no": "20260318001062", "ctr_prc": "50,000,000,000",
                 "ctr_pd_bgd": "2026년 03월", "ctr_pd_edd": "2026년 09월"}]
    return []


def test_treasury_decisions_restore_date_and_sort_desc():
    """일자가 복원되고 최신순으로 정렬되어야 한다."""
    with patch.object(dart_api, "_api") as mock_api:
        mock_api.return_value.get_dart_corp_map.return_value = {"005930": "00126380"}
        mock_api.return_value.call_dart.side_effect = _fake_call_dart
        rows = dart_api.get_dart_treasury_decisions("005930", "20260101", "20260722")

    assert [r["rcept_dt"] for r in rows] == ["20260713", "20260318", "20260107"]
    assert [r["kind"] for r in rows] == ["처분", "신탁체결", "취득"]
    assert all(r["rcept_dt"] for r in rows), "일자가 비어 있으면 화면에 공백으로 출력된다"


def test_treasury_decisions_survive_missing_rcept_no():
    """접수번호까지 없는 비정상 행이 섞여도 예외 없이 처리된다."""
    def broken(endpoint, params):
        if endpoint == "tsstkDpDecsn.json":
            return [{"dppln_stk_ostk": "10"}]   # rcept_no·rcept_dt 모두 없음
        return []

    with patch.object(dart_api, "_api") as mock_api:
        mock_api.return_value.get_dart_corp_map.return_value = {"005930": "00126380"}
        mock_api.return_value.call_dart.side_effect = broken
        rows = dart_api.get_dart_treasury_decisions("005930", "20260101", "20260722")

    assert rows and rows[0]["rcept_dt"] == ""


# ==========================================================
# 메자닌(CB/BW/EB)
# ==========================================================

def test_bond_issue_detail_restores_date():
    with patch.object(dart_api, "_api") as mock_api:
        mock_api.return_value.get_dart_corp_map.return_value = {"035720": "00258801"}
        mock_api.return_value.call_dart.return_value = [
            {"rcept_no": "20250527000422", "bd_fta": "50,200,000,000", "cv_prc": "37,527"}]
        rows = dart_api.get_dart_bond_issue_detail("035720", "20230101", "20260722", kind="CB")

    assert rows[0]["rcept_dt"] == "20250527"


def test_bw_endpoint_name_is_valid():
    """신주인수권부사채 엔드포인트는 bdwtIsDecsn.

    기존 'bwbdIsDecsn'는 DART에 존재하지 않는 URL이라 status 101(잘못된 URL)을 받고
    BW 오버행이 조회 자체가 되지 않았다. (실측 2026-07-22)
    """
    assert dart_api._BOND_ENDPOINTS["BW"] == "bdwtIsDecsn.json"
    assert dart_api._BOND_ENDPOINTS["CB"] == "cvbdIsDecsn.json"
    assert dart_api._BOND_ENDPOINTS["EB"] == "exbdIsDecsn.json"


def test_bond_issue_detail_uses_mapped_endpoint():
    """kind에 따라 매핑된 엔드포인트가 실제로 호출되는지 확인."""
    for kind, expect in (("CB", "cvbdIsDecsn.json"), ("BW", "bdwtIsDecsn.json"), ("EB", "exbdIsDecsn.json")):
        with patch.object(dart_api, "_api") as mock_api:
            mock_api.return_value.get_dart_corp_map.return_value = {"035720": "00258801"}
            mock_api.return_value.call_dart.return_value = []
            dart_api.get_dart_bond_issue_detail("035720", "20230101", "20260722", kind=kind)
            assert mock_api.return_value.call_dart.call_args.args[0] == expect


# ==========================================================
# 무상증자·감자 (동일 계열 — _decsn_rows 공통 경로)
# ==========================================================

@pytest.mark.parametrize("fn, endpoint", [
    (dart_api.get_dart_free_increase_detail, "fricDecsn.json"),
    (dart_api.get_dart_capital_reduction_detail, "crDecsn.json"),
])
def test_decsn_family_restores_date(fn, endpoint):
    with patch.object(dart_api, "_api") as mock_api:
        mock_api.return_value.get_dart_corp_map.return_value = {"005930": "00126380"}
        mock_api.return_value.call_dart.return_value = [{"rcept_no": "20260318001203"}]
        rows = fn("005930", "20260101", "20260722")

    assert rows[0]["rcept_dt"] == "20260318"
