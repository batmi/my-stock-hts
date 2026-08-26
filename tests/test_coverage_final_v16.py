import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
from datetime import datetime

import api
import config
from modules import analysis, theme_analysis, account, market, auto_trade
from modules.telegram_bot import TelegramCommander

@pytest.fixture(autouse=True)
def setup_env():
    TelegramCommander._instance = None
    auto_trade.AutoTrader._instance = None
    yield

# ==========================================================
# 1. theme_analysis.py (매크로 지표 평가 텍스트 분기)
# ==========================================================
def test_evaluate_market_indicator_branches():
    """수십 줄의 매크로 지표 임계값 텍스트 분기 커버리지 타격"""
    # 각 지표의 모든 분기를 최소 한 번씩 타격
    assert "재정 적자 우려" in theme_analysis.evaluate_market_indicator("미국채 30년물 금리", 6.0)
    assert "장기 인플레 우려" in theme_analysis.evaluate_market_indicator("미국채 30년물 금리", 5.2)
    assert "구조적 고금리 안착 경계" in theme_analysis.evaluate_market_indicator("미국채 30년물 금리", 5.0)
    assert "디플레이션 우려" in theme_analysis.evaluate_market_indicator("미국채 30년물 금리", 3.0)
    
    assert "에너지 쇼크" in theme_analysis.evaluate_market_indicator("가솔린 RBOB", 5.0)
    assert "시스템 위기" in theme_analysis.evaluate_market_indicator("가솔린 RBOB", 1.0)


def test_diesel_ulsd_bands():
    """디젤 ULSD 밴드 (2026-08-10 추가).

    [밴드 근거] 임의 숫자가 아니라 가솔린 RBOB 밴드가 놓인 **백분위**를 ULSD 분포에
    옮긴 값이다(15년 3,771봉, 2011-08~2026-08):
        RBOB 4.00(99.7%)→4.40 / 3.20(95.4%)→3.70 / 2.60(69.8%)→2.85
             2.10(49.5%)→2.25 / 1.60(21.3%)→1.70
    두 상품은 가격대가 달라(ULSD/RBOB 중앙 비율 1.055) 가솔린 숫자를 그대로 쓰면
    색이 거짓 신호를 낸다 — 실제로 현재가(ULSD 3.97 / RBOB 3.01)는 가솔린 기준으로는
    같은 '주황'이지만, 백분위로 보면 98% 대 91%로 다른 국면이다.
    """
    from modules import theme_analysis as ta
    assert "쇼크" in ta.evaluate_market_indicator("디젤 ULSD", 4.5)
    assert "임계점" in ta.evaluate_market_indicator("디젤 ULSD", 4.0)
    assert "골디락스" in ta.evaluate_market_indicator("디젤 ULSD", 2.5)
    assert "급랭" in ta.evaluate_market_indicator("디젤 ULSD", 1.5)


def test_diesel_is_wired_everywhere_gasoline_is():
    """가솔린이 등록된 자리마다 디젤도 있어야 한다.

    한 곳이라도 빠지면 조회는 되는데 표에 안 나오거나, 표에는 나오는데 선물 표기가
    빠지는 식으로 조용히 어긋난다.
    """
    import config
    from modules import market
    assert market.INDICES_MAP.get("디젤 ULSD") == "HO=F"
    assert "디젤 ULSD" in config.INDICES_GROUPS["6"]["indices"]

    # 함수 안의 지역 리스트(us_futures·is_futures)는 런타임으로 못 잡으므로 소스로 본다.
    #  이름 목록 줄은 '가솔린 RBOB'와 '천연가스'가 한 줄에 같이 있다 —
    #  티커 매핑 줄(("가솔린 RBOB", "RB=F"))에는 천연가스가 없어 이걸로 구분한다.
    import inspect
    lists = [ln for ln in inspect.getsource(market).splitlines()
             if "가솔린 RBOB" in ln and "천연가스" in ln]
    assert len(lists) >= 2, f"검사 대상 목록을 찾지 못했다({len(lists)}개) — 구조가 바뀌었다"
    for ln in lists:
        assert "디젤 ULSD" in ln, f"이 목록에 디젤이 빠졌다: {ln.strip()[:90]}"
    
    assert "물가 비상" in theme_analysis.evaluate_market_indicator("천연가스", 5.0)
    assert "수급 타이트" in theme_analysis.evaluate_market_indicator("천연가스", 3.5)
    
    assert "식량 안보 위기" in theme_analysis.evaluate_market_indicator("밀", 1000)
    assert "식량 인플레" in theme_analysis.evaluate_market_indicator("밀", 700)
    
    assert "안정/중립" in theme_analysis.evaluate_market_indicator("달러인덱스", 95)
    assert "초강세 원화" in theme_analysis.evaluate_market_indicator("달러환율", 1000)
    
    assert "위험" in theme_analysis.evaluate_market_indicator("VIX (변동성)", 25)
    assert "경계" in theme_analysis.evaluate_market_indicator("VIX (변동성)", 18)
    
    # [변경] 섹터 지수의 자산별 낙폭 문구는 폐지 — 공통 낙폭 문구를 사용한다(지수명 색상은 국면 룰로 통일)
    assert "침체/약세장 진입" in theme_analysis.evaluate_market_indicator("SOX (반도체)", 100, yh_rate=-30.0)
    assert "침체/약세장 진입" in theme_analysis.evaluate_market_indicator("일반지수", 100, yh_rate=-25.0)

