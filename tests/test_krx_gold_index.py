"""KRX 금현물(금 99.99_1Kg, 원/g) 지수 회귀 테스트.

[왜 묻는가] KRX 금시장 시세는 KIS·토스·yfinance·pykrx 어디에도 없어 네이버 원자재
API 전용 경로를 새로 뚫었다. 이 경로는 다른 지수와 두 가지가 다르다.
  1) 야후에 티커가 없다(^KRXGOLD는 자리표시자) → yfinance로 새어 나가면 그룹 전체
     다운로드가 404를 물고 온다. 폴백도 없으므로 실패는 '네이버' 실패로 안내해야 한다.
  2) 네이버 일별 시세는 종가만 준다(시·고·저가 0) → 종가로 평탄화한 봉을 만들지 않으면
     지표가 0가로 계산돼 통째로 망가진다.
캐시(현재가 60초 / 시계열 6시간)와 음성 캐시도 함께 검증한다 — 지수 화면은 반복(@)
조회가 기본이라 캐시가 풀리면 매 렌더마다 5페이지를 다시 받는다.
"""
import os
import sys
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
import config
from modules import analysis, market


GOLD = market.KRX_GOLD_INDEX


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _prices_payload(page, page_size, total_pages=1, base=200000.0):
    """네이버 일별 시세 응답 — 최근 날짜부터 내려오며 시·고·저는 0으로 온다.

    마지막 페이지는 실제 응답과 같이 상한(60)보다 짧게 준다(페이징 종료 조건).
    """
    if page > total_pages:
        return []
    rows = []
    start = (page - 1) * page_size
    count = page_size if page < total_pages else page_size // 2
    for i in range(count):
        idx = start + i
        day = pd.Timestamp("2026-08-21") - pd.Timedelta(days=idx)
        rows.append({
            'localTradedAt': f"{day.strftime('%Y-%m-%d')}T00:00:00+09:00",
            'closePrice': f"{base - idx * 100:,.0f}",
            'openPrice': '0', 'highPrice': '0', 'lowPrice': '0',
        })
    return rows


def _quote_payload(close="203,410", traded_at="2026-08-21T15:19:58+09:00"):
    return {'closePrice': close, 'localTradedAt': traded_at, 'unit': '원/g'}


def _fake_get(quote=None, total_pages=5, page_size=None, prices=None):
    """analysis.requests.get 대역 — 호출 URL로 시계열/현재가를 구분한다."""
    size = page_size or analysis._KRX_GOLD_PAGE_SIZE

    def _get(url, params=None, headers=None, timeout=None):
        if url.endswith('/prices'):
            if prices is not None:
                return _Resp(prices)
            return _Resp(_prices_payload((params or {}).get('page', 1), size, total_pages))
        return _Resp(_quote_payload() if quote is None else quote)

    return _get


@pytest.fixture(autouse=True)
def _clear_gold_cache():
    # 아래 대부분은 **네이버 폴백 경로**를 검증한다. 개발자가 KRX_ID를 셸에 걸어 둔 채
    #  돌리면 1순위(KRX)로 새어 결과가 갈리므로, 여기서 자격증명을 비워 경로를 고정한다.
    #  KRX 경로 자체는 아래 'KRX 공식' 절에서 _krx_gold_official을 직접 목으로 잡아 검증한다.
    analysis._KRX_GOLD_CACHE.clear()
    with patch.dict(os.environ, {"KRX_ID": "", "KRX_PW": ""}):
        yield
    analysis._KRX_GOLD_CACHE.clear()


# ---------------------------------------------------------------- 소스(네이버) 조회

def test_시계열은_종가로_평탄화된_OHLC로_돌아온다():
    with patch.object(analysis.requests, 'get', side_effect=_fake_get()):
        df = analysis.get_krx_gold_data()

    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    assert df.attrs['source'] == 'NAVER'
    # 시·고·저가 0으로 남으면 지표가 통째로 망가진다 → 종가와 같아야 한다
    for col in ('open', 'high', 'low'):
        assert (df[col] == df['close']).all()
    # 거래량 이력은 제공되지 않는다(OBV는 화면에서 '-'로 죽는다)
    assert (df['volume'] == 0).all()
    assert df['date'].is_monotonic_increasing
    assert len(df) == analysis._KRX_GOLD_PAGE_SIZE * 4 + analysis._KRX_GOLD_PAGE_SIZE // 2


