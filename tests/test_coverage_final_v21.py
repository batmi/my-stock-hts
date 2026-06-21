import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import config
import utils
from modules import settings, theme_analysis, backtest, manage, market, db_manager, account, telegram_bot
from modules.auto_trade import AutoTrader, RiskManager, ConclusionMonitor

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

# ---------------------------------------------------------
# 1. settings.py 커버리지 보완 (설정 프리셋 및 초기화 로직)
# ---------------------------------------------------------
def test_settings_apply_presets():
    """시장 국면별 전략 프리셋 적용 로직 검증"""
    # Bull 적용
    msg = settings.apply_strategy_preset("bull", interactive=False)
    assert "강세장" in msg
    assert config.settings.SYSTEM_INVEST_PER_STOCK == settings.DEFAULT_PRESETS["bull"]["SYSTEM_INVEST_PER_STOCK"]

    # Bear 적용
    msg = settings.apply_strategy_preset("bear", interactive=False)
    assert "약세장" in msg
    assert config.settings.SYSTEM_INVEST_PER_STOCK == settings.DEFAULT_PRESETS["bear"]["SYSTEM_INVEST_PER_STOCK"]
    
    # Sideways 적용
    msg = settings.apply_strategy_preset("sideways", interactive=False)
    assert "횡보장" in msg
    
    # Default 적용
    msg = settings.apply_strategy_preset("default", interactive=False)
    assert "기본설정" in msg

def test_settings_reset_to_default():
    """전체 설정 기본값 초기화 검증"""
    res = settings.reset_to_default(interactive=False)
    assert "초기화" in res

def test_custom_presets_save_load(tmp_path):
    """커스텀 프리셋 저장 및 로드 검증"""
    test_file = tmp_path / "presets.json"
    
    # 임시 파일을 config.PRESETS_FILE 경로로 사용하도록 패치
    with patch('config.PRESETS_FILE', str(test_file)):
        # 저장 테스트
        test_data = {"bull": {"BUY_SCORE": 9.9}}
        settings.save_custom_presets(test_data)
        
        # 로드 테스트
        loaded = settings.load_custom_presets()
        assert loaded == test_data
        
        # get_preset_values 호출 시 기본 프리셋과 병합되는지 확인
        merged = settings.get_preset_values("bull")
        assert merged["BUY_SCORE"] == 9.9
        
        # 설정하지 않은 값은 시스템 기본값을 유지하는지 확인
        assert merged["BUY_RSI_MAX"] == settings.DEFAULT_PRESETS["bull"]["BUY_RSI_MAX"]

@patch('modules.settings.utils.show_menu')
@patch('modules.settings.Prompt.ask')
@patch('modules.settings.save_custom_presets')
def test_edit_strategy_preset_menu_reset(mock_save, mock_ask, mock_menu):
    """커스텀 프리셋 전체 초기화 동작 검증"""
    # 0(전체 초기화) -> q(종료)
    mock_menu.side_effect = ["0", "q"]
    mock_ask.return_value = "y" # 초기화 확인
    
    with patch('config.console.print'), patch('modules.settings.utils.pause'):
        settings.edit_strategy_preset_menu()
        
    # 빈 딕셔너리로 저장하여 초기화하는지 검증
    mock_save.assert_called_once_with({})

@patch('modules.settings.utils.show_menu')
@patch('modules.settings.apply_strategy_preset')
def test_select_strategy_preset(mock_apply, mock_menu):
    """전략 프리셋 선택 메뉴 동작 검증"""
    mock_menu.side_effect = ["1", "2", "3", "9", "q"]
    
    with patch('modules.settings.utils.pause'):
        settings.select_strategy_preset()
        settings.select_strategy_preset()
        settings.select_strategy_preset()
        settings.select_strategy_preset()
        settings.select_strategy_preset()
        
    assert mock_apply.call_count == 4
    mock_apply.assert_any_call("bull")
    mock_apply.assert_any_call("bear")
    mock_apply.assert_any_call("sideways")
    mock_apply.assert_any_call("default")

# ---------------------------------------------------------
# 2. theme_analysis.py 커버리지 보완 (크롤링 파싱 및 평가)
# ---------------------------------------------------------
def test_evaluate_market_indicator():
    """거시 경제 지표 평가 텍스트 반환 검증"""
    assert theme_analysis.evaluate_market_indicator("미국채 10년물 금리", 5.5) == "시스템 위기/Valuation 붕괴"
    assert theme_analysis.evaluate_market_indicator("미국채 10년물 금리", 4.0) == "골디락스/적정 성장"
    assert theme_analysis.evaluate_market_indicator("WTI 원유", 130) == "에너지 쇼크/스태그플레이션"
    assert theme_analysis.evaluate_market_indicator("VIX (변동성)", 14) == "안정/골디락스장"
    assert theme_analysis.evaluate_market_indicator("달러인덱스", 95) == "안정/중립(골디락스)"

