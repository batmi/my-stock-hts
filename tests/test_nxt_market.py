import pytest
import sys
import os
from unittest.mock import MagicMock
from datetime import datetime

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules.auto_trade import AutoTrader
import api

@pytest.fixture
def trader():
    return AutoTrader()

@pytest.fixture
def nxt_hours():
    """자동매매 운용시간을 NXT 연장까지 넓힌 설정(0800~2000).

    기본값은 KRX 정규장(0900~1530)이므로, NXT 시간대 동작을 검증하려면 명시적으로 넓혀야 한다.

    [왜 monkeypatch 를 쓰지 않는가] monkeypatch.setattr(config.settings, ...) 은 **설정 당시의
    객체**를 기억했다가 그 객체에 되돌린다. 그런데 config.reset_all_settings() 는 settings 를
    새 객체로 **교체**하므로(참조 오염 방지가 목적), 그 사이 교체가 일어나면 복원 대상과 실제
    참조 대상이 갈린다 — 0800 이 아무도 안 보는 옛 객체로 되돌아가고 현재 객체에는 남는다.
    그러면 기본값(0900)을 검사하는 다른 테스트가 깨진다.

    teardown 시점에 config.settings 를 **다시 읽어** 되돌리면 교체 여부와 무관하게 안전하다.

    [관측 2026-08-10] 전체 스위트 5회 중 1회, test_market_open_krx_default 와
    test_after_hours_krx_close::test_auto_trade_window_defaults_to_krx_session 이 함께 실패했다
    (둘 다 기본 운용시간을 검사한다). 재현에는 실패했고 위 경로가 유일하게 성립하는 설명이라
    원인 확정 없이 이 부류를 없애는 쪽을 택했다. 같은 패턴을 쓰는 픽스처가 더 있다
    (test_journal_sync·test_market_filter_regime_gate 등) — 같은 증상이 보이면 여기부터 의심할 것.
    """
    saved = (config.settings.SYSTEM_TRADING_START_TIME,
             config.settings.SYSTEM_TRADING_END_TIME)
    config.settings.SYSTEM_TRADING_START_TIME = "0800"
    config.settings.SYSTEM_TRADING_END_TIME = "2000"
    yield
    # settings 객체가 교체됐더라도 '지금' 객체에 되돌린다.
    (config.settings.SYSTEM_TRADING_START_TIME,
     config.settings.SYSTEM_TRADING_END_TIME) = saved


def test_market_open_krx_default(trader, monkeypatch):
    """0. 기본 설정은 KRX 정규장(09:00~15:30) — NXT 시간대엔 자동매매가 돌지 않는다."""
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    assert config.settings.SYSTEM_TRADING_START_TIME == "0900"
    assert config.settings.SYSTEM_TRADING_END_TIME == "1530"

    for hh, mm, want in [(8, 30, False), (10, 0, True), (16, 0, False)]:
        monkeypatch.setattr('modules.auto_trade.common.datetime',
                            MagicMock(now=lambda hh=hh, mm=mm: datetime(2026, 6, 11, hh, mm)))
        assert trader.is_market_open() is want, f"{hh:02d}:{mm:02d}"


def test_market_open_regular_times(trader, monkeypatch, nxt_hours):
    """1. NXT까지 넓힌 설정에서의 거래 시간(정규장, 프리마켓, 애프터마켓) 인식 테스트"""
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    
    # 오전 8시 30분 (NXT 프리마켓)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 8, 30)))
    assert trader.is_market_open() is True
    
    # 오전 10시 (KRX 정규장)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 10, 0)))
    assert trader.is_market_open() is True
    
    # 오후 4시 (NXT 애프터마켓)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 16, 0)))
    assert trader.is_market_open() is True

def test_market_pause_times(trader, monkeypatch, nxt_hours):
    """2. NXT 장 휴게시간 (단일가 동기화 시간) 차단 테스트"""
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    
    # 08:55 (오전 휴게시간)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 8, 55)))
    assert trader.is_market_open() is False
    
    # 15:28 (오후 휴게시간)
    monkeypatch.setattr('modules.auto_trade.common.datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 15, 28)))
    assert trader.is_market_open() is False

def test_single_price_break_only_on_trading_day(monkeypatch):
    """2-1. 단일가 휴게 구간 판정은 거래일에만 성립한다 (주말·공휴일 15:28은 휴장일)."""
    from modules.auto_trade.common import is_single_price_break

    # 2026-06-11(목) 15:28 — 실제 종가 단일가 구간
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    monkeypatch.setattr('modules.auto_trade.common.datetime',
                        MagicMock(now=lambda: datetime(2026, 6, 11, 15, 28)))
    assert is_single_price_break() is True

    # 같은 시각이라도 휴장일(주말/공휴일)이면 단일가 구간이 아니다
    monkeypatch.setattr(api, 'is_holiday_today', lambda: True)
    assert is_single_price_break() is False

    # 거래일이어도 휴게 구간 밖이면 False
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)
    monkeypatch.setattr('modules.auto_trade.common.datetime',
                        MagicMock(now=lambda: datetime(2026, 6, 11, 10, 0)))
    assert is_single_price_break() is False


