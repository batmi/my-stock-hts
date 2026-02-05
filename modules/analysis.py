# modules/analysis.py
import time
from datetime import datetime
from rich.table import Table
from rich import box
from rich.prompt import Prompt
import config
import api
import indicators

def classify_stock_state(price, ema20, ema60, ema120, sar, rsi, prev_rsi, adx, cci, obv_trend):
    if price is None or ema60 is None or sar is None or rsi is None: return "-", "[dim]"
    is_severe_danger = False
    # [수정] 위험 조건 완화: 장기(120)와 중기(60) 이평선을 모두 이탈해야 '위험'으로 간주 (변동성 감소)
    if ema120 is not None and price < ema120 and price < ema60: is_severe_danger = True
    elif rsi <= (config.INDICATOR_PARAMS["RSI_LOWER"] - 10): is_severe_danger = True # 위험 기준은 하한선보다 더 낮게 설정 (예: 20)
    # [수정] ADX 과열 중 RSI 하락은 '위험'보다는 '주의'로 이동
    if is_severe_danger: return "위험", "[blue]"
    
    is_caution = False
    # [수정] 주의 조건: 60일선 이탈 또는 120일선 이탈 중 하나라도 해당되면 '주의' (완충 구간)
    if price < ema60 or (ema120 is not None and price < ema120): is_caution = True
    elif sar > price: is_caution = True
    elif rsi >= (config.INDICATOR_PARAMS["RSI_UPPER"] + 10) or rsi <= config.INDICATOR_PARAMS["RSI_LOWER"]: is_caution = True
    elif adx is not None and prev_rsi is not None and adx >= 40 and rsi < prev_rsi: is_caution = True
    if is_caution: return "주의", "[yellow]"
    
    score = 0
    if ema20 is not None and price > ema20: score += 1
    if ema20 is not None and ema60 is not None and ema20 > ema60: score += 1
    if ema60 is not None and ema120 is not None and ema60 > ema120: score += 1
    if sar < price: score += 1
    if (config.INDICATOR_PARAMS["RSI_MID"] - 10) <= rsi <= (config.INDICATOR_PARAMS["RSI_MID"] + 5): score += 2
    elif (config.INDICATOR_PARAMS["RSI_MID"] + 5 < rsi <= config.INDICATOR_PARAMS["RSI_UPPER"] - 5) or (config.INDICATOR_PARAMS["RSI_LOWER"] <= rsi < config.INDICATOR_PARAMS["RSI_MID"] - 10): score += 1
    if adx is not None:
        # [수정] 보수적 접근: ADX 25 이상일 때만 +1점 (중복 점수 제거)
        if adx >= 25: score += 1
    if cci is not None:
        if cci > 0: score += 1
        if cci > config.INDICATOR_PARAMS["CCI_UPPER"]: score += 1
    
    # [추가] OBV가 이동평균선(설정값) 위에 있으면 수급 양호 (+1점)
    if obv_trend: score += 1

    # [수정] 매수 기준 완화 (9 -> 8) 및 RSI 과열 방지 (60 미만일 때만 매수)
    if score >= 8 and rsi < 60: return "매수", "[red]"
    elif score >= 6: return "상승", "[orange3]"
    else: return "관망", "[white]"

