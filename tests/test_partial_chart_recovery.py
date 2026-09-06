"""조회 실패로 반쪽만 온 일봉을 그대로 쓰지 않는가.

[왜 중요한가] 일봉 페이지네이션은 **오늘부터 과거로** 훑는다. 도중에 실패하면 모아 둔
만큼만 돌아오는데, 그 프레임은 빈 프레임과 달리 '정상'으로 보인다 — 호출부의 검사는
`df.empty` 다. 그리고 ewm 기반 지표(ATR·RSI·ADX·MACD)는 rolling 과 달리 **첫 봉부터
값을 내므로**, 호출부는 그것이 반쪽인 줄 모른 채 확신 있는 숫자를 얻는다.

    entry_atr_stop_rate( 3봉) → -1.200%
    entry_atr_stop_rate(53봉) → -8.246%

-1.2% 손절선은 정상 눌림에서 곧바로 잘린다. `attrs['partial']` 은 캐시만 보고, 그
표식은 슬라이싱·copy 를 지나며 사라져 호출부까지 가지도 못한다.

과거 봉은 불변이고 반쪽 프레임에 남는 것은 항상 **최신 봉**이므로, 저장해 둔 옛
프레임과 병합하면 최신성을 잃지 않고 길이를 되찾는다.
"""
import numpy as np
import pandas as pd
import pytest

import api.chart_cache as cc


def _bars(dates, close):
    return pd.DataFrame({
        'date': dates, 'open': close, 'high': [c * 1.01 for c in close],
        'low': [c * 0.99 for c in close], 'close': close,
        'volume': [1000] * len(dates)})


_ALL_DATES = [d.strftime("%Y%m%d")
              for d in pd.date_range("2025-01-01", periods=400, freq="B")]


def _d(n, start=0):
    """연속된 영업일 문자열 n개. start 는 _ALL_DATES 안의 시작 위치."""
    return _ALL_DATES[start:start + n]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(cc, '_CHART_CACHE', {}, raising=False)
    monkeypatch.setattr(cc, '_chart_disk_get_any', lambda key: None)
    monkeypatch.setattr(cc, '_chart_disk_get', lambda *a, **k: None)
    monkeypatch.setattr(cc, '_chart_disk_set', lambda *a, **k: None)
    yield


def _fetch(df, partial):
    def f():
        out = df.copy()
        if partial:
            out.attrs['partial'] = True
        return out
    return f


def test_반쪽_차트는_저장본과_병합된다(monkeypatch):
    full = _bars(_d(200), list(np.linspace(10000, 12000, 200)))
    recent = full.tail(3).reset_index(drop=True)      # 최신 3봉만 왔다
    monkeypatch.setattr(cc, '_chart_disk_get_any', lambda key: full)

    out = cc._get_cached_chart("005930", is_overseas=False, is_index=False,
                               fetch_func=_fetch(recent, True), realtime_overlay=False)
    assert len(out) == 200, f"반쪽({len(recent)}봉)을 그대로 썼다: {len(out)}봉"
    assert out['date'].iloc[-1] == full['date'].iloc[-1]


def test_병합은_방금_받은_최신_봉을_이긴다(monkeypatch):
    """저장본의 마지막 봉이 낡았을 수 있다 — 그 자리는 방금 받은 값이 덮어야 한다."""
    stale = _bars(_d(100), [10000.0] * 100)
    fresh = _bars(_d(3, start=97), [77777.0, 88888.0, 99999.0])   # 저장본의 마지막 3일과 같은 날짜
    monkeypatch.setattr(cc, '_chart_disk_get_any', lambda key: stale)

    out = cc._get_cached_chart("005930", is_overseas=False, is_index=False,
                               fetch_func=_fetch(fresh, True), realtime_overlay=False)
    assert len(out) == 100
    assert out['close'].iloc[-1] == 99999.0, "낡은 저장본이 최신 봉을 덮었다"


def test_병합해도_캐시에_굳히지_않는다(monkeypatch):
    """다음 호출이 온전한 차트를 받으면 스스로 나아야 한다."""
    full = _bars(_d(200), list(np.linspace(10000, 12000, 200)))
    monkeypatch.setattr(cc, '_chart_disk_get_any', lambda key: full)
    saved = []
    monkeypatch.setattr(cc, '_chart_disk_set', lambda *a, **k: saved.append(a))

    cc._get_cached_chart("005930", is_overseas=False, is_index=False,
                         fetch_func=_fetch(full.tail(3).reset_index(drop=True), True),
                         realtime_overlay=False)
    assert not saved, "반쪽에서 복구한 프레임을 디스크에 굳혔다"
    assert not cc._CHART_CACHE, "메모리에도 굳혔다"


def test_저장본이_더_짧으면_건드리지_않는다(monkeypatch):
    """이미 받은 것이 더 온전하면 그대로 둔다 — 되레 깎으면 안 된다."""
    short_prev = _bars(_d(2), [10000.0, 10100.0])
    got = _bars(_d(50), list(np.linspace(10000, 11000, 50)))
    monkeypatch.setattr(cc, '_chart_disk_get_any', lambda key: short_prev)

    out = cc._get_cached_chart("005930", is_overseas=False, is_index=False,
                               fetch_func=_fetch(got, True), realtime_overlay=False)
    assert len(out) == 50


def test_온전한_차트는_종전대로_굳는다(monkeypatch):
    """대조군 — 복구 경로가 정상 캐싱을 망가뜨리지 않는다."""
    full = _bars(_d(200), list(np.linspace(10000, 12000, 200)))
    saved = []
    monkeypatch.setattr(cc, '_chart_disk_set', lambda *a, **k: saved.append(a))

    out = cc._get_cached_chart("005930", is_overseas=False, is_index=False,
                               fetch_func=_fetch(full, False), realtime_overlay=False)
    assert len(out) == 200
    assert saved, "정상 차트가 디스크에 저장되지 않는다"
