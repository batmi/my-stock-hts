# tests/test_multi_price.py
"""관심종목 멀티시세(FHKST11300006) 일괄 현재가 조회 및 print_table 통합 테스트."""
import pytest
import numpy as np
import pandas as pd
from unittest.mock import patch

import config
import api
from modules import analysis


def _mk_multi_response(codes):
    """멀티시세 TR 원본 응답 형태의 mock (공식 스펙 필드명)"""
    return {
        'rt_cd': '0',
        'output': [
            {
                'inter_shrn_iscd': c,
                'inter_kor_isnm': f'테스트{c}',
                'inter2_prpr': '70000',
                'inter2_prdy_vrss': '500',
                'prdy_vrss_sign': '2',
                'prdy_ctrt': '0.72',
                'acml_vol': '12345',
                'inter2_oprc': '69500',
                'inter2_hgpr': '70500',
                'inter2_lwpr': '69000',
                'inter2_sdpr': '69500',
                'inter2_prdy_clpr': '69500',
                'kospi_kosdaq_cls_name': '코스피',
            } for c in codes
        ]
    }


def _mk_chart_df(periods=260, start_price=60000.0):
    dates = pd.date_range(start="2024-01-02", periods=periods).strftime('%Y%m%d')
    close = np.linspace(start_price, start_price * 1.2, periods)
    return pd.DataFrame({
        'date': dates,
        'open': close * 0.995,
        'high': close * 1.01,
        'low': close * 0.99,
        'close': close,
        'volume': np.random.randint(100000, 500000, periods).astype(float),
    })


@pytest.fixture(autouse=True)
def enable_multi(monkeypatch):
    """conftest가 기본 비활성화한 멀티시세를 이 테스트 모듈에서만 활성화."""
    monkeypatch.setattr(config, 'USE_MULTI_PRICE', True, raising=False)
    monkeypatch.setattr(api, '_MULTI_PRICE_DISABLED', False, raising=False)


def test_multi_price_field_normalization():
    """원본 필드가 개별 현재가 API 필드명으로 정규화되는지 확인."""
    with patch('api.call_api', return_value=_mk_multi_response(['005930'])) as mock_call:
        res = api.get_multi_current_prices(['005930'])

    assert mock_call.call_count == 1
    _, kwargs = mock_call.call_args
    assert kwargs.get('tr_id') == 'FHKST11300006'

    out = res['005930']
    assert out['stck_prpr'] == '70000'
    assert out['prdy_vrss'] == '500'
    assert out['prdy_ctrt'] == '0.72'
    assert out['stck_sdpr'] == '69500'
    assert out['stck_oprc'] == '69500'
    assert out['rprs_mrkt_kor_name'] == '코스피'
    assert out['_src'] == 'multi'


def test_multi_price_chunking_over_30():
    """30종목 초과 시 30개 단위로 나눠 호출하는지 확인 (35개 → 30+5)."""
    codes = [f"{i:06d}" for i in range(1, 36)]
    chunk_sizes = []

    def fake_call(url, market, category, action, params=None, **kw):
        chunk = [v for k, v in params.items() if k.startswith('FID_INPUT_ISCD_')]
        chunk_sizes.append(len(chunk))
        return _mk_multi_response(chunk)

    with patch('api.call_api', side_effect=fake_call):
        res = api.get_multi_current_prices(codes)

    assert sorted(chunk_sizes) == [5, 30]
    assert len(res) == 35


def test_multi_price_failure_disables_for_session():
    """TR 미지원(rt_cd!=0) 시 None 반환 + 세션 동안 재시도하지 않는지 확인."""
    with patch('api.call_api', return_value={'rt_cd': '1', 'msg1': 'not supported'}) as mock_call:
        assert api.get_multi_current_prices(['005930']) is None
        assert api._MULTI_PRICE_DISABLED is True
        # 두 번째 호출은 call_api 없이 즉시 폴백(None)
        assert api.get_multi_current_prices(['005930']) is None

    assert mock_call.call_count == 1