@patch('modules.theme_analysis.requests.get')
def test_fetch_naver_themes(mock_get):
    """네이버 테마 크롤링 파싱 검증"""
    mock_resp = MagicMock()
    mock_resp.content = b'<html><table class="type_1"><tr><td><a href="/link">Test Theme</a></td><td>1.5%</td><td>2.0%</td><td>Dummy</td></tr></table></html>'
    mock_get.return_value = mock_resp
    themes = theme_analysis.fetch_naver_themes()
    assert len(themes) > 0
    assert themes[0]['name'] == 'Test Theme'
    assert themes[0]['rate'] == 1.5

@patch('modules.theme_analysis.requests.get')
def test_fetch_realtime_news(mock_get):
    """구글 뉴스 RSS 크롤링 파싱 검증"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '''<rss><channel>
        <item><title>Test News Title</title><link>http://test.com</link><source>Test Source</source><pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate></item>
    </channel></rss>'''
    mock_get.return_value = mock_resp
    news = theme_analysis.fetch_realtime_news("Samsung")
    assert "Test News Title" in news
    assert "http://test.com" in news

# ---------------------------------------------------------
# 3. db_manager.py 커버리지 보완 (트레일링 스탑, 반익절)
# ---------------------------------------------------------
def test_db_trailing_stop_and_half_tp():
    """트레일링 스탑 및 반익절 캐시 DB 작업 검증"""
    db = db_manager.db
    test_code = "TEST_001"
    
    # 트레일링 스탑
    db.update_highest_price(test_code, 80000)
    assert db.get_highest_price(test_code) == 80000
    db.delete_trailing_stop(test_code)
    assert db.get_highest_price(test_code) is None
    
    # 반익절
    db.insert_half_tp(test_code)
    assert test_code in db.get_all_half_tp()
    db.delete_half_tp(test_code)
    assert test_code not in db.get_all_half_tp()
    
    # 오래된 데이터 정리 및 최적화
    db.cleanup_old_data(365)
    db.run_vacuum()

# ---------------------------------------------------------
# 4. manage.py 커버리지 보완 (관심종목 출력)
# ---------------------------------------------------------
def test_manage_view_watchlist():
    """관심종목 리스트 출력 로직 무결성 검증"""
    config.session.stock_data['stocks_kr'] = [{'code': '005930', 'name': 'Samsung'}]
    with patch('config.console.print'):
        manage.view_watchlist()

# ---------------------------------------------------------
# 5. backtest.py 커버리지 보완 (몬테카를로 시뮬레이션)
# ---------------------------------------------------------
@patch('modules.backtest.simulate_strategy')
@patch('rich.prompt.Prompt.ask', return_value='n')
def test_run_monte_carlo_simulation(mock_ask, mock_sim):
    """몬테카를로 백테스팅 래퍼 로직 및 통계 출력 검증"""
    df = pd.DataFrame({
        'date': ['20231010', '20231011'],
        'close': [100, 105], 'open': [99, 101], 'high': [106, 107], 'low': [98, 100],
        'volume': [1000, 1200]
    })
    
    mock_sim.return_value = {
        'trades': [], 'final_asset': 1050000, 'total_return': 5.0, 'mdd': -1.5,
        'win_trades': 1, 'loss_trades': 0, 'gross_profit': 50000, 'gross_loss': 0,
        'daily_assets': [1000000, 1050000], 'max_score_observed': 8.5, 'score_8_count': 2,
        'missed_caution_count': 0, 'missed_danger_count': 0, 'missed_trades': []
    }
    
    with patch('config.console.print'):
        # Exception이나 에러 없이 정상적으로 1000번 루프를 생성하고 통계를 내는지 확인
        backtest.run_monte_carlo_simulation(df, 0, 1000000, 7.5, 65, False, -7.0, 20.0, 75.0, 5.0, 10.0, 3.0, 10, True, 2.0, True)

# ---------------------------------------------------------
# 6. auto_trade.py 커버리지 보완 (RiskManager, Monitor)
# ---------------------------------------------------------
def test_risk_manager_allocate():
    """ATR 및 리스크 기반 자산 배분 비중 조절 로직 검증"""
    trader = AutoTrader()
    trader.initial_asset = 10000000
    rm = RiskManager(trader)
    
    # 가용자산 1천만원, 투자비중 20%, 손절률 -5%, ATR 변동성 조절 반영 시 예산 도출 확인
    amt = rm.allocate_budget(10000000, 0.2, stop_loss_rate=-5.0, atr=1000, current_price=50000)
    assert amt > 0