def print_table(title, data_list, is_overseas=False):
    is_domestic_etf = ("ETF" in title and not is_overseas)
    use_investor_data = False
    if not is_overseas and data_list:
        test_data = api.get_investor_trend(data_list[0][1])
        if test_data:
            sample = test_data[0]
            if any(api.safe_int(sample.get(k)) != 0 for k in ['prsn_ntby_qty', 'frgn_ntby_qty', 'orgn_ntby_qty']): use_investor_data = True
    
    table = Table(title=f"\n{title}", box=box.SIMPLE, show_header=True, header_style="bold white")
    table.add_column("종목명", justify="left", style="white", no_wrap=True)
    table.add_column("코드", justify="center", style="dim")
    table.add_column("분류", justify="center") 
    table.add_column("현재가", justify="right")
    col_header = "등락폭 (등락률)"
    if not is_overseas and not use_investor_data: col_header += " [강도]"
    table.add_column(col_header, justify="right")
    table.add_column("EMA(5)", justify="right")
    table.add_column("EMA(20)", justify="right")
    table.add_column("EMA(60)", justify="right")
    table.add_column("EMA(120)", justify="right")
    table.add_column("SAR", justify="right") 
    table.add_column("RSI", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("CCI", justify="right")
    
    is_us_stock = is_overseas and ("주식" in title)
    is_us_etf = is_overseas and ("ETF" in title)
    
    if not is_overseas:
        table.add_column("52주", justify="right")
        if not is_domestic_etf: table.add_column("외인률", justify="right", style="dim")
        if use_investor_data: table.add_column("개인/외인/기관", justify="center")
        else: table.add_column("OBV", justify="right")
    else:
        table.add_column("52주", justify="right")
        if is_us_stock:
            table.add_column("PER", justify="right", style="dim")
            table.add_column("PBR", justify="right", style="dim") 
        elif is_us_etf:
            table.add_column("상장주수", justify="right", style="dim")

    for name, code in data_list:
        curr_data = api.get_current_price_data(code, is_overseas)
        chart_df = api.get_chart_data(code, is_overseas)
        
        ind = indicators.calculate_indicators(chart_df)
        w52_pos_str, per_str, pbr_str, shar_str = "-", "-", "-", "-"
        foreign_rate_str = "-"
        inv_str = "-"
        cached_ex = config.EXCHANGE_CACHE.get(code, "NAS") if is_overseas else None
        strength_display = ""

        if not is_overseas:
            if use_investor_data:
                inv_list = api.get_investor_trend(code)
                if inv_list:
                    p = api.safe_int(inv_list[0].get('prsn_ntby_qty'))
                    f = api.safe_int(inv_list[0].get('frgn_ntby_qty'))
                    i = api.safe_int(inv_list[0].get('orgn_ntby_qty'))
                    def fmt_inv(val):
                        if val == 0: return "[dim]-[/dim]"
                        abs_val = abs(val)
                        if abs_val >= 1_000_000: s = f"{val/1_000_000:,.1f}M"
                        elif abs_val >= 1000: s = f"{val/1000:,.0f}K"
                        else: s = f"{val:,}"
                        return f"[red]{s}[/]" if val > 0 else f"[blue]{s}[/]"
                    inv_str = f"{fmt_inv(p)} {fmt_inv(f)} {fmt_inv(i)}"
            if not use_investor_data:
                try:
                    rt_strength = api.get_realtime_vol_strength(code, is_overseas, cached_ex)
                    if rt_strength is not None:
                        s_color = "[red]" if rt_strength >= 100 else "[blue]"
                        strength_display = f" {s_color}[{rt_strength:,.0f}%][/]"
                    else: strength_display = " [dim][0%][/dim]"
                except: strength_display = " [dim][0%][/dim]"
            if curr_data.get('rt_cd') == '0':
                out = curr_data.get('output', {})
                foreign_rate_str = f"{out.get('hts_frgn_ehrt', '-')}%"
                try:
                    h52, l52, c = float(out.get('w52_hgpr', 0)), float(out.get('w52_lwpr', 0)), float(out.get('stck_prpr', 0))
                    if h52 > l52:
                        pos = (c - l52)/(h52 - l52)*100
                        if pos >= 90: w_color = "[red]"
                        elif pos >= 80: w_color = "[orange3]"
                        elif pos <= 30: w_color = "[blue]"
                        elif pos <= 50: w_color = "[yellow]"
                        else: w_color = "[white]"
                        w52_pos_str = f"{w_color}{pos:.1f}%[/]"
                except: pass
        else:
            detail = api.fetch_overseas_detail_price(code, cached_ex)
            if detail:
                if is_us_stock: 
                    per_str = detail.get('perx', '-')
                    pbr_str = detail.get('pbrx', '-') if detail.get('pbrx') != '-' else '-'
                if is_us_etf:
                    try:
                        shar_val = float(detail.get('shar', 0))
                        shar_str = f"{shar_val/1_000_000:.1f}M" if shar_val >= 1_000_000 else f"{shar_val:,.0f}"
                    except: pass
                try:
                    h52, l52, c = float(detail.get('h52p', 0)), float(detail.get('l52p', 0)), float(detail.get('last', 0))
                    if h52 > l52:
                        pos = (c - l52)/(h52 - l52)*100
                        if pos >= 90: w_color = "[red]"
                        elif pos >= 80: w_color = "[orange3]"
                        elif pos <= 30: w_color = "[blue]"
                        elif pos <= 50: w_color = "[yellow]"
                        else: w_color = "[white]"
                        w52_pos_str = f"{w_color}{pos:.1f}%[/]"
                except: pass

        if curr_data.get('rt_cd') == '0':
            out = curr_data['output']
            if is_overseas:
                curr = float(out.get('last', 0) or 0)
                rate = float(out.get('rate', 0) or 0)
                diff = float(out.get('diff', 0) or 0)
                if rate < 0 and diff > 0: diff = -diff
                curr_fmt = f"${curr:,.2f}"
                diff_str = f"{diff:+.2f}"
            else:
                curr = int(out['stck_prpr'])
                rate = float(out['prdy_ctrt'])
                diff = int(out['prdy_vrss'])
                curr_fmt = f"{curr:,}"
                diff_str = f"{diff:+}"

            rate_color = "[red]" if rate > 0 else ("[blue]" if rate < 0 else "[white]")
            rate_str = f"{rate_color}{diff_str} ({rate:+.2f}%)[/]{strength_display}"

            prev_rsi_val = None
            if chart_df is not None and not chart_df.empty and len(chart_df) >= 16:
                delta = chart_df['close'].diff()
                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                try: prev_rsi_val = (100 - (100 / (1 + gain/loss))).iloc[-2]
                except: pass

            class_name, class_color = classify_stock_state(curr, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], prev_rsi_val, ind['adx'], ind['cci'], ind.get('obv_trend'))
            
            def fmt(v): return f"{v:,.2f}" if is_overseas else f"{int(v):,}"
            def fmt_idx(val): return f"{int(val):,}" if val is not None else "-"
            def fmt_idx_float(val): return f"{val:,.2f}" if val is not None else "-"

            curr_price_color = "[white]"
            if ind['ema_5'] and ind['ema_20'] and ind['ema_60']:
                if ind['ema_5'] > ind['ema_20'] and ind['ema_20'] > ind['ema_60']:
                    if curr > ind['ema_5']: curr_price_color = "[red]"
                    elif curr < ind['ema_60']: curr_price_color = "[blue]"
                    elif curr < ind['ema_5'] or curr < ind['ema_20']: curr_price_color = "[dim]"
                elif ind['ema_5'] < ind['ema_20'] and ind['ema_5'] < ind['ema_60']:
                    if curr < ind['ema_5']: curr_price_color = "[blue]"
                    elif curr > ind['ema_20'] or curr > ind['ema_60']: curr_price_color = "[orange3]"
                    elif curr > ind['ema_5']: curr_price_color = "[white]"
                else:
                    if curr < ind['ema_5']: curr_price_color = "[blue]"
                    elif curr > ind['ema_20']: curr_price_color = "[orange3]"
                    elif curr < ind['ema_20']: curr_price_color = "[white]"
            curr_str = f"{curr_price_color}{curr_fmt}[/]"

            ema_5_color = "[white]"
            if ind['ema_5'] and ind['ema_20'] and ind['ema_60'] and ind['ema_120']:
                if ind['ema_5'] > ind['ema_20'] and ind['ema_5'] > ind['ema_60'] and ind['ema_5'] > ind['ema_120']: ema_5_color = "[red]"
                elif ind['ema_5'] < ind['ema_20'] and ind['ema_5'] < ind['ema_60'] and ind['ema_5'] < ind['ema_120']: ema_5_color = "[blue]"
                elif (ind['ema_20'] < ind['ema_5'] < ind['ema_60']) or (ind['ema_60'] < ind['ema_5'] < ind['ema_20']): ema_5_color = "[yellow]"
                elif (ind['ema_60'] < ind['ema_5'] < ind['ema_120']) or (ind['ema_120'] < ind['ema_5'] < ind['ema_60']): ema_5_color = "[orange3]"
            ema_5_str = f"{ema_5_color}{fmt_idx(ind['ema_5'])}[/]"

            ema_20_color = "[white]"
            if ind['ema_20'] and ind['ema_60'] and ind['ema_120']:
                if ind['ema_20'] > ind['ema_60'] and ind['ema_20'] > ind['ema_120']: ema_20_color = "[red]"
                elif ind['ema_20'] < ind['ema_60'] and ind['ema_20'] < ind['ema_120']: ema_20_color = "[blue]"
                elif (ind['ema_60'] < ind['ema_20'] < ind['ema_120']) or (ind['ema_120'] < ind['ema_20'] < ind['ema_60']): ema_20_color = "[yellow]"
            ema_20_str = f"{ema_20_color}{fmt_idx(ind['ema_20'])}[/]"

            ema_60_color = "[yellow]"
            if ind['ema_60'] and ind['ema_5'] and ind['ema_20'] and ind['ema_120']:
                if ind['ema_120'] > ind['ema_60'] and ind['ema_60'] > ind['ema_5'] and ind['ema_60'] > ind['ema_20']: ema_60_color = "[blue]"
                elif ind['ema_120'] < ind['ema_60'] and ind['ema_60'] < ind['ema_5'] and ind['ema_60'] < ind['ema_20']: ema_60_color = "[red]"
            ema_60_str = f"{ema_60_color}{fmt_idx(ind['ema_60'])}[/]"
            
            ema_120_color = "[white]"
            if ind['ema_120'] and ind['ema_60']:
                if ind['ema_60'] > ind['ema_120']: ema_120_color = "[red]" 
                elif ind['ema_60'] < ind['ema_120']: ema_120_color = "[blue]"
            ema_120_str = f"{ema_120_color}{fmt_idx(ind['ema_120'])}[/]"

            sar_str = "-"
            if ind['psar'] is not None:
                sar_val_str = fmt_idx_float(ind['psar']) if is_overseas else fmt_idx(ind['psar'])
                sar_str = f"[red]{sar_val_str}[/]" if curr > ind['psar'] else f"[blue]{sar_val_str}[/]"

            rsi_str = f"{ind['rsi']:.1f}" if ind['rsi'] is not None else "-"
            if ind['rsi'] is not None:
                if ind['rsi'] >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[magenta]{rsi_str}[/]"
                elif 55 <= ind['rsi'] < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[red]{rsi_str}[/]"
                elif 45 <= ind['rsi'] < 55: rsi_str = f"[orange3]{rsi_str}[/]"
                elif config.INDICATOR_PARAMS["RSI_LOWER"] < ind['rsi'] < 45: rsi_str = f"[yellow]{rsi_str}[/]"
                else: rsi_str = f"[blue]{rsi_str}[/]"

            adx_str = f"{ind['adx']:.1f}" if ind['adx'] is not None else "-"
            if ind['adx'] is not None:
                if ind['adx'] >= 40: adx_str = f"[magenta]{adx_str}[/]" 
                elif ind['adx'] >= 30: adx_str = f"[red]{adx_str}[/]"     
                elif ind['adx'] >= 20: adx_str = f"[orange3]{adx_str}[/]"
                elif ind['adx'] >= 15: adx_str = f"[yellow]{adx_str}[/]"
                else: adx_str = f"[white]{adx_str}[/]"

            cci_str = f"{ind['cci']:.1f}" if ind['cci'] is not None else "-"
            if ind['cci'] is not None:
                if ind['cci'] >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[red]{cci_str}[/]"
                elif 0 < ind['cci'] < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[orange3]{cci_str}[/]"
                elif config.INDICATOR_PARAMS["CCI_LOWER"] < ind['cci'] <= 0: cci_str = f"[yellow]{cci_str}[/]"
                else: cci_str = f"[blue]{cci_str}[/]"

            final_name_str = name
            if ind['ema_5'] and ind['ema_20'] and ind['ema_60'] and ind['adx'] and ind['rsi'] and ind['cci']:
                all_ema_green = (ind['ema_5'] > ind['ema_20'] and ind['ema_20'] > ind['ema_60'])
                all_ema_red = (ind['ema_5'] < ind['ema_20'] and ind['ema_20'] < ind['ema_60'])
                price_above_ema5 = (curr > ind['ema_5'])
                if ind['adx'] >= 40 and ind['rsi'] >= config.INDICATOR_PARAMS["RSI_UPPER"] and ind['cci'] >= config.INDICATOR_PARAMS["CCI_UPPER"]: final_name_str = f"[magenta]{name}[/]"
                elif all_ema_green and price_above_ema5 and ind['adx'] >= 30 and ind['rsi'] >= 55 and ind['cci'] >= config.INDICATOR_PARAMS["CCI_UPPER"]: final_name_str = f"[red]{name}[/]"
                elif all_ema_red and price_above_ema5 and ind['adx'] >= 20 and ind['rsi'] >= 45 and ind['cci'] >= 0: final_name_str = f"[orange3]{name}[/]"
                elif (ind['ema_20'] > ind['ema_60'] and ind['ema_60'] > ind['ema_5']) and ind['adx'] >= 30 and ind['rsi'] <= config.INDICATOR_PARAMS["RSI_LOWER"] and ind['cci'] <= config.INDICATOR_PARAMS["CCI_UPPER"]: final_name_str = f"[blue]{name}[/]"

            row_data = [final_name_str, f"{code}", f"{class_color}{class_name}[/]", curr_str, rate_str, ema_5_str, ema_20_str, ema_60_str, ema_120_str, sar_str, rsi_str, adx_str, cci_str]
            if not is_overseas:
                row_data.append(w52_pos_str)
                if not is_domestic_etf: row_data.append(foreign_rate_str)
                obv_display = f"{'[red]' if ind.get('obv_trend') else '[blue]'}{int(ind['obv']/1000):,}K[/]"
                row_data.append(inv_str if use_investor_data else obv_display)
            else:
                row_data.append(w52_pos_str)
                if is_us_stock: row_data.extend([per_str, pbr_str])
                elif is_us_etf: row_data.append(shar_str)
            table.add_row(*row_data)
        else:
            table.add_row(name, code, "-", "실패", *["-"] * (14 if not is_overseas else (12 if is_us_stock else 11)))

    config.console.print(table)

