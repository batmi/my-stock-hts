import pytest
from unittest.mock import patch, MagicMock
import pandas as pd

from modules import trading
import config

# 모의투자 및 계좌 설정을 고정하는 fixture
@pytest.fixture(autouse=True)
def setup_config_session():
    # 백업
    orig_cano = config.session.cano
    orig_acnt = config.session.acnt_prdt_cd

    
    config.session.cano = "12345678"
    config.session.acnt_prdt_cd = "01"
    config.session.auto_cano = None
    
    yield
    
    # 복원
    config.session.cano = orig_cano
    config.session.acnt_prdt_cd = orig_acnt



@patch('modules.trading.utils.show_menu')
def test_select_account_default(mock_show_menu):
    """기본 계좌가 정상적으로 선택되는지 검증"""
    # 자동 계좌가 없으면 show_menu를 호출하지 않고 바로 반환함
    cano, acnt, label = trading.select_account()
    assert cano == "12345678"
    assert acnt == "01"
    assert label == "한투증권"


@patch('modules.trading.utils.show_menu', return_value="1")
@patch('modules.trading.account.fetch_domestic_balance')
@patch('modules.trading.Prompt.ask', return_value="1")
def test_select_stock_from_balance_domestic(mock_ask, mock_fetch_dom, mock_show_menu):
    """국내 주식 잔고에서 종목을 선택하는 로직 검증"""
    # 가상의 잔고 데이터
    mock_fetch_dom.return_value = ([
        {
            'pdno': '005930',
            'prdt_name': '삼성전자',
            'hldg_qty': '10',
            'pchs_avg_pric': '70000',
            'prpr': '75000',
            'evlu_amt': '750000',
            'evlu_pfls_amt': '50000',
            'evlu_pfls_rt': '7.14'
        }
    ], "0")
    
    code, name, is_overseas, excd, info = trading.select_stock_from_balance("12345678", "01")
    
    assert code == "005930"
    assert name == "삼성전자"
    assert is_overseas is False
    assert excd is None
    assert info['qty'] == 10
    assert info['buy_price'] == 70000.0


@patch('modules.trading.db_manager.db.check_trade_exists', return_value=False)
@patch('modules.trading.api.get_current_price', return_value=50000)
@patch('modules.trading.db_manager.db.insert_trade')
def test_create_fill_history(mock_insert, mock_get_price, mock_check):
    """체결 히스토리 생성 로직 검증"""
    db_order = {
        'odno': '9999',
        'type': 'buy',
        'code': '000660',
        'name': 'SK하이닉스',
        'qty': '5',
        'price': '0', # 시장가
        'strategy_score': 8.5
    }
    
    price = trading._create_fill_history(db_order, "테스트 사유")
    
    assert price == 50000.0
    mock_insert.assert_called_once()
    args, kwargs = mock_insert.call_args
    assert args[1] == '000660'
    assert args[4] == 50000.0
    assert kwargs['order_status'] == '체결(추정)'


@patch('modules.trading.api.get_order_book')
def test_show_order_book(mock_get_ob):
    """호가창 출력 로직이 예외 없이 실행되는지 검증"""
    mock_get_ob.return_value = {
        'rt_cd': '0',
        'output1': {
            'askp1': '51000', 'askp_rsqn1': '100',
            'bidp1': '50000', 'bidp_rsqn1': '200'
        }
    }
    # 화면 출력이 정상 수행되는지만 확인
    trading._show_order_book('000660', 'SK하이닉스', False, levels=1)


@patch('modules.trading.api.get_domestic_open_orders')
@patch('modules.trading.api.get_overseas_open_orders', return_value=[])
@patch('modules.trading.db_manager.db.get_trades')
def test_show_open_orders(mock_get_trades, mock_get_ovrs, mock_get_dom):
    """미체결 내역 조회 로직 검증"""
    # API 기반 미체결
    mock_get_dom.return_value = [{
        'odno': '8888',
        'pdno': '005930',
        'prdt_name': '삼성전자',
        'sll_buy_dvsn_cd_name': '매수',
        'ord_qty': '10',
        'ord_unpr': '70000',
        'rmn_qty': '5',
        'ord_tmd': '100000'
    }]
    
    # [mode 1 폐기] 모의투자 API 누락을 DB '접수' 행으로 메우던 병합 경로는 사라졌다.
    #  이제 미체결은 거래소 응답이 유일한 출처다.
    mock_get_trades.side_effect = [[], [], [], []]

    with patch('modules.trading.api.get_domestic_balance', return_value=([], "0")):
        orders = trading.show_open_orders()

    assert len(orders) == 1
    assert orders[0]['odno'] == '8888'

