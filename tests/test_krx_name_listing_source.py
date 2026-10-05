"""종목명·상장목록이 KRX Open API 를 먼저 쓰는가 (2026-10-04).

· 종목명: KIS 마스터 → KRX 상장목록(Open API 스냅샷). 마스터에 없는 신규 상장·ETF 도 이름을
  얻는다. 둘 다 모르면 코드 그대로(네이버 3순위는 2026-10-05 제거).
· 상장목록: Open API 하나(코넥스까지 준다). FDR CSV 폴백은 2026-10-05 제거.
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


def test_unknown_code_returns_code_without_network():
    """마스터·상장목록이 모두 모르면 코드 그대로 — 네이버를 더는 묻지 않는다."""
    session = MagicMock()
    with patch("modules.analysis.get_stock_name_from_master", return_value=None), \
         patch.object(krx_daily, "get_listing_map", return_value={}), \
         patch("api.charts._api") as api_mod:
        api_mod.return_value.session = session
        from api import charts
        assert charts.get_stock_name_by_code("999990", False) == "999990"
    session.get.assert_not_called()


def test_listing_comes_from_openapi():
    oa = {"005930": {"name": "삼성전자", "marcap": 1.0, "market": "KOSPI"}}
    with patch.object(krx_daily, "_listing_map_from_openapi", return_value=dict(oa)):
        out = krx_daily.get_listing_map(use_cache=False)
    assert out == oa