def test_sor_order_routing_real(monkeypatch):
    """3. 실전 투자 시 SOR(최적주문집행) 거래소 코드가 올바르게 포함되는지 테스트"""
    config.session.is_simulation = False
    
    def mock_call_api(url_path, market, category, action, data=None, method="GET", timeout=None, retries=None, tr_id=None):
        assert data is not None
        assert data.get("EXCG_ID_DVSN_CD") == "SOR" # SOR 라우팅 확인
        return {'rt_cd': '0', 'output': {'ODNO': '12345'}}
        
    monkeypatch.setattr(api, 'call_api', mock_call_api)

    api.place_order("domestic", "buy", "005930", 1, 50000, "00")

def test_nxt_unsupported_order_routing_krx(monkeypatch):
    """4. NXT 미지원 종목(ETF 등)은 SOR 대신 KRX로 라우팅되는지 테스트 (APBK3026 방지)"""
    config.session.is_simulation = False
    monkeypatch.setattr(api, 'is_nxt_tradeable', lambda code: False)

    def mock_call_api(url_path, market, category, action, data=None, method="GET", timeout=None, retries=None, tr_id=None):
        assert data is not None
        assert data.get("EXCG_ID_DVSN_CD") == "KRX"  # NXT 미지원 → KRX 라우팅
        return {'rt_cd': '0', 'output': {'ODNO': '12345'}}

    monkeypatch.setattr(api, 'call_api', mock_call_api)

    # KODEX 삼성그룹 (NXT 미지원 ETF)
    api.place_order("domestic", "buy", "102780", 1, 25400, "00")


# ==========================================================
# 체결강도 시장 분리 (2026-07-27)
#  정규장에는 NXT(NX) 체결강도를 쓰지 않는다 — 정규장의 수백분의 1 거래량에서 나온
#  다른 시장의 값이 매수 수급 게이트로 들어가면 안 되기 때문. KRX 값을 못 구하면
#  None(판단 불가)으로 두고 다음 주기에 재조회한다.
# ==========================================================
@pytest.fixture
def vol_strength_env(monkeypatch):
    """체결강도 REST 경로만 타도록 WS·토스·모의투자·캐시를 정리한다."""
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', False, raising=False)
    monkeypatch.setattr(config, 'USE_WEBSOCKET', False, raising=False)
    api._MICRO_CACHE.clear()
    monkeypatch.setattr(api, 'is_holiday_today', lambda: False)


def _vol_api_recorder(j_value, nx_value):
    """call_api 대역: 조회된 시장구분(J/NX)을 기록하고 지정한 체결강도를 돌려준다."""
    seen = []

    def _mock(url_path, market, category, action, params=None, data=None,
              method="GET", timeout=None, retries=None, tr_id=None):
        div = (params or {}).get("FID_COND_MRKT_DIV_CODE")
        seen.append(div)
        val = j_value if div == "J" else nx_value
        return {'rt_cd': '0', 'output': [{'tday_rltv': str(val)}]}

    return _mock, seen


def test_vol_strength_regular_hours_no_nxt(monkeypatch, vol_strength_env):
    """5. 정규장에 KRX(J) 체결강도가 무효(0)여도 NXT를 조회하지 않고 None(보류)을 반환한다."""
    monkeypatch.setattr(api, 'datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 10, 0)))
    mock_api, seen = _vol_api_recorder(j_value=0, nx_value=180.0)
    monkeypatch.setattr(api, 'call_api', mock_api)

    assert api.get_realtime_vol_strength("005930") is None
    assert "NX" not in seen, f"정규장에 NXT 체결강도를 조회했다: {seen}"
    # 보류값은 캐시되지 않아야 다음 주기에 재조회된다
    assert api._get_micro_cache("vol_005930", ttl=3.0) is None


def test_vol_strength_regular_hours_uses_krx(monkeypatch, vol_strength_env):
    """6. 정규장에 KRX(J) 체결강도가 유효하면 그 값을 그대로 쓴다."""
    monkeypatch.setattr(api, 'datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 10, 0)))
    mock_api, seen = _vol_api_recorder(j_value=125.0, nx_value=180.0)
    monkeypatch.setattr(api, 'call_api', mock_api)

    assert api.get_realtime_vol_strength("000660") == 125.0
    assert "NX" not in seen


def test_vol_strength_nxt_hours_uses_nxt(monkeypatch, vol_strength_env):
    """7. NXT 단독시간(애프터마켓)에는 KRX가 닫혀 있으므로 NXT 체결강도를 채택한다."""
    monkeypatch.setattr(api, 'datetime', MagicMock(now=lambda: datetime(2026, 6, 11, 16, 0)))
    mock_api, seen = _vol_api_recorder(j_value=0, nx_value=180.0)
    monkeypatch.setattr(api, 'call_api', mock_api)

    assert api.get_realtime_vol_strength("035720") == 180.0
    assert "NX" in seen