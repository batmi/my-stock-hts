"""관심종목에서 뺀 보유 종목도 계속 청산 감시가 되는가.

[왜 묻는가] 매수 후보는 stock.json(관심목록)에서 뽑는다. 운영 중 종목을 관심목록에서
빼는 것은 흔한 일인데, 그때 **이미 들고 있는 포지션**의 손절·트레일링까지 함께 멈추면
그 포지션은 무방비가 된다. 잔고에는 남아 있으므로 아무도 눈치채지 못한다.

조사 결과 현재 구조는 안전하다 — 매도 경로는 잔고(API)를 돌고, 시장구분은 stock.json
미스 시 API로 폴백하며, 개별 룰은 DB, 일봉은 종목코드로 조회한다. ETF 판정도 관심목록이
아니라 종목명 휴리스틱을 함께 쓴다(api.is_domestic_etf_etn).

이 파일은 결함을 고치는 것이 아니라 **그 불변식을 못박는다**. 나중에 매도 경로에
관심목록 필터가 끼어들면 여기서 걸린다.
"""
import pytest
from unittest.mock import patch

import config
from modules.auto_trade import AutoTrader

OFF_CODE = "999999"      # 관심목록에 없는 보유 종목
OFF_NAME = "관심목록외종목"


@pytest.fixture
def trader():
    AutoTrader._instance = None
    t = AutoTrader()
    t.is_running = True
    t.buy_halted = False
    t.no_sellable_streak = {}
    yield t


def _holding(code=OFF_CODE, name=OFF_NAME):
    return {'pdno': code, 'prdt_name': name, 'hldg_qty': '10', 'ord_psbl_qty': '10',
            'pchs_avg_pric': '10000', 'prpr': '9000', 'evlu_pfls_amt': '-10000',
            'evlu_pfls_rt': '-10.0', 'evlu_amt': '90000', 'pchs_amt': '100000'}


def test_sell_analysis_runs_for_an_off_watchlist_holding(trader):
    """[핵심 불변식] 관심목록에 없어도 매도 분석까지 도달해야 한다."""
    # 관심목록을 다른 종목으로만 채운다 — 보유 종목은 거기 없다.
    watchlist = {'stocks_kr': [{'code': '005930', 'name': '삼성전자'}], 'etfs_kr': []}
    assert all(s['code'] != OFF_CODE for s in watchlist['stocks_kr']), "전제: 관심목록 밖이다"

    with patch.object(config.session, 'stock_data', watchlist), \
         patch.object(config.session, 'is_simulation', True), \
         patch.object(trader, 'is_market_open', return_value=True), \
         patch('modules.auto_trade.api.is_domestic_etf_etn', return_value=False), \
         patch('modules.auto_trade.api.get_current_price', return_value=9000), \
         patch('modules.auto_trade.api.get_chart_data', return_value=None), \
         patch('modules.auto_trade.db_manager.db.get_latest_buy_trades', return_value={}), \
         patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[]), \
         patch.object(trader.strategy, 'analyze_sell',
                      return_value={'decision': '보유', 'reason': '', 'state': '보유'}) as analyze:
        trader._check_sell_conditions([_holding()], is_market_open=True,
                                      rules_map={}, restricted_stocks=set())

    assert analyze.called, (
        "관심목록에 없다는 이유로 매도 분석이 건너뛰어졌다 — 그 포지션은 손절 무방비다")
    assert analyze.call_args[0][0] == OFF_CODE


def test_realtime_feed_keeps_covering_held_codes(trader):
    """실시간 시세 등록도 보유 종목을 최우선으로 유지해야 한다.

    빠지면 현재가가 REST 폴백으로 밀려 판정이 느려지고, 급락 시 대응이 늦는다.
    """
    watchlist = {'stocks_kr': [{'code': '005930', 'name': '삼성전자'}], 'etfs_kr': []}
    captured = {}

    def _update(priority, other):
        captured['priority'] = list(priority)

    with patch.object(config.session, 'stock_data', watchlist), \
         patch.object(config.session, 'is_simulation', True), \
         patch.object(trader, 'is_market_open', return_value=True), \
         patch('realtime.update_symbols', side_effect=_update), \
         patch('realtime.coverage', return_value=None), \
         patch('modules.auto_trade.api.is_domestic_etf_etn', return_value=False), \
         patch('modules.auto_trade.api.get_current_price', return_value=9000), \
         patch('modules.auto_trade.api.get_chart_data', return_value=None), \
         patch('modules.auto_trade.db_manager.db.get_latest_buy_trades', return_value={}), \
         patch('modules.auto_trade.db_manager.db.get_all_stock_strategies', return_value=[]), \
         patch.object(trader.strategy, 'analyze_sell',
                      return_value={'decision': '보유', 'reason': '', 'state': '보유'}):
        trader._check_sell_conditions([_holding()], is_market_open=True,
                                      rules_map={}, restricted_stocks=set())

    assert OFF_CODE in captured.get('priority', []), \
        "보유 종목이 실시간 우선 등록에서 빠졌다"


def test_market_type_falls_back_to_the_api_when_off_watchlist(trader):
    """시장구분은 stock.json 미스 시 API로 폴백해야 한다.

    폴백이 없으면 전부 KOSPI 로 취급되어 시장별 리스크 배수가 엉뚱하게 적용된다.
    """
    from modules.auto_trade import common
    cache = {}
    with patch.object(config.session, 'stock_data', {'stocks_kr': [], 'etfs_kr': []}), \
         patch('modules.auto_trade.common.api.get_current_price_data',
               return_value={'rt_cd': '0', 'output': {'rprs_mrkt_kor_name': '코스닥'}}):
        assert common.resolve_market_type(OFF_CODE, cache) == "KOSDAQ"


def test_sell_path_does_not_consult_the_watchlist_for_eligibility(trader):
    """매도 자격 판정에 관심목록을 쓰지 않는다 — 소스로 고정한다.

    ETF 판정은 종목명 휴리스틱(api.is_domestic_etf_etn)을 쓰므로 관심목록 조회가
    아니어야 한다. 여기에 stock_data 기반 필터가 새로 끼어들면 이 테스트가 깨진다.
    """
    import inspect
    src = inspect.getsource(trader._check_sell_conditions)
    # 실시간 피드 등록에서만 관심목록을 읽는다(매수 후보 우선순위 계산용).
    watchlist_uses = src.count("stock_data")
    assert watchlist_uses <= 2, (
        f"매도 경로가 관심목록을 {watchlist_uses}곳에서 참조한다 — 자격 판정에 쓰이는지 확인 필요")
