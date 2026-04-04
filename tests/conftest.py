import sys
import os
import pytest
import pandas as pd
import numpy as np

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import db_manager # [추가] DB 매니저 임포트
from modules.auto_trade import AutoTrader, ConclusionMonitor
from modules.telegram_bot import TelegramCommander

@pytest.fixture(scope="session", autouse=True)
def setup_config():
    """테스트 세션 동안 사용할 설정 초기화 (모의투자 모드 강제)"""
    # 테스트 중 실수로 실전 API가 호출되지 않도록 안전장치
    config.session.initialize(mode="1")

@pytest.fixture(autouse=True)
def isolate_test_files(tmp_path, monkeypatch):
    """
    모든 테스트 실행 전 자동으로 임시 경로를 할당하여 
    실제 운영 데이터(json, db)가 덮어써지는 것을 방지합니다.
    """
    test_json = tmp_path / "test_stock.json"
    test_token = tmp_path / "test_token_cache.json"
    test_db = tmp_path / "test_trade_history.db"

    # 기본 더미 데이터 초기화
    test_json.write_text('{"stocks_kr": [], "etfs_kr": [], "stocks_us": [], "etfs_us": []}', encoding='utf-8')

    monkeypatch.setattr(config, "STOCK_DATA_FILE", str(test_json))
    monkeypatch.setattr(config, "TOKEN_CACHE_FILE", str(test_token))
    monkeypatch.setattr(config, "DB_FILE_PATH", str(test_db))
    monkeypatch.setattr(config, "JSON_DIR", str(tmp_path)) # 시스템 설정(dynamic_config.json) 덮어쓰기 방지

    # [추가] 테스트 중 생성되는 파일(차트, 엑셀, 로그) 격리
    test_chart_dir = tmp_path / "chart"
    test_data_dir = tmp_path / "data"
    test_log_dir = tmp_path / "logs"
    test_chart_dir.mkdir(exist_ok=True)
    test_data_dir.mkdir(exist_ok=True)
    test_log_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(config, "CHART_DIR", str(test_chart_dir))
    monkeypatch.setattr(config, "DATA_DIR", str(test_data_dir))
    monkeypatch.setattr(config, "LOG_DIR", str(test_log_dir))
    monkeypatch.setattr(config, "SYSTEM_TRADING_LOG_DIR", str(test_log_dir))

    # [추가] 전역 DB 인스턴스의 경로를 임시 DB로 강제 변경하여 실제 DB 오염 방지
    real_db = getattr(db_manager.db, '_real_db', db_manager.db)
    monkeypatch.setattr(real_db, "db_path", str(test_db))
    
    if hasattr(real_db, 'local') and hasattr(real_db.local, 'conn'):
        if real_db.local.conn:
            try:
                real_db.local.conn.close()
            except Exception:
                pass
            real_db.local.conn = None
    real_db._init_db()

    # [추가] 테스트 중 실제 텔레그램 메시지 발송 원천 차단
    monkeypatch.setattr(config, "ENABLE_TELEGRAM", False)

@pytest.fixture(autouse=True)
def cleanup_global_db_connection():
    """
    각 테스트 실행 후 전역 DBManager의 스레드 로컬 연결을 닫습니다.
    ResourceWarning: unclosed database 방지
    """
    yield
    
    # 테스트 종료 후 정리
    real_db = getattr(db_manager.db, '_real_db', db_manager.db)
    if hasattr(real_db, 'local') and hasattr(real_db.local, 'conn'):
        if real_db.local.conn:
            try:
                real_db.local.conn.close()
            except Exception:
                pass
            real_db.local.conn = None

@pytest.fixture(autouse=True)
def reset_all_singletons():
    """
    [전역 설정] 
    모든 테스트 실행 전후로 싱글톤 객체의 상태를 강제 초기화하여 
    테스트 파일 간의 상태 누수(State Leak) 및 간섭을 원천 차단합니다.
    """
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    TelegramCommander._instance = None
    
    yield
    
    AutoTrader._instance = None
    ConclusionMonitor._instance = None
    TelegramCommander._instance = None

def create_mock_df(trend='up', periods=100, start_price=10000):
    """가상의 주가 데이터프레임 생성 헬퍼 함수"""
    dates = pd.date_range(start="2023-01-01", periods=periods)
    
    if trend == 'up':
        # 우상향: 10000 -> 15000
        close = np.linspace(start_price, start_price * 1.5, periods)
    elif trend == 'down':
        # 우하향: 10000 -> 5000
        close = np.linspace(start_price, start_price * 0.5, periods)
    else:
        # 횡보: 10000원 부근 진동
        close = np.linspace(start_price, start_price, periods)

    # 노이즈 추가
    noise = np.random.normal(0, start_price * 0.005, periods)
    close = close + noise
    
    df = pd.DataFrame({
        'date': dates,
        'close': close,
        'open': close * 0.99,
        'high': close * 1.02,
        'low': close * 0.98,
        'volume': np.random.randint(1000, 10000, periods)
    })
    return df

@pytest.fixture
def sample_uptrend_df():
    """상승장 데이터 (100일)"""
    return create_mock_df(trend='up')

@pytest.fixture
def sample_downtrend_df():
    """하락장 데이터 (100일)"""
    return create_mock_df(trend='down')

@pytest.fixture
def sample_sideways_df():
    """횡보장 데이터 (100일)"""
    return create_mock_df(trend='sideways')