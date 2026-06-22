"""마스터 파일 기반 시장 구분(_get_market_type_by_master) 테스트.

KOSPI/KOSDAQ 마스터 양쪽을 조회하여 정확히 분류하는지 검증한다.
"""
from unittest.mock import patch

import modules.analysis as analysis


def _reset_master_cache():
    analysis._MASTER_KOSDAQ_CODES = None
    analysis._MASTER_KOSPI_CODES = None


def test_market_type_uses_both_masters():
    def fake_master(market_type):
        if market_type == "KOSDAQ":
            return [{"code": "035720"}, {"code": "247540"}]  # 카카오게임즈 등(예시)
        return [{"code": "005930"}, {"code": "000660"}]      # 삼성전자, SK하이닉스(KOSPI)

    _reset_master_cache()
    try:
        with patch("modules.analysis._get_master_stock_list", side_effect=fake_master):
            assert analysis._get_market_type_by_master("247540") == "KOSDAQ"
            assert analysis._get_market_type_by_master("005930") == "KOSPI"
            # 어느 마스터에도 없는 코드는 KOSPI로 폴백
            assert analysis._get_market_type_by_master("999999") == "KOSPI"
    finally:
        _reset_master_cache()


def test_market_type_loads_kospi_master():
    """KOSPI 마스터도 로드되는지(양쪽 호출) 확인."""
    called = []

    def fake_master(market_type):
        called.append(market_type)
        return [{"code": "005930"}] if market_type == "KOSPI" else [{"code": "035720"}]

    _reset_master_cache()
    try:
        with patch("modules.analysis._get_master_stock_list", side_effect=fake_master):
            analysis._get_market_type_by_master("005930")
        assert "KOSPI" in called and "KOSDAQ" in called
    finally:
        _reset_master_cache()
