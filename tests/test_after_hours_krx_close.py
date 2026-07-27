"""국내 현재가·지표의 기준 시장(KRX/NXT) 분리 테스트.

배경: 정규장 밖의 '현재가'는 NXT(대체거래소) 체결가다. NXT 거래량은 정규장의 수백분의 1이라
소수 체결이 봉의 종가·고가·저가를 정하는데, 이것이 확정된 KRX 일봉을 덮으면
EMA·RSI·CCI·ATR·52주 위치가 함께 흔들린다.
(실측 2026-07-24 SK텔레콤: KRX 종가 100,000 / 애프터마켓 20:00 99,700)

[2026-07-28 정책] 게이트를 두 축으로 분리했다.
  - chart_overlay_enabled  (지표용): KRX 정규장에만 실시간가를 봉에 반영. **설정과 무관.**
  - display_price_krx_fixed(표시용): USE_KRX_CLOSE_AFTER_HOURS가 좌우. 모든 장 마감 후
    (20:00 종료 후·주말·휴장)에만 화면 현재가를 KRX 확정 종가로 고정하고, NXT 거래시간에는
    설정과 무관하게 NXT 현재가를 보여준다.
'무엇을 사고팔지'는 KRX 확정 데이터로 판단하고, '지금 얼마인지'(손절·트레일링 트리거,
주문가)는 실시간가로 본다.
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


def test_indicator_gate_ignores_setting():
    """[2026-07-28] 지표 오버레이는 설정과 무관하다 — 설정을 꺼도 정규장 밖에서는 반영하지 않는다.

    종전에는 False면 NXT 체결가가 확정 봉을 덮었다. 지표는 '판단'의 축이라 언제나
    KRX 확정 봉 하나로만 계산한다(설정은 표시 축만 좌우).
    """
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = False
    assert _overlay_price(99700, datetime(2026, 7, 26, 15, 0), holiday=True) == 0.0   # 휴장일
    assert _overlay_price(99700, datetime(2026, 7, 24, 16, 0)) == 0.0                 # NXT 애프터마켓
    assert _overlay_price(99700, datetime(2026, 7, 24, 8, 30)) == 0.0                 # NXT 프리마켓
    assert _overlay_price(99700, datetime(2026, 7, 24, 10, 0)) == 99700               # 정규장은 반영


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


def test_confirmed_bar_survives_even_when_setting_off():
    """[2026-07-28] 설정을 꺼도 확정 봉은 보존된다 (설정은 표시 축만 좌우)."""
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = False
    df = _skt_df()
    gated = _overlay_price(99700, datetime(2026, 7, 26, 15, 0), holiday=True)
    indicators.apply_realtime_price(df, gated, market_date='20260724')
    assert float(df.iloc[-1]['close']) == 100000


def test_confirmed_bar_survives_during_nxt_session():
    """NXT 애프터마켓 체결가도 확정된 KRX 당일 봉을 덮지 않는다 (ATR 왜곡 차단)."""
    df = _skt_df()
    gated = _overlay_price(104900, datetime(2026, 7, 24, 16, 0))   # 당일 고가(104,800) 위 체결
    indicators.apply_realtime_price(df, gated, market_date='20260724')
    assert float(df.iloc[-1]['close']) == 100000
    assert float(df.iloc[-1]['high']) == 104800    # True Range가 부풀지 않는다


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


def _table_row(setting, when=None, holiday=True):
    from modules import analysis
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = setting
    bundle = {
        'curr_data': {'rt_cd': '0', 'output': {
            'stck_prpr': '99700', 'stck_sdpr': '99500',       # 토스 lastPrice = NXT 애프터가
            'w52_hgpr': '139500', 'w52_lwpr': '51400'}},
        'chart_df': _long_skt_chart(), 'inv_list': None, 'rt_strength': None,
        'ask_bid_ratio': None, 'detail': None,
    }
    md, hp, dt = _at(when or datetime(2026, 7, 26, 15, 0), holiday=holiday)
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
    """자동매매 기본 운용시간은 KRX 정규장. 종가 단일가 구간(15:20~15:30)은 제외된다.

    실효 운용 시간은 '즉시 체결이 되는' 접속매매 구간(09:00~15:20)이다. 15:20~15:30에
    나간 주문은 접수만 되고 15:30 단일가로 넘어가, 미체결 자동취소에 걸려 헛주문이 된다.
    """
    from modules.auto_trade.common import is_system_market_open

    assert config.settings.SYSTEM_TRADING_START_TIME == "0900"
    assert config.settings.SYSTEM_TRADING_END_TIME == "1530"

    expected = {(8, 30): False, (8, 59): False, (9, 0): True, (12, 0): True,
                (15, 19): True, (15, 20): False, (15, 24): False, (15, 25): False,
                (15, 30): False, (16, 0): False}
    for (hh, mm), want in expected.items():
        with patch('modules.auto_trade.common.datetime') as m, \
             patch('modules.auto_trade.common.api.is_holiday_today', return_value=False):
            m.now.return_value = datetime(2026, 7, 24, hh, mm)
            assert is_system_market_open() is want, f"{hh:02d}:{mm:02d}"


@pytest.mark.parametrize("hh,mm", [(8, 30), (16, 0), (19, 30)])
def test_nxt_session_shows_live_price_but_not_in_indicators(hh, mm):
    """NXT 거래시간: 표시는 실시간(NXT)가, 지표는 KRX 확정 봉.

    표시 게이트가 '고정 아님'을 돌려주면 호출부가 실시간가를 현재가로 쓴다.
    """
    md, hp, dt = _at(datetime(2026, 7, 24, hh, mm), holiday=False)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.chart_overlay_enabled(False) is False       # 지표엔 반영 안 함
        assert api.display_price_krx_fixed(False) is False     # 표시는 NXT 실시간가


@pytest.mark.parametrize("setting", [True, False])
@pytest.mark.parametrize("hh,mm", [(8, 30), (10, 0), (16, 0), (19, 30)])
def test_display_not_fixed_while_any_market_open(setting, hh, mm):
    """장이 열려 있는 동안(정규장·NXT)에는 설정과 무관하게 표시 현재가를 고정하지 않는다."""
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = setting
    md, hp, dt = _at(datetime(2026, 7, 24, hh, mm), holiday=False)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.display_price_krx_fixed(False) is False


@pytest.mark.parametrize("dt,holiday", [
    (datetime(2026, 7, 24, 21, 0), False),   # 모든 장 마감 후(야간)
    (datetime(2026, 7, 24, 7, 0), False),    # NXT 프리마켓 개장 전
    (datetime(2026, 7, 26, 15, 0), True),    # 주말·휴장일
])
def test_display_fixed_after_all_markets_close(dt, holiday):
    """모든 장 마감 후에는 설정에 따라 표시 현재가 고정 여부가 갈린다."""
    for setting, expected in ((True, True), (False, False)):
        config.settings.USE_KRX_CLOSE_AFTER_HOURS = setting
        md, hp, _ = _at(dt, holiday)
        with md as m, hp:
            m.now.return_value = dt
            m.strptime = datetime.strptime
            assert api.display_price_krx_fixed(False) is expected


def test_display_gate_never_applies_to_overseas():
    """미국장은 별도 정책이므로 표시 고정 대상이 아니다."""
    config.settings.USE_KRX_CLOSE_AFTER_HOURS = True
    md, hp, dt = _at(datetime(2026, 7, 26, 15, 0), holiday=True)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.display_price_krx_fixed(True) is False


def test_table_row_w52_uses_same_basis():
    """52주 위치도 같은 현재가 기준이어야 한다 (100,000 → 55.2%)."""
    cells = _table_row(True)
    w52 = next(c for c in cells if isinstance(c, str) and '%' in c and '(' not in c)
    assert "55.2%" in w52


# ==========================================================
# 5. 자정~개장 전(00:00~09:00) 회귀 — krx_last_settled_day
#    market_today는 평일 새벽에도 '오늘'(아직 열리지 않은 날)을 돌려주므로,
#    'last_bar >= market_today' 비교가 항상 실패해 KRX 고정이 걸리지 않았다.
#    (2026-07-28 01:13 실측: 삼성전자에 전날 NXT 종가 255,000이 노출)
# ==========================================================

def test_settled_day_before_open_is_prev_trading_day():
    """개장 전 새벽에는 '최신 확정 세션'이 직전 거래일이어야 한다."""
    md, hp, dt = _at(datetime(2026, 7, 28, 1, 13), holiday=False)   # 화요일 새벽
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.market_today(False) == '20260728'        # 아직 열리지 않은 '오늘'
        assert api.krx_last_settled_day() == '20260727'     # 실제 마지막 확정 세션(월)


def test_settled_day_after_close_is_today():
    """정규장 마감(+확정 여유) 뒤에는 오늘이 최신 확정 세션이다."""
    md, hp, dt = _at(datetime(2026, 7, 27, 21, 0), holiday=False)
    with md as m, hp:
        m.now.return_value = dt
        m.strptime = datetime.strptime
        assert api.krx_last_settled_day() == '20260727'


def test_table_row_krx_fixed_in_predawn():
    """새벽에도 설정 True면 표시 현재가·등락이 KRX 확정 기준이어야 한다(회귀 방지)."""
    cells = _table_row(True, when=datetime(2026, 7, 27, 1, 13), holiday=False)
    assert "100,000" in cells[3]
    assert "+500" in cells[4] and "+0.50%" in cells[4]


def test_table_row_predawn_keeps_nxt_when_setting_off():
    """새벽에 설정 False면 마지막 실거래가(NXT)가 그대로 보인다."""
    cells = _table_row(False, when=datetime(2026, 7, 27, 1, 13), holiday=False)
    assert "99,700" in cells[3]