def show_stock_analysis():
    config.console.print("\n[bold]분석할 종목 그룹을 선택하세요:[/bold]")
    config.console.print("[1] 국내 주식")
    config.console.print("[2] 국내 ETF")
    config.console.print("[3] 미국 주식")
    config.console.print("[4] 미국 ETF")
    config.console.print("[5] 전체 보기")
    valid_choices = ["1", "2", "3", "4", "5", "12", "34", "11", "22", "33", "44", "55", "q", "Q"]
    config.console.print()
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=valid_choices, default="5", show_choices=True)
    if choice.lower() == 'q': return
    interval = 0
    real_choice = choice
    if choice in ["11", "22", "33", "44", "55"]: interval = 60; real_choice = choice[0] 
    elif choice in ["12", "34"]: real_choice = choice

    def get_list(key):
        return [(x['name'], x['code']) for x in config.STOCK_CONFIG_DATA.get(key, [])]

    stocks_kr = get_list('stocks_kr')
    etfs_kr = get_list('etfs_kr')
    stocks_us = get_list('stocks_us')
    etfs_us = get_list('etfs_us')

    if real_choice == "1": target_list = [("국내 주식 기술적 분석", stocks_kr, False)]
    elif real_choice == "2": target_list = [("국내 ETF 기술적 분석", etfs_kr, False)]
    elif real_choice == "3": target_list = [("미국 주식 기술적 분석", stocks_us, True)]
    elif real_choice == "4": target_list = [("미국 ETF 기술적 분석", etfs_us, True)]
    elif real_choice == "12": target_list = [("국내 주식 기술적 분석", stocks_kr, False), ("국내 ETF 기술적 분석", etfs_kr, False)]
    elif real_choice == "34": target_list = [("미국 주식 기술적 분석", stocks_us, True), ("미국 ETF 기술적 분석", etfs_us, True)]
    else: target_list = [("국내 주식 기술적 분석", stocks_kr, False), ("국내 ETF 기술적 분석", etfs_kr, False), ("미국 주식 기술적 분석", stocks_us, True), ("미국 ETF 기술적 분석", etfs_us, True)]

    try:
        while True:
            if interval > 0:
                now_str = datetime.now().strftime("%H:%M:%S")
                config.console.print(f"\n[dim]조회 시간: {now_str}[/dim]")
            with config.console.status("[bold green]종목 분석 중 (KIS API 데이터 수신)...[/bold green]"):
                for title, d_list, is_ovs in target_list:
                    if d_list: print_table(title, d_list, is_ovs)
            if interval == 0: break
            config.console.print() 
            try:
                for remaining in range(interval, -1, -1):
                    config.console.print(f"[bold yellow]다음 조회까지 {remaining}초 대기 중입니다. (중단: Ctrl+C)[/]   ", end="\r")
                    time.sleep(1)
            except KeyboardInterrupt:
                config.console.print("\n[yellow]반복 조회를 중단하고 메뉴로 돌아갑니다.[/yellow]")
                break
    except KeyboardInterrupt: config.console.print("\n[yellow]작업이 취소되었습니다.[/yellow]")
