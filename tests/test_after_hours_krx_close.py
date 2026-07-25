"""장 종료 후 국내 현재가·지표 기준(USE_KRX_CLOSE_AFTER_HOURS) 테스트.

배경: 토스 lastPrice는 NXT 프리/애프터 체결을 그대로 반영한다. 모든 장이 끝난 뒤에도
그 값이 마지막 NXT 체결가로 굳어 있는데, 이것이 확정된 KRX 일봉의 종가를 덮어쓰면
EMA·RSI·CCI·ATR·52주 위치가 함께 흔들린다.
(실측 2026-07-24 SK텔레콤: KRX 종가 100,000 / 애프터마켓 20:00 99,700)
"""
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

import api
import config
import indicators


def _at(dt, holiday=False):
    """api 모듈의 '현재 시각'과 휴장 판정을 고정한다."""
    md = patch('api.datetime')
    hp = patch('api.is_holiday_today', return_value=holiday)
    return md, hp, dt


def _overlay_price(price, dt, holiday=False, is_overseas=False):
    md, hp, dt = _at(dt, holiday)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        return api.chart_overlay_price(price, is_overseas)


@pytest.fixture(autouse=True)
def _default_on():
    prev = getattr(config.settings, 'USE_KRX_CLOSE_AFTER_HOURS', True)
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = True
    yield
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = prev


# ==========================================================
# 1. 시간대 판정
# ==========================================================

@pytest.mark.parametrize("hh,mm,expected", [
    (9, 0, True),    # KRX 정규장 시작
    (10, 0, True),   # 정규장
    (8, 30, True),   # NXT 프리마켓
    (16, 0, True),   # NXT 애프터마켓
    (19, 59, True),  # NXT 애프터마켓 종료 직전
    (20, 30, False),  # 모든 장 종료
    (23, 0, False),  # 야간
    (7, 0, False),   # NXT 프리마켓 개장 전
])
def test_session_window(hh, mm, expected):
    md, hp, dt = _at(datetime(2026, 7, 24, hh, mm), holiday=False)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.domestic_trading_session_open() is expected


def test_holiday_is_always_closed():
    md, hp, dt = _at(datetime(2026, 7, 26, 15, 0), holiday=True)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.domestic_trading_session_open() is False


# ==========================================================
# 2. 오버레이 가격 게이트
# ==========================================================

def test_price_passes_during_session():
    assert _overlay_price(100000, datetime(2026, 7, 24, 10, 0)) == 100000


def test_price_blocked_after_all_markets_close():
    """SK텔레콤 재현 — 일요일에는 NXT 애프터가(99,700)를 반영하지 않는다."""
    assert _overlay_price(99700, datetime(2026, 7, 26, 15, 0), holiday=True) == 0.0


def test_price_blocked_at_night_on_trading_day():
    assert _overlay_price(99700, datetime(2026, 7, 24, 21, 0)) == 0.0


def test_setting_off_restores_previous_behavior():
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = False
    assert _overlay_price(99700, datetime(2026, 7, 26, 15, 0), holiday=True) == 99700


def test_overseas_never_gated():
    """미국장은 별도 정책(프리/애프터 반영)이므로 이 게이트의 대상이 아니다."""
    assert _overlay_price(250.5, datetime(2026, 7, 26, 15, 0), holiday=True, is_overseas=True) == 250.5


@pytest.mark.parametrize("bad", [0, -1, None, "", "abc"])
def test_invalid_price_returns_zero(bad):
    assert _overlay_price(bad, datetime(2026, 7, 24, 10, 0)) == 0.0


def test_enabled_matches_price_gate():
    """chart_overlay_enabled는 조회 전 판정용이므로 가격 게이트와 결과가 일치해야 한다."""
    for dt, holiday in [(datetime(2026, 7, 24, 10, 0), False),
                        (datetime(2026, 7, 24, 21, 0), False),
                        (datetime(2026, 7, 26, 15, 0), True)]:
        md, hp, _ = _at(dt, holiday)
        with md as m, hp:
            m.now.return_value = dt
            m.strptime = datetime.strptime
            assert api.chart_overlay_enabled(False) == (api.chart_overlay_price(1.0, False) > 0)


# ==========================================================
# 3. 지표 영향 — 확정 봉이 보존되는지
# ==========================================================

def _skt_df():
    """SK텔레콤 실제 KRX 일봉(2026-07-20~24, pykrx/FDR/네이버 3소스 일치)."""
    return pd.DataFrame([
        {'date': '20260720', 'open': 85500, 'high': 85800, 'low': 80900, 'close': 84700, 'volume': 1094065},
        {'date': '20260721', 'open': 83900, 'high': 86100, 'low': 81000, 'close': 85100, 'volume': 853354},
        {'date': '20260722', 'open': 86600, 'high': 96000, 'low': 86600, 'close': 94100, 'volume': 1720147},
        {'date': '20260723', 'open': 92600, 'high': 100600, 'low': 91100, 'close': 99500, 'volume': 2538901},
        {'date': '20260724', 'open': 99200, 'high': 104800, 'low': 94300, 'close': 100000, 'volume': 3988854},
    ])


def test_confirmed_bar_survives_after_hours():
    """게이트가 0을 주면 apply_realtime_price가 no-op이 되어 KRX 종가가 남는다."""
    df = _skt_df()
    gated = _overlay_price(99700, datetime(2026, 7, 26, 15, 0), holiday=True)
    indicators.apply_realtime_price(df, gated, market_date='20260724')
    assert float(df.iloc[-1]['close']) == 100000
    assert len(df) == 5          # 가짜 봉도 추가되지 않는다


