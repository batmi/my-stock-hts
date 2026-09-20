"""지수 차트 반쪽 프레임·모르는 전일 종가 — api/indices.py 감사(2026-09-20).

[왜] 지수 일봉은 시장 필터(EMA80·이탈밴드 1%)와 국면 판정(EMA9/41)의 입력이다.
KIS 페이지네이션이 두 번째 페이지에서 끊기면 100봉짜리 프레임이 돌아오는데, 종목 일봉과
달리 지수는 `attrs['partial']` 표식이 없어 그대로 6시간 캐시에 굳었고 로그 한 줄 없었다.
100봉으로 낸 EMA80 은 300봉 기준과 약 1% 어긋난다 — 이탈밴드와 같은 크기다.
"""
import logging
from unittest.mock import patch

import pandas as pd
import pytest

import api
import config


def _page(n, start_day):
    """KIS 업종 일봉 응답 n건(최신→과거 순, 실제 TR 과 같은 방향)."""
    days = pd.bdate_range(end=start_day, periods=n)[::-1]
    return {'rt_cd': '0', 'output2': [{
        'stck_bsop_date': d.strftime('%Y%m%d'), 'bstp_nmix_prpr': '2500.5', 'bstp_nmix_oprc': '2490',
        'bstp_nmix_hgpr': '2510', 'bstp_nmix_lwpr': '2480', 'acml_vol': '1000',
    } for d in days]}


@pytest.fixture(autouse=True)
def _kis_mode():
    config.session.is_toss = False
    api.clear_chart_cache()
    yield
    api.clear_chart_cache()


@pytest.mark.real_index_chart
def test_두번째_페이지가_거부되면_반쪽_표식이_붙고_경고를_남긴다(caplog):
    pages = [_page(100, '2024-06-28'), {'rt_cd': '1', 'msg_cd': 'OPSQ0008', 'msg1': 'MCI전송 오류'}]
    with patch('api.call_api', side_effect=pages), caplog.at_level(logging.WARNING, logger='api'):
        df = api.get_domestic_index_chart('0001')
    assert len(df) == 100
    assert df.attrs.get('partial') is True
    assert any('여기까지 100건' in r.getMessage() for r in caplog.records)


@pytest.mark.real_index_chart
def test_반쪽_지수_차트는_캐시에_굳지_않는다():
    """같은 코드를 다시 부르면 캐시 적중이 아니라 재조회한다(온전한 차트가 오면 스스로 낫는다)."""
    pages = [_page(100, '2024-06-28'), {'rt_cd': '1', 'msg_cd': 'OPSQ0008', 'msg1': 'MCI전송 오류'},
             _page(100, '2024-06-28'), _page(100, '2024-02-09'), _page(100, '2023-09-22')]
    with patch('api.call_api', side_effect=pages) as m:
        first = api.get_domestic_index_chart('0001')
        second = api.get_domestic_index_chart('0001')
    assert first.attrs.get('partial') is True
    assert len(second) == 300 and not second.attrs.get('partial')
    assert m.call_count == 5
    with patch('api.call_api', side_effect=AssertionError('캐시가 있어야 한다')):
        with patch.object(api, '_get_micro_cache', return_value=None), \
             patch.object(api, 'get_domestic_index_price', return_value={'rt_cd': '9999'}):
            third = api.get_domestic_index_chart('0001')
    assert len(third) == 300


@pytest.mark.real_index_chart
def test_온전한_차트에는_표식이_없다():
    pages = [_page(100, '2024-06-28'), _page(100, '2024-02-09'), _page(100, '2023-09-22')]
    with patch('api.call_api', side_effect=pages):
        df = api.get_domestic_index_chart('0001')
    assert len(df) == 300
    assert not df.attrs.get('partial')


def test_선물_차트도_반쪽_표식을_붙이고_output2_None_에_죽지_않는다():
    def fut_page(n, end):
        p = _page(n, end)
        for it in p['output2']:
            it['futs_prpr'] = it.pop('bstp_nmix_prpr'); it['futs_oprc'] = it.pop('bstp_nmix_oprc')
            it['futs_hgpr'] = it.pop('bstp_nmix_hgpr'); it['futs_lwpr'] = it.pop('bstp_nmix_lwpr')
        return p
    with patch('api.call_api', side_effect=[fut_page(100, '2024-06-28'), {'rt_cd': '1', 'msg_cd': 'X', 'msg1': 'x'}]):
        df = api.get_k200_futures_chart('F', 'A01409')
    assert len(df) == 100 and df.attrs.get('partial') is True and df.attrs['source'] == 'KIS'
    with patch('api.call_api', side_effect=[{'rt_cd': '0', 'output2': None}]):
        assert api.get_k200_futures_chart('F', 'A01409').empty


def test_토스_모드_yfinance_지수_현재가는_전일값을_모르면_0으로_답한다():
    """모르는 전일 종가에 현재가를 넣으면 등락률 0% 가 만들어지고, 오버레이의 수정주가 검증이
    지수가 1.5% 이상 움직인 날마다 캐시를 파기·재조회한다. 토스 경로와 같은 '0 = 모름' 규약."""
    config.session.is_toss = True
    try:
        with patch.object(api, 'get_yf_fast_info', return_value={'last_price': 850.0,
                                                                  'regular_market_previous_close': None}):
            res = api.get_domestic_index_price('2001')     # 코스피200: 토스 심볼 없음 → yfinance
        assert res['rt_cd'] == '0'
        assert res['output']['bstp_nmix_prpr'] == '850.0'
        assert res['output']['bstp_nmix_prdy_clpr'] == '0'
        with patch.object(api, 'get_yf_fast_info', return_value={'last_price': 850.0,
                                                                  'regular_market_previous_close': 840.0}):
            res = api.get_domestic_index_price('2001')
        assert res['output']['bstp_nmix_prdy_clpr'] == '840.0'
    finally:
        config.session.is_toss = False


def test_반쪽_KIS_지수는_봉_수가_충분해도_다음_소스로_내려간다():
    """analysis 폴백 체인: partial 이면 tvDatafeed 를 시도하고, 그쪽이 온전하면 그것을 쓴다."""
    from modules import analysis
    partial = pd.DataFrame({'date': ['20240101'] * 100, 'open': 1.0, 'high': 1.0, 'low': 1.0,
                            'close': 1.0, 'volume': 0.0})
    partial.attrs['partial'] = True
    full = pd.DataFrame({'date': ['20240101'] * 300, 'open': 1.0, 'high': 1.0, 'low': 1.0,
                         'close': 1.0, 'volume': 0.0})
    full.attrs['source'] = 'TVDATAFEED'
    with patch.object(api, 'get_domestic_index_chart', return_value=partial), \
         patch.object(analysis, '_fetch_index_via_krx', return_value=None), \
         patch.object(analysis, '_fetch_index_via_tvdatafeed', return_value=full) as tv:
        out = analysis._fetch_domestic_index_data('KOSPI')
    assert tv.called
    assert len(out) == 300 and out.attrs.get('source') == 'TVDATAFEED'
    # 다음 소스마저 없으면 반쪽이라도 남긴다(있는 것이 없는 것보다 낫다)
    with patch.object(api, 'get_domestic_index_chart', return_value=partial), \
         patch.object(analysis, '_fetch_index_via_krx', return_value=None), \
         patch.object(analysis, '_fetch_index_via_tvdatafeed', return_value=None), \
         patch.object(api, 'get_chart_data', return_value=pd.DataFrame()):
        out = analysis._fetch_domestic_index_data('KOSPI')
    assert len(out) == 100
