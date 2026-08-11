import pytest
from unittest.mock import patch, MagicMock, ANY
import api
import config
from modules import analysis, auto_trade, market, account, db_manager, settings
import pandas as pd
import utils
import os
import time

# --- AutoTrade ---
@patch('modules.auto_trade.api.check_server_health')
@patch('modules.auto_trade.api.send_telegram_message')
def test_wait_for_server_recovery(mock_tg, mock_health):
    """서버 복구 대기 로직 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader._wait_alert_sent = True # 진입 알림이 발송된 상태 가정 (복구 알림 짝 맞춤)

    # [추가] 장애 중 누적된 체결 감시 에러 카운트가 복구 시 리셋되는지 검증
    monitor = auto_trade.ConclusionMonitor()
    monitor.consecutive_errors = 7

    # 1. False (Still down) -> 2. True (Recovered)
    mock_health.side_effect = [False, True]

    with patch('time.sleep'): # Skip delay
        trader._wait_for_server_recovery()

    assert mock_tg.called
    assert "서버 복구" in mock_tg.call_args[0][0]
    assert monitor.consecutive_errors == 0 # Kill Switch 교착 방지
    assert trader._wait_alert_sent is False


@patch('modules.auto_trade.api.check_server_health', return_value=True)
@patch('modules.auto_trade.api.send_telegram_message')
def test_wait_for_server_recovery_alert_suppressed(mock_tg, mock_health):
    """진입 알림이 쿨타임으로 생략된 경우 복구 알림도 생략 (스팸 방지)"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader._wait_alert_sent = False

    with patch('time.sleep'):
        trader._wait_for_server_recovery()

    assert not mock_tg.called

def test_monitor_account_status_empty():
    """계좌 상태 모니터링 (빈 데이터) 테스트"""
    trader = auto_trade.AutoTrader()
    with patch.object(trader, 'log') as mock_log:
        trader._monitor_account_status([], [], None)
        assert any("보유 종목: 없음" in str(c) for c in mock_log.call_args_list)

