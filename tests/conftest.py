import sys
import os
import pytest
import pandas as pd
import numpy as np

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from modules import db_manager # [추가] DB 매니저 임포트

@pytest.fixture(scope="session", autouse=True)
def setup_config():
    """테스트 세션 동안 사용할 설정 초기화 (모의투자 모드 강제)"""
    # 테스트 중 실수로 실전 API가 호출되지 않도록 안전장치
    config.session.initialize(mode="1")

@pytest.fixture(autouse=True)
def cleanup_global_db_connection():
    """
    각 테스트 실행 후 전역 DBManager의 스레드 로컬 연결을 닫습니다.
    ResourceWarning: unclosed database 방지
    """
    yield
    
    # 테스트 종료 후 정리
    if hasattr(db_manager.db, 'local') and hasattr(db_manager.db.local, 'conn'):
        if db_manager.db.local.conn:
            try:
                db_manager.db.local.conn.close()
            except Exception:
                pass
            del db_manager.db.local.conn

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