# ==========================================================
# 2. telegram_bot.py (명령어 포맷팅 및 서브루틴 분기)
# ==========================================================
@patch('modules.telegram_bot.db_manager.db.get_all_stock_strategies')
def test_telegram_cmd_rules_and_config(mock_rules):
    """/rules 및 /config 명령어 문자열 생성 로직 커버리지"""
    cmd = TelegramCommander()
    
    # /config 테스트
    config_str = cmd._cmd_config([])
    assert "매매 전략 설정" in config_str
    assert "적응형 임계값" in config_str
    
    # /rules 데이터 있음 테스트 (가중치 JSON 파싱 포함)
    mock_rules.return_value = [{
        'code': '005930', 'name': '삼성전자', 'buy_score': 8.0, 'take_profit': 10.0,
        'stop_loss': -5.0, 'ts_activation': 5.0, 'ts_callback': 2.0, 
        'weights': '{"TREND": 5.0, "MOMENTUM": 2.0}', 'use_atr_stop': 1, 'memo': '메모테스트'
    }]
    
    rules_str = cmd._cmd_rules(["삼성전자"]) # 특정 종목 필터링
    assert "삼성전자" in rules_str
    assert "ATR" in rules_str
    
    # /rules 데이터 없음 테스트
    mock_rules.return_value = []
    assert "없습니다" in cmd._cmd_rules([])

@patch('api.get_current_price_data', return_value={'rt_cd': '0', 'output': {'last': '150', 'rate': '-1.5', 'diff': '2.0'}})
@patch('api.get_chart_data')
def test_telegram_analyze_overseas(mock_chart, mock_cp):
    """/analyze 해외 주식 분석 시그널 포맷팅 커버리지"""
    cmd = TelegramCommander()
    
    # 지표를 풍부하게 주기 위해 더미 데이터 생성
    mock_chart.return_value = pd.DataFrame({
        'close': [150]*30, 'high': [155]*30, 'low': [145]*30, 'open': [150]*30, 'volume': [1000]*30
    })
    
    with patch('modules.telegram_bot.theme_analysis.analyze_stock_with_gemini', return_value="AI Reply") as mock_gemini:
        res = cmd._cmd_analyze(["AAPL"])
        assert "AI Reply" in res
        
        # 해외 주식 달러 포맷 확인 (AI에게 전달된 기술적 지표 문자열 검증)
        args, kwargs = mock_gemini.call_args
        assert "$" in args[2]

