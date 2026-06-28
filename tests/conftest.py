import sys
import os
import pytest
import pandas as pd
import numpy as np

# 프로젝트 루트 경로를 sys.path에 추가하여 모듈 임포트 가능하게 설정
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import api # [추가] 외부 API 차단용
from modules import db_manager # [추가] DB 매니저 임포트
from modules import analysis # [추가] 지수 조회 차단용
from modules.auto_trade import AutoTrader, ConclusionMonitor
from modules.telegram_bot import TelegramCommander

@pytest.fixture(scope="session", autouse=True)
def setup_config():
    """테스트 세션 동안 사용할 설정 초기화 (모의투자 모드 강제)"""
    # 테스트 중 실수로 실전 API가 호출되지 않도록 안전장치
    config.session.initialize(mode="1")


@pytest.fixture(autouse=True)
def isolate_session_state():
    """[격리] config.session은 전역 공유 객체이므로, 개별 테스트가 모드/키를
    바꿔도(예: is_simulation=False) 다음 테스트로 누수되지 않도록 매 테스트 후
    상태를 원복한다.

    누수를 방치하면 setup_config가 강제한 모의투자 모드가 풀려, 시세/지수 조회가
    실전 도메인(:9443)으로 나가 EGW00304(고객식별키 무효) 등이 발생할 수 있다.
    """
    snapshot = dict(config.session.__dict__)
    yield
    config.session.__dict__.clear()
    config.session.__dict__.update(snapshot)


def _mock_index_chart_df(periods=60):
    """KIS 지수 차트(get_domestic_index_chart)의 원시 응답 형태를 흉내 낸 더미 데이터.

    get_domestic_index_data가 이 컬럼들을 rename/숫자변환하므로 원시 컬럼명을 유지한다.
    충분한 길이(>= REGIME_MA_PERIOD)를 제공해 yfinance Fallback(실 네트워크)까지 차단한다.
    """
    dates = pd.date_range(end="2024-01-01", periods=periods).strftime("%Y%m%d")
    base = np.linspace(2400.0, 2450.0, periods)
    return pd.DataFrame({
        'stck_bsop_date': dates,
        'bstp_nmix_prpr': base,
        'bstp_nmix_oprc': base * 0.999,
        'bstp_nmix_hgpr': base * 1.005,
        'bstp_nmix_lwpr': base * 0.995,
        'acml_vol': np.random.randint(1000, 5000, periods),
    })


@pytest.fixture(autouse=True)
def block_external_market_api(request, monkeypatch):
    """[격리] 분석 워커(ThreadPoolExecutor) 등에서 지수 조회가 mock 없이 실행되면
    실제 한투 서버로 네트워크 요청이 나간다. 하위 진입점인 get_domestic_index_chart를
    더미 데이터로 대체해 실 호출과 yfinance Fallback을 모두 차단한다.

    개별 테스트가 직접 patch하면(예: test_strategy) 그 patch가 우선 적용되고
    종료 시 이 기본값으로 복원되므로 충돌하지 않는다.
    단, get_domestic_index_chart 자체의 로직을 검증하는 테스트는
    @pytest.mark.real_index_chart 로 이 mock을 비활성화한다.
    """
    if request.node.get_closest_marker("real_index_chart"):
        return
    monkeypatch.setattr(api, "get_domestic_index_chart",
                        lambda *a, **k: _mock_index_chart_df(), raising=False)

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
    # [추가] send_telegram_message()는 ENABLE_TELEGRAM을 보지 않고 토큰/챗ID 유무만 확인하므로,
    #        운영 .htsrc의 토큰이 환경변수로 설정돼 있으면 테스트가 실제 텔레그램으로 전송하며
    #        네트워크 타임아웃 hang을 유발한다. 토큰을 비워 early-return 시킨다.
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "", raising=False)

@pytest.fixture(autouse=True)
def cleanup_global_db_connection():
    """
    각 테스트 실행 후 전역 DBManager가 생성한 '모든 스레드'의 연결을 닫습니다.
    백그라운드 워커 스레드가 만든 thread-local 연결까지 정리하여
    ResourceWarning: unclosed database 를 방지합니다.
    """
    yield

    # 테스트 종료 후 정리 (전체 스레드 연결 일괄 종료)
    real_db = getattr(db_manager.db, '_real_db', db_manager.db)
    try:
        real_db.close_all_connections()
    except Exception:
        pass

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