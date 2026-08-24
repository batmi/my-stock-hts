from unittest.mock import patch
from core import utils
import config
from core import context


def test_get_tick_size_domestic():
    """국내 주식 호가 단위 테스트"""
    assert utils.get_tick_size(1000, False) == 1
    assert utils.get_tick_size(3000, False) == 5
    assert utils.get_tick_size(15000, False) == 10
    assert utils.get_tick_size(40000, False) == 50
    assert utils.get_tick_size(150000, False) == 100
    assert utils.get_tick_size(300000, False) == 500
    assert utils.get_tick_size(1000000, False) == 1000


def test_get_tick_size_overseas():
    """해외 주식 호가 단위 테스트"""
    assert utils.get_tick_size(150.50, True) == 0.01


def test_adjust_to_tick():
    """호가 단위에 맞춘 가격 보정 테스트"""
    # Domestic
    assert utils.adjust_to_tick(1234, False) == 1234  # 1 unit
    # 3456 / 5 = 691.2 -> 691 * 5 = 3455
    assert utils.adjust_to_tick(3456, False) == 3455

    # Overseas
    assert utils.adjust_to_tick(150.123, True) == 150.12


def test_account_context():
    """계좌 컨텍스트 매니저 테스트"""
    # 1. 설정: 가짜 계좌 정보 설정
    config.session.is_simulation = False
    config.session.cano = "11111111"
    config.session.auto_cano = "22222222"
    context.trade_context.use_auto_account = False

    # 2. 테스트: 자동 계좌 컨텍스트 진입 시 use_auto_account가 True로 설정되는지 확인
    with utils.AccountContext("22222222"):
        assert context.trade_context.use_auto_account is True

    # 3. 테스트: 컨텍스트 종료 후 use_auto_account가 원래대로 돌아오는지 확인
    assert context.trade_context.use_auto_account is False

    # 4. 테스트: 메인 계좌 컨텍스트 진입 시 use_auto_account가 False로 설정되는지 확인
    context.trade_context.use_auto_account = True  # 다시 True로 설정
    with utils.AccountContext("11111111"):
        assert context.trade_context.use_auto_account is False

    # 5. 최종 확인: 컨텍스트 종료 후 use_auto_account가 원래대로 돌아오는지 확인
    assert context.trade_context.use_auto_account is True


@patch("core.utils.yf.Ticker")
def test_get_exchange_rate_failure(mock_ticker):
    """환율 조회 실패 시 기본 환율 반환 테스트"""
    mock_ticker.side_effect = Exception("Connection Error")
    original_default_exchange_rate = config.DEFAULT_EXCHANGE_RATE
    config.DEFAULT_EXCHANGE_RATE = 1450.0  # 테스트를 위해 임시로 설정
    assert utils.get_exchange_rate() == 1450.0
    config.DEFAULT_EXCHANGE_RATE = original_default_exchange_rate # 원래 값으로 복원


def test_adjust_to_tick_invalid_input():
    """adjust_to_tick에 잘못된 입력이 들어왔을 때 0을 반환하는지 확인"""
    assert utils.adjust_to_tick("abc", False) == 0