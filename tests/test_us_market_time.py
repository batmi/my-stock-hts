import pytest
import datetime as dt
from unittest.mock import patch, MagicMock
import logging
import sys
import os

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import trading
import config

@pytest.fixture(autouse=True)
def setup_teardown(monkeypatch):
    """테스트 중 불필요한 콘솔 출력을 최소화합니다."""
    original_level = config.FILE_DEBUG_LEVEL
    config.FILE_DEBUG_LEVEL = "OFF"
    
    logging.disable(logging.CRITICAL)
    
    # 콘솔 출력 및 UI(Progress 바) 렌더링 완벽 차단
    monkeypatch.setattr(config.console, "print", MagicMock())
    monkeypatch.setattr(config.console, "log", MagicMock())
    progress_mock = MagicMock()
    progress_mock.__enter__.return_value = progress_mock
    monkeypatch.setattr("modules.trading.Progress", lambda *args, **kwargs: progress_mock)
    monkeypatch.setattr("builtins.print", MagicMock())
    
    yield
    
    logging.disable(logging.NOTSET)
    config.FILE_DEBUG_LEVEL = original_level

@pytest.mark.parametrize("test_time_utc, expected_ord_dvsn, expected_name", [
    # ----------------------------------------------------
    # [서머타임 적용 기간] - EDT (UTC-4)
    # ----------------------------------------------------
    # 06:00 EDT (10:00 UTC) -> 프리마켓 (04:00 ~ 09:30 EDT) 지정가 32
    ("2024-06-01 10:00:00", "32", "프리마켓"),
    # 11:00 EDT (15:00 UTC) -> 정규장 (09:30 ~ 16:00 EDT) 지정가 00
    ("2024-06-01 15:00:00", "00", "정규장"),
    # 18:00 EDT (22:00 UTC) -> 애프터마켓 (16:00 ~ 20:00 EDT) 지정가 34
    ("2024-06-01 22:00:00", "34", "애프터마켓"),
    # 22:00 EDT (02:00 UTC next day) -> 데이마켓(주간거래) (20:00 ~ 04:00 EDT) 지정가 31
    ("2024-06-02 02:00:00", "31", "데이마켓(주간거래)"),
    
    # ----------------------------------------------------
    # [서머타임 미적용 기간] - EST (UTC-5)
    # ----------------------------------------------------
    # 06:00 EST (11:00 UTC) -> 프리마켓 (04:00 ~ 09:30 EST) 지정가 32
    ("2024-12-01 11:00:00", "32", "프리마켓"),
    # 11:00 EST (16:00 UTC) -> 정규장 (09:30 ~ 16:00 EST) 지정가 00
    ("2024-12-01 16:00:00", "00", "정규장"),
    # 18:00 EST (23:00 UTC) -> 애프터마켓 (16:00 ~ 20:00 EST) 지정가 34
    ("2024-12-01 23:00:00", "34", "애프터마켓"),
    # 22:00 EST (03:00 UTC next day) -> 데이마켓(주간거래) (20:00 ~ 04:00 EST) 지정가 31
    ("2024-12-02 03:00:00", "31", "데이마켓(주간거래)"),
])
@patch('modules.trading.api.place_order')
@patch('modules.trading.api.send_telegram_message')
@patch('modules.trading.api.get_chart_data')
@patch('modules.trading.api.get_current_price')
@patch('modules.trading.api.get_stock_name_by_code')
@patch('modules.trading.api.find_best_exchange_code')
@patch('modules.trading.api.fetch_overseas_buyable_quantity')
@patch('modules.trading.analysis.print_table')
@patch('modules.trading.Prompt.ask')
@patch('modules.trading.utils.validate_and_confirm_stock')
@patch('modules.trading.utils.show_menu')
@patch('modules.trading.select_account')
@patch('modules.trading.db_manager.db')
@patch('modules.trading.auto_trade.AutoTrader')
@patch('modules.trading.auto_trade.ConclusionMonitor')
def test_us_market_order_time(
    mock_monitor, mock_trader, mock_db, mock_select_acc, mock_show_menu, 
    mock_validate, mock_ask, mock_print_table, mock_buyable_qty, 
    mock_find_excd, mock_get_name, mock_get_price, mock_get_chart, mock_send_telegram, 
    mock_place_order, test_time_utc, expected_ord_dvsn, expected_name
):
    """미국 주식 매매 시 시간에 따른 서머타임 판별 및 시장(프리/정규/애프터) 코드 지정 로직 테스트"""
    
    mock_now_utc = dt.datetime.strptime(test_time_utc, "%Y-%m-%d %H:%M:%S")
    
    # datetime.now(timezone.utc) 호출을 제어하기 위한 Fake 클래스 주입
    class FakeDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            # 코드가 now(timezone.utc).replace(tzinfo=None)로 UTC를 얻으므로 tz-aware로 반환
            if tz is not None:
                return mock_now_utc.replace(tzinfo=tz)
            return mock_now_utc
            
    with patch('modules.trading.datetime', FakeDatetime):
        # 모킹 값 설정 (네트워크 및 의존성 격리)
        mock_select_acc.return_value = ("12345678", "01", "실전투자")
        mock_show_menu.return_value = "5" # 직접 입력 (매수 메뉴)
        mock_validate.return_value = True
        mock_find_excd.return_value = "NAS"
        mock_get_price.return_value = 150.0
        mock_buyable_qty.return_value = 10
        mock_get_chart.return_value = None # 차트 데이터는 스킵
        mock_get_name.return_value = "Apple Inc."
        
        # 사용자 프롬프트 순서 모킹: 1.종목코드, 2.수량, 3.단가, 4.최종확인
        mock_ask.side_effect = ["AAPL", "1", "150", "y"]
        
        # 주문 접수 API 결과 모킹
        mock_place_order.return_value = {'rt_cd': '0', 'output': {'ODNO': '000123'}}
        
        # 수동 주문 메뉴 실행 (Buy)
        trading.send_order("buy")
        
        # 검증 파트
        mock_place_order.assert_called_once()
        called_args, called_kwargs = mock_place_order.call_args
        
        # api.place_order(market_api_param, order_type, stock_code, qty, price, ord_dvsn, exchange_code=excd)
        passed_market_param = called_args[0]
        passed_ord_dvsn = called_args[5]
        
        assert passed_market_param == "overseas"
        assert passed_ord_dvsn == expected_ord_dvsn, f"UTC {test_time_utc} 기준 예상 시장 코드는 {expected_ord_dvsn}({expected_name})이나, {passed_ord_dvsn}가 반환되었습니다."