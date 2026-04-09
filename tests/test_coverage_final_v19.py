import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
from datetime import datetime

import api
import config
import context
from modules import theme_analysis, settings, trading, auto_trade, db_manager
from modules.telegram_bot import TelegramCommander

@pytest.fixture(autouse=True)
def setup_teardown():
    yield
    try:
        db_manager.db.close_connection()
    except: pass

# ==========================================================
# 1. theme_analysis.py 커버리지 부스트
# ==========================================================
@patch('modules.theme_analysis.genai', None)
def test_theme_analysis_genai_none_handling():
    """genai 라이브러리가 임포트되지 않았을 때의 모든 방어 로직 커버리지"""
    res1 = theme_analysis.analyze_market_trends_with_gemini()
    res2 = theme_analysis.analyze_stock_with_gemini("005930", "삼성전자", "")
    res3 = theme_analysis.evaluate_backtest_with_gemini("005930", "삼성전자", "")
    res4 = theme_analysis.generate_trading_autopsy("005930", "삼성전자", "", 0, "", 0, 0)
    res5 = theme_analysis.generate_portfolio_diagnosis("")
    res6 = theme_analysis.generate_morning_briefing("")
    res7 = theme_analysis.generate_stock_curation()
    res8 = theme_analysis.ask_gemini("hi")
    res9 = theme_analysis.get_latest_news_with_gemini("samsung")
    
    assert res1 is None
    assert "⚠️" in res2 or "설정되지 않았" in res2
    assert "⚠️" in res3
    assert "⚠️" in res4
    assert "⚠️" in res5
    assert res6 is None
    assert "⚠️" in res7
    assert "⚠️" in res8
    assert "⚠️" in res9

def test_run_tradingview_screener_presets():
    """TradingView 스크리너의 다양한 프리셋 쿼리 빌드 커버리지"""
    mock_query_inst = MagicMock()
    mock_query_inst.where.return_value = mock_query_inst
    mock_query_inst.order_by.return_value = mock_query_inst
    mock_query_inst.limit.return_value = mock_query_inst
    mock_query_inst.get_scanner_data.return_value = (1, pd.DataFrame({
        'name': ['005930'], 'description': ['삼성전자'], 'close': [60000], 'change': [1.5], 'volume': [100000]
    }))
    
    mock_query_cls = MagicMock()
    mock_query_cls.return_value.set_markets.return_value.select.return_value = mock_query_inst

    with patch.dict('sys.modules', {'tradingview_screener': MagicMock(Query=mock_query_cls, Column=MagicMock())}):
        with patch('config.console.print'):
            # 프리셋 4번 (거래량 급증)
            with patch('rich.prompt.Prompt.ask', side_effect=["1", "4", "n"]):
                theme_analysis._run_tradingview_screener()
            # 프리셋 6번 (신고가 랠리)
            with patch('rich.prompt.Prompt.ask', side_effect=["1", "6", "n"]):
                theme_analysis._run_tradingview_screener()
            # 프리셋 12번 (당일 급상승 상위)
            with patch('rich.prompt.Prompt.ask', side_effect=["1", "12", "n"]):
                theme_analysis._run_tradingview_screener()

# ==========================================================
# 2. settings.py 커버리지 부스트
# ==========================================================
def test_view_system_config_render():
    """시스템 설정 전체 조회 렌더링 정상 동작 테스트"""
    with patch('config.console.print') as mock_print:
        settings.view_system_config()
        assert mock_print.call_count > 0

def test_apply_strategy_preset_invalid():
    """잘못된 프리셋 이름 입력 시 방어 로직 커버리지"""
    res = settings.apply_strategy_preset("invalid_preset", interactive=False)
    assert "알 수 없는 프리셋" in res

@patch('rich.prompt.Prompt.ask', return_value='n')
def test_reset_to_default_cancel(mock_ask):
    """시스템 설정 초기화 중 취소 시의 분기 커버리지"""
    res = settings.reset_to_default(interactive=True)
    assert res is False

# ==========================================================
# 3. trading.py 커버리지 부스트
# ==========================================================
@patch('rich.prompt.Prompt.ask', return_value='2')
def test_select_account_auto_branch(mock_ask):
    """실전 모드에서 자동매매 계좌와 메인 계좌가 분리되어 있을 때 계좌 선택 로직 커버리지"""
    config.session.is_simulation = False
    config.session.cano = "11111111"
    config.session.auto_cano = "87654321"
    config.session.auto_acnt_prdt_cd = "01"
    
    cano, acnt, label = trading.select_account()
    assert cano == "87654321"
    assert label == "자동투자"

# ==========================================================
# 4. telegram_bot.py 커버리지 부스트
# ==========================================================
@patch('modules.auto_trade.load_restricted_stocks', return_value={})
@patch('modules.auto_trade.save_restricted_stocks')
def test_telegram_cmd_addrestrict(mock_save, mock_load):
    cmd = TelegramCommander()
    with patch.object(cmd, '_resolve_stock', return_value=("005930", "삼성전자", False)):
        res = cmd._cmd_addrestrict(["005930", "사유테스트"])
        assert "차단되었습니다" in res
        mock_save.assert_called_once()

@patch('modules.auto_trade.load_restricted_stocks', return_value={"005930": {"name": "삼성전자"}})
@patch('modules.auto_trade.save_restricted_stocks')
def test_telegram_cmd_delrestrict(mock_save, mock_load):
    cmd = TelegramCommander()
    with patch.object(cmd, '_resolve_stock', return_value=("005930", "삼성전자", False)):
        res = cmd._cmd_delrestrict(["005930"])
        assert "해제되었습니다" in res
        mock_save.assert_called_once()

@patch('api.get_domestic_open_orders')
@patch('api.get_overseas_open_orders')
def test_telegram_cmd_pending_list(mock_ovs, mock_dom):
    cmd = TelegramCommander()
    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    
    mock_dom.return_value = [{'prdt_name': '삼성', 'pdno': '005930', 'odno': '1', 'rmn_qty': '10', 'ord_unpr': '50000', 'sll_buy_dvsn_cd_name': '매수'}]
    mock_ovs.return_value = [{'prdt_name': 'Apple', 'pdno': 'AAPL', 'odno': '2', 'nccs_qty': '5', 'ft_ord_unpr3': '150', 'sll_buy_dvsn_cd': '01'}]
    
    res = cmd._cmd_pending([])
    assert "삼성" in res
    assert "Apple" in res
    assert "매수" in res
    assert "매도" in res

# ==========================================================
# 5. auto_trade.py 커버리지 부스트
# ==========================================================
@patch('api.place_order')
@patch('api.send_telegram_message')
def test_order_manager_fatal_error(mock_tg, mock_place):
    """주문 API 호출 시 치명적인 에러(OPSQ2000 등) 발생 시 강제 예외 발생 분기 커버리지"""
    trader = auto_trade.AutoTrader()
    trader.log = MagicMock()
    om = auto_trade.OrderManager(trader)
    
    mock_place.return_value = {'rt_cd': '1', 'msg1': 'Fatal Network Err', 'msg_cd': 'OPSQ2000'}
    
    with pytest.raises(Exception, match="치명적 오류"):
        om.send_order("005930", 10, "buy", price=50000)