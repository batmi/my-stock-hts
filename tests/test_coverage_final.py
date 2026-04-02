import pytest
from unittest.mock import patch, MagicMock
import config
import api
from modules import auto_trade, analysis, market
import pandas as pd
from datetime import datetime

def test_autotrader_singleton():
    """AutoTrader 싱글톤 패턴 테스트"""
    t1 = auto_trade.AutoTrader()
    t2 = auto_trade.AutoTrader()
    assert t1 is t2

def test_conclusion_monitor_singleton():
    """ConclusionMonitor 싱글톤 패턴 테스트"""
    m1 = auto_trade.ConclusionMonitor()
    m2 = auto_trade.ConclusionMonitor()
    assert m1 is m2

@patch('modules.auto_trade.api.get_unfilled_orders')
@patch('modules.auto_trade.api.revise_cancel_order')
def test_manage_unfilled_orders(mock_revise, mock_get):
    """미체결 주문 관리 테스트"""
    trader = auto_trade.AutoTrader()
    # 오래된 미체결 주문 모킹
    mock_get.return_value = [{
        'odno': '123', 'pdno': '005930', 'prdt_name': 'Samsung', 'rmn_qty': '10', 'ord_tmd': '090000'
    }]
    
    # datetime 모킹 (12시로 설정하여 09시 주문이 취소 대상이 되도록 함)
    class MockDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2023, 1, 1, 12, 0, 0)

    with patch('modules.auto_trade.datetime', MockDatetime):
        trader.order_manager.manage_unfilled_orders()
        mock_revise.assert_called()

@patch('modules.analysis.api.get_domestic_index_chart')
def test_get_market_regime_fallback(mock_get_index):
    """시장 국면 판단 Fallback 테스트"""
    # KIS API 실패 시뮬레이션
    mock_get_index.return_value = pd.DataFrame()
    
    # yfinance 데이터 모킹
    with patch('modules.analysis.api.get_chart_data') as mock_yf:
        mock_yf.return_value = pd.DataFrame({'close': [100]*60})
        regime, adj = analysis.get_market_regime("KOSPI")
        # Fallback이 동작하여 결과가 반환되어야 함
        assert regime in ["Bull", "Bear", "Sideways"]
        mock_yf.assert_called()

@patch('modules.market.api.fetch_yfinance_data')
def test_show_market_indices_data_handling(mock_fetch):
    """시장 지수 조회 데이터 처리 테스트"""
    # 데이터가 비어있을 때 에러 없이 처리되는지 확인
    mock_fetch.return_value = pd.DataFrame() 
    
    # [수정] 사용자 입력을 모킹 (메뉴 선택 '8', 재시도 'n', 메인화면 'q')
    with patch('rich.prompt.Prompt.ask', side_effect=["8", "n", "n", "q"]):
        with patch('config.console.print') as mock_print:
            market.show_market_indices()
            assert mock_print.call_count > 0

def test_main_show_help():
    """메인 도움말 출력 테스트"""
    import main
    with patch('config.console.print') as mock_print:
        main.show_help()
        assert mock_print.call_count > 0