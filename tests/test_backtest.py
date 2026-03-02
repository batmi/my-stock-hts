import pytest
import pandas as pd
from modules.auto_trade import DefaultStrategy
import config

# 백테스팅 로직을 시뮬레이션하는 헬퍼 함수
def run_simulation(strategy, df, initial_balance=10_000_000):
    balance = initial_balance
    shares = 0
    avg_price = 0
    trades = []
    
    # 데이터가 최소 60일 이상이어야 지표 계산 가능 (이평선 등)
    start_idx = 60
    if len(df) <= start_idx:
        return initial_balance, []

    # 시뮬레이션 루프
    for i in range(start_idx, len(df)):
        # 현재 시점까지의 데이터 슬라이싱 (Look-ahead Bias 방지)
        current_slice = df.iloc[:i+1]
        current_row = df.iloc[i]
        current_price = float(current_row['close'])
        current_date = current_row['date']
        
        # 매수 상태가 아닐 때: 매수 조건 점검
        if shares == 0:
            thresholds = {
                "BUY_SCORE": 8.0,
                "BUY_RSI_MAX": 70,
                "BUY_VOL_STRENGTH": 0 # 테스트 데이터에는 체결강도가 없거나 임의값이므로 무시
            }
            
            # 전략 실행
            res = strategy.analyze_buy("TEST", "TestStock", current_slice, current_price, vol_strength=100, thresholds=thresholds)
            
            if res and res['action'] == 'buy':
                qty = int(balance / current_price)
                if qty > 0:
                    cost = qty * current_price
                    balance -= cost
                    shares = qty
                    avg_price = current_price
                    trades.append({
                        "type": "buy", 
                        "price": current_price, 
                        "date": current_date, 
                        "score": res['score']
                    })
        
        # 매수 상태일 때: 매도 조건 점검
        else:
            profit_rate = ((current_price - avg_price) / avg_price) * 100
            
            thresholds = {
                "TAKE_PROFIT_RATE": 30.0,
                "STOP_LOSS_RATE": -7.0,
                "TAKE_PROFIT_RSI": 80,
                "SELL_SCORE": 5.0
            }
            
            res = strategy.analyze_sell("TEST", "TestStock", current_slice, current_price, avg_price, profit_rate, thresholds=thresholds)
            
            if res['action'] == 'sell':
                revenue = shares * current_price
                balance += revenue
                profit = revenue - (shares * avg_price)
                trades.append({
                    "type": "sell", 
                    "price": current_price, 
                    "date": current_date, 
                    "profit": profit, 
                    "reason": res['reason']
                })
                shares = 0
                avg_price = 0

    # 최종 자산 평가
    final_asset = balance + (shares * current_price if shares > 0 else 0)
    return final_asset, trades

def test_backtest_uptrend_profit(sample_uptrend_df):
    """상승장 시뮬레이션: 수익 발생 여부 검증"""
    strategy = DefaultStrategy()
    initial_asset = 10_000_000
    
    final_asset, trades = run_simulation(strategy, sample_uptrend_df, initial_asset)
    
    # 상승장이므로 자산이 증가해야 함 (또는 최소한 손실은 없어야 함)
    assert final_asset >= initial_asset
    
    # 거래가 발생했는지 확인 (상승장이면 매수 신호가 나와야 함)
    if len(trades) > 0:
        assert trades[0]['type'] == 'buy'

def test_backtest_downtrend_defense(sample_downtrend_df):
    """하락장 시뮬레이션: 손실 방어 여부 검증"""
    strategy = DefaultStrategy()
    initial_asset = 10_000_000
    
    final_asset, trades = run_simulation(strategy, sample_downtrend_df, initial_asset)
    
    # 하락장에서는 매수를 안 하거나, 손절로 막아야 함
    # 큰 손실(-15% 이상)이 발생하지 않았는지 확인
    loss_pct = (final_asset - initial_asset) / initial_asset * 100
    assert loss_pct > -15.0

def test_backtest_sideways_churn(sample_sideways_df):
    """횡보장 시뮬레이션: 잦은 매매 및 손실 여부 확인"""
    strategy = DefaultStrategy()
    initial_asset = 10_000_000
    
    final_asset, trades = run_simulation(strategy, sample_sideways_df, initial_asset)
    
    # 횡보장에서는 자산이 크게 줄어들지 않아야 함
    loss_pct = (final_asset - initial_asset) / initial_asset * 100
    assert loss_pct > -10.0