def test_multi_price_market_name_fallback_from_stock_json(monkeypatch):
    """kospi_kosdaq_cls_name이 빈 값이면(실전 실측) stock.json exchange로 시장구분 보강."""
    response = _mk_multi_response(['247540'])
    response['output'][0]['kospi_kosdaq_cls_name'] = ''  # 실전 응답 실측 케이스
    monkeypatch.setattr(config.session, 'stock_data', {
        'stocks_kr': [{'code': '247540', 'name': '에코프로비엠', 'exchange': 'KOSDAQ'}],
        'etfs_kr': [],
    }, raising=False)

    with patch('api.call_api', return_value=response):
        res = api.get_multi_current_prices(['247540'])

    assert res['247540']['rprs_mrkt_kor_name'] == 'KOSDAQ'


def test_multi_price_disabled_by_config(monkeypatch):
    """USE_MULTI_PRICE=False면 호출 자체를 생략."""
    monkeypatch.setattr(config, 'USE_MULTI_PRICE', False, raising=False)
    with patch('api.call_api') as mock_call:
        assert api.get_multi_current_prices(['005930']) is None
    mock_call.assert_not_called()


def test_collect_table_data_uses_preloaded_curr():
    """preloaded_curr가 있으면 종목별 현재가 REST를 생략하는지 확인."""
    chart_df = _mk_chart_df()
    preloaded = {'rt_cd': '0', 'output': {'stck_prpr': '70000', 'stck_sdpr': '69500', '_src': 'multi'}}

    with patch('modules.analysis.api.get_current_price_data') as mock_cp, \
         patch('modules.analysis.api.get_realtime_vol_strength', return_value=120.0):
        bundle = analysis._collect_table_data(
            ('삼성전자', '005930'), '국내 주식 기술적 분석', False, False,
            chart_df=chart_df, preloaded_curr=preloaded
        )

    mock_cp.assert_not_called()
    assert bundle['curr_data'] is preloaded
    assert bundle['rt_strength'] == 120.0


def test_collect_table_data_fallback_without_preloaded():
    """preloaded_curr가 없으면 종전대로 종목별 현재가를 조회(폴백 경로 동일 동작)."""
    chart_df = _mk_chart_df()
    single = {'rt_cd': '0', 'output': {'stck_prpr': '70000'}}

    with patch('modules.analysis.api.get_current_price_data', return_value=single) as mock_cp, \
         patch('modules.analysis.api.get_realtime_vol_strength', return_value=None):
        bundle = analysis._collect_table_data(
            ('삼성전자', '005930'), '국내 주식 기술적 분석', False, False,
            chart_df=chart_df
        )

    assert mock_cp.call_count == 1
    assert bundle['curr_data'] is single


def test_analyze_table_row_fills_w52_from_chart():
    """멀티시세 응답(_src='multi')은 52주 고저를 차트(250봉)로 보강하는지 확인."""
    chart_df = _mk_chart_df()
    curr_price = str(int(chart_df['close'].iloc[-1]))
    bundle = {
        'curr_data': {'rt_cd': '0', 'output': {
            '_src': 'multi', 'stck_prpr': curr_price, 'stck_sdpr': '69500',
            'prdy_vrss': '500', 'prdy_ctrt': '0.72', 'rprs_mrkt_kor_name': '코스피',
        }},
        'chart_df': chart_df, 'inv_list': None,
        'rt_strength': 110.0, 'ask_bid_ratio': None, 'detail': None,
    }

    with patch('modules.analysis.check_smart_money_turnaround', return_value=(False, "")):
        result = analysis._analyze_table_row(
            ('삼성전자', '005930'), '국내 주식 기술적 분석', False, False,
            set(), {}, {}, set(), set(), bundle
        )

    assert result is not None
    row_data = result[0]
    # 52주 컬럼(인덱스 5)이 보강되어 '-'가 아닌 % 위치로 표시되어야 함
    assert '%' in row_data[5]
    # 보강 값이 output에 주입되었는지 확인
    out = bundle['curr_data']['output']
    assert float(out['w52_hgpr']) > 0
    assert float(out['w52_lwpr']) > 0


