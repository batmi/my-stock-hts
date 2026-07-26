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


class TestTossTvFallback:
    """mode 3(토스): 해외 현재가 조회 실패 시 TV 폴백 (mode 1/2와 동일 경로)."""

    @pytest.fixture(autouse=True)
    def _toss_session(self):
        orig = getattr(config.session, 'is_toss', False)
        config.session.is_toss = True
        api._MICRO_CACHE.clear()
        yield
        config.session.is_toss = orig
        api._MICRO_CACHE.clear()

    def test_overseas_falls_back_to_tv_on_api_error(self):
        """토스 get_price가 TossApiError면 해외는 TV(src='tv')로 폴백한다"""
        import toss_api
        fi = {'last_price': 50.5, 'regular_market_previous_close': 50.0,
              'src': 'tv', 'is_extended': True}
        with patch.object(toss_api, 'get_price', side_effect=toss_api.TossApiError("ERR", "boom")), \
             patch.object(api, 'get_yf_fast_info', return_value=fi):
            res = api.get_current_price_data('TSTTOSSFB', True)
        assert res['rt_cd'] == '0'
        assert float(res['output']['last']) == pytest.approx(50.5)
        assert res['output']['_src'] == 'tv_fallback'

    def test_overseas_falls_back_to_tv_on_zero_price(self):
        """토스가 lastPrice=0(무효)을 주면 해외는 TV로 폴백한다"""
        fi = {'last_price': 12.3, 'regular_market_previous_close': 12.0,
              'src': 'tv', 'is_extended': False}
        with patch('toss_api.get_price', return_value={'lastPrice': '0'}), \
             patch.object(api, 'get_yf_fast_info', return_value=fi):
            res = api.get_current_price_data('TSTTOSSZERO', True)
        assert res['rt_cd'] == '0'
        assert float(res['output']['last']) == pytest.approx(12.3)

    def test_overseas_no_tv_available_returns_fail(self):
        """토스도 TV도 없으면 실패 코드를 반환한다"""
        with patch('toss_api.get_price', return_value=None), \
             patch.object(api, 'get_yf_fast_info', return_value=None):
            res = api.get_current_price_data('TSTTOSSNONE', True)
        assert res['rt_cd'] == '9999'

    def test_domestic_does_not_fall_back_to_tv(self):
        """국내는 KRX/NXT 전용 → TV 폴백하지 않고 실패 코드를 반환한다"""
        m_fi = patch.object(api, 'get_yf_fast_info')
        with patch('toss_api.get_price', return_value=None), m_fi as mock_fi:
            res = api.get_current_price_data('005930', False)
        assert res['rt_cd'] == '9999'
        mock_fi.assert_not_called()


class TestTossDailyChartTvFallback:
    """mode 3(토스): 해외 일봉이 토스 캔들로 부족할 때 tvDatafeed(TradingView) 폴백."""

    import pandas as pd

    def _daily(self, n):
        import pandas as pd
        dates = pd.date_range('2025-01-01', periods=n).strftime('%Y%m%d')
        return pd.DataFrame({
            'date': dates, 'open': 1.0, 'high': 1.0, 'low': 1.0,
            'close': 1.0, 'volume': 1.0,
        })

    def test_overseas_empty_toss_uses_tv(self):
        """토스 캔들이 비면 해외는 tvDatafeed 일봉으로 폴백한다"""
        import pandas as pd
        import modules.analysis as analysis
        tv_df = self._daily(200)
        with patch.object(api, '_toss_chart_data', return_value=pd.DataFrame()), \
             patch.object(analysis, 'fetch_overseas_daily_via_tvdatafeed', return_value=tv_df) as m_tv:
            out = api._toss_daily_chart_with_tv_fallback('SKHY', True)
        assert len(out) == 200
        m_tv.assert_called_once()

    def test_overseas_insufficient_toss_uses_tv(self):
        """토스가 120봉 미만이면(EMA120 불가) 더 긴 TV 일봉으로 폴백한다"""
        import modules.analysis as analysis
        short = self._daily(40)
        tv_df = self._daily(250)
        with patch.object(api, '_toss_chart_data', return_value=short), \
             patch.object(analysis, 'fetch_overseas_daily_via_tvdatafeed', return_value=tv_df):
            out = api._toss_daily_chart_with_tv_fallback('SPCX', True)
        assert len(out) == 250

    def test_overseas_sufficient_toss_no_tv_call(self):
        """토스가 120봉 이상이면 TV를 호출하지 않는다"""
        import modules.analysis as analysis
        full = self._daily(200)
        with patch.object(api, '_toss_chart_data', return_value=full), \
             patch.object(analysis, 'fetch_overseas_daily_via_tvdatafeed') as m_tv:
            out = api._toss_daily_chart_with_tv_fallback('AAPL', True)
        assert len(out) == 200
        m_tv.assert_not_called()

    def test_domestic_never_uses_tv(self):
        """국내 종목은 tvDatafeed 폴백하지 않는다(빈 결과 그대로)"""
        import pandas as pd
        import modules.analysis as analysis
        with patch.object(api, '_toss_chart_data', return_value=pd.DataFrame()), \
             patch.object(api, '_krx_daily_chart', return_value=None), \
             patch.object(analysis, 'fetch_overseas_daily_via_tvdatafeed') as m_tv:
            out = api._toss_daily_chart_with_tv_fallback('005930', False)
        assert out.empty
        m_tv.assert_not_called()


