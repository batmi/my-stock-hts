"""토스 모드 주봉도 KIS 와 같은 창(~3년)을 보여야 한다.

[배경 · 2026-09-04] 토스는 주봉 API 가 없어 일봉을 주 단위로 묶는다. 그런데 그 재료로
**화면 일봉**을 그대로 재활용하고 있었다. 화면 일봉은 '52주 위치·EMA120' 기준으로
250봉(≈1년)에서 잘리므로, 주봉이 그 자름을 그대로 물려받아 54주밖에 나오지 않았다.
KIS 는 _fetch_kis_weekly_domestic(lookback_days=1100) 으로 ~157주를 받는다 —
같은 메뉴가 모드에 따라 3배 다른 기간을 보여주고 있었다.

실측(005930, 2026-09-04): 종전 250봉 → 54주 / 수정 후 732봉 → 158주.
"""
import pandas as pd
import pytest

import api
import config
from api import charts


def _daily(n, end="2026-09-04"):
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return pd.DataFrame({
        "date": days.strftime("%Y%m%d"),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0,
    })


# ─────────────────────────────────────────────
# 1. 창 — 주봉이 1년에서 끊기지 않는가
# ─────────────────────────────────────────────

def test_three_years_of_dailies_become_three_years_of_weeks():
    w = charts._resample_weekly(_daily(750))
    assert 140 <= len(w) <= 170, f"{len(w)}주 — 3년(약 157주)이 아니다"


def test_the_old_screen_daily_window_was_only_one_year():
    """왜 고쳤는지를 남긴다 — 250봉을 묶으면 52주가 된다."""
    assert len(charts._resample_weekly(_daily(250))) < 60


# ─────────────────────────────────────────────
# 2. 배선 — 주봉이 화면 일봉을 재활용하지 않는가
# ─────────────────────────────────────────────

@pytest.fixture
def toss(monkeypatch):
    monkeypatch.setattr(config.session, "is_toss", True, raising=False)


def test_weekly_does_not_reuse_the_screen_daily_path(toss, monkeypatch):
    """화면 일봉(get_chart_data(...,'daily'))을 부르면 250봉 자름을 다시 물려받는다."""
    monkeypatch.setattr(api, "index_source_kind", lambda code: None)
    monkeypatch.setattr(api, "_toss_long_daily", lambda code, ovs, **k: _daily(750))
    monkeypatch.setattr(api, "_get_cached_chart",
                        lambda code, ovs, is_index, fetch_func, **k: fetch_func())
    monkeypatch.setattr(charts, "get_chart_data",
                        lambda *a, **k: pytest.fail("주봉이 화면 일봉 경로를 재사용했다"))

    w = charts._get_weekly_chart_data("005930", False)
    assert len(w) > 100


def test_weekly_cache_key_is_separate_from_the_daily_one(toss, monkeypatch):
    """같은 키를 쓰면 3년 주봉과 1년 일봉이 서로를 덮는다."""
    seen = {}

    def _cached(code, ovs, is_index, fetch_func, **k):
        seen['code'] = code
        seen['overlay'] = k.get('realtime_overlay')
        return fetch_func()

    monkeypatch.setattr(api, "index_source_kind", lambda code: None)
    monkeypatch.setattr(api, "_toss_long_daily", lambda code, ovs, **k: _daily(750))
    monkeypatch.setattr(api, "_get_cached_chart", _cached)

    charts._get_weekly_chart_data("005930", False)
    assert seen['code'] == "005930_W"
    # 당일 현재가는 '주 마감(금)' 라벨의 캔들에 맞지 않는다(지수 주봉과 같은 이유).
    assert seen['overlay'] is False


# ─────────────────────────────────────────────
# 3. 재료 — 긴 일봉을 어디서 받는가
# ─────────────────────────────────────────────

def test_domestic_long_daily_asks_krx_for_three_years(monkeypatch):
    """국내는 KRX 정규장 기준을 쓴다 — 토스 캔들은 NXT 체결이 섞여 주봉 고·저를 흔든다."""
    from modules import krx_daily
    got = {}

    def _get_daily(code, lookback_days=None, **k):
        got['lookback'] = lookback_days
        return _daily(750)

    monkeypatch.setattr(krx_daily, "is_domestic_code", lambda c: True)
    monkeypatch.setattr(krx_daily, "get_daily", _get_daily)
    monkeypatch.setattr(api.toss, "_toss_chart_data",
                        lambda *a, **k: pytest.fail("KRX 가 충분한데 토스 캔들로 갔다"))

    df = api._toss_long_daily("005930", False)
    assert got['lookback'] == 1100, "KIS 주봉(lookback_days=1100)과 같은 창이어야 한다"
    assert len(df) == 750 and df.attrs['source'].startswith("KRX/")


def test_it_falls_back_to_toss_candles_when_krx_is_short(monkeypatch):
    """KRX 가 짧으면(신규 상장·조회 실패) 토스 캔들로 간다 — 빈 차트보다 낫다."""
    from modules import krx_daily
    asked = {}

    def _candles(code, period_type, is_overseas, target_bars=None):
        asked['target'] = target_bars
        return _daily(600)

    monkeypatch.setattr(krx_daily, "is_domestic_code", lambda c: True)
    monkeypatch.setattr(krx_daily, "get_daily", lambda *a, **k: _daily(30))
    monkeypatch.setattr(api.toss, "_toss_chart_data", _candles)

    df = api._toss_long_daily("005930", False)
    assert len(df) == 600
    assert asked['target'] > 250, "주봉인데 화면 일봉과 같은 봉 수를 요청했다"


def test_overseas_goes_straight_to_toss_candles(monkeypatch):
    asked = {}

    def _candles(code, period_type, is_overseas, target_bars=None):
        asked.update(period_type=period_type, target=target_bars, ovs=is_overseas)
        return _daily(700)

    monkeypatch.setattr(api.toss, "_toss_chart_data", _candles)
    api._toss_long_daily("AAPL", True)
    assert asked['ovs'] is True and asked['period_type'] == 'daily'
    assert asked['target'] > 250


# ─────────────────────────────────────────────
# 4. 화면 일봉은 그대로인가
# ─────────────────────────────────────────────

def test_screen_daily_still_asks_for_the_short_window(monkeypatch):
    """일봉 경로가 덩달아 길어지면 매 조회가 토스 캔들 페이지를 더 밟는다(5 RPS)."""
    import inspect

    src = inspect.getsource(api.toss._toss_chart_data)
    assert "target_bars" in src
    # 기본값(None)일 때의 목표·자름이 종전 수치 그대로인지
    assert "else 260" in src and "else 250" in src
