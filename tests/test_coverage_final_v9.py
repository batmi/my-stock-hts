# tests/test_coverage_final_v9.py
import pytest
from unittest.mock import patch, MagicMock, ANY
import pandas as pd
import numpy as np
from modules import backtest
import config
import api
from modules import db_manager

@pytest.fixture(autouse=True)
def cleanup_db_connection():
    yield
    db_manager.db.close_connection()

@pytest.fixture
def sample_df():
    dates = pd.date_range(start="2023-01-01", periods=100)
    df = pd.DataFrame({
        'date': dates.strftime("%Y%m%d"),
        'close': np.linspace(10000, 11000, 100), # Uptrend
        'open': np.linspace(10000, 11000, 100),
        'high': np.linspace(10100, 11100, 100),
        'low': np.linspace(9900, 10900, 100),
        'volume': [1000] * 100,
        # Perfect Uptrend Alignment: Price > 20 > 60 > 120
        'EMA20': np.linspace(9900, 10900, 100),
        'EMA60': np.linspace(9800, 10800, 100),
        'EMA120': np.linspace(9700, 10700, 100),
        'SAR': np.linspace(9000, 10000, 100),
        'RSI': [60.0] * 100,
        'ADX': [30.0] * 100,
        'CCI': [150.0] * 100,
        'OBV': [2000.0] * 100,
        'OBV_MA': [1000.0] * 100,
        'ATR': [100.0] * 100,
        'MACD': [50.0] * 100,
        'MACD_Signal': [40.0] * 100,
        'PLUS_DI': [30.0] * 100,
        'MINUS_DI': [10.0] * 100
    })
    return df

@patch('modules.backtest.api.fetch_yfinance_data')
@patch('modules.backtest.api.get_chart_data')
@patch('modules.krx_daily.get_daily', return_value=None)
def test_get_backtest_data_fallback(mock_krx, mock_get_chart, mock_yf):
    """yfinance 실패 시 KIS API Fallback 테스트"""
    mock_yf.side_effect = Exception("YF Error")
    mock_get_chart.return_value = pd.DataFrame({'close': [100]})
    
    df = backtest.get_backtest_data("005930", False, 100)
    
    assert not df.empty
    mock_get_chart.assert_called_once()

def test_simulate_strategy_atr_stop(sample_df):
    """ATR 기반 손절 로직 테스트"""
    sample_df.loc[50, 'close'] = 9700.0 # -3% Drop
    sample_df.loc[50, 'high'] = 9700.0 # Ensure high doesn't trigger TS
    # ATR=100, Mult=2.0 -> Stop=200 (2%)
    # Price 10000 -> Stop Price 9800
    # 9700 < 9800 -> Sell
    
    # 본전청산(BEP)이 ATR손절보다 먼저 발동하지 않도록 BEP를 끄고 ATR손절을 격리 검증.
    # [주의] BEP 비활성화는 BREAK_EVEN_PROFIT_RATE로 할 수 없다 — ATR 손절 적용 시
    #   발동 기준이 손절폭(1R)으로 덮어써지기 때문(backtest.py: bep_activation = abs(sl_rate_to_use)).
    #   손절선 상향 목표치(BREAK_EVEN_STOP_RATE)를 낮춰 `sl < bep_stop` 조건을 깨야 한다.
    with patch.dict(config.SELL_STRATEGY, {"USE_ATR_STOP": True, "ATR_STOP_MULTIPLIER": 2.0, "STOP_LOSS_RATE": -10.0, "TIME_STOP_USE": False, "BREAK_EVEN_STOP_RATE": -999.0}):
        res = backtest.simulate_strategy(
            sample_df, sample_df.iloc[0], 10000000, 
            buy_score_limit=6.0, buy_rsi_limit=70, is_overseas=False
        )
        
        trades = res['trades']
        sell_trades = [t for t in trades if t['type'].startswith("매도")]
        assert len(sell_trades) > 0
        assert "ATR손절" in sell_trades[0]['type']

def test_simulate_strategy_missed_trades(sample_df):
    """매수 보류(Missed Trades) 로직 테스트"""
    sample_df['RSI'] = 80.0 # Overbought
    
    res = backtest.simulate_strategy(
        sample_df, sample_df.iloc[0], 10000000, 
        buy_score_limit=5.0,
        buy_rsi_limit=70,
        is_overseas=False
    )
    
    assert res['win_trades'] == 0
    assert len(res['missed_trades']) > 0
    assert "RSI" in res['missed_trades'][0]['reason']