def test_현재가가_마지막_봉을_덮어쓴다():
    """장중 현재가는 그날 종가 자리에 들어가야 한다(같은 날짜 봉이 둘로 갈라지면 안 된다)."""
    with patch.object(analysis.requests, 'get',
                      side_effect=_fake_get(quote=_quote_payload(close="210,000"))):
        df = analysis.get_krx_gold_data()

    assert float(df['close'].iloc[-1]) == 210000.0
    assert df['date'].iloc[-1] == pd.Timestamp("2026-08-21")
    assert df['date'].duplicated().sum() == 0


def test_현재가만_실패해도_시계열로_표시한다():
    """부분 실패는 화면을 비우지 않는다 — 마지막 종가로라도 행을 채운다."""
    def _get(url, params=None, headers=None, timeout=None):
        if url.endswith('/prices'):
            return _Resp(_prices_payload((params or {}).get('page', 1),
                                         analysis._KRX_GOLD_PAGE_SIZE, 5))
        raise RuntimeError("네이버 현재가 장애")

    with patch.object(analysis.requests, 'get', side_effect=_get):
        df = analysis.get_krx_gold_data()

    assert df is not None and not df.empty
    assert float(df['close'].iloc[-1]) == 200000.0


def test_마지막_페이지에서_페이징을_멈춘다():
    calls = []

    def _get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if url.endswith('/prices'):
            return _Resp(_prices_payload((params or {}).get('page', 1),
                                         analysis._KRX_GOLD_PAGE_SIZE, total_pages=2))
        return _Resp(_quote_payload())

    with patch.object(analysis.requests, 'get', side_effect=_get):
        df = analysis.get_krx_gold_data()

    # 2페이지째가 상한(60)보다 짧아지는 순간 멈춘다 → 3페이지 이상 요청하지 않는다
    assert sum(1 for u in calls if u.endswith('/prices')) == 2
    assert len(df) == analysis._KRX_GOLD_PAGE_SIZE + analysis._KRX_GOLD_PAGE_SIZE // 2


def test_시계열_캐시는_반복조회에서_재요청하지_않는다():
    """지수 화면은 반복(@) 조회가 기본 — 60초 안의 재조회는 HTTP가 나가면 안 된다."""
    with patch.object(analysis.requests, 'get', side_effect=_fake_get()) as m:
        analysis.get_krx_gold_data()
        first = m.call_count
        analysis.get_krx_gold_data()
        assert m.call_count == first


def test_현재가_TTL이_지나면_현재가만_다시_받는다():
    with patch.object(analysis.requests, 'get', side_effect=_fake_get()) as m:
        analysis.get_krx_gold_data()
        first = m.call_count

        ent = analysis._KRX_GOLD_CACHE[config.KRX_GOLD_SYMBOL]
        ent["quote_time"] = datetime.now() - pd.Timedelta(
            seconds=analysis._KRX_GOLD_QUOTE_TTL_SEC + 1).to_pytimedelta()
        analysis.get_krx_gold_data()

    # 시계열(5페이지)은 그대로 두고 현재가 1콜만 더 나간다
    assert m.call_count == first + 1


def test_시계열_실패는_음성캐시로_재시도를_묶는다():
    def _boom(url, params=None, headers=None, timeout=None):
        raise RuntimeError("네이버 장애")

    with patch.object(analysis.requests, 'get', side_effect=_boom) as m:
        assert analysis.get_krx_gold_data() is None
        blocked = m.call_count
        assert analysis.get_krx_gold_data() is None
        assert m.call_count == blocked          # 음성 캐시 구간엔 재요청하지 않는다

        analysis.reset_krx_gold_failures()      # 사용자가 '재시도(y)'를 고른 상황
        assert analysis.get_krx_gold_data() is None
        assert m.call_count > blocked


def test_차단된_응답도_예외없이_실패로_흐른다():
    """빈 dict/list(테스트 격리·네이버 스펙 변경)를 파서가 예외로 만들면 안 된다."""
    with patch.object(analysis.requests, 'get', side_effect=_fake_get(quote={}, prices={})):
        assert analysis.get_krx_gold_data() is None


