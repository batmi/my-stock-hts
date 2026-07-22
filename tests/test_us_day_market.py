"""미국 주간거래(데이마켓) 시세 조회 테스트.

KIS는 미국 야간 ATS 세션(ET 20:00~04:00 = KST 09:00~17:00 서머타임 기준)을 '주간거래'로
부르며 정규장과 다른 거래소 코드(BAQ/BAY/BAA)로 시세를 제공한다. 이 코드로 조회하지 않으면
세션 내내 직전 정규장 마감가가 그대로 굳는다.

실측(2026-07-22 ET 02:50): MU NAS $970.82 +12.17%(전일 마감 동결) vs BAQ $949.00 -2.25%(라이브).
주문 경로(modules/trading.py)는 이미 ord_dvsn '31'로 데이마켓을 인지하고 있었다.
"""
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

import api
import config


# ==========================================================
# 세션 판정 — us_day_market_session
# ==========================================================

@pytest.mark.parametrize("et, expect", [
    # 야간 ATS 세션은 ET 20:00 시작 → 다음 거래일 세션으로 귀속
    (datetime(2026, 7, 21, 21, 0), "20260722"),   # 화 밤 → 수 세션
    (datetime(2026, 7, 22, 2, 50), "20260722"),   # 수 새벽 → 수 세션 (사용자 관측 시각)
    (datetime(2026, 7, 22, 3, 59), "20260722"),   # 세션 종료 직전
    # 세션 밖
    (datetime(2026, 7, 22, 4, 0), None),          # 프리마켓 시작 → 데이마켓 종료
    (datetime(2026, 7, 22, 10, 0), None),         # 정규장
    (datetime(2026, 7, 22, 17, 0), None),         # 애프터마켓
    (datetime(2026, 7, 22, 19, 59), None),        # 세션 시작 직전
    # 주말 경계 — 귀속일이 비거래일이면 세션 없음
    (datetime(2026, 7, 24, 21, 0), None),         # 금 밤 → 토 귀속 → 닫힘
    (datetime(2026, 7, 25, 2, 0), None),          # 토 새벽 → 토 귀속 → 닫힘
    (datetime(2026, 7, 26, 21, 0), "20260727"),   # 일 밤 → 월 세션 (Blue Ocean 주간 시작)
])
def test_day_market_session_window(et, expect):
    with patch('api.now_us_eastern', return_value=et):
        assert api.us_day_market_session() == expect


def test_day_market_session_closed_day_is_none():
    """세션 귀속일이 미국 휴장일이면 열리지 않는다."""
    with patch('api.now_us_eastern', return_value=datetime(2026, 7, 22, 2, 0)), \
         patch('api._is_closed_day', return_value=True):
        assert api.us_day_market_session() is None


# ==========================================================
# 거래소 코드 후보 — us_excd_candidates
# ==========================================================

def test_excd_candidates_regular_hours_unchanged():
    """정규 시간대에는 기존 동작 그대로 (주간 코드 미포함)."""
    with patch('api.us_day_market_session', return_value=None):
        got = api.us_excd_candidates("NAS")
    assert got[0] == "NAS"
    assert not ({"BAQ", "BAY", "BAA"} & set(got))


def test_excd_candidates_day_market_prefers_day_codes():
    """데이마켓 중에는 주간 코드를 먼저 시도하고 정규 코드로 폴백한다."""
    with patch('api.us_day_market_session', return_value="20260722"):
        got = api.us_excd_candidates("NAS")
    assert got[0] == "BAQ"                       # 캐시된 거래소(NAS)의 주간 코드가 최우선
    assert set(got[:3]) == {"BAQ", "BAY", "BAA"}  # 주간 코드가 앞쪽에 모임
    assert "NAS" in got                           # 정규 코드 폴백 유지


def test_excd_candidates_amex_maps_to_baa():
    """AMS(아멕스) 종목은 BAA가 최우선이어야 한다 (SOXL·SPY·GLD 등)."""
    with patch('api.us_day_market_session', return_value="20260722"):
        got = api.us_excd_candidates("AMS")
    assert got[0] == "BAA"


def test_excd_mapping_is_bidirectional():
    """정규↔주간 매핑이 일관되어야 캐시 역변환이 안전하다."""
    for reg, day in (("NAS", "BAQ"), ("NYS", "BAY"), ("AMS", "BAA")):
        assert api.US_DAY_MARKET_EXCD[reg] == day
        assert api.US_REGULAR_EXCD[day] == reg


# ==========================================================
# market_today — 데이마켓 세션 귀속일
# ==========================================================

def test_market_today_uses_day_market_session_date():
    """ET 20:00~24:00에도 세션 귀속일(다음 거래일)을 반환해야 한다.

    ET 달력일(=직전 정규장일)을 그대로 쓰면 주간거래 체결가가 확정된 과거 봉을 덮어써 오염된다.
    """
    with patch('api.now_us_eastern', return_value=datetime(2026, 7, 21, 21, 0)):
        assert api.market_today(True) == "20260722"


def test_market_today_regular_hours_unaffected():
    with patch('api.us_day_market_session', return_value=None), \
         patch('api.now_us_eastern', return_value=datetime(2026, 7, 22, 11, 0)):
        assert api.market_today(True) == "20260722"