def _mk_nxt_response(codes, prpr='71000', vol='99999'):
    """NXT(NX) 멀티시세 원본 응답 mock (NXT 체결가/거래량)"""
    return {
        'rt_cd': '0',
        'output': [
            {'inter_shrn_iscd': c, 'inter2_prpr': prpr, 'acml_vol': vol} for c in codes
        ]
    }


def test_multi_price_nxt_merge_overrides_price(monkeypatch):
    """[근본개선] 장전/장후 NXT 시간: KRX(J)에 NXT(NX) 체결가를 배치 병합해 stck_prpr을 교체한다.
    (종목별 fetch_nxt_price 팬아웃 → EGW00201 stale 폴백 제거)"""
    monkeypatch.setattr(api, '_MULTI_PRICE_NXT_DISABLED', False, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', False, raising=False)

    def fake_call(url, market, category, action, params=None, **kw):
        mkt = params.get('FID_COND_MRKT_DIV_CODE_1')
        codes = [v for k, v in params.items() if k.startswith('FID_INPUT_ISCD_')]
        return _mk_nxt_response(codes) if mkt == 'NX' else _mk_multi_response(codes)

    with patch('api.call_api', side_effect=fake_call):
        res = api.get_multi_current_prices_nxt(['005930'])

    out = res['005930']
    assert out['stck_prpr'] == '71000'      # NXT 체결가로 교체됨
    assert out['acml_vol'] == '99999'       # NXT 거래량으로 교체됨
    assert out['stck_sdpr'] == '69500'      # 기준가(전일종가)는 KRX 값 유지 → 등락률 재계산 근거


def test_multi_price_nxt_missing_keeps_krx(monkeypatch):
    """NXT에 체결가가 없는 종목(nxtSupported=false, prpr 0)은 KRX 값을 그대로 유지한다."""
    monkeypatch.setattr(api, '_MULTI_PRICE_NXT_DISABLED', False, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', False, raising=False)

    def fake_call(url, market, category, action, params=None, **kw):
        mkt = params.get('FID_COND_MRKT_DIV_CODE_1')
        codes = [v for k, v in params.items() if k.startswith('FID_INPUT_ISCD_')]
        if mkt == 'NX':
            return _mk_nxt_response(codes, prpr='0', vol='0')  # NXT 미지원 → 0
        return _mk_multi_response(codes)

    with patch('api.call_api', side_effect=fake_call):
        res = api.get_multi_current_prices_nxt(['003490'])

    assert res['003490']['stck_prpr'] == '70000'  # KRX 값 유지(0으로 덮어쓰지 않음)


def test_multi_price_nxt_failure_falls_back_to_krx(monkeypatch):
    """NXT 배치 실패/미지원 시 KRX 결과만 반환하고 NXT 병합을 세션 내 비활성화한다."""
    monkeypatch.setattr(api, '_MULTI_PRICE_NXT_DISABLED', False, raising=False)
    monkeypatch.setattr(config.session, 'is_simulation', False, raising=False)

    def fake_call(url, market, category, action, params=None, **kw):
        mkt = params.get('FID_COND_MRKT_DIV_CODE_1')
        codes = [v for k, v in params.items() if k.startswith('FID_INPUT_ISCD_')]
        if mkt == 'NX':
            return {'rt_cd': '1', 'msg1': 'NX 미지원'}  # NXT 실패
        return _mk_multi_response(codes)

    with patch('api.call_api', side_effect=fake_call):
        res = api.get_multi_current_prices_nxt(['005930'])

    assert res['005930']['stck_prpr'] == '70000'   # KRX 값 그대로
    assert api._MULTI_PRICE_NXT_DISABLED is True    # 세션 내 NXT 병합 비활성
