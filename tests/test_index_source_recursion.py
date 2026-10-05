"""지수 조회가 자기 자신을 다시 부르지 않는가 (토스 모드 코스피200·코스닥150 멈춤).

[무슨 일이 있었나] 2026-08-19 토스 모드에서 전체 지수가 96%에서 멈춘다는 신고. 원인은
느린 소스가 아니라 **호출 그래프의 순환**이었다.

    get_domestic_index_data("KOSPI200")     ← market_type별 single-flight 락을 잡는다
      → _fetch_index_via_tvdatafeed
        → _merge_index_volume_from_yfinance  (OBV용 거래량 보강, 2026-10-05 제거)
          → api.get_chart_data("^KS200")
            → _index_source_chart_data       ← 지수 '단일 소스' 규칙(2026-07)
              → get_domestic_index_data("KOSPI200")   ← 같은 락을 같은 스레드가 다시!

threading.Lock은 재진입이 안 되므로 여기서 **영구 교착**한다. 두 기능은 각자 옳았고,
나중에 들어온 단일 소스 규칙이 그 사이에 변을 놓으면서 고리가 닫혔다. 화면에는 '조회 중'
으로만 보여 원인을 짚기 어려웠다(실측: 300초 넘게 반환 없음 → 수정 후 2.1초).

[무엇을 고정하나] ① tvDatafeed 지수 조회는 바깥(차트 경로·yfinance)을 부르지 않는다 — 고리의
   변이었던 거래량 보강은 KRX 확정 봉이 이력 거래량을 주면서 2026-10-05 지웠다.
② 그럼에도 순환이 다시 생기면 교착이 아니라 경고 + 캐시값으로 빠진다.
"""
import threading

import pandas as pd
import pytest

import api
from modules import analysis


@pytest.fixture(autouse=True)
def clean_index_state():
    analysis._INDEX_FETCH_INPROGRESS.__dict__.pop("types", None)
    yield
    analysis._INDEX_FETCH_INPROGRESS.__dict__.pop("types", None)


class _FakeTv:
    """tvDatafeed 대역 — 지수라 거래량이 0이다."""
    def get_hist(self, **_k):
        return pd.DataFrame({
            'open': [400.0, 401.0, 402.0], 'high': [403.0] * 3, 'low': [399.0] * 3,
            'close': [401.0, 402.0, 403.0], 'volume': [0.0, 0.0, 0.0],
        }, index=pd.DatetimeIndex(pd.to_datetime(["2026-08-17", "2026-08-18", "2026-08-19"]),
                                  name='datetime'))


def test_tvdatafeed_지수_조회는_바깥_경로를_부르지_않는다(monkeypatch):
    """[핵심] 지수 조회 안에서 api.get_chart_data·yfinance 를 부르면 ^KS200 이 지수 소스로
    되돌려져 고리가 닫힌다. 거래량은 0 그대로 두고(이력 거래량은 KRX 확정 봉 몫) 아무것도 부르지 않는다."""
    def _forbidden(*a, **k):
        raise AssertionError("지수 조회가 바깥 경로를 불렀다 — 순환의 변이 되살아났다")

    monkeypatch.setattr(api, "get_chart_data", _forbidden)
    monkeypatch.setattr(api, "fetch_yfinance_data", _forbidden)
    monkeypatch.setattr(analysis, "_get_tvdatafeed", lambda: _FakeTv())

    out = analysis._fetch_index_via_tvdatafeed("KOSPI200")

    assert out is not None and out.attrs['source'] == 'TVDATAFEED'
    assert list(out['volume']) == [0.0, 0.0, 0.0]
    assert list(out['close']) == [401.0, 402.0, 403.0]


def test_순환_호출은_교착이_아니라_캐시로_빠진다(monkeypatch):
    """가드가 없으면 이 테스트는 영원히 끝나지 않는다 — 그래서 스레드로 시간을 잰다."""
    seen = []

    def _recursive_fetch(market_type):
        # 조회 도중 같은 지수를 다시 부르는 상황(순환)을 그대로 재현한다.
        seen.append(market_type)
        analysis.get_domestic_index_data(market_type)
        return pd.DataFrame()

    monkeypatch.setattr(analysis, "_fetch_domestic_index_data", _recursive_fetch)
    monkeypatch.setattr(analysis, "_index_cache_enabled", lambda: True)

    done = threading.Event()

    def _run():
        try:
            analysis.get_domestic_index_data("KOSPI200", force_refresh=True)
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert done.wait(10), "지수 조회가 자기 자신을 기다리며 교착했다"
    assert seen, "재귀 경로를 타지 않았다 — 표본이 무효다"


def test_가드는_다른_지수_조회를_막지_않는다(monkeypatch):
    """같은 스레드라도 market_type이 다르면 정상 조회다(코스피 안에서 코스닥을 볼 수 있다)."""
    calls = []

    def _fetch(market_type):
        calls.append(market_type)
        if market_type == "KOSPI200":
            analysis.get_domestic_index_data("KOSDAQ150", force_refresh=True)
        return pd.DataFrame()

    monkeypatch.setattr(analysis, "_fetch_domestic_index_data", _fetch)
    monkeypatch.setattr(analysis, "_index_cache_enabled", lambda: True)

    analysis.get_domestic_index_data("KOSPI200", force_refresh=True)
    assert calls == ["KOSPI200", "KOSDAQ150"], f"다른 지수 조회가 막혔다: {calls}"