# ==========================================================
# 3. api.py (시봉/분봉 파싱 및 해외 Fallback)
# ==========================================================
@patch('api.fetch_yfinance_data')
def test_api_intraday_chart_yfinance_fallback(mock_yf):
    """해외 지수의 분봉 조회 시 yfinance Fallback 및 시간대 변환 로직 커버리지"""
    # yfinance 분봉 데이터 모킹
    dates = pd.date_range(start="2023-01-01", periods=500, tz='UTC') # 390개 초과시켜 슬라이싱 유도
    df = pd.DataFrame({
        'Datetime': dates, 'Close': [100]*500, 'High': [100]*500, 'Low': [100]*500, 'Open': [100]*500, 'Volume': [100]*500
    })
    mock_yf.return_value = df
    
    res = api._get_intraday_chart_data("^IXIC", is_overseas=True)
    assert len(res) == 390 # 최근 390개만 슬라이싱되는지 확인
    assert 'date' in res.columns

@patch('api.fetch_yfinance_data')
def test_api_hourly_chart_parsing(mock_yf):
    """yfinance 시봉 조회 및 MultiIndex/Tuple 컬럼 평탄화 커버리지"""
    # 튜플 형태의 MultiIndex 컬럼 모킹
    cols = pd.MultiIndex.from_tuples([('Close', 'AAPL'), ('Open', 'AAPL'), ('High', 'AAPL'), ('Low', 'AAPL'), ('Volume', 'AAPL')])
    dates = pd.date_range(start="2023-01-01", periods=10)
    df = pd.DataFrame(100, index=dates, columns=cols)
    mock_yf.return_value = df
    
    res = api._get_hourly_chart_data("AAPL", is_overseas=True)
    assert not res.empty
    assert 'close' in res.columns

# ==========================================================
# 4. analysis.py (테이블 렌더링 색상 매핑 분기)
# ==========================================================
@patch('api.get_current_price_data')
@patch('api.get_chart_data')
@patch('api.fetch_overseas_detail_price')
def test_print_table_worker_overseas_etf(mock_detail, mock_chart, mock_cp):
    """해외 ETF 테이블 렌더링 시 상장주수(Shar) 등 전용 포맷팅 커버리지"""
    # 해외 ETF 디테일 모킹
    mock_detail.return_value = {'shar': '1500000', 'h52p': '200', 'l52p': '100', 'last': '150'}
    mock_cp.return_value = {'rt_cd': '0', 'output': {'last': '150', 'rate': '-5.0', 'diff': '2.0'}}
    
    # 차트: 120일선 하락 확인을 위해 150일치 생성
    closes = np.linspace(200, 100, 150) # 하락 추세
    mock_chart.return_value = pd.DataFrame({
        'close': closes, 'high': closes+5, 'low': closes-5, 'open': closes, 'volume': [1000]*150
    })
    
    item = ("QQQ", "QQQ")
    # is_overseas=True, is_us_etf_context=True("ETF" in title)
    row_data, is_res, is_cust, is_mem, is_rsv, is_failed = analysis._print_table_worker(item, "미국 ETF", True, False, {}, {}, None, set(), set())
    
    assert "1.5M" in row_data[-1] # 상장주수 1.5M 표기 확인
    assert "[blue]" in row_data[4] # 하락률 파란색 확인

# ==========================================================
# 6. account.py (체결 내역 동기화 파싱)
# ==========================================================
@patch('api.get_today_history', return_value={'rt_cd': '1'})
@patch('api.get_overseas_today_history')
def test_sync_today_trades_overseas(mock_ovs_hist, mock_dom_hist):
    """해외주식 체결 내역 파싱 및 DB 동기화 커버리지"""
    config.session.cano = "123"
    config.session.acnt_prdt_cd = "01"
    # 자동매매 계좌가 갈려 있으면 같은 응답을 두 계좌에서 두 번 읽는다(단일계좌로 고정).
    config.session.auto_cano = "123"
    config.session.auto_acnt_prdt_cd = "01"
    
    # 해외 체결 응답 모킹
    mock_ovs_hist.return_value = {
        'rt_cd': '0',
        'output': [{'odno': '999', 'ft_ccld_unpr3': '150.50', 'ft_ccld_qty': '10', 'ovrs_item_name': 'Apple', 'pdno': 'AAPL', 'sll_buy_dvsn_cd': '02'}]
    }
    
    with patch('modules.db_manager.db.check_trade_exists', return_value=False), \
         patch('modules.db_manager.db.insert_trade') as mock_insert:
        
        count = account.sync_today_trades()
        assert count == 1
        mock_insert.assert_called_once()