def test_simulate_strategy_mr_grace_loss(sample_df):
    """역매수 유예 기간 중 허용 손실률(MR_GRACE_LOSS_RATE) 이탈 시 매도 로직 테스트"""
    # 시뮬레이션: 역매수 조건으로 진입 후, 유예 기간(10일) 내에 손실이 한도(-5.0%)를 초과하는 상황
    
    # 1. 초기 매수 조건을 역매수로 조작하기 위해 상태 분류 모킹
    with patch('modules.backtest.calculate_daily_status') as mock_calc:
        # 항상 역매수(점수 4.0) 상태로 반환하도록 설정
        mock_calc.return_value = (4.0, 4.0, True, "역매수", "낙폭과대")
        
        # 2. 주가가 매수가(10000)에서 -6% 하락한 9400으로 떨어짐 (2일차)
        sample_df.loc[1, 'close'] = 9400.0
        sample_df.loc[1, 'high'] = 9400.0
        sample_df.loc[1, 'low'] = 9300.0
        
        with patch.dict(config.SELL_STRATEGY, {
            "MR_GRACE_LOSS_RATE": -5.0, 
            "TIME_STOP_DAYS": 10, 
            "SELL_SCORE": 5.0,
            "USE_ATR_STOP": False,      # 일반 ATR 손절이 먼저 발동하는 것 방지
            "STOP_LOSS_RATE": -10.0     # 고정 손절선도 -6%보다 낮게 설정
        }):
            res = backtest.simulate_strategy(
                sample_df.head(5).copy(), sample_df.iloc[0], 10000000, 
                buy_score_limit=6.0, buy_rsi_limit=70, is_overseas=False
            )
            
            trades = res['trades']
            sell_trades = [t for t in trades if "매도" in t['type']]
            
            # 유예 기간(10일 이내)이더라도 손실률(-6.0%)이 허용 한도(-5.0%)를 넘었으므로 '점수하락' 사유로 즉시 손절되어야 함
            assert len(sell_trades) > 0
            assert "점수하락" in sell_trades[0]['type']

@patch('rich.prompt.Prompt.ask', return_value='n')
@patch('modules.backtest.simulate_strategy')
@patch('config.console.print')
def test_run_monte_carlo_simulation_logic(mock_print, mock_sim, mock_ask, sample_df):
    """몬테카를로 시뮬레이션 집계 로직 테스트"""
    mock_res = {
        "trades": [{'type': '매도', 'profit': 5.0, 'days': 10}],
        "final_asset": 11000000,
        "total_return": 10.0,
        "mdd": -5.0,
        "win_trades": 1,
        "loss_trades": 0,
        "gross_profit": 1000000,
        "gross_loss": 0,
        "daily_assets": [10000000, 11000000],
        "max_score_observed": 9.0,
        "score_8_count": 5,
        "missed_caution_count": 0,
        "missed_danger_count": 0
    }
    mock_sim.return_value = mock_res
    
    backtest.run_monte_carlo_simulation(
        sample_df, 0, 10000000,
        8.0, 70, False, -7.0, 30.0, 75, 5.0, 10.0, 3.0, 5, True, 2.0, False
    )
    
    assert mock_sim.call_count == 1000
    assert mock_print.call_count > 0

@patch('modules.backtest.utils.validate_and_confirm_stock', return_value=True)
@patch('rich.prompt.Prompt.ask')
@patch('modules.backtest.get_backtest_data')
@patch('modules.backtest.api.get_stock_name_by_code', return_value="TestStock")
@patch('config.console.print')
@patch('config.console.status')
def test_run_backtest_full_flow(mock_status, mock_print, mock_name, mock_get_data, mock_ask, mock_val, sample_df):
    """백테스팅 전체 흐름 (단일 실행 + 최적화) 테스트"""
    mock_get_data.return_value = sample_df
    
    # 6(Manual) -> Code -> n(Preset) -> n(No settings change) -> 1(Single Run) -> n(AI) -> 메인화면(q)
    mock_ask.side_effect = ["6", "005930", "n", "n", "1", "n", "q"]
    
    mock_status.return_value.__enter__.return_value = MagicMock()
    
    backtest.run_backtest()
    
    mock_get_data.assert_called()
    # Check for optimization output
    from rich.table import Table
    found = False
    for call in mock_print.call_args_list:
        args, _ = call
        if args and isinstance(args[0], Table) and args[0].title and "매수 점수 최적화" in args[0].title:
            found = True
    assert found

