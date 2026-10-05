"""KRX 공식 시세 입구(modules/krx_data.py) 검증.

[2026-10-05] data.krx.co.kr 웹 로그인 경로(pykrx)를 걷어내 이 모듈은 Open API 앞의 입구 + 캐시만
남았다. 네트워크는 타지 않는다 — krx_openapi 의 조회 함수를 목으로 갈아끼우고 라우팅·캐시 규약과
'pykrx 를 다시 들이지 않는다'를 고정한다. Open API 자체의 파싱·저장소는 test_krx_openapi 가 잰다.
"""
import ast
import inspect
import sys as _sys
from unittest.mock import patch

import pandas as pd
import pytest

from modules import krx_daily, krx_data, krx_openapi


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """인증키를 채워 두고(실제 호출 없음) 캐시를 비운다."""
    monkeypatch.setenv("KRX_OPENAPI_KEY", "K")
    krx_data.clear_cache()
    yield
    krx_data.clear_cache()


def _bars(n=3):
    return pd.DataFrame({"date": [f"2026090{i + 1}" for i in range(n)],
                         "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0})


# ---------------------------------------------------------------------------
# 기동 점검 문구 — Open API 상태가 곧 이 모듈의 상태다
# ---------------------------------------------------------------------------
def test_status_text_points_to_openapi_key(monkeypatch):
    monkeypatch.delenv("KRX_OPENAPI_KEY", raising=False)
    ok, msg = krx_data.status_text()
    assert ok is False and "KRX_OPENAPI_KEY" in msg and "KRX_ID" not in msg
    monkeypatch.setenv("KRX_OPENAPI_KEY", "K")
    krx_openapi._DISABLED_UNTIL[0] = 0.0
    ok, msg = krx_data.status_text()
    assert ok is True and "Open API" in msg


# ---------------------------------------------------------------------------
# 라우팅 — 각 입구가 Open API 의 어느 함수를 부르는가
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("call,fn,args", [
    (lambda: krx_data.get_gold_daily(400), "gold_daily", (400,)),
    (lambda: krx_data.get_index_daily("KOSPI200", 400), "index_daily", ("KOSPI200", 400)),
    (lambda: krx_data.get_vkospi_daily(400), "index_daily", ("VKOSPI", 400)),
    (lambda: krx_data.get_k200_futures_daily("CM", 400), "k200_futures_daily", ("CM", 400)),
])
def test_each_entry_routes_to_openapi(call, fn, args):
    with patch.object(krx_openapi, fn, return_value=_bars()) as m:
        df = call()
    m.assert_called_once_with(*args)
    assert len(df) == 3


def test_index_rejects_unsupported_type():
    """V코스피200·선물은 전용 함수가 있다 — 지수 입구로 들어오면 부르지도 않는다."""
    with patch.object(krx_openapi, "index_daily") as m:
        assert krx_data.get_index_daily("VKOSPI") is None
        assert krx_data.get_index_daily("K200FUT_F") is None
    m.assert_not_called()


def test_none_without_key(monkeypatch):
    monkeypatch.delenv("KRX_OPENAPI_KEY", raising=False)
    with patch.object(krx_openapi, "gold_daily") as m:
        assert krx_data.get_gold_daily(400) is None
    m.assert_not_called()


def test_openapi_exception_becomes_none():
    with patch.object(krx_openapi, "gold_daily", side_effect=RuntimeError("장애")):
        assert krx_data.get_gold_daily(400) is None


# ---------------------------------------------------------------------------
# 캐시 — 성공분만 기억한다
# ---------------------------------------------------------------------------
def test_cache_hit_skips_refetch():
    with patch.object(krx_openapi, "index_daily", return_value=_bars()) as m:
        krx_data.get_index_daily("KOSPI", 400)
        krx_data.get_index_daily("KOSPI", 400)
    assert m.call_count == 1


def test_use_cache_false_refetches():
    with patch.object(krx_openapi, "index_daily", return_value=_bars()) as m:
        krx_data.get_index_daily("KOSPI", 400)
        krx_data.get_index_daily("KOSPI", 400, use_cache=False)
    assert m.call_count == 2


@pytest.mark.parametrize("miss", [None, pd.DataFrame()])
def test_failure_is_not_cached(miss):
    """실패 대부분은 저장소를 백그라운드가 채우는 중이다 — 채워지면 바로 받아야 한다."""
    with patch.object(krx_openapi, "gold_daily", return_value=miss):
        assert krx_data.get_gold_daily(400) is None
    with patch.object(krx_openapi, "gold_daily", return_value=_bars()) as m:
        assert len(krx_data.get_gold_daily(400)) == 3
    m.assert_called_once()


def test_cache_returns_a_copy():
    with patch.object(krx_openapi, "gold_daily", return_value=_bars()):
        krx_data.get_gold_daily(400)
    a = krx_data.get_gold_daily(400)
    a.loc[0, "close"] = -1
    assert krx_data.get_gold_daily(400).loc[0, "close"] == 1.5


# ---------------------------------------------------------------------------
# pykrx 를 다시 들이지 않는다 — 2026-10-05
# ---------------------------------------------------------------------------
#  pykrx 는 import 만으로 환경변수 KRX_ID/KRX_PW 계정으로 data.krx.co.kr 에 로그인했다(webio 모듈
#  최상단의 build_krx_session()). 게이트(KRX_WEB_SCRAPING_ALLOWED)가 꺼져 있어도 krx_daily 가 일봉
#  조회 때 패키지를 적재해 약관 위반으로 IP 를 차단당한 사이트를 두드렸다. 게이트가 아니라 import
#  자체를 막아야 하므로 소스에서 이름이 사라졌는지를 본다.
def _imports(module):
    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("module", [krx_data, krx_daily], ids=["krx_data", "krx_daily"])
def test_krx_modules_do_not_import_pykrx(module):
    assert "pykrx" not in _imports(module)


def test_daily_fetch_does_not_load_pykrx():
    """일봉 조회 경로를 실제로 돌려도 pykrx 가 sys.modules 에 들어오지 않는다."""
    _sys.modules.pop("pykrx", None)
    with patch.object(krx_daily, "_fetch_openapi", return_value=None), \
         patch.object(krx_daily, "_fetch_fdr", return_value=None), \
         patch.object(krx_daily, "get_listing_map", return_value=None):
        krx_daily.clear_cache()
        krx_daily.get_daily("005930", lookback_days=4, use_cache=False)
        krx_daily.get_ticker_name("005930")
    assert "pykrx" not in _sys.modules
