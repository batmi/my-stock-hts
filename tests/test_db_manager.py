import pytest
import os
import sqlite3
from modules.db_manager import DBManager
import config

@pytest.fixture
def db_manager(tmp_path):
    """테스트용 DBManager 인스턴스 생성 (임시 파일 사용)"""
    # 임시 DB 파일 경로 설정
    db_file = tmp_path / "test_db.sqlite"
    
    # config.DB_FILE_PATH를 임시 경로로 변경 (테스트 격리)
    original_db_path = config.DB_FILE_PATH
    config.DB_FILE_PATH = str(db_file)
    
    # DBManager 인스턴스 생성 (이때 테이블이 생성된다고 가정)
    manager = DBManager()
    
    yield manager
    
    # 테스트 종료 후 정리
    if hasattr(manager, 'local') and hasattr(manager.local, 'conn') and manager.local.conn:
        manager.local.conn.close()
    config.DB_FILE_PATH = original_db_path

def test_strategy_operations(db_manager):
    """전략(Strategy) CRUD 테스트"""
    code = "005930"
    name = "삼성전자"
    strategy_data = {
        "buy_score": 8.0,
        "buy_rsi": 65,
        "sell_score": 5.0,
        "stop_loss": -7.0,
        "take_profit": 30.0,
        "take_profit_rsi": 75,
        "ts_activation": 10.0,
        "ts_callback": 3.0,
        "weights": {"TREND": 4.0}
    }
    
    # 1. 저장
    db_manager.save_stock_strategy(code, name, strategy_data)
    
    # 2. 조회 (단건)
    saved = db_manager.get_stock_strategy(code)
    assert saved is not None
    assert saved['code'] == code
    assert saved['name'] == name
    assert saved['buy_score'] == 8.0
    # weights 필드 확인 (구현에 따라 dict 또는 json string일 수 있음)
    assert 'weights' in saved
    
    # 3. 조회 (전체)
    all_strategies = db_manager.get_all_stock_strategies()
    assert len(all_strategies) == 1
    assert all_strategies[0]['code'] == code
    
    # 4. 삭제
    db_manager.delete_stock_strategy(code)
    assert db_manager.get_stock_strategy(code) is None
    assert len(db_manager.get_all_stock_strategies()) == 0

def test_trade_operations(db_manager):
    """매매(Trade) 기록 CRUD 테스트"""
    # 1. 매수 기록 삽입
    odno = "100001"
    db_manager.insert_trade(
        type_str="buy", 
        code="005930", 
        name="삼성전자", 
        qty=10, 
        price=70000, 
        odno=odno, 
        order_status="접수", 
        reason="전략매수"
    )
    
    # 2. 존재 여부 확인
    assert db_manager.check_trade_exists(odno, "접수") is True
    assert db_manager.check_trade_exists("999999", "체결") is False
    
    # 3. 조회 (odno)
    trade = db_manager.get_trade_by_odno(odno)
    assert trade is not None
    assert trade['code'] == "005930"
    assert trade['qty'] == '10'
    
    # 4. 업데이트 (가격 수정)
    db_manager.update_trade(odno, price=70500)
    updated_trade = db_manager.get_trade_by_odno(odno)
    assert updated_trade['price'] == '70500'
    
    # 5. 최근 매수 내역 조회
    latest_buy = db_manager.get_latest_buy_trade("005930")
    assert latest_buy is not None
    assert latest_buy['odno'] == odno
    
    # 6. 전체 조회 (limit)
    trades = db_manager.get_trades(limit=5)
    assert len(trades) >= 1

def test_latest_buy_trade_external_only(db_manager):
    """수동/외부 매수처럼 '체결 확인' 레코드만 존재해도 매수 시각을 조회할 수 있어야 한다.

    외부(앱/HTS) 매수는 '접수' 원본 없이 체결 확인 레코드만 DB에 남는데,
    이를 조회하지 못하면 holding_days=0이 되어 시간청산 등이 무력화된다.
    """
    code = "035720"
    db_manager.insert_trade(
        type_str="매수(외부)",
        code=code,
        name="카카오",
        qty=5,
        price=50000,
        odno="200001",
        order_status="체결",
        reason="체결 확인 (앱/HTS 외부 주문)",
        custom_time="2024-01-02 09:30:00",
        stop_loss_rate=0.0,
    )

    latest_buy = db_manager.get_latest_buy_trade(code)
    assert latest_buy is not None
    assert latest_buy['time'] == "2024-01-02 09:30:00"


def test_latest_buy_trade_prefers_original_over_confirmation(db_manager):
    """'접수' 원본(ATR 손절률 보유)과 '체결 확인' 더미가 공존하면 원본을 우선해야 한다."""
    code = "005930"
    # 시스템 매수: ATR 손절률이 살아있는 '접수' 원본
    db_manager.insert_trade(
        type_str="buy", code=code, name="삼성전자", qty=10, price=70000,
        odno="300001", order_status="접수", reason="전략매수", stop_loss_rate=-5.0,
    )
    # 체결 확인 더미 (ATR 손절률 누락)
    db_manager.insert_trade(
        type_str="buy", code=code, name="삼성전자", qty=10, price=70000,
        odno="300002", order_status="체결", reason="체결 확인 (전략매수)", stop_loss_rate=0.0,
    )

    latest_buy = db_manager.get_latest_buy_trade(code)
    assert latest_buy is not None
    assert latest_buy['odno'] == "300001"  # ATR 손절률이 살아있는 원본


def test_trailing_stop_operations(db_manager):
    """트레일링 스탑 최고가 관리 테스트"""
    code = "005930"
    
    # 1. 초기 상태 (없음)
    assert db_manager.get_highest_price(code) is None
    
    # 2. 최고가 업데이트 (신규)
    db_manager.update_highest_price(code, 80000)
    assert db_manager.get_highest_price(code) == 80000
    
    # 3. 최고가 업데이트 (갱신)
    db_manager.update_highest_price(code, 82000)
    assert db_manager.get_highest_price(code) == 82000
    
    # 4. 삭제
    db_manager.delete_trailing_stop(code)
    assert db_manager.get_highest_price(code) is None