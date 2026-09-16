"""KIS 일봉의 '오늘' 봉 종가는 16:00~새벽 배치 사이에 애프터 최종가(임시값)다 — 정규장 종가로 되돌린다.

[실측 2026-09-14~15 삼성전자] 15:30 단일가 249,000 / 저녁 KIS 일봉 248,500 / 다음날 07:13 KIS 일봉·기준가
249,000. 저녁에 받은 봉을 캐시에 굳히면 지표·화면이 밤새 틀린다(어제 화면 249,500 = 16:1x 애프터 체결가).
"""
from unittest.mock import patch

import api
from api import charts


def _minute_rows():
    return {'rt_cd': '0', 'output2': [
        {'stck_bsop_date': '20260914', 'stck_cntg_hour': '153000', 'stck_prpr': '249000'},
        {'stck_bsop_date': '20260914', 'stck_cntg_hour': '151900', 'stck_prpr': '249500'},
    ]}


def test_regular_close_is_the_1530_bar_and_cached():
    charts._KIS_REGULAR_CLOSE_CACHE.clear()
    with patch.object(api, 'call_api', return_value=_minute_rows()) as m:
        assert charts.kis_regular_close('005930', '20260914') == 249000.0
        assert charts.kis_regular_close('005930', '20260914') == 249000.0
    assert m.call_count == 1, "종목·일자당 한 번이면 충분하다"
    assert m.call_args.kwargs.get('tr_id') == 'FHKST03010230'


def test_regular_close_skips_after_market_rows_and_unknown_is_zero():
    charts._KIS_REGULAR_CLOSE_CACHE.clear()
    rows = {'rt_cd': '0', 'output2': [
        {'stck_bsop_date': '20260914', 'stck_cntg_hour': '160500', 'stck_prpr': '249500'},   # 애프터 봉이 섞여 와도
        {'stck_bsop_date': '20260914', 'stck_cntg_hour': '153000', 'stck_prpr': '249000'},
    ]}
    with patch.object(api, 'call_api', return_value=rows):
        assert charts.kis_regular_close('005930', '20260914') == 249000.0
    charts._KIS_REGULAR_CLOSE_CACHE.clear()
    with patch.object(api, 'call_api', return_value={'rt_cd': '1', 'msg1': 'x'}):
        assert charts.kis_regular_close('005930', '20260914') == 0.0
    with patch.object(api, 'call_api', side_effect=RuntimeError('net')):
        assert charts.kis_regular_close('005930', '20260914') == 0.0


def test_provisional_window_is_after_1600_same_day():
    with patch.object(api, '_nxt_quote_phase', lambda: 'krx_after'), \
            patch.object(charts, 'datetime') as dt:
        dt.now.return_value.strftime.return_value = "1630"
        assert charts._kis_daily_close_is_provisional() is True
    with patch.object(api, '_nxt_quote_phase', lambda: 'break'), \
            patch.object(charts, 'datetime') as dt:
        dt.now.return_value.strftime.return_value = "1545"
        assert charts._kis_daily_close_is_provisional() is False, "휴게엔 애프터 체결이 없어 그대로 정규장 종가"
    with patch.object(api, '_nxt_quote_phase', lambda: 'skip'), \
            patch.object(charts, 'datetime') as dt:
        dt.now.return_value.strftime.return_value = "1400"
        assert charts._kis_daily_close_is_provisional() is False


def test_weekly_this_week_bar_close_is_restored_in_provisional_window():
    """[2026-09-16] 주봉의 이번 주 봉 종가 = 오늘 종가라 같은 저녁 임시값 문제가 있다."""
    from datetime import datetime as _dt
    charts._KIS_REGULAR_CLOSE_CACHE.clear()
    now = _dt(2026, 9, 16, 18, 0)               # 수요일 저녁(애프터)
    weekly = {'rt_cd': '0', 'output2': [
        {'stck_bsop_date': '20260914', 'stck_clpr': '251000', 'stck_oprc': '249000', 'stck_hgpr': '255000', 'stck_lwpr': '247000', 'acml_vol': '30000000'},
        {'stck_bsop_date': '20260907', 'stck_clpr': '259500', 'stck_oprc': '260000', 'stck_hgpr': '270000', 'stck_lwpr': '255000', 'acml_vol': '60000000'},
    ]}
    def fake_call(url, *a, **k):
        if 'time-dailychartprice' in url:
            return {'rt_cd': '0', 'output2': [{'stck_bsop_date': '20260916', 'stck_cntg_hour': '153000', 'stck_prpr': '252000'}]}
        return weekly
    class _Now(_dt):
        @classmethod
        def now(cls, tz=None): return now
    with patch.object(api, 'call_api', side_effect=fake_call), \
            patch.object(charts, 'datetime', _Now), \
            patch.object(api, '_nxt_quote_phase', lambda: 'krx_after'):
        df = charts._fetch_kis_weekly_domestic('005930')
    assert float(df.iloc[-1]['close']) == 252000.0, "이번 주 봉은 오늘 15:30 종가로"
    assert float(df.iloc[-2]['close']) == 259500.0, "지난주 봉은 그대로"
