# modules/backtest.py
import pandas as pd
import numpy as np
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich import box
import yfinance as yf
from datetime import datetime, timedelta
import config
import api
import utils
import indicators

def calculate_daily_score(row, prev_row):
    """analysis.py의 로직을 기반으로 일별 점수 계산 (보수적 기준 적용)"""
    price = row['close']
    ema20 = row['EMA20']
    ema60 = row['EMA60']
    ema120 = row['EMA120']
    sar = row['SAR']
    rsi = row['RSI']
    adx = row['ADX']
    cci = row['CCI']
    
    # OBV Trend
    obv = row['OBV']
    obv_ma = row['OBV_MA']
    obv_trend = (obv > obv_ma)

    # Previous RSI for divergence check
    prev_rsi = prev_row['RSI'] if prev_row is not None else None

    # 1. 점수 계산 (raw_score) - 필터링 전 순수 점수
    raw_score = 0
    if ema20 is not None and price > ema20: raw_score += 1
    if ema20 is not None and ema60 is not None and ema20 > ema60: raw_score += 1
    if ema60 is not None and ema120 is not None and ema60 > ema120: raw_score += 1
    if sar < price: raw_score += 1
    
    # RSI Score
    if (config.INDICATOR_PARAMS["RSI_MID"] - 10) <= rsi <= (config.INDICATOR_PARAMS["RSI_MID"] + 5): raw_score += 2
    elif (config.INDICATOR_PARAMS["RSI_MID"] + 5 < rsi <= config.INDICATOR_PARAMS["RSI_UPPER"] - 5) or (config.INDICATOR_PARAMS["RSI_LOWER"] <= rsi < config.INDICATOR_PARAMS["RSI_MID"] - 10): raw_score += 1
    
    # ADX Score (Conservative: >= 25)
    if adx is not None and adx >= 25: raw_score += 1
    
    # CCI Score
    if cci is not None:
        if cci > 0: raw_score += 1
        if cci > config.INDICATOR_PARAMS["CCI_UPPER"]: raw_score += 1
    
    # OBV Score
    if obv_trend: raw_score += 1

    # 2. 위험/주의 필터링 (analysis.py 로직) -> final_score 결정
    final_score = raw_score

    # 위험 (Severe Danger)
    if ema120 is not None and price < ema120 and price < ema60: final_score = 0
    elif rsi <= (config.INDICATOR_PARAMS["RSI_LOWER"] - 10): final_score = 0
    
    else:
        # 주의 (Caution) -> 점수를 0으로 처리하여 매도 유도
        is_caution = False
        if price < ema60 or (ema120 is not None and price < ema120): is_caution = True
        elif sar > price: is_caution = True
        elif rsi >= (config.INDICATOR_PARAMS["RSI_UPPER"] + 10) or rsi <= config.INDICATOR_PARAMS["RSI_LOWER"]: is_caution = True
        elif adx is not None and prev_rsi is not None and adx >= 40 and rsi < prev_rsi: is_caution = True
        
        if is_caution: final_score = 0
    
    return final_score, raw_score

def get_backtest_data(code, is_overseas, days):
    """백테스팅용 데이터 조회 (yfinance 우선 사용 -> KIS API 실패 시 사용)"""
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
            if config.DEBUG_LEVEL == "TRACE":
                config.console.print(f"[dim cyan][TRACE] REQ (yfinance) | Ticker: {t} | Start: {start_str}[/dim cyan]")
            
            df = yf.download(t, start=start_str, progress=False, threads=False)
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

