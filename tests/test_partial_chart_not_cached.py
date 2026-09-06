"""반쪽 차트를 캐시에 굳히지 않는가 (감사 2026-09-06, 배치 61).

[무엇이 걸려 있는가] 국내·해외 일봉은 250봉을 채울 때까지 페이지네이션으로 받는다.
그 도중 조회가 **실패**하면 종전에는 모아 둔 만큼만 돌려줬다. 그 프레임은 빈 프레임과
달리 '정상'으로 보인다 — 호출부의 검사는 `df is None or df.empty` 이므로 그대로 통과한다.
그리고 _get_cached_chart 가 그것을 **6시간 메모리 + 디스크**에 굳힌다.

봉이 모자라면 EMA120·52주 밴드가 통째로 어긋나고, 그 위에 선 점수·상태·진입 판정이
조용히 틀어진다. 빈 차트는 판정 게이트가 막아 주지만(state='-'), 반쪽 차트는 막지 못한다.

'더 받을 과거 봉이 없다'(정상 종료 — 신규 상장·짧은 이력)와 '못 받았다'(실패)를 가른다.
이번 호출에는 있는 대로 돌려주되(있는 것이 없는 것보다 낫다) 굳히지는 않는다.
"""
from unittest.mock import patch

import pytest

import api
import config
from api import charts, chart_cache


def _page(dates):
    return {'rt_cd': '0', 'output2': [
        {'stck_bsop_date': d, 'stck_clpr': '100', 'stck_oprc': '100',
         'stck_hgpr': '100', 'stck_lwpr': '100', 'acml_vol': '1000'} for d in dates]}


_FAIL = {'rt_cd': '1', 'msg_cd': 'OPSQ0001', 'msg1': '조회 실패'}


@pytest.fixture
def kis(monkeypatch):
    monkeypatch.setattr(config.session, 'is_toss', False, raising=False)
    chart_cache._CHART_CACHE.clear()
    yield
    chart_cache._CHART_CACHE.clear()


def _run(kis_pages, monkeypatch):
    seq = list(kis_pages)
    state = {'i': 0}

    def _call(*a, **k):
        i = min(state['i'], len(seq) - 1)
        state['i'] += 1
        return seq[i]

    with patch.object(api, 'call_api', side_effect=_call), \
         patch.object(chart_cache, '_chart_disk_get', return_value=None), \
         patch.object(chart_cache, '_chart_disk_set') as disk_set, \
         patch.object(api, 'is_holiday_today', return_value=False):
        df = charts.get_chart_data('005930', is_overseas=False, realtime=False)
    return df, disk_set


def test_페이지_도중_실패한_차트는_캐시에_굳지_않는다(kis, monkeypatch):
    df, disk_set = _run([_page(['20260904', '20260903', '20260902']), _FAIL], monkeypatch)

    assert len(df) == 3, "받은 만큼은 돌려준다(있는 것이 없는 것보다 낫다)"
    assert df.attrs.get('partial') is True, "반쪽이라는 표시가 없다"
    assert not chart_cache._CHART_CACHE, "반쪽 차트가 메모리 캐시에 들어갔다"
    assert disk_set.call_count == 0, "반쪽 차트가 디스크 캐시에 들어갔다"


def test_이력이_짧아_끝난_차트는_정상이므로_캐시한다(kis, monkeypatch):
    """신규 상장·짧은 이력은 실패가 아니다 — 매번 다시 받으면 순수 낭비다."""
    # 같은 페이지를 반복 응답 = 더 받을 과거 봉이 없다(정상 종료)
    df, disk_set = _run([_page(['20260904', '20260903', '20260902'])] * 3, monkeypatch)

    assert len(df) == 3
    assert not df.attrs.get('partial')
    assert chart_cache._CHART_CACHE, "정상 종료한 차트를 캐시하지 않았다"
    assert disk_set.call_count == 1


def test_첫_페이지부터_실패하면_빈_프레임이다(kis, monkeypatch):
    """받은 것이 하나도 없으면 종전대로 빈 프레임 — 호출부가 '판정 불가'로 다룬다."""
    df, disk_set = _run([_FAIL], monkeypatch)

    assert df is not None and df.empty
    assert not chart_cache._CHART_CACHE
    assert disk_set.call_count == 0
