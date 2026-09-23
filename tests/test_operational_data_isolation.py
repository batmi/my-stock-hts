"""테스트가 운영 데이터·실제 네트워크에 닿지 않는가 — requests 밖의 두 구멍(2026-09-24).

1) KRX Open API 스냅샷: config.KRX_OPENAPI_DB_PATH 는 import 때 DATA_DIR 로 계산된 **별도 상수**라
   테스트마다 DATA_DIR 을 바꿔도 따라오지 않았다 → mock 이 '부족'이면 실제 스냅샷(실제 코스피)을 읽었다.
2) urllib: KIS 마스터는 urllib.request.urlretrieve 로 받는데 conftest 차단은 requests 만 봤다
   → 테스트 11개가 매 실행 실제로 22번 다운로드했다.
"""
import os
import urllib.request

import pytest

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_openapi_snapshot_is_not_the_production_file():
    prod = os.path.join(ROOT, "data", "krx_openapi.db")
    assert os.path.abspath(config.KRX_OPENAPI_DB_PATH) != os.path.abspath(prod)
    from modules import krx_openapi
    assert os.path.abspath(krx_openapi._db_path()) != os.path.abspath(prod)


def test_urllib_download_is_blocked():
    with pytest.raises(OSError):
        urllib.request.urlretrieve("https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
                                   os.devnull)
    with pytest.raises(OSError):
        urllib.request.urlopen("https://example.com/")


def test_regime_mock_is_not_overridden_by_real_index_data():
    """재현 사례: 하락 mock 이 '부족'이어도 실제 스냅샷으로 새지 않아야 한다(모름 = Sideways 폴백)."""
    from unittest.mock import patch
    from modules import analysis
    from tests.conftest import create_mock_df
    analysis._MARKET_REGIME_CACHE.clear() if hasattr(analysis._MARKET_REGIME_CACHE, "clear") else None
    with patch('modules.analysis.api.get_domestic_index_chart', return_value=create_mock_df(trend='down')):
        df = analysis.get_domestic_index_data("KOSPI")
    src = (getattr(df, "attrs", {}) or {}).get("source") if df is not None else None
    assert src != "OPENAPI", "테스트가 실제 KRX 스냅샷을 읽었다"
