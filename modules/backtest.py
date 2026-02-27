# modules/backtest.py
import pandas as pd
import numpy as np
from rich.table import Table
from rich.prompt import Prompt
from rich import box
from datetime import datetime, timedelta
import config
import api
import utils
import indicators
from modules import analysis
import logging

logger = logging.getLogger(__name__)

def calculate_daily_status(row, prev_row, thresholds=None):
    """
    analysis.py의 로직을 기반으로 일별 상태 및 점수 계산
    반환값: (raw_score, sell_check_score, can_buy_state, state, reason)
    """
    price = row['close']
    ema20 = row['EMA20']
    ema60 = row['EMA60']
    ema120 = row['EMA120']
    sar = row['SAR']
    rsi = row['RSI']
    adx = row['ADX']
    cci = row['CCI']
    
    macd = row.get('MACD')
    macd_signal = row.get('MACD_Signal')
    # OBV Trend
    obv = row['OBV']
    obv_ma = row['OBV_MA']
    obv_trend = (obv > obv_ma)

    # Previous RSI for divergence check
    prev_rsi = prev_row['RSI'] if prev_row is not None else None

    # [수정] analysis 모듈을 사용하여 로직 동기화
    # 1. 상태 분류 (위험/주의/관망/상승/매수)
    state, _, reason = analysis.classify_stock_state(
        price, ema20, ema60, ema120, sar, rsi, prev_rsi, adx, cci, obv_trend, macd, macd_signal,
        thresholds=thresholds
    )
    
    # 2. 점수 계산
    raw_score, _ = analysis.calculate_score(
        price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd, macd_signal
    )
    
    # 3. 백테스팅용 플래그 변환
    can_buy_state = (state not in ["매도", "주의"]) # 매도/주의 상태가 아니면 매수 후보
    sell_check_score = 0 if state == "매도" else raw_score # 매도 상태면 점수 0점 처리 (매도 유도)
    
    return raw_score, sell_check_score, can_buy_state, state, reason

def get_backtest_data(code, is_overseas, days):
    # 1. yfinance 시도 (장기간 데이터 확보 유리)
    try:
        start_dt = datetime.now() - timedelta(days=days + 120) # 지표 계산용 여유 기간 포함
        start_str = start_dt.strftime("%Y-%m-%d")
        
        tickers = []
        if is_overseas:
            tickers.append(code)
        else:
            tickers.append(f"{code}.KS") # 코스피
            tickers.append(f"{code}.KQ") # 코스닥
            
        for t in tickers:
            if config.SCREEN_DEBUG_LEVEL == "TRACE":
                config.console.print(f"[dim cyan][TRACE] REQ (yfinance) | Ticker: {t} | Start: {start_str}[/dim cyan]")
            
            df = api.fetch_yfinance_data(t, start=start_str)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    try: df = df.xs(t, axis=1, level=1)
                    except: pass
                
                df.columns = [c.lower() for c in df.columns]
                df = df.reset_index()
                df.rename(columns={'Date': 'date', 'Close': 'close'}, inplace=True) # 컬럼명 통일
                
                # 날짜 포맷 통일 (YYYYMMDD 문자열)
                df['date'] = df['date'].apply(lambda x: x.strftime('%Y%m%d') if isinstance(x, datetime) else str(x).replace('-', '')[:8])
                
                return df
    except Exception as e:
        pass

    # 2. KIS API 시도 (Fallback)
    return api.get_chart_data(code, is_overseas)

def get_tick_size(price):
    """국내 주식 호가 단위 계산"""
    if price < 2000: return 1
    if price < 5000: return 5
    if price < 20000: return 10
    if price < 50000: return 50
    if price < 200000: return 100
    if price < 500000: return 500
    return 1000

def simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score_limit, buy_rsi_limit, is_overseas, 
                      stop_loss_rate=None, take_profit_rate=None, 
                      take_profit_rsi=None, sell_score=None, 
                      ts_activation_rate=None, ts_callback_rate=None):
    """주어진 설정으로 백테스팅 시뮬레이션을 수행하고 결과를 반환"""
    
    # 시뮬레이션 변수
    balance = initial_capital
    holdings = 0
    avg_price = 0
    trades = []
    buy_date = None
    
    # 통계 변수
    max_score_observed = 0
    score_8_count = 0
    
    # [추가] 매수 보류 카운트
    missed_caution_count = 0
    missed_danger_count = 0
    missed_trades = [] # [추가] 매수 보류 상세 내역
    
    # 매도 설정값 로드 (Config 참조)
    stop_loss_limit = stop_loss_rate if stop_loss_rate is not None else config.SELL_STRATEGY["STOP_LOSS_RATE"]
    take_profit_limit = take_profit_rate if take_profit_rate is not None else config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    take_profit_rsi_limit = take_profit_rsi if take_profit_rsi is not None else config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    sell_score_limit = sell_score if sell_score is not None else config.SELL_STRATEGY["SELL_SCORE"]
    
    ts_activation = ts_activation_rate if ts_activation_rate is not None else config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    ts_callback = ts_callback_rate if ts_callback_rate is not None else config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)

    # [추가] 리스크 관리 설정 로드
    risk_per_trade = getattr(config, 'SYSTEM_RISK_PER_TRADE', 5.0)
    
    # [추가] ATR 기반 손절 설정 로드
    use_atr_stop = config.SELL_STRATEGY.get("USE_ATR_STOP", False)
    atr_mult = config.SELL_STRATEGY.get("ATR_STOP_MULTIPLIER", 2.0)
    use_vol_target = getattr(config, 'USE_VOLATILITY_TARGETING', True)

    peak_asset = initial_capital
    mdd = 0.0
    win_trades = 0
    loss_trades = 0
    gross_profit = 0
    gross_loss = 0
    cum_profit = 0
    daily_assets = []
    
    ts_highest_price = 0
    prev_row = prev_row_init
    
    # [추가] 시뮬레이션용 임계값 설정 (상태 분류 동기화)
    current_thresholds = {
        "BUY_SCORE": buy_score_limit,
        "BUY_RSI_MAX": buy_rsi_limit,
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"] # 기본값 유지
    }

    for i in range(len(sim_df)):
        row = sim_df.iloc[i]
        date = row['date']
        price = row['close']
        high_price = row['high']
        
        current_asset = balance + (holdings * price)
        daily_assets.append(current_asset)
        if current_asset > peak_asset: peak_asset = current_asset
        if peak_asset > 0:
            dd = (current_asset - peak_asset) / peak_asset * 100
            if dd < mdd: mdd = dd
        
        # 상태 및 점수 계산
        raw_score, sell_check_score, can_buy_state, state, reason = calculate_daily_status(row, prev_row, thresholds=current_thresholds)
        
        if raw_score > max_score_observed: max_score_observed = raw_score
        if raw_score >= buy_score_limit: score_8_count += 1
        
        # 매매 로직
        # [매수]
        if holdings == 0:
            # [수정] 매수 조건 체크 (상태 필터링 통계 추가)
            is_score_ok = raw_score >= buy_score_limit
            is_rsi_ok = row['RSI'] < buy_rsi_limit
            
            if is_score_ok:
                if is_rsi_ok and can_buy_state:
                    # [수정] 슬리피지 적용 (국내 주식: 1호가 높게 매수)
                    buy_price = price
                    if not is_overseas:
                        tick = get_tick_size(price)
                        buy_price = int(price + tick)

                    # [추가] ATR 기반 동적 손절률 계산
                    current_sl_rate = stop_loss_limit
                    atr_val = row.get('ATR', 0) # indicators.py에서 계산된 ATR 컬럼 필요 (현재는 없음)
                    # indicators.py의 get_atr_full_series 결과를 df에 병합해야 함 (아래 get_backtest_data 수정 필요)
                    
                    # 임시: ATR 컬럼이 없으면 고정 손절률 사용
                    # (실제로는 get_backtest_data에서 ATR 계산 후 병합하는 로직이 선행되어야 함)
                    
                    # [추가] 리스크 기반 포지션 사이징 적용 (백테스팅)
                    invest_amt = balance
                    if risk_per_trade > 0 and current_sl_rate and abs(current_sl_rate) > 0:
                        # 현재 자산(balance) 기준 리스크 계산
                        max_loss_amt = balance * (risk_per_trade / 100.0)
                        sl_ratio = abs(current_sl_rate) / 100.0
                        risk_based_amt = int(max_loss_amt / sl_ratio)
                        invest_amt = min(balance, risk_based_amt)
                        
                    # [추가] 변동성 타겟팅 스케일링 (간이 구현)
                    # ATR 데이터가 있다면 적용 가능
                    # if use_vol_target and atr_val > 0:
                    #     daily_vol = atr_val / buy_price
                    #     annual_vol = daily_vol * (252 ** 0.5)
                    #     scale = config.TARGET_VOLATILITY / annual_vol
                    #     invest_amt = int(invest_amt * scale)

                    qty = int(invest_amt / buy_price)
                    
                    if qty > 0:
                        cost = qty * buy_price
                        balance -= cost
                        holdings = qty
                        avg_price = buy_price
                        buy_date = date
                        ts_highest_price = buy_price
                        trades.append({
                            "date": date, "type": "매수", "price": buy_price, "qty": qty, "balance": balance, 
                            "profit": 0, "profit_amt": 0, "days": 0, 
                            "score": raw_score, "rsi": row['RSI'], "adx": row['ADX'], "cci": row['CCI'], "obv": row['OBV'], "obv_trend": (row['OBV'] > row['OBV_MA']),
                            "cum_profit": cum_profit
                        })
                else:
                    # [추가] 매수 보류 카운팅
                    if state == "주의": missed_caution_count += 1
                    elif state == "매도": missed_danger_count += 1
                    
                    # [추가] 보류 사유 상세화
                    missed_reason = reason
                    if not can_buy_state:
                        missed_reason = f"{state}: {reason}"
                    elif not is_rsi_ok:
                        missed_reason = f"RSI 과열 ({row['RSI']:.1f} >= {buy_rsi_limit})"

                    # [추가] 보류 내역 저장
                    missed_trades.append({
                        "date": date,
                        "score": raw_score,
                        "state": state,
                        "reason": missed_reason,
                        "rsi": row['RSI'],
                        "adx": row['ADX'],
                        "cci": row['CCI'],
                        "price": price
                    })
        # [매도]
        elif holdings > 0:
            loss_rate = (price - avg_price) / avg_price * 100
            if high_price > ts_highest_price: ts_highest_price = high_price

            sell_signal = False
            reason = ""

            if loss_rate >= take_profit_limit: sell_signal = True; reason = "익절"
            elif loss_rate <= stop_loss_limit: sell_signal = True; reason = "손절"
            elif ts_highest_price > 0:
                max_profit_rate = ((ts_highest_price - avg_price) / avg_price) * 100
                if max_profit_rate >= ts_activation:
                    drop_rate = ((ts_highest_price - price) / ts_highest_price) * 100
                    if drop_rate >= ts_callback: sell_signal = True; reason = "트레일링스탑"
            
            if not sell_signal and row['RSI'] > take_profit_rsi_limit: sell_signal = True; reason = "RSI과열"
            if not sell_signal and sell_check_score < sell_score_limit: sell_signal = True; reason = "점수하락"
            
            if sell_signal:
                # [수정] 슬리피지 적용 (국내 주식: 1호가 낮게 매도)
                sell_price = price
                if not is_overseas:
                    tick = get_tick_size(price)
                    sell_price = int(price - tick)
                    if sell_price <= 0: sell_price = int(price)

                sell_amt = holdings * sell_price
                fee = sell_amt * 0.0023
                if not is_overseas: fee = int(fee)
                sell_amt -= fee
                
                profit = sell_amt - (holdings * avg_price)
                profit_rate = (profit / (holdings * avg_price)) * 100
                
                if profit > 0: 
                    win_trades += 1
                    gross_profit += profit
                else: 
                    loss_trades += 1
                    gross_loss += abs(profit)
                
                cum_profit += profit
                
                holding_days = 0
                if buy_date:
                    try:
                        d1 = datetime.strptime(str(buy_date), "%Y%m%d")
                        d2 = datetime.strptime(str(date), "%Y%m%d")
                        holding_days = (d2 - d1).days
                    except: pass
                
                balance += sell_amt
                sold_qty = holdings
                holdings = 0
                avg_price = 0
                buy_date = None
                ts_highest_price = 0
                
                if reason == "점수하락" and sell_check_score == 0 and raw_score > 0: reason = "매도"
                    
                trades.append({
                    "date": date, "type": f"매도({reason})", "price": sell_price, "qty": sold_qty, "balance": balance, 
                    "profit": profit_rate, "profit_amt": profit, "days": holding_days, 
                    "score": sell_check_score, "rsi": row['RSI'], "adx": row['ADX'], "cci": row['CCI'], "obv": row['OBV'], "obv_trend": (row['OBV'] > row['OBV_MA']),
                    "cum_profit": cum_profit
                })
        
        prev_row = row

    final_asset = balance + (holdings * sim_df.iloc[-1]['close'])
    total_return = (final_asset - initial_capital) / initial_capital * 100 if initial_capital > 0 else 0.0
    
    return {
        "trades": trades,
        "final_asset": final_asset,
        "total_return": total_return,
        "mdd": mdd,
        "win_trades": win_trades,
        "loss_trades": loss_trades,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "daily_assets": daily_assets,
        "max_score_observed": max_score_observed,
        "score_8_count": score_8_count,
        "missed_caution_count": missed_caution_count,
        "missed_danger_count": missed_danger_count,
        "missed_trades": missed_trades
    }