class _FakeQuery:
    """tradingview_screener.Query 대역: fluent 체인을 흡수하고 지정한 df를 반환."""
    def __init__(self, df):
        self._df = df
        self.query = {'filter2': object(), 'filter': []}
    def select(self, *a, **k): return self
    def set_markets(self, *a, **k): return self
    def where(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def get_scanner_data(self):
        return (0 if self._df is None else len(self._df), self._df)


class TestTossOverseasFundamentals:
    """mode 3(토스): 해외 PER/PBR/상장주수를 TradingView 스캐너로 보강."""

    def _run(self, df):
        import pandas as pd
        with patch('tradingview_screener.Query', lambda *a, **k: _FakeQuery(df)):
            return api._tv_overseas_fundamentals('TST')

    def test_stock_per_pbr(self):
        """주식: PER/PBR을 문자열로, 상장주수도 함께 채운다"""
        import pandas as pd
        df = pd.DataFrame([{'price_earnings_ttm': 39.27, 'price_book_fq': 44.71,
                            'total_shares_outstanding': 14_687_400_000,
                            'aum': None, 'nav': None, 'type': 'stock'}])
        out = self._run(df)
        assert out['perx'] == '39.27'
        assert out['pbrx'] == '44.71'
        assert out['shar'] == pytest.approx(14_687_400_000)

    def test_loss_maker_per_computed_from_abs_eps(self):
        """적자 기업(PER_ttm=None): KIS와 동일하게 주가/|희석EPS|로 PER 계산"""
        import pandas as pd
        df = pd.DataFrame([{'close': 102.32, 'price_earnings_ttm': None,
                            'price_book_fq': 4.59,
                            'earnings_per_share_diluted_ttm': -0.6263,
                            'earnings_per_share_basic_ttm': -0.6229,
                            'earnings_per_share_diluted_fy': -0.0589,
                            'total_shares_outstanding': 5_026_000_000,
                            'aum': None, 'nav': None, 'type': 'stock'}])
        out = self._run(df)
        assert out['perx'] == f"{102.32 / 0.6263:.2f}"  # ≈ 163.37
        assert out['pbrx'] == '4.59'

    def test_per_falls_back_to_fy_eps(self):
        """TTM EPS가 없으면 희석(FY) EPS로 PER 계산(SPCX 사례)"""
        import pandas as pd
        df = pd.DataFrame([{'close': 136.79, 'price_earnings_ttm': None,
                            'price_book_fq': 51.74,
                            'earnings_per_share_diluted_ttm': None,
                            'earnings_per_share_basic_ttm': None,
                            'earnings_per_share_diluted_fy': -0.3776,
                            'total_shares_outstanding': 7_571_397_000,
                            'aum': None, 'nav': None, 'type': 'stock'}])
        out = self._run(df)
        assert out['perx'] == f"{136.79 / 0.3776:.2f}"

    def test_no_eps_leaves_per_absent(self):
        """EPS 자체가 없으면(ETF 등) PER은 생략('-')"""
        import pandas as pd
        df = pd.DataFrame([{'close': 715.5, 'price_earnings_ttm': None,
                            'price_book_fq': None,
                            'earnings_per_share_diluted_ttm': None,
                            'earnings_per_share_basic_ttm': None,
                            'earnings_per_share_diluted_fy': None,
                            'total_shares_outstanding': None,
                            'aum': 476_072_425_000, 'nav': 719.718, 'type': 'fund'}])
        out = self._run(df)
        assert 'perx' not in out

    def test_etf_shares_derived_from_aum_nav(self):
        """ETF: total_shares_outstanding이 비면 aum/nav로 상장주수 역산, PER/PBR은 생략"""
        import pandas as pd
        df = pd.DataFrame([{'price_earnings_ttm': None, 'price_book_fq': None,
                            'total_shares_outstanding': None,
                            'aum': 476_072_425_000, 'nav': 719.718, 'type': 'fund'}])
        out = self._run(df)
        assert 'perx' not in out and 'pbrx' not in out
        assert out['shar'] == pytest.approx(476_072_425_000 / 719.718)

    def test_empty_result_returns_empty(self):
        """스캐너 미매칭이면 빈 dict"""
        import pandas as pd
        assert self._run(pd.DataFrame()) == {}

    def test_query_error_returns_empty(self):
        """스캐너 예외 시 빈 dict(폴백)"""
        with patch('tradingview_screener.Query', side_effect=RuntimeError("boom")):
            assert api._tv_overseas_fundamentals('TST') == {}

    def test_detail_path_uses_tv_in_toss(self):
        """토스 모드 fetch_overseas_detail_price는 TV 펀더멘털 dict를 반환한다"""
        orig = getattr(config.session, 'is_toss', False)
        config.session.is_toss = True
        api._MICRO_CACHE.clear()
        try:
            with patch.object(api, '_tv_overseas_fundamentals',
                              return_value={'perx': '10.00', 'shar': 1.0}) as m:
                res = api.fetch_overseas_detail_price('TST', 'NAS')
            assert res == {'perx': '10.00', 'shar': 1.0}
            m.assert_called_once_with('TST')
        finally:
            config.session.is_toss = orig
            api._MICRO_CACHE.clear()


class TestTvDatafeedLogin:
    """tvDatafeed 인스턴스 생성 시 TradingView 로그인 처리.

    TV_USERNAME/TV_PASSWORD가 있으면 로그인 토큰을 주입(성공 시 INFO 로그),
    없거나 로그인 실패면 익명(nologin)으로 동작하고 WARNING 로그를 남긴다.
    """

    class _FakeTv:
        def __init__(self, username=None, password=None):
            self.token = "unauthorized_user_token"

    @pytest.fixture(autouse=True)
    def _reset_singleton(self, tmp_path):
        """싱글턴 초기화 + 토큰 캐시 경로 격리(운영 캐시 파일 덮어쓰기 방지)"""
        import modules.analysis as analysis
        analysis._TVDATAFEED_INSTANCE = None
        analysis._TVDATAFEED_LOGGED_IN = False
        with patch.object(analysis, '_TV_TOKEN_CACHE_PATH', str(tmp_path / "tv_token.json")):
            yield
        analysis._TVDATAFEED_INSTANCE = None
        analysis._TVDATAFEED_LOGGED_IN = False

    def _make(self, user, pw, signin_result):
        """config 자격증명과 signin 결과를 지정해 _get_tvdatafeed()를 호출한다."""
        import sys
        import types
        import modules.analysis as analysis
        fake_mod = types.ModuleType('tvDatafeed')
        fake_mod.TvDatafeed = self._FakeTv
        with patch.dict(sys.modules, {'tvDatafeed': fake_mod}), \
             patch.object(config, 'TV_USERNAME', user), \
             patch.object(config, 'TV_PASSWORD', pw), \
             patch.object(analysis, '_tv_signin', return_value=signin_result) as m_signin:
            tv = analysis._get_tvdatafeed()
        return tv, m_signin, analysis

    def test_login_success_injects_token(self, caplog):
        """자격증명이 있고 로그인 성공하면 토큰을 주입하고 INFO 로그를 남긴다"""
        import logging
        with caplog.at_level(logging.INFO, logger='modules.analysis'):
            tv, m_signin, analysis = self._make('user', 'pw', ('tok-abc', None))
        assert tv.token == 'tok-abc'
        assert analysis._TVDATAFEED_LOGGED_IN is True
        m_signin.assert_called_once_with('user', 'pw')
        assert any(r.levelno == logging.INFO and '로그인 성공' in r.message for r in caplog.records)

    def test_login_failure_falls_back_to_anonymous(self, caplog):
        """로그인 실패 시 익명 토큰 유지 + WARNING(사유 포함)"""
        import logging
        with caplog.at_level(logging.WARNING, logger='modules.analysis'):
            tv, _, analysis = self._make('user', 'pw', (None, 'HTTP 401, 사유: invalid'))
        assert tv.token == 'unauthorized_user_token'
        assert analysis._TVDATAFEED_LOGGED_IN is False
        assert any(r.levelno == logging.WARNING and '로그인 실패' in r.message
                   and 'HTTP 401' in r.message for r in caplog.records)

    @pytest.mark.parametrize('user,pw', [('', ''), ('user', ''), ('', 'pw')])
    def test_missing_credentials_warns_and_skips_signin(self, caplog, user, pw):
        """자격증명이 없거나 한쪽만 있으면 signin 시도 없이 익명 + WARNING"""
        import logging
        with caplog.at_level(logging.WARNING, logger='modules.analysis'):
            tv, m_signin, analysis = self._make(user, pw, (None, None))
        assert tv is not None
        assert analysis._TVDATAFEED_LOGGED_IN is False
        m_signin.assert_not_called()
        assert any(r.levelno == logging.WARNING and 'TV_USERNAME/TV_PASSWORD' in r.message
                   for r in caplog.records)

    def test_signin_password_not_in_failure_reason(self):
        """실패 사유 문자열에 비밀번호가 섞이지 않는다(파일 로그 유출 방지)"""
        import modules.analysis as analysis

        class _Resp:
            status_code = 401
            def json(self):
                return {'error': 'invalid username or password'}

        with patch('requests.post', return_value=_Resp()):
            token, reason = analysis._tv_signin('user', 'topsecret')
        assert token is None
        assert 'topsecret' not in reason
        assert 'invalid username or password' in reason

    def test_cached_token_skips_signin(self, tmp_path):
        """캐시된 토큰이 있으면 signin 없이 재사용한다(캡차 유발 최소화)"""
        import json
        import time
        import modules.analysis as analysis
        cache = tmp_path / "tv_token.json"
        cache.write_text(json.dumps({'user': 'user', 'token': 'cached-tok', 'ts': time.time()}))
        with patch.object(analysis, '_TV_TOKEN_CACHE_PATH', str(cache)):
            tv, m_signin, analysis = self._make('user', 'pw', ('fresh-tok', None))
        assert tv.token == 'cached-tok'
        m_signin.assert_not_called()

    @pytest.mark.parametrize('payload,label', [
        ({'user': 'other', 'token': 'tok', 'ts': None}, '계정 불일치'),
        ({'user': 'user', 'token': 'tok', 'ts': 0}, '만료'),
        ({'user': 'user', 'token': 'unauthorized_user_token', 'ts': None}, '익명 토큰'),
    ])
    def test_invalid_cache_triggers_signin(self, tmp_path, payload, label):
        """계정 불일치·만료·익명 토큰 캐시는 무시하고 다시 로그인한다"""
        import json
        import time
        import modules.analysis as analysis
        if payload['ts'] is None:
            payload['ts'] = time.time()
        cache = tmp_path / "tv_token.json"
        cache.write_text(json.dumps(payload))
        with patch.object(analysis, '_TV_TOKEN_CACHE_PATH', str(cache)):
            tv, m_signin, analysis = self._make('user', 'pw', ('fresh-tok', None))
        assert tv.token == 'fresh-tok', label
        m_signin.assert_called_once()
        # 새 토큰이 캐시에 기록된다
        assert json.loads(cache.read_text())['token'] == 'fresh-tok'
