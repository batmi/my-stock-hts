"""종목명·상장목록이 KRX Open API 를 먼저 쓰는가 (2026-10-04).

· 종목명: KIS 마스터 → KRX 상장목록(Open API 스냅샷) → 네이버. 마스터에 없는 신규 상장·ETF 가
  네트워크(네이버) 없이 이름을 얻는다.
· 상장목록: Open API 가 코넥스까지 주므로 FDR 보충을 부르지 않는다.
"""
from unittest.mock import MagicMock, patch

from modules import krx_daily


def test_name_from_krx_listing_before_naver():
    session = MagicMock()
    with patch("modules.analysis.get_stock_name_from_master", return_value=None), \
         patch.object(krx_daily, "get_listing_map", return_value={"0080G0": {"name": "KODEX 신규"}}), \
         patch("api.charts._api") as api_mod:
        api_mod.return_value.session = session
        from api import charts
        assert charts.get_stock_name_by_code("0080G0", False) == "KODEX 신규"
    session.get.assert_not_called()


def test_name_falls_back_to_naver_when_not_listed():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"stockName": "네이버이름"}
    with patch("modules.analysis.get_stock_name_from_master", return_value=None), \
         patch.object(krx_daily, "get_listing_map", return_value={}), \
         patch("api.charts._api") as api_mod:
        api_mod.return_value.session.get.return_value = resp
        from api import charts
        assert charts.get_stock_name_by_code("999990", False) == "네이버이름"


def test_openapi_listing_skips_fdr_supplement():
    oa = {"005930": {"name": "삼성전자", "marcap": 1.0, "market": "KOSPI"}}
    with patch.object(krx_daily, "_listing_map_from_openapi", return_value=dict(oa)), \
         patch.object(krx_daily, "_listing_map_from_fdr") as fdr:
        out = krx_daily.get_listing_map(use_cache=False)
    fdr.assert_not_called()
    assert out == oa