def run_backtest():
    config.console.print("\n[magenta]=== 전략 백테스팅 (Backtest) ===[/]")
    
    # 1. 종목 선택
    # [수정] 백테스팅 메뉴 순서 변경 (5번: 시장 지수, 6번: 직접 입력)
    config.console.print("\n[bold]백테스팅할 종목을 선택하세요:[/bold]")
    config.console.print("[1] 국내 주식")
    config.console.print("[2] 국내 ETF")
    config.console.print("[3] 미국 주식")
    config.console.print("[4] 미국 ETF")
    config.console.print("[5] 시장 지수")
    config.console.print("[6] 직접 입력 (코드 검색)")
    config.console.print()
    
    sub_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "6", "q"], default="6")
    if sub_choice.lower() == 'q': return

    code, name, is_overseas = None, None, False

    if sub_choice == '6':
        raw_input = Prompt.ask("종목코드(6자리/티커) 입력 [dim](취소: q)[/dim]")
        if raw_input and raw_input.lower() != 'q':
            if raw_input.isdigit() and len(raw_input) == 6:
                code = raw_input
                name = api.get_stock_name_by_code(code, False) or code
                is_overseas = False
            else:
                code = raw_input.upper()
                name = api.get_stock_name_by_code(code, True) or code
                is_overseas = True
    elif sub_choice == '5':
        indices_list = [
            ("코스피", "^KS11"), ("코스닥", "^KQ11"),
            ("나스닥", "^IXIC"), ("S&P500", "^GSPC"), ("다우존스", "^DJI"),
            ("금", "GC=F"), ("은", "SI=F"), ("구리", "HG=F"),
            ("WTI 원유", "CL=F"), ("천연가스", "NG=F"), ("밀", "ZW=F"),
            ("달러인덱스", "DX-Y.NYB"), ("달러환율", "KRW=X"),
            ("VIX (변동성)", "^VIX"), ("SOX (반도체)", "^SOX")
        ]
        config.console.print(f"\n[bold]시장 지수 목록:[/bold]")
        for i, (n, c) in enumerate(indices_list):
            config.console.print(f"[{i+1}] {n}")
        
        config.console.print()
        sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
        if sel.lower() != 'q' and sel.isdigit() and 1 <= int(sel) <= len(indices_list):
            name, code = indices_list[int(sel)-1]
            is_overseas = True
    elif sub_choice in ["1", "2", "3", "4"]:
        key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
        s_list = config.session.stock_data.get(key_map[sub_choice], [])
        if s_list:
            for i, s in enumerate(s_list):
                config.console.print(f"[{i+1}] {s['name']} ({s['code']})")
            
            config.console.print()
            sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
            if sel.lower() != 'q' and sel.isdigit() and 1 <= int(sel) <= len(s_list):
                item = s_list[int(sel)-1]
                code, name = item['code'], item['name']
                is_overseas = (sub_choice in ["3", "4"])
        else:
            config.console.print("[yellow]목록이 비어있습니다.[/yellow]")
            return

    if not code: return

    # 2. 설정 입력
    change_settings = Prompt.ask("시뮬레이션 조건을 변경하시겠습니까?", choices=["y", "n", "q"], default="n")
    if change_settings == 'q': return
    
    # 기본값 설정
    days = 365
    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    sell_score = config.SELL_STRATEGY["SELL_SCORE"]
    stop_loss = config.SELL_STRATEGY["STOP_LOSS_RATE"]
    take_profit = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
    take_profit_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
    ts_activation = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
    ts_callback = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)

    if change_settings == "y":
        days_input = Prompt.ask("분석 기간 (일 단위)", default="365")
        if days_input.lower() == 'q': return
        try:
            days = int(days_input)
        except:
            days = 365
        
        # 매수 조건
        def_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        val = Prompt.ask(f"매수 기준 점수 (기본: {def_buy_score}점)\n[dim]설명: 이 점수 이상일 때 매수 진입 (지표 종합 점수)[/dim]", default=str(def_buy_score))
        if val.lower() == 'q': return
        try: buy_score = float(val)
        except: buy_score = float(def_buy_score)
        
        def_buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        val = Prompt.ask(f"매수 허용 RSI 상한 (기본: {def_buy_rsi})\n[dim]설명: RSI가 이 값보다 낮아야 매수 (과열 방지)[/dim]", default=str(def_buy_rsi))
        if val.lower() == 'q': return
        buy_rsi = float(val)
        
        def_tp_rsi = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        val = Prompt.ask(f"익절 RSI 기준 (기본: {def_tp_rsi})\n[dim]설명: RSI가 이 값을 초과하면 과열로 판단하여 매도[/dim]", default=str(def_tp_rsi))
        if val.lower() == 'q': return
        take_profit_rsi = float(val)
        
        # 매도 조건
        def_sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        val = Prompt.ask(f"매도(추세이탈) 기준 점수 (기본: {def_sell_score}점)\n[dim]설명: 점수가 이 값 미만으로 떨어지면 매도[/dim]", default=str(def_sell_score))
        if val.lower() == 'q': return
        try: sell_score = float(val)
        except: sell_score = float(def_sell_score)
        
        def_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        val = Prompt.ask(f"손절 수익률(%) (기본: {def_sl}%)\n[dim]설명: 손실이 이 비율에 도달하면 손절매[/dim]", default=str(def_sl))
        if val.lower() == 'q': return
        stop_loss = float(val)
        
        def_tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        val = Prompt.ask(f"익절 수익률(%) (기본: {def_tp}%)\n[dim]설명: 수익이 이 비율에 도달하면 이익 실현[/dim]", default=str(def_tp))
        if val.lower() == 'q': return
        take_profit = float(val)
        
        def_ts_act = config.SELL_STRATEGY.get("TRAILING_STOP_ACTIVATION_RATE", 10.0)
        val = Prompt.ask(f"트레일링 스탑 발동 수익률(%) (기본: {def_ts_act}%)\n[dim]설명: 수익률이 이 값 이상일 때 트레일링 스탑 감시 시작[/dim]", default=str(def_ts_act))
        if val.lower() == 'q': return
        ts_activation = float(val)
        
        def_ts_call = config.SELL_STRATEGY.get("TRAILING_STOP_CALLBACK_RATE", 3.0)
        val = Prompt.ask(f"트레일링 스탑 하락 감지율(%) (기본: {def_ts_call}%)\n[dim]설명: 최고가 대비 이 비율만큼 하락 시 매도[/dim]", default=str(def_ts_call))
        if val.lower() == 'q': return
        ts_callback = float(val)
        
        config.console.print(f"\n[dim]설정한 조건으로 진행합니다. (기간: {days}일, 매수: {buy_score}점/RSI{buy_rsi}, 매도: {sell_score}점, 익절: {take_profit}%/RSI{take_profit_rsi}, 손절: {stop_loss}%, 트레일링: {ts_activation}%/{ts_callback}%)[/dim]")
    else:
        config.console.print(f"\n[dim]기본 설정으로 진행합니다. (기간: {days}일, 매수: {buy_score}점/RSI{buy_rsi}, 매도: {sell_score}점, 익절: {take_profit}%/RSI{take_profit_rsi}, 손절: {stop_loss}%, 트레일링: {ts_activation}%/{ts_callback}%)[/dim]")
    
    # [수정] 초기 자본금 및 환율 설정
    initial_capital_krw = 10_000_000
    exchange_rate = 1.0
    
    if is_overseas:
        exchange_rate = config.DEFAULT_EXCHANGE_RATE
        initial_capital = initial_capital_krw / exchange_rate
    else:
        initial_capital = initial_capital_krw

    logger.info(f"운영자 실행: {' - '.join(config.USER_ACTION_BREADCRUMB)}")

    # 3. 데이터 준비
    with config.console.status(f"[green]{name} ({code}) 데이터 분석 및 시뮬레이션 준비 중...[/]"):
        # KIS API 사용 시를 대비해 설정 변경 (yfinance 실패 시 동작)
        original_lookback = config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"]
        needed_days = days + 120 
        if needed_days > original_lookback:
            config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] = needed_days
            
        try:
            # [수정] yfinance 우선 조회 함수 사용
            df = get_backtest_data(code, is_overseas, days)
        finally:
            config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"] = original_lookback

        if df is None or df.empty:
            config.console.print("[red]데이터를 불러올 수 없습니다.[/red]")
            return

        # 지표 계산
        df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['EMA60'] = df['close'].ewm(span=60, adjust=False).mean()
        df['EMA120'] = df['close'].ewm(span=120, adjust=False).mean()
        df['SAR'] = indicators.get_psar_full_series(df)
        df['ADX'] = indicators.get_adx_full_series(df)
        df['RSI'] = indicators.get_rsi_full_series(df)
        df['CCI'] = indicators.get_cci_full_series(df)
        df['OBV'] = indicators.get_obv_full_series(df)
        df['OBV_MA'] = df['OBV'].rolling(window=config.INDICATOR_PARAMS["OBV_MA_PERIOD"]).mean()
        df['ATR'] = indicators.get_atr_full_series(df) # [추가] ATR 계산
        df['MACD'], df['MACD_Signal'], _ = indicators.get_macd_full_series(df)

        # 분석 기간 필터링
        # [수정] 행 개수 기준이 아닌 날짜 기준으로 필터링
        cutoff_dt = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff_dt.strftime("%Y%m%d")
        
        start_idx = 0
        mask = df['date'] >= cutoff_str
        if mask.any():
            start_idx = mask.idxmax()
        
        sim_df = df.iloc[start_idx:].copy()
        prev_row_init = df.iloc[start_idx-1] if start_idx > 0 else None
        
        # [수정] 경고 로직: 실제 기간(일수)과 요청 기간 비교
        if not sim_df.empty:
            actual_days = (datetime.strptime(str(sim_df.iloc[-1]['date']), "%Y%m%d") - datetime.strptime(str(sim_df.iloc[0]['date']), "%Y%m%d")).days
            if actual_days < days * 0.9: # 90% 미만일 때만 경고
                config.console.print(f"[dim yellow]주의: 요청 기간({days}일)보다 실제 분석 기간({actual_days}일)이 짧습니다.[/dim yellow]")

    # === 단일 실행 모드 (기존 로직) ===
    res = simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score, buy_rsi, is_overseas, 
                            stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                            take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                            ts_activation_rate=ts_activation, ts_callback_rate=ts_callback)
    
    # 결과 변수 매핑 (기존 출력 로직 호환)
    final_asset = res['final_asset']
    total_return = res['total_return']
    trades = res['trades']
    win_trades = res['win_trades']
    loss_trades = res['loss_trades']
    gross_profit = res['gross_profit']
    gross_loss = res['gross_loss']
    mdd = res['mdd']
    daily_assets = res['daily_assets']
    max_score_observed = res['max_score_observed']
    score_8_count = res['score_8_count']
    missed_caution = res.get('missed_caution_count', 0)
    missed_danger = res.get('missed_danger_count', 0)
    missed_trades = res.get('missed_trades', [])

    # 4. 결과 출력
    # [추가] 샤프 지수 계산 (연율화: 252일 기준)
    sharpe_ratio = 0.0
    if len(daily_assets) > 1:
        returns = pd.Series(daily_assets).pct_change().dropna()
        if not returns.empty and returns.std() > 0:
            sharpe_ratio = (returns.mean() / returns.std()) * np.sqrt(252)

    # [추가] 포맷팅 헬퍼 함수
    def fmt_money(val):
        if is_overseas: return f"${val:,.2f}"
        return f"{int(val):,}원"

    # [수정] 결과 리포트를 테이블로 출력
    config.console.print()
    summary_table = Table(title=f"백테스팅 결과 리포트: {name}", box=box.HORIZONTALS, show_header=False, border_style="dim")
    summary_table.add_column("항목", style="cyan", justify="left")
    summary_table.add_column("값", justify="left")
    
    start_date_str = str(sim_df.iloc[0]['date'])
    end_date_str = str(sim_df.iloc[-1]['date'])
    if len(start_date_str) == 8 and start_date_str.isdigit(): start_date_str = f"{start_date_str[:4]}-{start_date_str[4:6]}-{start_date_str[6:]}"
    if len(end_date_str) == 8 and end_date_str.isdigit(): end_date_str = f"{end_date_str[:4]}-{end_date_str[4:6]}-{end_date_str[6:]}"
    
    summary_table.add_row("기간", f"{days}일간 ({start_date_str} ~ {end_date_str})")
    summary_table.add_row("초기 자본금", fmt_money(initial_capital))
    summary_table.add_row("최종 평가액", fmt_money(final_asset))
    
    color = "red" if total_return > 0 else "blue"
    summary_table.add_row("누적 수익률", f"[{color}]{total_return:+.2f}%[/]")
    
    summary_table.add_section()
    
    # [수정] 매매 횟수 상세 및 승률/MDD 출력
    sell_count = win_trades + loss_trades
    sell_trades = [t for t in trades if t['type'].startswith("매도")]
    win_rate = (win_trades / sell_count * 100) if sell_count > 0 else 0.0
    summary_table.add_row("총 매매 횟수", f"{len(trades)}건 (진입 {len(trades)-sell_count} / 청산 {sell_count})")
    
    # [추가] 매수 보류 통계 출력
    if missed_caution > 0 or missed_danger > 0:
        summary_table.add_row("매수 보류 (상태)", f"[yellow]주의 {missed_caution}회[/] / [blue]매도 {missed_danger}회[/] (점수 충족했으나 진입 불가)")
    
    if sell_count > 0:
        summary_table.add_row("승률 (Win Rate)", f"{win_rate:.1f}% ({win_trades}승 {loss_trades}패)")
        
        # [순서 변경] 평균 수익률 먼저 출력
        profits = [t['profit'] for t in sell_trades if t['profit'] > 0]
        losses = [t['profit'] for t in sell_trades if t['profit'] <= 0]
        
        avg_profit = sum(profits) / len(profits) if profits else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        
        summary_table.add_row("평균 수익률", f"[red]{avg_profit:+.2f}%[/] / 평균 손실률: [blue]{avg_loss:+.2f}%[/]")
        
        # [추가] 손익 구조 분석 (평균 손익비 기반)
        structure_msg = "-"
        if avg_loss == 0:
            structure_msg = "[green]무손실 (완벽한 방어)[/]"
        else:
            # 손익비 = 평균수익 / |평균손실|
            pl_ratio = abs(avg_profit / avg_loss)
            # 손익분기 승률 = 1 / (손익비 + 1)
            be_win_rate = (1 / (pl_ratio + 1)) * 100
            
            if pl_ratio >= 2.0:
                structure_msg = f"[green]매우 우수 (승률 {be_win_rate:.0f}%만 넘으면 수익)[/]"
            elif pl_ratio >= 1.5:
                structure_msg = f"[green]양호 (승률 {be_win_rate:.0f}%만 넘으면 수익)[/]"
            elif pl_ratio >= 1.0:
                structure_msg = f"[yellow]보통 (승률 {be_win_rate:.0f}% 이상 필요)[/]"
            else:
                structure_msg = f"[red]불리함 (승률 {be_win_rate:.0f}% 이상 필요)[/]"
        
        summary_table.add_row("손익 구조 분석", structure_msg)

        # [추가] 보유 기간 통계
        holding_days_list = [t['days'] for t in sell_trades]
        if holding_days_list:
            max_days = max(holding_days_list)
            min_days = min(holding_days_list)
            avg_days = sum(holding_days_list) / len(holding_days_list)
            summary_table.add_row("보유 기간", f"평균 {avg_days:.1f}일 (최대 {max_days}일 / 최소 {min_days}일)")
        
        # [수정] 손익비(Profit Factor) 출력 (설명 추가)
        pf_str = "Inf"
        pf_desc = ""
        if gross_loss > 0:
            pf = gross_profit / gross_loss
            if pf >= 2.0: pf_color = "red"; pf_desc = " (매우 훌륭)"
            elif pf >= 1.5: pf_color = "orange3"; pf_desc = " (우수)"
            elif pf >= 1.0: pf_color = "green"; pf_desc = " (평범)"
            else: pf_color = "blue"; pf_desc = " (손실)"
            pf_str = f"[{pf_color}]{pf:.2f}[/]"
        elif gross_profit > 0: 
            pf_str = "[dim]0.00[/]"
            pf_desc = " (손실 없음)"
        else: pf_str = "[dim]-[/]"
        
        summary_table.add_row("손익비 (Profit Factor)", f"{pf_str}{pf_desc} (총 이익 [red]+{fmt_money(gross_profit)}[/] / 총 손실 [blue]-{fmt_money(gross_loss)}[/])")
        
        # [수정] 샤프 지수 출력 (설명 추가)
        sharpe_desc = ""
        if sharpe_ratio >= 1.0: sharpe_color = "red"; sharpe_desc = " (매우 우수)"
        elif sharpe_ratio >= 0.5: sharpe_color = "green"; sharpe_desc = " (양호)"
        else: sharpe_color = "blue"; sharpe_desc = " (미흡)"
        summary_table.add_row("샤프 지수 (Sharpe)", f"[{sharpe_color}]{sharpe_ratio:.2f}[/]{sharpe_desc}")
    
    mdd_color = "red"
    mdd_desc = " (위험)"
    if mdd >= -10: mdd_color = "green"; mdd_desc = " (안정)"
    elif mdd >= -20: mdd_color = "yellow"; mdd_desc = " (보통)"
    elif mdd >= -30: mdd_color = "orange3"; mdd_desc = " (주의)"
    
    summary_table.add_row("최대 낙폭 (MDD)", f"[{mdd_color}]{mdd:.2f}%[/]{mdd_desc}")
    
    config.console.print(summary_table)
    
    # [추가] 매매가 없을 경우 진단 정보 출력
    if len(trades) == 0:
        config.console.print("\n[yellow]※ 매매가 발생하지 않았습니다. (조건 미충족)[/yellow]")
        config.console.print(f"  - 기간 내 최고 점수: {max_score_observed:.1f}점 (매수 기준: {buy_score}점)")
        config.console.print(f"  - {buy_score}점 이상 도달 횟수: {score_8_count}회")
        config.console.print(f"  [안내] 현재 설정된 매수 조건({buy_score}점 이상 & RSI<{buy_rsi})이 엄격하여 진입 기회가 없었습니다.")
        config.console.print("  [Tip] 매수 조건을 완화하거나 분석 기간을 늘려보세요.")
    
    if trades:
        config.console.print()
        t_table = Table(title="상세 매매 일지", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        t_table.add_column("일자", justify="center")
        t_table.add_column("구분", justify="center")
        t_table.add_column("점수", justify="center")
        t_table.add_column("RSI", justify="right")
        t_table.add_column("ADX", justify="right")
        t_table.add_column("CCI", justify="right")
        t_table.add_column("OBV", justify="right")
        t_table.add_column("수량", justify="right")
        t_table.add_column("단가", justify="right")
        t_table.add_column("수익금", justify="right")
        t_table.add_column("수익률", justify="right")
        t_table.add_column("누적손익", justify="right")
        t_table.add_column("보유기간", justify="right")

        for t in trades:
            p_str = f"{t['profit']:+.2f}%" if t['type'].startswith("매도") else "-"
            p_color = "[red]" if t['profit'] > 0 else ("[blue]" if t['profit'] < 0 else "")
            profit_display = f"{p_color}{p_str}[/]" if p_color else p_str
            type_color = "[red]" if "매수" in t['type'] else "[blue]"
            date_str = str(t['date'])
            if len(date_str) == 8 and date_str.isdigit(): date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            
            # 지표 색상 적용
            # RSI
            rsi_val = t.get('rsi', 0)
            if rsi_val is None or np.isnan(rsi_val):
                rsi_str = "-"
                rsi_c = "dim"
            else:
                rsi_str = f"{rsi_val:.1f}"
                rsi_c = "blue"
                if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_c = "magenta"
                elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_c = "red"
                elif 45 <= rsi_val < 55: rsi_c = "orange3"
                elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: rsi_c = "yellow"
            
            # ADX
            adx_val = t.get('adx', 0)
            if adx_val is None or np.isnan(adx_val):
                adx_str = "-"
                adx_c = "dim"
            else:
                adx_str = f"{adx_val:.1f}"
                adx_c = "white"
                if adx_val >= 40: adx_c = "magenta"
                elif adx_val >= 30: adx_c = "red"
                elif adx_val >= 20: adx_c = "orange3"
                elif adx_val >= 15: adx_c = "yellow"
            
            # CCI
            cci_val = t.get('cci', 0)
            if cci_val is None or np.isnan(cci_val):
                cci_str = "-"
                cci_c = "dim"
            else:
                cci_str = f"{cci_val:.1f}"
                cci_c = "blue"
                if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_c = "red"
                elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_c = "orange3"
                elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: cci_c = "yellow"
            
            # OBV
            obv_val = t.get('obv') or 0
            obv_c = "red" if t.get('obv_trend') else "blue"
            
            qty_str = f"{t['qty']:,}"
            
            # [수정] 금액 포맷팅 (해외/국내 분기)
            price_str = f"{t['price']:,.2f}" if is_overseas else f"{t['price']:,.0f}"
            
            amt_val = t.get('profit_amt', 0)
            amt_str = "-"
            if t['type'].startswith("매도"):
                s_val = fmt_money(abs(amt_val)).replace('$', '').replace('원', '')
                prefix = "$" if is_overseas else ""
                suffix = "" if is_overseas else "원"
                if amt_val > 0: amt_str = f"[red]+{prefix}{s_val}{suffix}[/]"
                elif amt_val < 0: amt_str = f"[blue]-{prefix}{s_val}{suffix}[/]"
                else: amt_str = f"{prefix}{s_val}{suffix}"
            
            cum_val = t.get('cum_profit', 0)
            s_cum = fmt_money(abs(cum_val)).replace('$', '').replace('원', '')
            prefix = "$" if is_overseas else ""
            suffix = "" if is_overseas else "원"
            cum_p_str = f"{prefix}{s_cum}{suffix}"
            if cum_val > 0: cum_p_str = f"[red]+{cum_p_str}[/]"
            elif cum_val < 0: cum_p_str = f"[blue]-{cum_p_str}[/]"
            
            days_str = "-"
            if t['type'].startswith("매도"):
                days_str = f"{t.get('days', 0)}일"

            t_table.add_row(
                date_str[:10], 
                f"{type_color}{t['type']}[/]", 
                f"{t.get('score', 0):.1f}",
                f"[{rsi_c}]{rsi_str}[/]",
                f"[{adx_c}]{adx_str}[/]",
                f"[{cci_c}]{cci_str}[/]",
                f"[{obv_c}]{int(obv_val/1000):,}K[/]",
                qty_str, 
                price_str, 
                amt_str, 
                profit_display, 
                cum_p_str,
                days_str
            )
        config.console.print(t_table)

    # [추가] 매매 사유별 통계 분석 (상세 매매 일지 다음에 출력)
    if sell_trades:
        reason_stats = {}
        for t in sell_trades:
            # 사유 추출 (예: "매도(익절)" -> "익절")
            raw_type = t['type']
            reason = raw_type.replace("매도(", "").replace(")", "")
            
            if reason == "RSI과열":
                reason = f"RSI과열({take_profit_rsi})"
            elif reason == "점수하락":
                reason = f"점수하락({sell_score})"

            if reason not in reason_stats:
                reason_stats[reason] = {
                    'count': 0, 
                    'profit_rate_sum': 0.0, 
                    'profit_amt_sum': 0, 
                    'win_count': 0, 
                    'days_sum': 0,
                    'max_profit': -9999.0,
                    'min_profit': 9999.0
                }
            
            reason_stats[reason]['count'] += 1
            reason_stats[reason]['profit_rate_sum'] += t['profit']
            reason_stats[reason]['profit_amt_sum'] += t.get('profit_amt', 0)
            reason_stats[reason]['days_sum'] += t.get('days', 0)
            if t['profit'] > 0:
                reason_stats[reason]['win_count'] += 1
            
            if t['profit'] > reason_stats[reason]['max_profit']:
                reason_stats[reason]['max_profit'] = t['profit']
            
            if t['profit'] < reason_stats[reason]['min_profit']:
                reason_stats[reason]['min_profit'] = t['profit']
            
        reason_table = Table(title="매매 사유별 성과 분석", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        reason_table.add_column("사유", justify="left", style="cyan")
        reason_table.add_column("횟수", justify="right")
        reason_table.add_column("비중", justify="right")
        reason_table.add_column("승률", justify="right")
        reason_table.add_column("평균 수익률", justify="right")
        reason_table.add_column("최대 수익률", justify="right")
        reason_table.add_column("최대 손실률", justify="right")
        reason_table.add_column("총 손익", justify="right")
        reason_table.add_column("평균 보유", justify="right")
        
        total_sells = len(sell_trades)
        
        # 수익률 높은 순으로 정렬
        sorted_reasons = sorted(reason_stats.items(), key=lambda x: x[1]['profit_rate_sum'] / x[1]['count'], reverse=True)
        
        for reason, stat in sorted_reasons:
            cnt = stat['count']
            avg_p = stat['profit_rate_sum'] / cnt
            ratio = (cnt / total_sells) * 100
            win_rate = (stat['win_count'] / cnt) * 100
            total_amt = stat['profit_amt_sum']
            avg_days = stat['days_sum'] / cnt
            max_p = stat['max_profit']
            min_p = stat['min_profit']
            
            p_color = "[red]" if avg_p > 0 else ("[blue]" if avg_p < 0 else "[white]")
            max_p_color = "[red]" if max_p > 0 else ("[blue]" if max_p < 0 else "[white]")
            min_p_color = "[red]" if min_p > 0 else ("[blue]" if min_p < 0 else "[white]")
            amt_color = "[red]" if total_amt > 0 else ("[blue]" if total_amt < 0 else "[white]")
            
            amt_str = fmt_money(total_amt)
            if total_amt > 0: amt_str = f"+{amt_str}"
            
            reason_table.add_row(
                reason, 
                f"{cnt}회", 
                f"{ratio:.1f}%", 
                f"{win_rate:.1f}%",
                f"{p_color}{avg_p:+.2f}%[/]",
                f"{max_p_color}{max_p:+.2f}%[/]",
                f"{min_p_color}{min_p:+.2f}%[/]",
                f"{amt_color}{amt_str}[/]",
                f"{avg_days:.1f}일"
            )
            
        config.console.print()
        config.console.print(reason_table)

    # [이동] 매수 보류 상세 내역 테이블 출력 (매매 사유별 분석 아래로 이동)
    if missed_trades:
        config.console.print()
        m_table = Table(title=f"매수 보류 상세 내역 (기준 점수 {buy_score}점)", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        m_table.add_column("일자", justify="center")
        m_table.add_column("점수", justify="center")
        m_table.add_column("상태", justify="center")
        m_table.add_column("당시 주가", justify="right")
        m_table.add_column("RSI", justify="right")
        m_table.add_column("ADX", justify="right")
        m_table.add_column("CCI", justify="right")
        m_table.add_column("사유", justify="left")
        
        for m in missed_trades:
            date_str = str(m['date'])
            if len(date_str) == 8: date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            
            state = m['state']
            state_color = "white"
            if state == "매수": state_color = "red"
            elif state == "상승": state_color = "orange3"
            elif state == "관망": state_color = "white"
            elif state == "주의": state_color = "yellow"
            elif state == "매도": state_color = "blue"

            adx_str = f"{m.get('adx', 0):.1f}"
            cci_str = f"{m.get('cci', 0):.1f}"
            price_str = fmt_money(m.get('price', 0))
            m_table.add_row(date_str, f"{m['score']:.1f}", f"[{state_color}]{m['state']}[/]", price_str, f"{m['rsi']:.1f}", adx_str, cci_str, m['reason'])
            
        config.console.print(m_table)

    # === 최적화 모드 (매수 점수) ===
    table = Table(title=f"\n매수 점수 최적화 분석 ({name})", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("매수 점수", justify="center")
    table.add_column("수익률", justify="right")
    table.add_column("승률", justify="right")
    table.add_column("MDD", justify="right")
    table.add_column("매매 횟수", justify="right")
    table.add_column("손익비", justify="right")
    
    best_return_score = 0
    best_return = -999.0
    
    best_mdd_score = 0
    best_mdd = -999.0
    
    best_win_score = 0
    best_win_rate = -1.0
    
    with config.console.status("[green]점수별 시뮬레이션 진행 중...[/]"):
        # [수정] 0.5점 단위로 시뮬레이션 (4.0 ~ 9.5)
        scores = [x * 0.5 for x in range(8, 20)]
        for score in scores:
            res = simulate_strategy(sim_df, prev_row_init, initial_capital, score, buy_rsi, is_overseas,
                                    stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                                    take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                    ts_activation_rate=ts_activation, ts_callback_rate=ts_callback)
            
            # 결과 계산
            total_trades = len(res['trades'])
            sell_trades = res['win_trades'] + res['loss_trades']
            win_rate = (res['win_trades'] / sell_trades * 100) if sell_trades > 0 else 0.0
            pf = (res['gross_profit'] / res['gross_loss']) if res['gross_loss'] > 0 else (99.9 if res['gross_profit'] > 0 else 0.0)
            
            # 1. 수익률 기준 갱신
            if res['total_return'] > best_return:
                best_return = res['total_return']
                best_return_score = score
            
            # 2. MDD 기준 갱신 (값이 클수록 좋음, 예: -5 > -20)
            if res['mdd'] > best_mdd:
                best_mdd = res['mdd']
                best_mdd_score = score
            
            # 3. 승률 기준 갱신
            if win_rate > best_win_rate:
                best_win_rate = win_rate
                best_win_score = score
            
            # 테이블 행 추가
            r_color = "[red]" if res['total_return'] > 0 else "[blue]"
            table.add_row(
                f"{score}점",
                f"{r_color}{res['total_return']:+.2f}%[/]",
                f"{win_rate:.1f}%",
                f"{res['mdd']:.2f}%",
                f"{total_trades}건",
                f"{pf:.2f}"
            )
    
    config.console.print(table)
    config.console.print(f"\n[green]추천 (수익률):[/] {best_return_score}점 (수익률 {best_return:+.2f}%)")
    config.console.print(f"[cyan]추천 (안정성):[/] {best_mdd_score}점 (MDD {best_mdd:.2f}%)")
    config.console.print(f"[magenta]추천 (승률):[/]   {best_win_score}점 (승률 {best_win_rate:.1f}%)")

    # === RSI 최적화 모드 ===
    table = Table(title=f"\nRSI 최적화 분석 ({name}) / 점수 {buy_score}점 / RSI {buy_rsi}", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("RSI 기준", justify="center")
    table.add_column("수익률", justify="right")
    table.add_column("승률", justify="right")
    table.add_column("MDD", justify="right")
    table.add_column("매매 횟수", justify="right")
    table.add_column("손익비", justify="right")
    
    best_return_rsi = 0
    best_return = -999.0
    
    rsi_candidates = [45, 50, 55, 60, 65, 70, 75, 80]
    
    with config.console.status("[green]RSI 기준별 시뮬레이션 진행 중...[/]"):
        for rsi_limit in rsi_candidates:
            res = simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score, rsi_limit, is_overseas,
                                    stop_loss_rate=stop_loss, take_profit_rate=take_profit,
                                    take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                    ts_activation_rate=ts_activation, ts_callback_rate=ts_callback)
            
            total_trades = len(res['trades'])
            sell_trades = res['win_trades'] + res['loss_trades']
            win_rate = (res['win_trades'] / sell_trades * 100) if sell_trades > 0 else 0.0
            pf = (res['gross_profit'] / res['gross_loss']) if res['gross_loss'] > 0 else (99.9 if res['gross_profit'] > 0 else 0.0)
            
            if res['total_return'] > best_return:
                best_return = res['total_return']
                best_return_rsi = rsi_limit
            
            r_color = "[red]" if res['total_return'] > 0 else "[blue]"
            table.add_row(
                f"RSI < {rsi_limit}",
                f"{r_color}{res['total_return']:+.2f}%[/]",
                f"{win_rate:.1f}%",
                f"{res['mdd']:.2f}%",
                f"{total_trades}건",
                f"{pf:.2f}"
            )
    
    config.console.print(table)
    config.console.print(f"\n[green]추천 (수익률):[/] RSI < {best_return_rsi} (수익률 {best_return:+.2f}%)")

    # === 익절/손절 최적화 모드 ===
    table = Table(title=f"\n익절/손절 비율 최적화 분석 ({name}) / 기준 매수 점수 ({buy_score}점)", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("익절/손절", justify="center")
    table.add_column("수익률", justify="right")
    table.add_column("승률", justify="right")
    table.add_column("MDD", justify="right")
    table.add_column("매매 횟수", justify="right")
    table.add_column("손익비", justify="right")
    
    best_return_set = None
    best_return = -999.0
    
    best_mdd_set = None
    best_mdd = -999.0
    
    # 테스트할 범위 설정
    tp_candidates = [10.0, 15.0, 20.0, 30.0, 40.0, 50.0]
    sl_candidates = [-3.0, -5.0, -7.0, -10.0, -15.0]

    row_count = 0
    total_combinations = len(tp_candidates) * len(sl_candidates)

    with config.console.status("[green]다양한 익절/손절 조합 시뮬레이션 중...[/]"):
        for tp in tp_candidates:
            for sl in sl_candidates:
                res = simulate_strategy(sim_df, prev_row_init, initial_capital, buy_score, buy_rsi, is_overseas, 
                                        stop_loss_rate=sl, take_profit_rate=tp,
                                        take_profit_rsi=take_profit_rsi, sell_score=sell_score,
                                        ts_activation_rate=ts_activation, ts_callback_rate=ts_callback)
                
                total_trades = len(res['trades'])
                sell_trades = res['win_trades'] + res['loss_trades']
                win_rate = (res['win_trades'] / sell_trades * 100) if sell_trades > 0 else 0.0
                pf = (res['gross_profit'] / res['gross_loss']) if res['gross_loss'] > 0 else (99.9 if res['gross_profit'] > 0 else 0.0)
                
                if res['total_return'] > best_return:
                    best_return = res['total_return']
                    best_return_set = (tp, sl)
                
                if res['mdd'] > best_mdd:
                    best_mdd = res['mdd']
                    best_mdd_set = (tp, sl)
                
                label = f"익절 +{tp}% / 손절 {sl}%"
                r_color = "[red]" if res['total_return'] > 0 else "[blue]"
                table.add_row(label, f"{r_color}{res['total_return']:+.2f}%[/]", f"{win_rate:.1f}%", f"{res['mdd']:.2f}%", f"{total_trades}건", f"{pf:.2f}")
                
                row_count += 1
                if row_count % 5 == 0 and row_count < total_combinations:
                    table.add_section()
    
    config.console.print(table)
    
    if best_return_set:
        config.console.print(f"\n[green]추천 (수익률):[/] 익절 +{best_return_set[0]}% / 손절 {best_return_set[1]}% (수익률 {best_return:+.2f}%)")
    if best_mdd_set:
        config.console.print(f"[cyan]추천 (안정성):[/] 익절 +{best_mdd_set[0]}% / 손절 {best_mdd_set[1]}% (MDD {best_mdd:.2f}%)")
        
    config.console.print("\n[bold red]경고: 과거의 성과가 미래의 수익을 보장하지는 않습니다.[/bold red]")