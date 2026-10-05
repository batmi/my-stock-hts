"""KRX 금현물(금 99.99_1Kg, 원/g) 지수 회귀 테스트.

[왜 묻는가] KRX 금시장 시세는 KIS·토스·yfinance 어디에도 없어 전용 경로를 쓴다.
  · 이력 = KRX Open API 확정 봉(krx_data.get_gold_daily), 장중 현재가 = 네이버 원자재 API.
  · 야후에 티커가 없다(^KRXGOLD는 자리표시자) → yfinance로 새어 나가면 그룹 전체
    다운로드가 404를 물고 온다. 폴백이 없으므로 실패는 그대로 실패로 알린다.
  · [2026-10-05] 네이버 일별 시세(종가만·시고저 0) 폴백은 제거됐다 — 확정 봉이 없으면 None.
현재가 캐시(60초)와 음성 캐시(180초)도 함께 검증한다 — 지수 화면은 반복(@) 조회가 기본이다.
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


@pytest.fixture(autouse=True)
def _clear_gold_cache():
    analysis._KRX_GOLD_CACHE.clear()
    yield
    analysis._KRX_GOLD_CACHE.clear()


def _official_frame():
    """krx_data.get_gold_daily 가 주는 모양 — date는 'YYYYMMDD' 문자열이다."""
    return pd.DataFrame([
        {"date": "20260820", "open": 201420.0, "high": 202060.0, "low": 200850.0,
         "close": 201620.0, "volume": 262774.0},
        {"date": "20260821", "open": 202390.0, "high": 203410.0, "low": 201170.0,
         "close": 203410.0, "volume": 216327.0},
    ])


# ---------------------------------------------------------------- 소스 조회

def test_확정봉이_없으면_None이고_네이버_시계열로_대신하지_않는다():
    """종가만 주는 네이버 시계열로 평탄화한 봉을 만들면 ATR·ADX 가 왜곡된다 — 실패로 둔다."""
    with patch.object(analysis, '_krx_gold_official', return_value=None), \
         patch.object(analysis.requests, 'get') as http:
        assert analysis.get_krx_gold_data() is None
    http.assert_not_called()                     # 현재가 한 점도 받지 않는다(쓸 데가 없다)


def test_KRX_확정봉은_실제_OHLC와_거래량이다():
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote', return_value=None):
        df = analysis.get_krx_gold_data()
    assert list(df.columns) == ['date', 'open', 'high', 'low', 'close', 'volume']
    assert df.attrs['source'] == 'KRX'
    assert (df['high'] > df['low']).all()          # 평탄화가 아니다
    assert (df['volume'] > 0).all()                # OBV가 산다
    assert df['date'].is_monotonic_increasing


def test_현재가_TTL_안에서는_다시_받지_않는다():
    """지수 화면은 반복(@) 조회가 기본 — 60초 안의 재조회는 HTTP가 나가면 안 된다."""
    quote = {'date': pd.to_datetime('2026-08-21'), 'close': 203500.0}
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote', return_value=quote) as q:
        analysis.get_krx_gold_data()
        analysis.get_krx_gold_data()
        assert q.call_count == 1

        ent = analysis._KRX_GOLD_CACHE[config.KRX_GOLD_SYMBOL]
        ent["quote_time"] = datetime.now() - pd.Timedelta(
            seconds=analysis._KRX_GOLD_QUOTE_TTL_SEC + 1).to_pytimedelta()
        analysis.get_krx_gold_data()
        assert q.call_count == 2


def test_현재가_장애_재시도는_사용자가_풀_수_있다():
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote', side_effect=RuntimeError('네이버 장애')) as q:
        analysis.get_krx_gold_data()
        blocked = q.call_count
        analysis.get_krx_gold_data()
        assert q.call_count == blocked          # 음성 캐시 구간엔 재요청하지 않는다
        analysis.reset_krx_gold_failures()      # 사용자가 '재시도(y)'를 고른 상황
        analysis.get_krx_gold_data()
        assert q.call_count > blocked


def test_차단된_현재가_응답도_예외없이_흐른다():
    """빈 dict(테스트 격리·네이버 스펙 변경)를 파서가 예외로 만들면 안 된다."""
    class _Resp:
        def json(self):
            return {}
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis.requests, 'get', return_value=_Resp()):
        df = analysis.get_krx_gold_data()
    assert df is not None and len(df) == 2


# ---------------------------------------------------------------- 지수 화면(메뉴 1)

def _gold_df(periods=300, base=200000.0):
    dates = pd.date_range(end=pd.Timestamp("2026-08-21"), periods=periods)
    close = [base + i * 50 for i in range(periods)]
    out = pd.DataFrame({'date': dates, 'open': close, 'high': close,
                        'low': close, 'close': close, 'volume': [0.0] * periods})
    out.attrs['source'] = 'KRX'
    return out


def test_지수_행은_정수로_표시되고_OBV는_죽는다():
    with patch.object(analysis, 'get_krx_gold_data', return_value=_gold_df()), \
         patch.object(market, 'is_market_open_for_index', return_value=False):
        res = market._process_index_worker(GOLD, "^KRXGOLD", pd.DataFrame(), pd.DataFrame())

    assert res['status'] == 'success'
    name_cell, curr_cell, change_cell, high52_cell = res['row_data'][:4]
    #  등락 셀은 값 경계(utils.CELL_PART_SEP)로 나뉜 채 만들어지고, 표에 넣기 직전에
    #  폭이 맞춰진다 — 화면에 찍히는 모양으로 되돌려 본다.
    from rich.markup import render

    from core import utils as _u
    change_cell = render(change_cell.replace(_u.CELL_PART_SEP, " ")).plain
    assert GOLD in name_cell
    assert "214,950" in curr_cell             # 마지막 종가 = 200000 + 299*50
    assert "원" not in curr_cell              # 지수 표는 단위 없이 숫자만 쓴다
    assert "+50 " in change_cell and "(+0.02%)" in change_cell
    assert "214,950" in high52_cell            # 52주 고점도 종가 기준
    assert res['row_data'][-1] == "[dim]-[/dim]"   # 거래량 이력이 없어 OBV는 '-'
    # 종가만 있어도 추세 지표는 산출된다(고·저 대신 종가 차분이 True Range가 된다)
    assert "-" not in res['row_data'][10]      # RSI


def test_금현물_실패는_야후로_새지_않고_실패로_알린다():
    with patch.object(analysis, 'get_krx_gold_data', return_value=None), \
         patch.object(api, 'get_yf_fast_info') as fast_info:
        res = market._process_index_worker(GOLD, "^KRXGOLD", pd.DataFrame(), pd.DataFrame())

    assert res == {'status': 'failed', 'name': GOLD, 'src': 'KRX'}
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


# ---------------------------------------------------------------- 확정 봉 + 장중 현재가

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


def test_현재가_장애는_음성캐시로_묶인다():
    """현재가 장애 중 매 렌더마다 네이버를 다시 두드리지 않는다."""
    with patch.object(analysis, '_krx_gold_official', return_value=_official_frame()), \
         patch.object(analysis, '_fetch_krx_gold_quote',
                      side_effect=RuntimeError('네이버 장애')) as q:
        analysis.get_krx_gold_data()
        first = q.call_count
        analysis.get_krx_gold_data()
        assert q.call_count == first            # 장애 구간엔 재요청하지 않는다
    ent = analysis._krx_gold_entry(config.KRX_GOLD_SYMBOL)
    assert ent['quote_fail'] is not None