def test_market_today_domestic_unaffected():
    """국내 경로는 데이마켓 판정을 타지 않는다."""
    with patch('api.us_day_market_session', side_effect=AssertionError("국내에서 호출되면 안 됨")):
        assert api.market_today(False) == api.last_trading_day(datetime.now(), 'KR')


# ==========================================================
# 현재가 조회 경로 — 주간 코드 사용 + 캐시에는 정규 코드 저장
# ==========================================================

def _price_res(last, base, rate):
    return {'rt_cd': '0', 'output': {'last': str(last), 'base': str(base), 'rate': str(rate)}}


def test_current_price_uses_day_market_code_and_caches_regular():
    """데이마켓 중 BAQ로 조회하고, 캐시에는 정규 코드(NAS)를 저장해야 한다.

    update_cache_and_save는 stock.json에 영속되므로 주간 코드가 박히면
    정규장 시간대 조회와 주문 경로가 함께 깨진다.
    """
    config.session.is_toss = False
    calls = []

    def fake_call_api(url, *a, **kw):
        excd = kw.get('params', {}).get('EXCD')
        calls.append(excd)
        if excd == "BAQ":
            return _price_res(949.00, 970.82, -2.25)   # 라이브 주간거래
        return _price_res(970.82, 865.46, 12.17)       # 정규장 동결값

    with patch('api.us_day_market_session', return_value="20260722"), \
         patch('api.call_api', side_effect=fake_call_api), \
         patch.dict(config.session.exchange_cache, {"MU": "NAS"}, clear=False), \
         patch.object(config.session, 'update_cache_and_save') as mock_save, \
         patch('api._get_micro_cache', return_value=None), \
         patch('api._set_micro_cache'):
        res = api.get_current_price_data("MU", is_overseas=True)

    assert calls[0] == "BAQ", "주간 코드를 먼저 시도해야 함"
    assert res['output']['last'] == "949.0"           # 정합성 보정 후에도 주간 가격 유지
    assert float(res['output']['rate']) == -2.25
    # 캐시 저장이 일어난다면 반드시 정규 코드여야 한다
    for call in mock_save.call_args_list:
        assert call.args[1] in ("NAS", "NYS", "AMS"), f"주간 코드가 저장됨: {call.args[1]}"


def test_current_price_falls_back_to_regular_when_day_code_empty():
    """주간 코드에 체결이 없으면(빈 응답) 정규 코드로 폴백한다."""
    config.session.is_toss = False

    def fake_call_api(url, *a, **kw):
        excd = kw.get('params', {}).get('EXCD')
        if excd in ("BAQ", "BAY", "BAA"):
            return {'rt_cd': '0', 'output': {'last': '', 'base': '', 'rate': ''}}
        return _price_res(970.82, 865.46, 12.17)

    with patch('api.us_day_market_session', return_value="20260722"), \
         patch('api.call_api', side_effect=fake_call_api), \
         patch.dict(config.session.exchange_cache, {"MU": "NAS"}, clear=False), \
         patch.object(config.session, 'update_cache_and_save'), \
         patch('api._get_micro_cache', return_value=None), \
         patch('api._set_micro_cache'):
        res = api.get_current_price_data("MU", is_overseas=True)

    assert float(res['output']['last']) == 970.82


def test_current_price_regular_hours_skips_day_codes():
    """정규 시간대에는 주간 코드를 조회하지 않는다 (불필요한 TPS 소모 방지)."""
    config.session.is_toss = False
    calls = []

    def fake_call_api(url, *a, **kw):
        calls.append(kw.get('params', {}).get('EXCD'))
        return _price_res(970.82, 865.46, 12.17)

    with patch('api.us_day_market_session', return_value=None), \
         patch('api.call_api', side_effect=fake_call_api), \
         patch.dict(config.session.exchange_cache, {"MU": "NAS"}, clear=False), \
         patch.object(config.session, 'update_cache_and_save'), \
         patch('api._get_micro_cache', return_value=None), \
         patch('api._set_micro_cache'):
        api.get_current_price_data("MU", is_overseas=True)

    assert not ({"BAQ", "BAY", "BAA"} & set(calls))


# ==========================================================
# 상세(52주 위치) 경로
# ==========================================================

def test_detail_price_uses_day_market_code_and_caches_regular():
    """상세 TR도 주간 코드를 먼저 쓰고, 캐시에는 정규 코드를 저장한다."""
    config.session.is_toss = False
    calls = []

    def fake_call_api(url, *a, **kw):
        excd = kw.get('params', {}).get('EXCD')
        calls.append(excd)
        last = "948.55" if excd == "BAQ" else "970.82"
        return {'rt_cd': '0', 'output': {'last': last, 'h52p': '1254.80', 'l52p': '600.0', 'perx': '21.47'}}

    with patch('api.us_day_market_session', return_value="20260722"), \
         patch('api.call_api', side_effect=fake_call_api), \
         patch.object(config.session, 'update_cache_and_save') as mock_save, \
         patch('api._get_micro_cache', return_value=None), \
         patch('api._set_micro_cache'):
        out = api.fetch_overseas_detail_price("MU", "NAS")

    assert calls[0] == "BAQ"
    assert out['last'] == "948.55"   # 52주 위치가 라이브 가격으로 계산되도록
    for call in mock_save.call_args_list:
        assert call.args[1] in ("NAS", "NYS", "AMS")