# ---------------------------------------------------------------- 지수 화면(메뉴 1)

def _gold_df(periods=300, base=200000.0):
    dates = pd.date_range(end=pd.Timestamp("2026-08-21"), periods=periods)
    close = [base + i * 50 for i in range(periods)]
    out = pd.DataFrame({'date': dates, 'open': close, 'high': close,
                        'low': close, 'close': close, 'volume': [0.0] * periods})
    out.attrs['source'] = 'NAVER'
    return out


def test_지수_행은_정수로_표시되고_OBV는_죽는다():
    with patch.object(analysis, 'get_krx_gold_data', return_value=_gold_df()), \
         patch.object(market, 'is_market_open_for_index', return_value=False):
        res = market._process_index_worker(GOLD, "^KRXGOLD", pd.DataFrame(), pd.DataFrame())

    assert res['status'] == 'success'
    name_cell, curr_cell, change_cell, high52_cell = res['row_data'][:4]
    assert GOLD in name_cell
    assert "214,950" in curr_cell             # 마지막 종가 = 200000 + 299*50
    assert "원" not in curr_cell              # 지수 표는 단위 없이 숫자만 쓴다
    assert "+50 " in change_cell and "(+0.02%)" in change_cell
    assert "214,950" in high52_cell            # 52주 고점도 종가 기준
    assert res['row_data'][-1] == "[dim]-[/dim]"   # 거래량 이력이 없어 OBV는 '-'
    # 종가만 있어도 추세 지표는 산출된다(고·저 대신 종가 차분이 True Range가 된다)
    assert "-" not in res['row_data'][10]      # RSI


def test_네이버_실패는_야후로_새지_않고_네이버_실패로_알린다():
    with patch.object(analysis, 'get_krx_gold_data', return_value=None), \
         patch.object(api, 'get_yf_fast_info') as fast_info:
        res = market._process_index_worker(GOLD, "^KRXGOLD", pd.DataFrame(), pd.DataFrame())

    assert res == {'status': 'failed', 'name': GOLD, 'src': '네이버'}
    fast_info.assert_not_called()


def test_지수화면은_자리표시자_티커를_야후에_묻지_않는다():
    with patch.object(analysis, 'get_krx_gold_data', return_value=_gold_df()), \
         patch.object(api, 'fetch_yfinance_data') as yf_dl, \
         patch.object(api, 'get_yf_fast_info') as fast_info, \
         patch.object(market, 'is_market_open_for_index', return_value=False):
        failed = market._show_market_indices_core(target_indices=[GOLD])

    assert failed == []
    yf_dl.assert_not_called()
    fast_info.assert_not_called()


def test_텔레그램_시세도_같은_소스를_쓴다():
    with patch.object(analysis, 'get_krx_gold_data', return_value=_gold_df()) as m:
        name, current, prev = market.fetch_index_quote(GOLD, "^KRXGOLD")

    m.assert_called_once()
    assert name == GOLD
    assert (current, prev) == (214950.0, 214900.0)


# ---------------------------------------------------------------- 목록·개폐·차트 라우팅

def test_지수_목록과_그룹이_정합적이다():
    assert market.INDICES_MAP[GOLD] == "^KRXGOLD"
    # 국내 상품이라 국내 지수 그룹, 코스닥150 바로 뒤에 온다
    assert config.INDICES_GROUPS["1"]["indices"][-1] == GOLD
    names = [n for n, _ in market.ALL_INDICES]
    assert names[names.index("코스닥150") + 1] == GOLD
    # 모드 제한이 없는 지수다(KIS 실전 전용 목록과 무관) → 토스·모의에서도 보인다
    config.session.is_toss = True
    assert GOLD in [n for n, _ in market.selectable_indices()]


@pytest.mark.parametrize("hhmm,holiday,expected", [
    ("09:30", False, True),
    ("15:29", False, True),
    ("08:59", False, False),
    ("15:31", False, False),
    ("09:30", True, False),     # KRX 금시장 휴장일 = 주식 정규장과 같다
])
def test_개장_표시는_KRX_정규장_시간을_따른다(hhmm, holiday, expected):
    now = datetime.strptime(f"2026-08-21 {hhmm}", "%Y-%m-%d %H:%M")
    with patch.object(market, 'datetime') as dt, \
         patch.object(api, 'is_holiday_today', return_value=holiday):
        dt.now.return_value = now
        assert market.is_market_open_for_index(GOLD) is expected
    assert market._index_session_group(GOLD) == "KRX 정규장"