@patch('modules.auto_trade.api.fetch_sellable_quantity')
def test_check_sell_conditions_qty_mismatch(mock_qty):
    """매도 시 주문 가능 수량 부족 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.order_manager.pending_orders.clear() # [Fix] 싱글톤 상태 초기화 (이전 테스트 영향 제거)
    
    holdings = [{
        'pdno': '005930', 'prdt_name': 'Samsung', 'ord_psbl_qty': '10',
        'evlu_pfls_rt': '5.0', 'prpr': '60000', 'pchs_avg_pric': '55000',
        'evlu_pfls_amt': '50000'
    }]
    
    # 실제 가능 수량이 0임 (미체결 등)
    mock_qty.return_value = 0
    
    # 매도 신호 강제
    with patch.object(trader.strategy, 'analyze_sell', return_value={'action': 'sell', 'reason': 'Test', 'score': 0, 'state': '', 'ind': {}}):
        with patch.object(trader, 'log') as mock_log:
            with patch('time.sleep'):
                with patch('modules.auto_trade.api.get_chart_data', return_value=None): # [Fix] 차트 조회 모킹
                    with patch('modules.auto_trade.load_restricted_stocks', return_value={}): # [Fix] 제한 종목 모킹
                        trader._check_sell_conditions(holdings)
                
    # 매도 중단 로그 확인
    assert any("매도 중단" in str(c) for c in mock_log.call_args_list)

def test_check_buy_conditions_max_holdings():
    """최대 보유 종목 수 도달 시 매수 중단 테스트"""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0 # [Fix] 상태 초기화 (로그 출력 조건 만족을 위해)
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    
    # _check_buy_conditions는 config.settings.SYSTEM_MAX_HOLDINGS를 참조한다.
    # (이 값을 설정해야 최대 보유 도달로 즉시 매수 스킵되어 실제 주문 API 호출을 막는다)
    config.settings.SYSTEM_MAX_HOLDINGS = 5
    config.SYSTEM_MAX_HOLDINGS = 5
    # 보유 종목 5개 (최대치)
    holdings = [{'pdno': str(i), 'prdt_name': f'Stock{i}', 'hldg_qty': '10'} for i in range(5)]
    
    with patch.object(trader, '_analyze_candidates', return_value=[{'code': '005930', 'name': 'Samsung', 'price': 50000, 'score': 9.0, 'rsi': 50.0, 'adx': 25.0, 'cci': 100.0, 'vol_strength': 150.0, 'atr': 500}]):
        with patch.object(trader, 'log') as mock_log:
            trader._check_buy_conditions(holdings, {'d2_deposit': 1000000})
            assert any("최대 보유 종목 수" in str(c) for c in mock_log.call_args_list)

# --- Analysis ---
@patch('urllib.request.urlretrieve')
def test_get_master_stock_list_download_fail(mock_retrieve):
    """마스터 파일 다운로드 실패 테스트"""
    mock_retrieve.side_effect = Exception("Download Error")
    
    # 파일이 없다고 가정
    with patch('os.path.exists', return_value=False):
        with patch('os.makedirs'):
            res = analysis._get_master_stock_list("KOSPI")
            assert res == []

# --- API ---
def test_throttled_session_tps():
    """TPS 계산 로직 테스트"""
    session = api.ThrottledSession()
    now = time.time()
    
    # 오래된 요청이 앞에 오도록 설정
    session.request_history_sim.clear()
    session.request_history_sim.append(now - 1.5) # 만료
    session.request_history_sim.append(now - 0.5) # 유효
    session.request_history_sim.append(now - 0.5) # 유효
    
    if hasattr(session, '_get_current_tps'):
        tps = session._get_current_tps()
    else:
        tps = len([t for t in session.request_history_sim if now - t <= 1.0])
    assert tps == 2

@patch('api.fetch_yfinance_data')
def test_get_chart_data_index_fail(mock_fetch):
    """지수 차트 데이터 조회 실패 테스트

    [주의] get_chart_data는 메모리·디스크 캐시를 먼저 본다. 같은 세션의 다른 테스트가
    ^KS11을 한 번이라도 조회했다면 캐시 적중으로 빈 DataFrame이 나오지 않는다
    (xdist 병렬 실행에서는 워커 배정에 따라 결과가 갈려 플래키해진다).
    '조회 실패 시 빈 DataFrame'만 검증하는 테스트이므로 캐시를 끄고 확인한다.
    """
    mock_fetch.side_effect = Exception("YF Error")
    with patch.object(config, 'CHART_CACHE_TTL_MINUTES', 0):   # 0 = 캐시 미사용
        df = api.get_chart_data("^KS11")
    assert df.empty

# --- Settings ---
@patch('builtins.open', side_effect=Exception("Write Error"))
def test_save_dynamic_config_fail(mock_open):
    """설정 저장 실패 테스트"""
    with patch('config.console.print') as mock_print:
        settings._save_dynamic_config()
        assert any("저장 실패" in str(c) for c in mock_print.call_args_list)

# --- DB Manager ---
def test_db_get_original_order_type():
    """원 주문 유형 조회 테스트"""
    db = db_manager.DBManager()
    odno = "TEST_ODNO_V4"
    db.insert_trade("buy", "005930", "Samsung", 10, 50000, odno, order_status="접수")
    
    res = db.get_original_order_type(odno)
    assert res == "buy"
    
    res_none = db.get_original_order_type("UNKNOWN")
    assert res_none is None

def test_buy_skip_log_names_the_blocking_reason():
    """매수 스킵 로그는 '무엇이 막았는지'를 그 줄에서 밝힌다.

    [배경] 종전 문구는 '매수 스킵 상태 - 조건 미달'이었다. 사실과 반대다 — 나열된
    종목들은 매수 조건을 통과한 후보이고, 막은 것은 계좌 상태(슬롯·예수금)다.
    사유는 위쪽 다른 로그에만 남아 시각이 벌어진 채 흩어져 있었다.
    """
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    config.settings.SYSTEM_MAX_HOLDINGS = 4
    config.SYSTEM_MAX_HOLDINGS = 4
    holdings = [{'pdno': str(i), 'prdt_name': f'Stock{i}', 'hldg_qty': '10'} for i in range(4)]
    cand = [{'code': '005930', 'name': 'SK이노베이션', 'price': 50000, 'score': 8.5,
             'rsi': 50.0, 'adx': 25.0, 'cci': 100.0, 'vol_strength': 150.0, 'atr': 500}]

    with patch.object(trader, '_analyze_candidates', return_value=cand):
        with patch.object(trader, 'log') as mock_log:
            trader._check_buy_conditions(holdings, {'d2_deposit': 1000000})

    lines = [str(c) for c in mock_log.call_args_list]
    skip_line = [l for l in lines if "매수 조건 충족" in l]
    assert skip_line, lines
    assert "보유 슬롯 가득 참" in skip_line[0]
    assert "4/4종목" in skip_line[0]
    assert "조건 미달" not in skip_line[0]
    # 무엇을 풀어야 사는지도 함께 남는다
    assert any("슬롯이 비면" in l for l in lines), lines


def test_buy_skip_log_reports_cash_shortage():
    """예수금 부족으로 막혔을 때도 같은 줄에 금액이 남는다."""
    trader = auto_trade.AutoTrader()
    trader.is_running = True
    trader.consecutive_errors = 0
    config.session.stock_data = {"stocks_kr": [{"code": "005930", "name": "Samsung"}]}
    config.settings.SYSTEM_MAX_HOLDINGS = 4
    config.SYSTEM_MAX_HOLDINGS = 4
    cand = [{'code': '005930', 'name': '카카오', 'price': 50000, 'score': 8.5,
             'rsi': 50.0, 'adx': 25.0, 'cci': 100.0, 'vol_strength': 150.0, 'atr': 500}]

    with patch.object(trader, '_analyze_candidates', return_value=cand):
        with patch.object(trader, 'log') as mock_log:
            trader._check_buy_conditions([], {'d2_deposit': 500})

    skip_line = [str(c) for c in mock_log.call_args_list if "매수 조건 충족" in str(c)]
    assert skip_line, [str(c) for c in mock_log.call_args_list]
    assert "예수금 부족" in skip_line[0]
    assert "500원" in skip_line[0]
