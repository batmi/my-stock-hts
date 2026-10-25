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


def test_settings_start_from_class_defaults_not_the_operator_file():
    """[2026-10-25] 운용 기기에서는 전체 테스트가 운용자 설정 파일(json/dynamic_config.json) 위에서 돌았다.

    config import 와 세션 초기화가 그 파일을 읽고, 테스트별 JSON_DIR 임시화는 그 뒤였다. 실측 맥북: 매매일지 연동이
    켜져 있었고 RSI 상한이 70 이 아니라 70.0 이었다 — 같은 코드가 기기마다 다른 설정으로 검증된다. 이제 conftest 가
    세션 시작에 클래스 기본값으로 되돌린다. 글자(형)까지 같아야 한다(70 과 70.0 은 == 로는 같다).
    텔레그램 발송은 테스트마다 따로 끈다(isolate_test_files) — 그 한 값만 뺀다.
    """
    import json

    def _dump(st):
        d = st.model_dump()
        d.pop("ENABLE_TELEGRAM")
        return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)

    assert _dump(config.settings) == _dump(config.GlobalSettings())