def test_confirmed_bar_overwritten_when_setting_off():
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = False
    df = _skt_df()
    gated = _overlay_price(99700, datetime(2026, 7, 26, 15, 0), holiday=True)
    indicators.apply_realtime_price(df, gated, market_date='20260724')
    assert float(df.iloc[-1]['close']) == 99700


def test_session_price_still_applies():
    """정규장 중에는 종전대로 실시간가가 마지막 봉에 반영돼야 한다."""
    df = _skt_df()
    gated = _overlay_price(101500, datetime(2026, 7, 24, 10, 0))
    indicators.apply_realtime_price(df, gated, market_date='20260724')
    assert float(df.iloc[-1]['close']) == 101500
    assert float(df.iloc[-1]['high']) == 104800   # 기존 고가 유지


# ==========================================================
# 4. 시세 테이블(print_table) 행 — 현재가·등락도 KRX 기준인지
#    이 행의 현재가는 차트가 아니라 curr_data(ats_prpr/stck_prpr)에서 직접 온다.
# ==========================================================

def _long_skt_chart():
    """52주 밴드(51,400~139,500)를 채운 250봉 + 실제 마지막 5봉.

    _w52_high_low는 '최근 365일' 창으로 자르므로 극값 봉이 창 안에 들어오도록 350일로 잡는다.
    """
    from datetime import timedelta
    rows, d, px = [], datetime(2026, 7, 24) - timedelta(days=350), 60000
    while len(rows) < 245:
        if d.weekday() < 5:
            rows.append({'date': d.strftime('%Y%m%d'), 'open': px, 'high': px + 800,
                         'low': px - 800, 'close': px, 'volume': 100000})
            px += 60
        d += timedelta(days=1)
    rows[0].update(low=51400)
    rows[1].update(high=139500)
    return pd.DataFrame(rows + _skt_df().to_dict('records'))


def _table_row(setting):
    from modules import analysis
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = setting
    bundle = {
        'curr_data': {'rt_cd': '0', 'output': {
            'stck_prpr': '99700', 'stck_sdpr': '99500',       # 토스 lastPrice = NXT 애프터가
            'w52_hgpr': '139500', 'w52_lwpr': '51400'}},
        'chart_df': _long_skt_chart(), 'inv_list': None, 'rt_strength': None,
        'ask_bid_ratio': None, 'detail': None,
    }
    md, hp, dt = _at(datetime(2026, 7, 26, 15, 0), holiday=True)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        row = analysis._analyze_table_row(('SK텔레콤', '017670'), '국내 주식', False, False,
                                          {}, {}, 0.0, set(), set(), bundle)
    return row[0] if isinstance(row, tuple) else row


def test_table_row_shows_krx_close_after_hours():
    """실측 재현 — 표시 현재가·등락이 KRX 정규장 기준(100,000 / +500 +0.50%)이어야 한다."""
    cells = _table_row(True)
    assert "100,000" in cells[3]
    assert "+500" in cells[4] and "+0.50%" in cells[4]


def test_table_row_keeps_nxt_price_when_setting_off():
    cells = _table_row(False)
    assert "99,700" in cells[3]
    assert "+200" in cells[4] and "+0.20%" in cells[4]


def test_reserved_monitor_not_tied_to_auto_trade_hours():
    """예약 주문 감시는 자동매매 운용시간(KRX)이 아니라 국내 시장 개장(NXT 포함)에 연동한다.

    사용자가 직접 건 주문이므로 자동매매 창을 좁혀도 NXT 프리/애프터에서 발동해야 한다.
    """
    import inspect

    from modules import reserved_order_monitor
    src = inspect.getsource(reserved_order_monitor)
    assert "api.domestic_trading_session_open()" in src
    assert "SYSTEM_TRADING_START_TIME" not in src
    assert "SYSTEM_TRADING_END_TIME" not in src


def test_auto_trade_window_defaults_to_krx_session():
    """자동매매 기본 운용시간은 KRX 정규장. 단일가 구간(15:25~15:30)은 제외된다."""
    from modules.auto_trade.common import is_system_market_open

    assert config.settings.SYSTEM_TRADING_START_TIME == "0900"
    assert config.settings.SYSTEM_TRADING_END_TIME == "1530"

    expected = {(8, 30): False, (8, 59): False, (9, 0): True, (12, 0): True,
                (15, 24): True, (15, 25): False, (15, 30): False, (16, 0): False}
    for (hh, mm), want in expected.items():
        with patch('modules.auto_trade.common.datetime') as m, \
             patch('modules.auto_trade.common.api.is_holiday_today', return_value=False):
            m.now.return_value = datetime(2026, 7, 24, hh, mm)
            assert is_system_market_open() is want, f"{hh:02d}:{mm:02d}"


@pytest.mark.parametrize("hh,mm", [(8, 30), (9, 30), (16, 0), (19, 30)])
def test_live_price_still_available_during_nxt(hh, mm):
    """자동매매 창을 좁혀도 NXT 프리/애프터 실시간가 조회는 막히지 않아야 한다."""
    md, hp, dt = _at(datetime(2026, 7, 24, hh, mm), holiday=False)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.chart_overlay_enabled(False) is True
        assert api.chart_overlay_price(99700, False) == 99700


def test_table_row_w52_uses_same_basis():
    """52주 위치도 같은 현재가 기준이어야 한다 (100,000 → 55.2%)."""
    cells = _table_row(True)
    w52 = next(c for c in cells if isinstance(c, str) and '%' in c and '(' not in c)
    assert "55.2%" in w52
