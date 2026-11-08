"""미국 휴장 구간 — 모든 모드에서 정규장 확정 봉 하나로 보이게 한다(2026-11-08).

[실측 2026-11-08 일 ET 03시, NVDA]
  KIS(mode 1·2) 현재가 186.64 +5.04(+2.78%) = 금요일 정규장 종가 (8종목 전부 일봉 종가·yfinance 와 일치)
  토스(mode 3)  현재가 188.72 +7.12(+3.92%) = 금요일 애프터 최종가 (yfinance 19:55 봉 188.70)
  해외는 실시간가를 일봉 마지막 봉에 늘 덮어썼으므로 지표까지 갈렸다
  (RSI 30.8 vs 32.8, CCI −195.1 vs −190.7, EMA5 193 vs 194 — 덮어쓴 종가 하나로 전부 재현).
휴장 구간에는 덮어쓰지 않고, 표시가·등락은 일봉의 정규장 종가와 직전 종가로 맞추고,
마지막 정규장 세션이 끝나기 전에 받은 캐시는 버린다. 장이 열린 구간은 종전대로다.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

import api
import config

pytestmark = pytest.mark.real_us_session   # 휴장 고정 자체를 검증한다(conftest 자동 고정 제외)

SUN_ET = datetime(2026, 11, 8, 3, 24)      # 일요일 ET 03:24 — 휴장
SUN_KST = datetime(2026, 11, 8, 17, 24)    # 같은 순간의 KST(EST +14h)
WED_AFTER_ET = datetime(2026, 11, 4, 17, 0)  # 수요일 애프터마켓


class _Frozen(datetime):
    _now = SUN_KST

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _at_et(monkeypatch, et, day_session=None, local=None):
    monkeypatch.setattr(api, 'now_us_eastern', lambda: et)
    monkeypatch.setattr(api, 'us_day_market_session', lambda: day_session)
    if local is not None:
        _Frozen._now = local
        monkeypatch.setattr(api, 'datetime', _Frozen)


# ==========================================================
# 1. 게이트
# ==========================================================

def test_closed_phase_fixes_regular_close(monkeypatch):
    _at_et(monkeypatch, SUN_ET)
    assert api.us_regular_close_fixed() is True
    assert api.chart_overlay_enabled(True) is False      # 지표: 덮어쓰지 않는다
    assert api.display_price_krx_fixed(True) is True     # 표시: 정규장 종가
    assert api.chart_overlay_price(188.72, True) == 0.0


def test_open_sessions_keep_live_price(monkeypatch):
    """프리·정규·애프터·데이마켓은 종전대로 살아 있는 가격을 반영한다."""
    _at_et(monkeypatch, WED_AFTER_ET, day_session="20261105")
    assert api.us_regular_close_fixed() is False
    assert api.chart_overlay_enabled(True) is True
    assert api.display_price_krx_fixed(True) is False
    assert api.chart_overlay_price(188.72, True) == 188.72


def test_closed_label_names_regular_close(monkeypatch):
    _at_et(monkeypatch, SUN_ET)
    text, style = api.market_session_label(True)
    assert text == "휴장(주말·공휴일) · 정규장 종가 · ET 03:24"
    assert style == "dim"


def test_domestic_gates_untouched(monkeypatch):
    """국내 판정은 미국 세션을 보지 않는다."""
    monkeypatch.setattr(api, 'us_regular_close_fixed',
                        lambda: (_ for _ in ()).throw(AssertionError("국내에서 호출되면 안 됨")))
    monkeypatch.setattr(api, '_nxt_quote_phase', lambda: 'skip')
    assert api.chart_overlay_enabled(False) is True


# ==========================================================
# 2. 캐시 기준선 — 마지막 정규장 세션이 끝난 뒤(ET 20:00)에 받은 것만
# ==========================================================

def test_us_close_passed_at_is_session_end_in_local_time(monkeypatch):
    """일요일 KST 17:24 에서 본 기준선 = 금요일 ET 20:00 = 토요일 KST 10:00."""
    _at_et(monkeypatch, SUN_ET, local=SUN_KST)
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: "20261106")
    assert api._us_close_passed_at() == datetime(2026, 11, 7, 10, 0)


def test_us_close_passed_at_none_when_session_open(monkeypatch):
    _at_et(monkeypatch, WED_AFTER_ET, day_session="20261105")
    assert api._us_close_passed_at() is None


def _chart(last_date="20261106", closes=(181.60, 186.64), bars=150):
    end = datetime.strptime(last_date, "%Y%m%d")
    rows, d, px = [], end - timedelta(days=int(bars * 1.5)), 150.0
    while d < end - timedelta(days=1):
        if d.weekday() < 5:
            rows.append({'date': d.strftime('%Y%m%d'), 'open': px, 'high': px + 2, 'low': px - 2,
                         'close': px, 'volume': 1000})
            px += 0.1
        d += timedelta(days=1)
    rows = rows[-(bars - 2):]
    for day, c in zip(("20261105", last_date), closes):
        rows.append({'date': day, 'open': c, 'high': c + 1, 'low': c - 1, 'close': c, 'volume': 1000})
    return pd.DataFrame(rows)


@pytest.fixture
def mem_cache(monkeypatch):
    """메모리 캐시만 쓴다(운영 chart_cache.db 격리)."""
    monkeypatch.setattr(api, '_chart_disk_get', lambda *a, **k: None)
    monkeypatch.setattr(api, '_chart_disk_set', lambda *a, **k: None)
    monkeypatch.setattr(api, '_chart_disk_delete', lambda *a, **k: None)
    api.clear_chart_cache()
    config.settings.CHART_CACHE_TTL_MINUTES = 360
    yield
    api.clear_chart_cache()


def test_cache_from_before_session_end_is_refetched(monkeypatch, mem_cache):
    """정규장 도중(금 ET 15:00)에 받은 미완성 금요일 봉은 휴장 구간에 버리고 다시 받는다.
    덮어쓰기가 꺼졌으므로, 남겨 두면 그 봉의 장중 종가가 '정규장 종가'로 보인다."""
    monkeypatch.setattr(api, 'us_regular_close_fixed', lambda: True)
    session_end = datetime.now() - timedelta(minutes=30)
    monkeypatch.setattr(api, '_us_close_passed_at', lambda: session_end)
    stale = MagicMock(return_value=_chart(closes=(181.60, 184.00)))     # 장중 184.00
    api._get_cached_chart('NVDA', True, False, stale)
    key = api._chart_cache_key('NVDA', True, False)
    api._CHART_CACHE[key]['timestamp'] = session_end - timedelta(hours=1)  # 세션 종료 전에 받은 것

    fresh = MagicMock(return_value=_chart())                              # 확정 186.64
    df = api._get_cached_chart('NVDA', True, False, fresh)
    fresh.assert_called_once()
    assert float(df.iloc[-1]['close']) == 186.64


def test_cache_after_session_end_is_kept_and_not_overlaid(monkeypatch, mem_cache):
    """세션 종료 뒤에 받은 캐시는 그대로 쓰고, 현재가(토스 애프터 188.72)를 덮어쓰지 않는다."""
    monkeypatch.setattr(api, 'us_regular_close_fixed', lambda: True)
    monkeypatch.setattr(api, '_us_close_passed_at', lambda: datetime.now() - timedelta(hours=2))
    price = MagicMock(return_value={'rt_cd': '0', 'output': {'last': '188.72', 'base': '181.60'}})
    monkeypatch.setattr(api, 'get_current_price_data', price)
    api._get_cached_chart('NVDA', True, False, MagicMock(return_value=_chart()))

    again = MagicMock()
    df = api._get_cached_chart('NVDA', True, False, again)
    again.assert_not_called()
    price.assert_not_called()                    # 휴장에는 현재가를 묻지도 않는다
    assert float(df.iloc[-1]['close']) == 186.64


def test_overseas_index_still_overlaid_when_us_closed(monkeypatch, mem_cache):
    """해외 지수·환율·코인(yfinance 지수 경로)은 주말에도 움직이는 것이 있다 — 종전대로 반영한다."""
    monkeypatch.setattr(api, 'us_regular_close_fixed', lambda: True)
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: "20261106")
    monkeypatch.setattr(api, 'get_yf_fast_info',
                        lambda *a, **k: {'last_price': 99000.0, 'regular_market_previous_close': 181.60,
                                         'last_volume': 1})
    api._get_cached_chart('BTC-USD', True, True, MagicMock(return_value=_chart()))
    df = api._get_cached_chart('BTC-USD', True, True, MagicMock())
    assert float(df.iloc[-1]['close']) == 99000.0


# ==========================================================
# 3. 시세 표 행 — KIS·토스가 같은 값을 보인다
# ==========================================================

def _us_row(monkeypatch, last, is_toss, fixed=True):
    from modules import analysis
    monkeypatch.setattr(api, 'us_regular_close_fixed', lambda: fixed)
    monkeypatch.setattr(api, 'market_today', lambda is_overseas=False: "20261106")
    monkeypatch.setattr(config.session, 'is_toss', is_toss, raising=False)
    bundle = {
        'curr_data': {'rt_cd': '0', 'output': {
            'last': str(last), 'diff': f"{last - 181.60:.2f}",
            'rate': f"{(last - 181.60) / 181.60 * 100:.2f}", 'base': '181.60'}},
        'chart_df': _chart(), 'inv_list': None, 'rt_strength': None,
        'ask_bid_ratio': None, 'detail': {}, }
    row = analysis._analyze_table_row(('NVIDIA Corporation', 'NVDA'), '미국 주식 기술적 분석', True, False,
                                      {}, {}, 0.0, set(), set(), bundle)
    return row[0] if isinstance(row, tuple) else row


@pytest.mark.parametrize("last, is_toss", [
    (186.64, False),   # KIS: 주말 last = 금요일 정규장 종가
    (188.72, True),    # 토스: lastPrice = 금요일 애프터 최종가
])
def test_closed_row_shows_regular_close_in_every_mode(monkeypatch, last, is_toss):
    cells = _us_row(monkeypatch, last, is_toss)
    assert "$186.64" in cells[3]
    assert "+5.04" in cells[4] and "+2.78%" in cells[4]


def test_open_session_row_keeps_live_price(monkeypatch):
    """애프터마켓 등 장이 열린 구간은 살아 있는 가격(토스 188.72)을 그대로 보인다."""
    cells = _us_row(monkeypatch, 188.72, True, fixed=False)
    assert "$188.72" in cells[3]