@patch('modules.backtest.utils.validate_and_confirm_stock', return_value=True)
@patch('rich.prompt.Prompt.ask')
@patch('modules.backtest.get_backtest_data')
@patch('modules.backtest.api.get_stock_name_by_code', return_value="TestStock")
@patch('config.console.print')
@patch('config.console.status')
def test_run_backtest_settings_change(mock_status, mock_print, mock_name, mock_get_data, mock_ask, mock_val, sample_df):
    """백테스팅 조건 변경 — 열린 항목은 입력이 반영되고, 잠긴 항목은 설정값이 유지된다.

    설정 메뉴에서 숨긴 다이얼(ANTI_TREND/BACKTESTED_HIDDEN_KEYS)은 백테스트 조건 변경에서도
    묻지 않는다. 한쪽만 잠그면 메뉴에서 막은 값을 백테스트로 바꿔보고 그 결과로 판단하게 되어
    잠금이 무의미해지기 때문이다. 여기서는 그 대칭이 실제로 지켜지는지 본다.
    """
    mock_get_data.return_value = sample_df

    # run_backtest 대화 흐름 순서대로 응답 (프롬프트가 늘거나 잠금 목록이 바뀌면 함께 갱신할 것)
    mock_ask.side_effect = [
        "6",        # 1. 메뉴 선택 (단일 종목 백테스트)
        "005930",   # 2. 종목코드
        "y",        # 3. 시뮬레이션 조건 변경?   (시장 상황 프리셋 질문은 폐지되어 없음)
        "100",      # 4. 분석 기간(일)
        "9.0",      # 5. 매수 기준 점수          ← 열린 항목: 반영되어야 함
        "60",       # 6. 매수 허용 RSI 상한
        "10.0",     # 7. TS 발동 수익률(%)
        "y",        # 8. 시장 필터 사용?
        "n",        # 9. 피라미딩 차수 변경?
        "n",        # 10. 가중치 변경?
        "1",        # 11. 실행 모드 선택 (1=단일 실행)
        "n",        # 12. AI 성과 진단?
        "q",        # 13. 메뉴 선택 (종료)
    ]

    mock_status.return_value.__enter__.return_value = MagicMock()

    # [주의] 시장 필터를 켜면 run_backtest가 지수(^KS11)를 실제로 조회한다. 테스트에서 네트워크를
    #  타면 느릴 뿐 아니라 지수 차트 캐시가 채워져 다른 테스트의 '조회 실패' 전제를 깨뜨린다.
    with patch('modules.backtest.simulate_strategy') as mock_sim, \
         patch('modules.backtest.prepare_market_filter', return_value=None):
        mock_sim.return_value = {
            "trades": [], "final_asset": 10000000, "total_return": 0, "mdd": 0,
            "win_trades": 0, "loss_trades": 0, "gross_profit": 0, "gross_loss": 0,
            "daily_assets": [], "max_score_observed": 0, "score_8_count": 0
        }
        backtest.run_backtest()

        args, kwargs = mock_sim.call_args_list[0]
        # 열린 항목: 입력값이 그대로 전달된다
        assert args[3] == 9.0
        # 잠긴 항목(STOP_LOSS_RATE): 묻지 않았으므로 설정값이 유지된다
        assert kwargs['stop_loss_rate'] == config.SELL_STRATEGY["STOP_LOSS_RATE"]


def test_backtest_prompt_locks_match_settings_menu():
    """백테스트 조건 변경의 잠금 목록은 설정 메뉴의 숨김 목록과 같은 소스를 쓴다."""
    from modules import settings

    locked = backtest._locked_setting_keys()
    assert settings.ANTI_TREND_HIDDEN_KEYS <= locked
    assert settings.BACKTESTED_HIDDEN_KEYS <= locked
    # 추세추종 보호 대상은 반드시 잠겨 있어야 한다
    for key in ("TAKE_PROFIT_RATE", "HALF_TAKE_PROFIT_USE", "TAKE_PROFIT_RSI", "SELL_SCORE",
                "TIME_STOP_DAYS", "USE_ATR_STOP", "ATR_STOP_MULTIPLIER", "STOP_LOSS_RATE",
                "TRAILING_STOP_CALLBACK_RATE"):
        assert key in locked, f"{key}가 잠금 목록에서 빠졌다"
    # 조정 가능한 항목은 잠기면 안 된다
    for key in ("BUY_SCORE", "BUY_RSI_MAX", "USE_MARKET_FILTER",
                "TRAILING_STOP_ACTIVATION_RATE", "PYRAMIDING_MAX_COUNT"):
        assert key not in locked, f"{key}는 조정 가능해야 한다"