def test_차트_분석도_지수화면과_같은_소스를_탄다():
    assert api.index_source_kind("^KRXGOLD") == 'krx_gold'
    with patch.object(analysis, 'get_krx_gold_data', return_value=_gold_df()) as m:
        df = api.get_chart_data("^KRXGOLD", is_overseas=True, period_type='daily')

    m.assert_called_once_with(config.KRX_GOLD_SYMBOL)
    assert not df.empty
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']


def test_티커맵은_지수목록과_정합적이다():
    """맵이 어긋나면 차트만 다른 상품(미니금 등)을 그린다."""
    for ticker, symbol in config.KRX_GOLD_TICKERS.items():
        assert market.INDICES_MAP[GOLD] == ticker
        assert symbol == config.KRX_GOLD_SYMBOL


# ---------------------------------------------------------------- 종목 자리에 넣기([9]-5)

@pytest.mark.parametrize("raw", ["KRXGOLD", "krxgold", "^KRXGOLD", "금현물", "KRX 금현물"])
def test_티커_별칭은_금현물로_해석된다(raw):
    assert market.resolve_index_product(raw) == ("^KRXGOLD", GOLD, False)


@pytest.mark.parametrize("raw", ["005930", "AAPL", "", None])
def test_일반_종목은_지수상품이_아니다(raw):
    assert market.resolve_index_product(raw) is None


def test_현재가는_전용소스에서_오고_증권사를_부르지_않는다():
    """'종목 자리'로 들어와도 증권사 시세 TR을 타면 안 된다(코드 자체가 없다)."""
    with patch.object(analysis, 'get_krx_gold_data', return_value=_gold_df()), \
         patch.object(api, 'get_current_price_data') as kis:
        price = api.get_current_price("^KRXGOLD", False)

    assert price == 214950.0
    kis.assert_not_called()


def _direct_input(raw, allow):
    """직접 입력(5) 경로로 _select_stock_for_rules를 태운다."""
    from modules.auto_trade import menu as at_menu

    with patch.object(at_menu.utils, 'show_menu', return_value='5'), \
         patch.object(at_menu.utils, 'print_breadcrumb'), \
         patch.object(at_menu.Prompt, 'ask', side_effect=[raw, 'y']), \
         patch.object(at_menu.utils, 'validate_and_confirm_stock', return_value=True) as validate, \
         patch.object(api, 'get_current_price', return_value=203410.0), \
         patch.object(api, 'get_stock_name_by_code', return_value=None):
        return at_menu._select_stock_for_rules(allow_index_products=allow), validate


def test_포지션분석_직접입력은_KRXGOLD를_받는다():
    (code, name, is_overseas), validate = _direct_input("KRXGOLD", allow=True)

    assert (code, name, is_overseas) == ("^KRXGOLD", GOLD, False)
    # 증권사 종목 검증 TR은 이 코드에 쓸 수 없다 → 전용 소스 현재가로 확인한다
    validate.assert_not_called()


def test_자동매매_룰_경로에는_지수상품을_열지_않는다():
    """주문을 낼 수 없는 대상이라 룰이 성립하지 않는다 → 종전대로 해외 티커 취급."""
    (code, name, is_overseas), validate = _direct_input("KRXGOLD", allow=False)

    assert (code, is_overseas) == ("KRXGOLD", True)
    validate.assert_called_once()


def test_포지션분석은_시장구분_조회를_건너뛴다():
    """KOSPI/KOSDAQ 구분이 없는 상품이라 조회하면 오류 로그와 TPS만 태운다."""
    from modules.auto_trade import common, engine

    entry = {'code': "^KRXGOLD", 'name': GOLD, 'buy_price': 190000.0,
             'current_price': 203410.0, 'profit_rate': 7.06, 'is_overseas': False,
             'qty': 100, 'holding_days': 30}
    with patch.object(common, 'resolve_market_type') as m_type, \
         patch.object(analysis, 'get_krx_gold_data', return_value=_gold_df()):
        res = engine.analyze_holdings([entry])

    m_type.assert_not_called()
    assert res.get("^KRXGOLD", {}).get('action')


