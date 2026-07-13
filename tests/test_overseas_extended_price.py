"""해외주식 프리/애프터장 현재가 로직 테스트.

설계 (KIS API 값 1차 신뢰 + TV 폴백, yfinance 제외):
  1. KIS last/diff/rate를 그대로 사용 (외부 소스로 덮어쓰지 않음)
  2. last가 base·rate와 불일치(last 동결)하면 KIS 자체 필드로 역산 보정 (last = base×(1+rate/100))
  3. KIS 전 거래소 조회 실패 시에만 TradingView(src='tv') 폴백, diff/rate는 base 기준 재계산
  4. yfinance(src='yf') 값은 현재가 폴백에 사용하지 않음
"""

import pytest
from unittest.mock import patch

import api
import config


def _kis_resp(last, base, rate, diff=0.0):
    return {'rt_cd': '0', 'output': {
        'last': str(last), 'base': str(base), 'rate': str(rate), 'diff': str(diff),
    }}


@pytest.fixture(autouse=True)
def _kis_session():
    """KIS 세션 기준으로 테스트 (토스 어댑터 우회)"""
    orig = getattr(config.session, 'is_toss', False)
    config.session.is_toss = False
    yield
    config.session.is_toss = orig


class TestKisPrimary:
    def test_consistent_last_kept_without_external_call(self):
        """KIS last·rate가 정합이면 그대로 사용하고 TV/yfinance를 호출하지 않는다"""
        resp = _kis_resp(last=105.0, base=100.0, rate=5.0, diff=5.0)
        with patch.object(api, 'call_api', return_value=resp), \
             patch.object(config.session, 'update_cache_and_save'), \
             patch.object(api, 'get_yf_fast_info') as m_fi:
            res = api.get_current_price_data('TSTCONS', True, cache_ttl=0)
        assert res['rt_cd'] == '0'
        assert float(res['output']['last']) == pytest.approx(105.0)
        assert float(res['output']['rate']) == pytest.approx(5.0)
        m_fi.assert_not_called()

    def test_frozen_last_corrected_from_kis_rate(self):
        """last가 전일 종가에 동결되고 rate만 갱신되면 base×(1+rate/100)로 역산 보정 (보고된 프리장 증상)"""
        resp = _kis_resp(last=100.0, base=100.0, rate=3.5, diff=3.5)
        with patch.object(api, 'call_api', return_value=resp), \
             patch.object(config.session, 'update_cache_and_save'), \
             patch.object(api, 'get_yf_fast_info') as m_fi:
            res = api.get_current_price_data('TSTFROZ', True, cache_ttl=0)
        assert float(res['output']['last']) == pytest.approx(103.5)
        assert float(res['output']['rate']) == pytest.approx(3.5)  # 가격·등락률 일관
        m_fi.assert_not_called()

    def test_small_rounding_gap_not_corrected(self):
        """rate 반올림 수준의 미세 오차(0.1% 미만)는 last를 보정하지 않는다"""
        resp = _kis_resp(last=103.52, base=100.0, rate=3.5, diff=3.5)
        with patch.object(api, 'call_api', return_value=resp), \
             patch.object(config.session, 'update_cache_and_save'):
            res = api.get_current_price_data('TSTROUND', True, cache_ttl=0)
        assert float(res['output']['last']) == pytest.approx(103.52)

    def test_zero_rate_leaves_last_untouched(self):
        """rate=0(보합 또는 미제공)이면 last를 신뢰하고 보정하지 않는다"""
        resp = _kis_resp(last=101.0, base=100.0, rate=0.0, diff=0.0)
        with patch.object(api, 'call_api', return_value=resp), \
             patch.object(config.session, 'update_cache_and_save'):
            res = api.get_current_price_data('TSTZERO', True, cache_ttl=0)
        assert float(res['output']['last']) == pytest.approx(101.0)


class TestTvFallback:
    def test_tv_fallback_when_kis_fails(self):
        """KIS 전 거래소 실패 시 TV(src='tv') 가격으로 폴백하고 diff/rate를 재계산한다"""
        fi = {'last_price': 50.5, 'regular_market_previous_close': 50.0,
              'src': 'tv', 'is_extended': True}
        with patch.object(api, 'call_api', return_value={'rt_cd': '1'}), \
             patch.object(api, 'get_yf_fast_info', return_value=fi):
            res = api.get_current_price_data('TSTTVFB', True, cache_ttl=0)
        assert res['rt_cd'] == '0'
        out = res['output']
        assert float(out['last']) == pytest.approx(50.5)
        assert float(out['base']) == pytest.approx(50.0)
        assert float(out['rate']) == pytest.approx(1.0)
        assert float(out['diff']) == pytest.approx(0.5)
        assert out['_src'] == 'tv_fallback'

    def test_yfinance_source_not_used_for_fallback(self):
        """yfinance(src='yf') 값은 정규장가만 제공하므로 현재가 폴백에 쓰지 않는다"""
        fi = {'last_price': 50.5, 'regular_market_previous_close': 50.0,
              'src': 'yf', 'is_extended': False}
        with patch.object(api, 'call_api', return_value={'rt_cd': '1'}), \
             patch.object(api, 'get_yf_fast_info', return_value=fi):
            res = api.get_current_price_data('TSTYFNO', True, cache_ttl=0)
        assert res['rt_cd'] == '9999'

    def test_fallback_none_fast_info(self):
        """TV 폴백조차 없으면 기존과 동일하게 실패 코드를 반환한다"""
        with patch.object(api, 'call_api', return_value={'rt_cd': '1'}), \
             patch.object(api, 'get_yf_fast_info', return_value=None):
            res = api.get_current_price_data('TSTNONE', True, cache_ttl=0)
        assert res['rt_cd'] == '9999'