def run_backtest():
    config.console.print("\n[bold magenta]=== 전략 백테스팅 (Backtest) ===[/]")
    
    # 1. 종목 선택
    code, name, is_overseas = utils.select_stock_for_chart()
    if not code: return

    # 2. 설정 입력
    days_input = Prompt.ask("분석 기간 (일 단위)", default="365")
    try:
        days = int(days_input)
    except:
        days = 365
    
    # [수정] 초기 자본금 및 환율 설정
    initial_capital_krw = 10_000_000
    exchange_rate = 1.0
    
    if is_overseas:
        exchange_rate = config.DEFAULT_EXCHANGE_RATE
        initial_capital = initial_capital_krw / exchange_rate
    else:
        initial_capital = initial_capital_krw

    # 3. 데이터 준비
    with config.console.status(f"[bold green]{name} ({code}) 데이터 분석 중...[/]"):
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

        # 분석 기간 필터링
        # [수정] 행 개수 기준이 아닌 날짜 기준으로 필터링
        cutoff_dt = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff_dt.strftime("%Y%m%d")
        
        start_idx = 0
        mask = df['date'] >= cutoff_str
        if mask.any():
            start_idx = mask.idxmax()
        
        sim_df = df.iloc[start_idx:].copy()
        
        # [수정] 경고 로직: 실제 기간(일수)과 요청 기간 비교
        if not sim_df.empty:
            actual_days = (datetime.strptime(str(sim_df.iloc[-1]['date']), "%Y%m%d") - datetime.strptime(str(sim_df.iloc[0]['date']), "%Y%m%d")).days
            if actual_days < days * 0.9: # 90% 미만일 때만 경고
                config.console.print(f"[dim yellow]주의: 요청 기간({days}일)보다 실제 분석 기간({actual_days}일)이 짧습니다.[/dim yellow]")
                d_start = str(sim_df.iloc[0]['date'])
                d_start_fmt = f"{d_start[:4]}-{d_start[4:6]}-{d_start[6:]}"
                config.console.print(f"[dim yellow]      (데이터 시작일: {d_start_fmt} - 신규 상장 종목이거나 과거 데이터가 부족합니다)[/dim yellow]")
        
        # 시뮬레이션 변수
        balance = initial_capital
        holdings = 0
        avg_price = 0
        trades = []
        buy_date = None
        
        # [진단용] 통계 변수
        max_score_observed = 0
        score_8_count = 0  # 8점 이상 횟수 (아까운 경우)
        
        # Config 설정값 로드
        buy_score_limit = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        rise_score_limit = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        buy_rsi_limit = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        
        # [추가] 매도 설정값 로드
        stop_loss_limit = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        take_profit_limit = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        take_profit_rsi_limit = config.SELL_STRATEGY["TAKE_PROFIT_RSI"]
        sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]

        # [추가] MDD 및 승률 계산 변수
        peak_asset = initial_capital
        mdd = 0.0
        win_trades = 0
        loss_trades = 0
        gross_profit = 0
        gross_loss = 0
        cum_profit = 0
        daily_assets = []
        
        # 시뮬레이션 루프
        # prev_row 초기화: 시뮬레이션 시작 전일 데이터가 있으면 사용
        prev_row = df.iloc[start_idx-1] if start_idx > 0 else None
        
        for i in range(len(sim_df)):
            row = sim_df.iloc[i]
            date = row['date']
            price = row['close']
            
            # [추가] 일별 자산 평가 및 MDD 갱신
            current_asset = balance + (holdings * price)
            daily_assets.append(current_asset)
            if current_asset > peak_asset: peak_asset = current_asset
            dd = (current_asset - peak_asset) / peak_asset * 100
            if dd < mdd: mdd = dd
            
            # 점수 계산
            score, raw_score = calculate_daily_score(row, prev_row)
            
            # [진단] 최고 점수 기록
            if score > max_score_observed: max_score_observed = score
            if score >= buy_score_limit: score_8_count += 1
            
            # 매매 로직
            action = None
            profit_rate = 0.0
            
            # [매수 조건] 설정된 점수 이상 AND RSI 과열 미만 AND 미보유
            if holdings == 0:
                if score >= buy_score_limit and row['RSI'] < buy_rsi_limit:
                    qty = int(balance / price)
                    if qty > 0:
                        cost = qty * price
                        balance -= cost
                        holdings = qty
                        avg_price = price
                        buy_date = date
                        action = "매수"
                        trades.append({"date": date, "type": "매수", "price": price, "qty": qty, "balance": balance, "profit": 0, "profit_amt": 0, "days": 0, "score": raw_score, "rsi": row['RSI'], "cum_profit": cum_profit})
            
            # [매도 조건] 보유 중일 때
            elif holdings > 0:
                # 1. 손절매
                loss_rate = (price - avg_price) / avg_price * 100
                is_stop_loss = loss_rate <= stop_loss_limit
                
                # 2. 점수 하락
                is_score_drop = score < sell_score_limit

                # 3. 익절 (Take Profit or RSI 과열)
                is_take_profit = loss_rate >= take_profit_limit or row['RSI'] > take_profit_rsi_limit
                
                if is_stop_loss or is_score_drop or is_take_profit:
                    sell_amt = holdings * price
                    # 수수료/세금 약 0.23% 가정
                    fee = sell_amt * 0.0023
                    if not is_overseas: fee = int(fee)
                    sell_amt -= fee
                    
                    profit = sell_amt - (holdings * avg_price)
                    profit_rate = (profit / (holdings * avg_price)) * 100
                    
                    # [추가] 승패 카운트
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
                    action = "매도"
                    reason = "손절" if is_stop_loss else ("익절" if is_take_profit else "점수하락")
                    trades.append({"date": date, "type": f"매도({reason})", "price": price, "qty": sold_qty, "balance": balance, "profit": profit_rate, "profit_amt": profit, "days": holding_days, "score": raw_score, "rsi": row['RSI'], "cum_profit": cum_profit})
            
            prev_row = row

    # 4. 결과 출력
    final_asset = balance + (holdings * sim_df.iloc[-1]['close'])
    total_return = (final_asset - initial_capital) / initial_capital * 100
    
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
    win_rate = (win_trades / sell_count * 100) if sell_count > 0 else 0.0
    summary_table.add_row("총 매매 횟수", f"{len(trades)}건 (진입 {len(trades)-sell_count} / 청산 {sell_count})")
    
    if sell_count > 0:
        summary_table.add_row("승률 (Win Rate)", f"{win_rate:.1f}% ({win_trades}승 {loss_trades}패)")
        
        # [순서 변경] 평균 수익률 먼저 출력
        sell_trades = [t for t in trades if t['type'].startswith("매도")]
        profits = [t['profit'] for t in sell_trades if t['profit'] > 0]
        losses = [t['profit'] for t in sell_trades if t['profit'] <= 0]
        
        avg_profit = sum(profits) / len(profits) if profits else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        
        summary_table.add_row("평균 수익률", f"[red]{avg_profit:+.2f}%[/] / 평균 손실률: [blue]{avg_loss:+.2f}%[/]")
        
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
        config.console.print("\n[bold yellow]※ 매매가 발생하지 않았습니다. (조건 미충족)[/bold yellow]")
        config.console.print(f"  - 기간 내 최고 점수: [bold]{max_score_observed}점[/bold] (매수 기준: {buy_score_limit}점)")
        config.console.print(f"  - {buy_score_limit}점 이상 도달 횟수: {score_8_count}회")
        config.console.print(f"  [안내] 현재 설정된 매수 조건({buy_score_limit}점 이상 & RSI<{buy_rsi_limit})이 엄격하여 진입 기회가 없었습니다.")
        config.console.print("  [Tip] config.py 에서 매수 조건을 완화하거나 분석 기간을 늘려보세요.")
    
    if trades:
        config.console.print()
        t_table = Table(title="[상세 매매 일지]", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        t_table.add_column("일자"); t_table.add_column("구분"); t_table.add_column("점수(RSI)", justify="center"); t_table.add_column("수량", justify="right"); t_table.add_column("단가", justify="right"); t_table.add_column("수익금", justify="right"); t_table.add_column("수익률", justify="right"); t_table.add_column("누적손익", justify="right")
        for t in trades:
            p_str = f"{t['profit']:+.2f}%" if t['type'].startswith("매도") else "-"
            p_color = "[red]" if t['profit'] > 0 else ("[blue]" if t['profit'] < 0 else "")
            profit_display = f"{p_color}{p_str}[/]" if p_color else p_str
            type_color = "[red]" if "매수" in t['type'] else "[blue]"
            date_str = str(t['date'])
            if len(date_str) == 8 and date_str.isdigit(): date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            
            score_rsi = f"{t.get('score', 0)} ({t.get('rsi', 0):.0f})"
            
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
            
            t_table.add_row(date_str[:10], f"{type_color}{t['type']}[/]", score_rsi, qty_str, price_str, amt_str, profit_display, cum_p_str)
        config.console.print(t_table)