# ---------------------------------------------------------------- KRX 공식(1순위)
#  2026-08-25: data.krx.co.kr 로그인이 생기면서 금현물도 실제 OHLC·거래량을 받게 됐다.
#  네이버는 폴백으로 남는다(위 절이 그 경로를 계속 지킨다).

def _official_frame():
    """krx_data.get_gold_daily 가 주는 모양 — date는 'YYYYMMDD' 문자열이다."""
    return pd.DataFrame([
        {"date": "20260820", "open": 201420.0, "high": 202060.0, "low": 200850.0,
         "close": 201620.0, "volume": 262774.0},
        {"date": "20260821", "open": 202390.0, "high": 203410.0, "low": 201170.0,
         "close": 203410.0, "volume": 216327.0},
    ])


def test_KRX가_1순위이고_네이버_시계열을_부르지_않는다():
    """네이버 한계(종가만·거래량 0)를 피하는 것이 이 경로의 존재 이유다."""
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_history') as hist, \
         patch.object(analysis, '_fetch_krx_gold_quote', return_value=None):
        df = analysis.get_krx_gold_data()
    hist.assert_not_called()
    assert df.attrs['source'] == 'KRX'
    assert (df['high'] > df['low']).all()          # 평탄화가 아니다
    assert (df['volume'] > 0).all()                # OBV가 산다


def test_KRX_실패시_네이버로_폴백한다():
    with patch.object(analysis, '_krx_gold_official', return_value=None), \
         patch.object(analysis, '_fetch_krx_gold_history',
                      return_value=[(pd.to_datetime('2026-08-21'), 203410.0)]), \
         patch.object(analysis, '_fetch_krx_gold_quote', return_value=None):
        df = analysis.get_krx_gold_data()
    assert df.attrs['source'] == 'NAVER'
    assert len(df) == 1


def test_장중_현재가는_확정봉_위에_덧대진다():
    """KRX는 마감 후 확정 봉만 준다 — 오늘 봉이 없으면 현재가로 새 봉을 만든다."""
    quote = {'date': pd.to_datetime('2026-08-24'), 'close': 207490.0}
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote', return_value=quote):
        df = analysis.get_krx_gold_data()
    assert len(df) == 3
    assert df['close'].iloc[-1] == 207490.0
    assert df['date'].iloc[-1] == pd.to_datetime('2026-08-24')


def test_현재가가_봉_밖으로_나가면_고저를_넓힌다():
    """종가가 [저,고] 밖에 있으면 True Range가 음수가 되어 ATR·SAR이 망가진다."""
    quote = {'date': pd.to_datetime('2026-08-21'), 'close': 209000.0}   # 그날 고가(203,410) 위
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote', return_value=quote):
        df = analysis.get_krx_gold_data()
    last = df.iloc[-1]
    assert last['close'] == 209000.0
    assert last['high'] >= last['close'] and last['low'] <= last['close']


def test_현재가_장애는_확정봉_표시를_막지_않는다():
    """네이버가 죽어도 KRX 확정 봉만으로 표를 채울 수 있어야 한다."""
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote', side_effect=RuntimeError('네이버 장애')):
        df = analysis.get_krx_gold_data()
    assert df is not None and len(df) == 2
    assert df.attrs['source'] == 'KRX'


def test_현재가_장애는_음성캐시로_묶이고_시계열과_섞이지_않는다():
    """현재가 실패가 시계열(fail) 마커를 건드리면 시계열 재시도까지 막힌다."""
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote',
                      side_effect=RuntimeError('네이버 장애')) as q:
        analysis.get_krx_gold_data()
        first = q.call_count
        analysis.get_krx_gold_data()
        assert q.call_count == first            # 장애 구간엔 재요청하지 않는다
    ent = analysis._krx_gold_entry(config.KRX_GOLD_SYMBOL)
    assert ent['quote_fail'] is not None
    assert ent['fail'] is None                  # 시계열 음성 캐시는 건드리지 않았다
