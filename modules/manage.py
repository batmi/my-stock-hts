# modules/manage.py
import logging
from rich.prompt import Prompt
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
import time
from datetime import datetime
import pandas as pd
import config
import context # [추가]
import api
import utils
import constants
import re

logger = logging.getLogger(__name__)

def show_extended_info(code, is_overseas, basic_output=None):
    # [로그] 상세 정보 조회 시작
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 상세/기간별 시세 정보 조회 요청: {code} (Overseas: {is_overseas})[/dim cyan]")

    def _fmt_vol(v):
        val = float(v)
        if val == 0: return "0"
        if val >= 1_000_000: return f"{val/1_000_000:,.1f}M"
        if val >= 1_000: return f"{val/1_000:,.0f}K"
        return f"{val:,.0f}"

    if not is_overseas:
        if basic_output:
            config.console.print()
            # [수정] 전체 정보 출력 + 그룹화 + 줄무늬 스타일 적용
            table = Table(title="[국내주식] 상세 정보 (전체)", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
            table.add_column("항목", justify="left", style="cyan")
            table.add_column("값", justify="right")
            table.add_column("항목", justify="left", style="cyan")
            table.add_column("값", justify="right")
            
            # 그룹 정의 (논리적 순서)
            groups = [
                # 1. 기본/상태
                ["rprs_mrkt_kor_name", "bstp_kor_isnm", "iscd_stat_cls_code", "stck_shrn_iscd", "stac_month", "lstn_stcn", "cpfn", "stck_fcam"],
                # 2. 가격 정보
                ["stck_prpr", "prdy_vrss", "prdy_ctrt", "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_mxpr", "stck_llam", "stck_sdpr"],
                # 3. 거래/수급
                ["acml_vol", "acml_tr_pbmn", "prdy_vrss_vol_rate", "vol_tnrt", "hts_frgn_ehrt", "frgn_ntby_qty", "pgtr_ntby_qty", "whol_loan_rmnd_rate"],
                # 4. 투자지표
                ["hts_avls", "per", "pbr", "eps", "bps", "d250_hgpr", "d250_lwpr", "w52_hgpr", "w52_lwpr"]
            ]

            def _fmt(v, label):
                s = str(v).strip()
                if label.endswith("일자") and len(s) == 8 and s.isdigit(): return f"{s[:4]}/{s[4:6]}/{s[6:]}"
                if s.replace('-','').isdigit(): return f"{int(s):,}"
                return s

            # 전체 키 추적용
            processed_keys = set()
            
            # (1) 그룹별 출력
            for group_keys in groups:
                items = []
                for k in group_keys:
                    if k in basic_output:
                        items.append((k, basic_output[k]))
                        processed_keys.add(k)
                
                if items:
                    for i in range(0, len(items), 2):
                        k1, v1 = items[i]
                        label1 = constants.FIELD_MAP_DOMESTIC.get(k1, k1)
                        k2, v2 = items[i+1] if i+1 < len(items) else ("", "")
                        label2 = constants.FIELD_MAP_DOMESTIC.get(k2, k2) if k2 else ""
                        table.add_row(label1, _fmt(v1, label1), label2, _fmt(v2, label2) if k2 else "")
                    table.add_section()

            # (2) 나머지(기타) 출력
            remaining = []
            for k, v in basic_output.items():
                if k not in processed_keys:
                    remaining.append((k, v))
            
            if remaining:
                for i in range(0, len(remaining), 2):
                    k1, v1 = remaining[i]
                    label1 = constants.FIELD_MAP_DOMESTIC.get(k1, k1)
                    k2, v2 = remaining[i+1] if i+1 < len(remaining) else ("", "")
                    label2 = constants.FIELD_MAP_DOMESTIC.get(k2, k2) if k2 else ""
                    table.add_row(label1, _fmt(v1, label1), label2, _fmt(v2, label2) if k2 else "")

            config.console.print(table)

        # [수정] 기간별 시세 출력 (analysis.py와 동일한 형식 및 로직 적용)
        df = None
        investor_map = {} # [추가]

        # [수정] 단순 조회이므로 status 사용
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]기간별 시세 데이터 조회 중...[/cyan]", total=None)
            df = api.get_chart_data(code, is_overseas=False)
            # [추가] 수급 데이터 조회
            try:
                inv_data = api.get_investor_trend(code)
                if inv_data:
                    for item in inv_data:
                        investor_map[item['stck_bsop_date']] = item
                        
                # 외인 소진율 데이터 조회 및 병합 (최근 30일)
                frate_data = api.get_daily_foreign_rate(code)
                if frate_data:
                    for item in frate_data:
                        d_key = item['stck_bsop_date']
                        if d_key in investor_map:
                            investor_map[d_key]['hts_frgn_ehrt'] = item.get('hts_frgn_ehrt')
                        else:
                            investor_map[d_key] = {'hts_frgn_ehrt': item.get('hts_frgn_ehrt')}
                            
                # [추가] 실제 외국인 지분율 역산 로직 (상장주수, 외국인보유수량 기반)
                frgn_rates_map = {}
                cp_res = api.get_current_price_data(code, is_overseas=False)
                if cp_res.get('rt_cd') == '0':
                    out = cp_res['output']
                    lstn_stcn = api.safe_int(out.get('lstn_stcn'))
                    frgn_hldn_qty = api.safe_int(out.get('frgn_hldn_qty'))
                    
                    if lstn_stcn > 0:
                        sorted_dates = sorted(list(investor_map.keys()), reverse=True)
                        current_hldn = frgn_hldn_qty
                        for d_key in sorted_dates:
                            frgn_rates_map[d_key] = (current_hldn / lstn_stcn) * 100
                            # 과거로 가면서 해당 일자의 순매수를 빼줌 (과거 보유량 = 현재 보유량 - 최근 순매수량)
                            f_net = api.safe_int(investor_map[d_key].get('frgn_ntby_qty'))
                            current_hldn -= f_net
            except: pass
        
        if df is not None and not df.empty:
            # 이동평균선 계산
            for w in [5, 20, 60, 120]:
                df[f'ma{w}'] = df['close'].rolling(window=w).mean()

            # 등락폭/등락률 계산
            df['diff'] = df['close'].diff()
            df['rate'] = df['close'].pct_change() * 100

            # 최신순 정렬 및 20개 추출
            recent_df = df.sort_values('date', ascending=False).head(20)

            config.console.print()
            table_d = Table(title="[국내주식] 기간별 시세 (최근 20일)", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
            table_d.add_column("일자", justify="center")
            table_d.add_column("종가", justify="right")
            table_d.add_column("등락폭 (등락률)", justify="right")
            table_d.add_column("시가", justify="right")
            table_d.add_column("고가", justify="right")
            table_d.add_column("저가", justify="right")
            table_d.add_column("5일선", justify="right")
            table_d.add_column("20일선", justify="right")
            table_d.add_column("60일선", justify="right")
            table_d.add_column("120일선", justify="right")
            table_d.add_column("거래량", justify="right") # [이동]
            table_d.add_column("외인률", justify="right") # [추가]
            table_d.add_column("수급(개/외/기)", justify="center") # [수정]
            
            for i, (idx, row) in enumerate(recent_df.iterrows()):
                date_str = str(row['date'])
                if len(date_str) == 8: date_str = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
                
                close = row['close']
                diff = row['diff'] if not pd.isna(row['diff']) else 0
                rate = row['rate'] if not pd.isna(row['rate']) else 0
                
                def fmt_p(val): return f"{int(val):,}"
                def fmt_diff(val): return f"{int(val):+}"
                
                c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                diff_str = f"{c_color}{fmt_diff(diff)} ({rate:+.2f}%)[/]"
                
                ma5_val, ma20_val = row['ma5'], row['ma20']
                ma60_val, ma120_val = row['ma60'], row['ma120']

                def get_ma_color(val, ma_type):
                    if pd.isna(val): return "white"
                    
                    if ma_type == 5:
                        if pd.isna(ma20_val): return "white"
                        return "red" if val > ma20_val else "blue"
                    elif ma_type == 20:
                        if pd.isna(ma60_val): return "white"
                        return "red" if val > ma60_val else "blue"
                    elif ma_type == 60:
                        if pd.isna(ma120_val): return "white"
                        return "red" if val > ma120_val else "blue"
                    elif ma_type == 120:
                        if i + 1 < len(recent_df):
                            prev_ma120 = recent_df.iloc[i+1]['ma120']
                            if not pd.isna(prev_ma120):
                                return "red" if val > prev_ma120 else "blue"
                        return "white"
                    return "white"

                def fmt_ma(val, color):
                    if pd.isna(val): return "-"
                    return f"[{color}]{fmt_p(val)}[/]"

                # [추가] 수급 데이터 포맷팅
                inv_str = "-"
                foreign_rate_str = "-"
                d_key = str(row['date']).replace('-', '')[:8]
                if d_key in investor_map:
                    item = investor_map[d_key]
                    p = api.safe_int(item.get('prsn_ntby_qty'))
                    f = api.safe_int(item.get('frgn_ntby_qty'))
                    o = api.safe_int(item.get('orgn_ntby_qty'))
                    
                    # [수정] 역산된 실제 외국인 지분율 우선 적용, 실패 시 기존 소진율 사용
                    if d_key in frgn_rates_map:
                        foreign_rate_str = f"{frgn_rates_map[d_key]:.2f}%"
                    else:
                        f_rate = item.get('hts_frgn_ehrt')
                        if f_rate is not None and str(f_rate).strip():
                            try: foreign_rate_str = f"{float(f_rate):.2f}%"
                            except: pass

                    def _fmt_i(val):
                        if val == 0: return "[dim]-[/dim]"
                        abs_val = abs(val)
                        if abs_val >= 1_000_000: s = f"{val/1_000_000:,.1f}M"
                        elif abs_val >= 1000: s = f"{val/1000:,.0f}K"
                        else: s = f"{val:,}"
                        return f"[red]{s}[/]" if val > 0 else f"[blue]{s}[/]"
                    
                    inv_str = f"{_fmt_i(p)} {_fmt_i(f)} {_fmt_i(o)}"

                table_d.add_row(
                    date_str, fmt_p(close), diff_str, fmt_p(row['open']), fmt_p(row['high']), fmt_p(row['low']),
                    fmt_ma(ma5_val, get_ma_color(ma5_val, 5)), fmt_ma(ma20_val, get_ma_color(ma20_val, 20)),
                    fmt_ma(ma60_val, get_ma_color(ma60_val, 60)), fmt_ma(ma120_val, get_ma_color(ma120_val, 120)),
                    _fmt_vol(row['volume']), foreign_rate_str, inv_str
                )
                
                if (i + 1) % 5 == 0 and (i + 1) < len(recent_df):
                    table_d.add_section()
            config.console.print(table_d)
        else:
            config.console.print("[yellow]기간별 시세 데이터가 없습니다.[/yellow]")

    else:
        excd = config.session.exchange_cache.get(code, "NAS")
        detail_data = api.fetch_overseas_detail_price(code, excd)
        if detail_data:
            config.console.print()
            table = Table(title="[해외주식] 현재가 상세 정보 (전체)", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
            table.add_column("항목", justify="left", style="cyan")
            table.add_column("값", justify="right")
            table.add_column("항목", justify="left", style="cyan")
            table.add_column("값", justify="right")
            
            groups = [
                # 1. 기본
                ["rsym", "ovrs_nm", "excd", "curr", "zdiv", "vnit", "e_ordyn", "e_icod"],
                # 2. 가격
                ["last", "diff", "rate", "open", "high", "low", "base", "uplp", "dnlp"],
                # 3. 거래/재무
                ["tvol", "tamt", "tomv", "mcap", "shar", "perx", "pbrx", "epsx", "bpsx"],
                # 4. 52주
                ["h52p", "l52p", "h52d", "l52d"],
                # 5. 원화 환산
                ["t_xprc", "t_xdif", "t_xrat", "t_rate"]
            ]

            def _fmt(v, label):
                s = str(v).strip()
                if label.endswith("일자") and len(s) == 8 and s.isdigit(): return f"{s[:4]}/{s[4:6]}/{s[6:]}"
                try:
                    if s.replace('-','').isdigit(): return f"{int(s):,}"
                    if s.replace('.','',1).replace('-','').isdigit(): return f"{float(s):,.2f}"
                except: pass
                return s

            processed_keys = set()
            for group_keys in groups:
                items = []
                for k in group_keys:
                    if k in detail_data:
                        items.append((k, detail_data[k]))
                        processed_keys.add(k)
                
                if items:
                    for i in range(0, len(items), 2):
                        k1, v1 = items[i]
                        label1 = constants.FIELD_MAP_OVERSEAS_DETAIL.get(k1, k1)
                        k2, v2 = items[i+1] if i+1 < len(items) else ("", "")
                        label2 = constants.FIELD_MAP_OVERSEAS_DETAIL.get(k2, k2) if k2 else ""
                        table.add_row(label1, _fmt(v1, label1), label2, _fmt(v2, label2) if k2 else "")
                    table.add_section()

            remaining = []
            for k, v in detail_data.items():
                if k not in processed_keys: remaining.append((k, v))
            
            if remaining:
                for i in range(0, len(remaining), 2):
                    k1, v1 = remaining[i]
                    label1 = constants.FIELD_MAP_OVERSEAS_DETAIL.get(k1, k1)
                    k2, v2 = remaining[i+1] if i+1 < len(remaining) else ("", "")
                    label2 = constants.FIELD_MAP_OVERSEAS_DETAIL.get(k2, k2) if k2 else ""
                    table.add_row(label1, _fmt(v1, label1), label2, _fmt(v2, label2) if k2 else "")

            config.console.print(table)
        
        # [수정] 해외 주식 기간별 시세 출력 (analysis.py와 동일한 형식 및 로직 적용)
        df = None
        # [수정] 단순 조회이므로 status 사용
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[cyan]기간별 시세 데이터 조회 중...[/cyan]", total=None)
            df = api.get_chart_data(code, is_overseas=True)
        
        if df is not None and not df.empty:
            # 이동평균선 계산
            for w in [5, 20, 60, 120]:
                df[f'ma{w}'] = df['close'].rolling(window=w).mean()

            # 등락폭/등락률 계산
            df['diff'] = df['close'].diff()
            df['rate'] = df['close'].pct_change() * 100

            # 최신순 정렬 및 20개 추출
            recent_df = df.sort_values('date', ascending=False).head(20)

            config.console.print()
            table_d = Table(title="[해외주식] 기간별 시세 (최근 20일)", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
            table_d.add_column("일자", justify="center")
            table_d.add_column("종가", justify="right")
            table_d.add_column("등락폭 (등락률)", justify="right")
            table_d.add_column("시가", justify="right")
            table_d.add_column("고가", justify="right")
            table_d.add_column("저가", justify="right")
            table_d.add_column("거래량", justify="right")
            table_d.add_column("5일선", justify="right")
            table_d.add_column("20일선", justify="right")
            table_d.add_column("60일선", justify="right")
            table_d.add_column("120일선", justify="right")
            
            for i, (idx, row) in enumerate(recent_df.iterrows()):
                date_str = str(row['date'])
                if len(date_str) == 8: date_str = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
                
                close = row['close']
                diff = row['diff'] if not pd.isna(row['diff']) else 0
                rate = row['rate'] if not pd.isna(row['rate']) else 0
                
                def fmt_p(val): return f"{val:,.2f}"
                def fmt_diff(val): return f"{val:+.2f}"
                
                c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                diff_str = f"{c_color}{fmt_diff(diff)} ({rate:+.2f}%)[/]"
                
                ma5_val, ma20_val = row['ma5'], row['ma20']
                ma60_val, ma120_val = row['ma60'], row['ma120']

                def get_ma_color(val, ma_type):
                    if pd.isna(val): return "white"
                    
                    if ma_type == 5:
                        if pd.isna(ma20_val): return "white"
                        return "red" if val > ma20_val else "blue"
                    elif ma_type == 20:
                        if pd.isna(ma60_val): return "white"
                        return "red" if val > ma60_val else "blue"
                    elif ma_type == 60:
                        if pd.isna(ma120_val): return "white"
                        return "red" if val > ma120_val else "blue"
                    elif ma_type == 120:
                        if i + 1 < len(recent_df):
                            prev_ma120 = recent_df.iloc[i+1]['ma120']
                            if not pd.isna(prev_ma120):
                                return "red" if val > prev_ma120 else "blue"
                        return "white"
                    return "white"

                def fmt_ma(val, color):
                    if pd.isna(val): return "-"
                    return f"[{color}]{fmt_p(val)}[/]"

                table_d.add_row(
                    date_str, fmt_p(close), diff_str, fmt_p(row['open']), fmt_p(row['high']), fmt_p(row['low']), _fmt_vol(row['volume']),
                    fmt_ma(ma5_val, get_ma_color(ma5_val, 5)), fmt_ma(ma20_val, get_ma_color(ma20_val, 20)),
                    fmt_ma(ma60_val, get_ma_color(ma60_val, 60)), fmt_ma(ma120_val, get_ma_color(ma120_val, 120))
                )
                
                if (i + 1) % 5 == 0 and (i + 1) < len(recent_df):
                    table_d.add_section()
            config.console.print(table_d)

def get_current_price(mode='add'):
    config.console.print()
    # [로그] 메뉴 진입
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 종목 검색/추가 메뉴 진입[/dim cyan]")

    utils.print_breadcrumb()
    raw_input = Prompt.ask("조회할 주식 종목코드(6자리/티커) 또는 '종목명 코드' [dim](이전: b, 메인: q)[/dim]")
    config.console.print()
    if raw_input.lower() not in ['b', 'q'] and raw_input.strip():
        context.USER_ACTION_BREADCRUMB.append(f"[종목조회] {raw_input}")
        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    if raw_input.lower() in ['b', 'q']: return False
    if not raw_input.strip(): 
        config.console.print("[yellow]종목코드가 입력되지 않았습니다.[/yellow]")
        return

    parts = raw_input.split()
    code = parts[-1].upper()
    guessed_name = " ".join(parts[:-1])
    is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())

    # [로그] 입력 파싱 결과
    if config.FILE_DEBUG_LEVEL == "DEBUG":
        logger.debug(f"사용자 입력 파싱: Code={code}, Name={guessed_name}, Overseas={is_overseas}")

    stock_name = guessed_name if guessed_name else api.get_stock_name_by_code(code, is_overseas)
    if not stock_name or stock_name in ["Npay 증권", "네이버 페이 증권", "증권"]: stock_name = code 
    
    # [수정] 단순 조회이므로 status 사용
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[cyan]현재가 시세 조회 중...[/cyan]", total=None)
        res = api.get_current_price_data(code, is_overseas)
    
    # [로그] 시세 조회 결과 확인
    if config.FILE_DEBUG_LEVEL == "DEBUG":
        rt_cd = res.get('rt_cd') if res else "None"
        logger.debug(f"시세 조회 결과 코드: {rt_cd}")
        
    if res and res.get('rt_cd') == '0':
        output = res['output']
        is_valid = False
        if not is_overseas:
            if int(output.get('stck_prpr') or 0) > 0: is_valid = True
        else:
            if float(output.get('last') or 0) > 0: is_valid = True
            
        if not is_valid:
            config.console.print(f"\n[bold red]오류: 유효하지 않은 종목 코드이거나 시세가 없습니다. ({code})[/bold red]\n")
            return

        try:
            show_extended_info(code, is_overseas, basic_output=output)
            config.console.print(f"\n[bold cyan]종목명: {stock_name}[/bold cyan] [dim]({code})[/dim]")
        except Exception as e:
            logger.error(f"상세 정보 출력 중 오류: {e}")

        # [수정] 단순 조회 모드일 경우 여기서 종료
        if mode == 'simple': return

        config.console.print()
        ans = Prompt.ask("이 종목을 관심 종목 리스트에 추가하시겠습니까?", choices=["y", "n"], default="n")
        config.console.print()
        if ans == "y":
            config.console.print()
            input_name = Prompt.ask("저장할 종목명 입력", default=stock_name)
            config.console.print()
            config.console.print("[bold]어떤 그룹에 추가할까요?[/bold]")
            grid = Table.grid(padding=(0, 2))
            grid.add_column(justify="left")
            grid.add_column(justify="left", style="dim")
            grid.add_row("[1] 국내 주식", "(Domestic Stock)")
            grid.add_row("[2] 국내 ETF", "(Domestic ETF)")
            grid.add_row("[3] 미국 주식", "(US Stock)")
            grid.add_row("[4] 미국 ETF", "(US ETF)")
            config.console.print(grid)
            config.console.print()
            cat_choice = Prompt.ask("선택 [dim](이전: b, 메인: q)[/dim]", choices=["1", "2", "3", "4", "b", "q"], default="1")
            config.console.print()
            
            if cat_choice.lower() not in ['b', 'q']:
                context.USER_ACTION_BREADCRUMB.append(f"[그룹선택] {cat_choice}")
                logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
            
            if cat_choice.lower() in ['b', 'q']: return False
            target_list_key = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}.get(cat_choice)

            new_item = {"name": input_name, "code": code}
            
            if not is_overseas and res and res.get('rt_cd') == '0':
                market_name = res['output'].get('rprs_mrkt_kor_name', '')
                if "KOSDAQ" in market_name or "코스닥" in market_name:
                    new_item["exchange"] = "KOSDAQ"
                else:
                    new_item["exchange"] = "KOSPI"
            elif code in config.session.exchange_cache: 
                new_item["exchange"] = config.session.exchange_cache[code]
            
            target_list = config.session.stock_data.get(target_list_key, [])
            if not any(item['code'] == code for item in target_list):
                group_map = {"1": "국내 주식", "2": "국내 ETF", "3": "미국 주식", "4": "미국 ETF"}
                group_name = group_map.get(cat_choice, "선택")
                
                if target_list:
                    config.console.print(f"\n[bold]{group_name} 목록:[/bold]")
                    for i, item in enumerate(target_list):
                        config.console.print(f"[{i+1}] {item['name']} ({item['code']})")
                
                config.console.print(f"현재 '{group_name}' 그룹에 {len(target_list)}개의 종목이 있습니다.")
                config.console.print()
                pos_input = Prompt.ask("추가할 위치 번호를 입력하세요 [dim](그냥 Enter 입력 시 맨 끝에 추가, 이전: b, 메인: q)[/dim]", default="")
                config.console.print()
                
                if pos_input.lower() in ['b', 'q']:
                    config.console.print("[yellow]종목 추가가 취소되었습니다.[/yellow]")
                    return False
                
                if pos_input.isdigit() and 1 <= int(pos_input) <= len(target_list) + 1:
                    insert_idx = int(pos_input) - 1
                    config.session.stock_data[target_list_key].insert(insert_idx, new_item)
                    config.console.print(f"\n[green]'{input_name}' 종목이 {insert_idx + 1}번 위치에 추가되었습니다.[/green]")
                else:
                    config.session.stock_data[target_list_key].append(new_item)
                    config.console.print(f"\n[green]'{input_name}' 종목이 맨 끝에 추가되었습니다.[/green]")
                    
                config.session.save_stock_config(config.session.stock_data)
                config.session.load_stock_config()
            else:
                config.console.print("\n[yellow]이미 등록된 종목입니다.[/yellow]")
    else:
         config.console.print(f"\n[bold red]조회 실패: {res.get('msg1')}[/bold red]\n")

def delete_stock():
    # [로그] 메뉴 진입
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 종목 삭제 메뉴 진입[/dim cyan]")

    menu_items = [("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"), ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF")]
    cat_choice = utils.show_menu("어떤 그룹에서 삭제하시겠습니까?", menu_items, default_choice="1")
    if cat_choice.lower() not in ['b', 'q']:
        menu_map_dict = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{cat_choice}] {menu_map_dict[cat_choice]}")
    if cat_choice.lower() in ['b', 'q']: return False

    group_map = {"1": ("stocks_kr", "국내 주식"), "2": ("etfs_kr", "국내 ETF"), "3": ("stocks_us", "미국 주식"), "4": ("etfs_us", "미국 ETF")}
    target_key, group_name = group_map[cat_choice]
    
    # [로그] 그룹 선택
    if config.FILE_DEBUG_LEVEL == "DEBUG":
        logger.debug(f"삭제 대상 그룹 선택: {group_name} ({target_key})")
        
    target_list = config.session.stock_data[target_key]
    
    if not target_list:
        config.console.print(f"[yellow]'{group_name}' 그룹에 저장된 종목이 없습니다.[/yellow]")
        return
        
    m_codes = utils.get_memo_codes()
    idx, item_to_del = utils.search_stock_in_list(target_list, title=f"{group_name} 목록", display_func=lambda i, item: f"[{i+1}] {item['name']} ({item['code']}) {'[M]' if item['code'] in m_codes else ''}".rstrip())
    if not item_to_del: return False
    
    context.USER_ACTION_BREADCRUMB.append(f"[종목선택] {item_to_del['name']}")
    utils.print_breadcrumb()
    ans = Prompt.ask(f"정말 '{item_to_del['name']}'을(를) 삭제하시겠습니까?", choices=["y", "n"], default="n")
    config.console.print()
    if ans == "y":
        logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
        del config.session.stock_data[target_key][idx]
        config.session.save_stock_config(config.session.stock_data)
        config.session.load_stock_config()
        
        if item_to_del['code'] in m_codes:
            if Prompt.ask("이 종목에 작성된 메모도 모두 삭제하시겠습니까?", choices=["y", "n"], default="n") == 'y':
                utils.delete_all_stock_memos(item_to_del['code'])
                config.console.print("[dim]관련 메모가 모두 삭제되었습니다.[/dim]")
                
        config.console.print(f"\n[green]삭제되었습니다.[/green]")
    else:
        return False

def reorder_stock():
    """관심 종목 순서 재배치"""
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 종목 순서 변경 메뉴 진입[/dim cyan]")

    menu_items = [("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"), ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF")]
    cat_choice = utils.show_menu("어떤 그룹의 순서를 변경하시겠습니까?", menu_items, default_choice="1")
    if cat_choice.lower() not in ['b', 'q']:
        menu_map_dict = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{cat_choice}] {menu_map_dict[cat_choice]}")
    if cat_choice.lower() in ['b', 'q']: return False

    group_map = {"1": ("stocks_kr", "국내 주식"), "2": ("etfs_kr", "국내 ETF"), "3": ("stocks_us", "미국 주식"), "4": ("etfs_us", "미국 ETF")}
    target_key, group_name = group_map[cat_choice]
    
    target_list = config.session.stock_data[target_key]
    
    if len(target_list) < 2:
        config.console.print(f"[yellow]'{group_name}' 그룹에 순서를 변경할 만큼 종목이 충분하지 않습니다.[/yellow]")
        return
        
    m_codes = utils.get_memo_codes()
    from_idx, target_stock = utils.search_stock_in_list(target_list, title=f"{group_name} 목록", display_func=lambda i, item: f"[{i+1}] {item['name']} ({item['code']}) {'[M]' if item['code'] in m_codes else ''}".rstrip())
    if not target_stock: return False
    context.USER_ACTION_BREADCRUMB.append(f"[이동대상] {target_stock['name']}")
    
    config.console.print()
    to_idx_str = Prompt.ask(f"'{target_stock['name']}' 종목을 몇 번 위치로 이동하시겠습니까? (1~{len(target_list)}) [dim](이전: b, 메인: q)[/dim]")
    config.console.print()
    if to_idx_str.lower() not in ['b', 'q']:
        context.USER_ACTION_BREADCRUMB.append(f"[목표위치] {to_idx_str}")
    if to_idx_str.lower() in ['b', 'q'] or not to_idx_str.isdigit(): return False
    
    to_idx = int(to_idx_str) - 1
    if to_idx < 0 or to_idx >= len(target_list):
        config.console.print("[red]잘못된 번호입니다.[/red]")
        return
        
    if from_idx == to_idx:
        config.console.print("[yellow]현재 위치와 같습니다.[/yellow]")
        return
        
    logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
    
    # 리스트 순서 변경 (pop 후 insert)
    target_list.insert(to_idx, target_list.pop(from_idx))
    
    config.session.save_stock_config(config.session.stock_data)
    config.session.load_stock_config()
    
    config.console.print(f"\n[bold green]'{target_stock['name']}' 종목이 {to_idx + 1}번 위치로 이동되었습니다.[/bold green]")

def _manage_specific_stock_memos(code, name, mode='view'):
    """(내부) 특정 종목의 메모 리스트 상세 관리"""
    mode_name_map = {'view': '조회', 'delete': '삭제'}
    context.USER_ACTION_BREADCRUMB.append(f"[{name}] 상세")
    
    while True:
        utils.clear_screen()
        config.console.print()
        utils.print_breadcrumb()
        
        memos = utils.get_stock_memos(code)
        if not memos:
            config.console.print(f"[dim]'{name}'에 저장된 메모가 모두 삭제되어 이전 메뉴로 돌아갑니다.[/dim]")
            time.sleep(1.5)
            context.USER_ACTION_BREADCRUMB.pop()
            return True # 이전 리스트로 돌아감
        
        config.console.print(f"[bold cyan][{name} ({code}) 메모 현황][/bold cyan]")
        table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("No.", justify="right", width=4)
        table.add_column("수정일", justify="center", width=19)
        table.add_column("메모 요약", justify="left")
        
        for i, m in enumerate(memos):
            first_line = m['memo'].split('\n')[0][:40]
            if len(m['memo']) > len(first_line) or len(m['memo'].split('\n')[0]) > 40:
                first_line += "..."
            table.add_row(str(i+1), m['updated_at'], first_line)
        config.console.print(table)
        config.console.print()
        
        help_text = "다중: 1,3 / 전체: 0 / " if mode == 'view' else "다중: 1,3 / "
        idx_str = Prompt.ask(f"{mode_name_map[mode]}할 메모 번호 선택 [dim]({help_text}이전: b, 메인: q, 취소: Enter)[/dim]")
        
        if idx_str.lower() in ['b', 'q']: 
            context.USER_ACTION_BREADCRUMB.pop()
            return 'quit_to_menu'
            
        if idx_str == "":
            context.USER_ACTION_BREADCRUMB.pop()
            return True
        
        target_indices = []
        if idx_str == '0':
            target_indices = list(range(1, len(memos) + 1))
        elif ',' in idx_str or ' ' in idx_str:
            parts = re.split(r'[, ]+', idx_str)
            target_indices = [int(p) for p in parts if p.isdigit()]
        elif idx_str.isdigit():
            target_indices = [int(idx_str)]

        valid_indices = []
        for i in target_indices:
            if 1 <= i <= len(memos) and i not in valid_indices:
                valid_indices.append(i)

        if not valid_indices:
            config.console.print("\n[red]잘못된 번호입니다.[/red]")
            time.sleep(1)
            continue
            
        if mode == 'view':
            config.console.print(f"\n[bold cyan]━━━ {name} ({code}) 메모 상세 (총 {len(valid_indices)}건) ━━━[/bold cyan]")
            
            for idx, m_idx in enumerate(valid_indices):
                m = memos[m_idx - 1]
                
                memo_lines = m['memo'].split('\n')
                max_len = 50
                for line in memo_lines:
                    line_len = sum(2 if ord(c) > 127 else 1 for c in line)
                    if line_len > max_len:
                        max_len = line_len
                max_len = min(max_len, 120)
                
                if idx > 0:
                    config.console.print()
                
                config.console.print(f"[bold]No.{m_idx}[/bold] [dim](수정일: {m['updated_at']})[/dim]")
                config.console.print("[dim]" + "─" * max_len + "[/dim]")
                config.console.print(m['memo'])
                config.console.print("[dim]" + "─" * max_len + "[/dim]")
                
            ans = Prompt.ask("\n[dim](취소: Enter / 이전: b, 메인: q)[/dim]", default="", show_default=False)
            if ans.lower() in ['b', 'q']:
                context.USER_ACTION_BREADCRUMB.pop()
                return 'quit_to_menu'
                
        elif mode == 'delete':
            idx_display = ", ".join(map(str, valid_indices))
            msg = f"정말 {idx_display}번 메모를 삭제하시겠습니까?" if len(valid_indices) > 1 else f"정말 {valid_indices[0]}번 메모를 삭제하시겠습니까?"
            ans = Prompt.ask(msg, choices=["y", "n"], default="n")
            if ans == 'y':
                success_cnt = 0
                for m_idx in valid_indices:
                    m = memos[m_idx - 1]
                    if utils.delete_stock_memo_by_id(m['id']):
                        success_cnt += 1
                
                if success_cnt == len(valid_indices):
                    config.console.print(f"\n[green]선택한 메모 {success_cnt}건이 삭제되었습니다.[/green]")
                else:
                    config.console.print(f"\n[yellow]일부 메모 삭제에 실패했습니다. ({success_cnt}/{len(valid_indices)}건 성공)[/yellow]")
                time.sleep(1)
                context.USER_ACTION_BREADCRUMB.pop()
                return 'deleted'

def add_new_stock_memo():
    """새 종목 메모 추가"""
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 새 종목 메모 추가 메뉴 진입[/dim cyan]")

    menu_items = [
        ("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"), 
        ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF"), ("5", "직접 입력", "Direct Input")
    ]
    cat_choice = utils.show_menu("메모를 추가할 종목 선택", menu_items, default_choice="5")
    if cat_choice.lower() in ['b', 'q']: return False

    code, name, is_overseas = None, None, False

    if cat_choice == '5':
        utils.print_breadcrumb()
        raw_input = Prompt.ask("종목코드(6자리/티커) 또는 종목명 [dim](이전: b, 메인: q)[/dim]")
        config.console.print()
        if not raw_input or raw_input.lower() in ['b', 'q']: return False
        
        parts = raw_input.split()
        code = parts[-1].upper()
        guessed_name = " ".join(parts[:-1])
        is_overseas = not (len(code) == 6 and code[0].isdigit() and code.isalnum())
        
        name = guessed_name if guessed_name else api.get_stock_name_by_code(code, is_overseas)
        if not name or name in ["Npay 증권", "네이버 페이 증권", "증권"]: name = code
    else:
        group_map = {"1": ("stocks_kr", "국내 주식"), "2": ("etfs_kr", "국내 ETF"), "3": ("stocks_us", "미국 주식"), "4": ("etfs_us", "미국 ETF")}
        target_key, group_name = group_map[cat_choice]
        target_list = config.session.stock_data[target_key]
        
        if not target_list:
            config.console.print(f"[yellow]'{group_name}' 그룹에 저장된 종목이 없습니다.[/yellow]")
            time.sleep(1)
            return False
            
        idx, target_stock = utils.search_stock_in_list(target_list, title=f"{group_name} 목록")
        if not target_stock: return False
        
        code = target_stock['code']
        name = target_stock['name']

    if not code: return False

    config.console.print(f"\n[bold]{name} ({code})의 새 메모를 입력하세요.[/bold]")
    config.console.print("[dim](입력을 완료하려면 새 줄에서 ':q' 또는 '종료'를 입력하세요)[/dim]\n")
    
    lines = []
    while True:
        try:
            line = config.console.input("> ")
            if line.strip() in [':q', '종료']:
                break
            lines.append(line)
        except UnicodeDecodeError:
            # 한글 바이트 깨짐 방어 (프로그램 강제 종료 방지)
            config.console.print("[red]⚠️ 입력 인코딩 오류 발생 (한글 백스페이스 충돌 등). 방금 작성하던 줄을 다시 입력해주세요.[/red]")
        except (KeyboardInterrupt, EOFError):
            config.console.print("\n[yellow]입력이 취소되었습니다.[/yellow]")
            return False
        
    if lines:
        memo_text = "\n".join(lines)
        if utils.add_stock_memo(code, name, memo_text):
            config.console.print("\n[green]새 메모가 저장되었습니다.[/green]")
        else:
            config.console.print("\n[red]메모 저장에 실패했습니다.[/red]")
        time.sleep(1)
    else:
        config.console.print("\n[yellow]입력된 내용이 없어 취소되었습니다.[/yellow]")
        time.sleep(1)

def manage_stock_memos_by_mode(mode):
    """전체 종목 다중 라인 메모 통합 관리 (모드별 그룹핑)"""
    mode_name_map = {'view': '조회', 'delete': '삭제'}
    
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 종목 메모 {mode_name_map[mode]} 메뉴 진입[/dim cyan]")

    while True:
        utils.clear_screen()
        config.console.print()
        utils.print_breadcrumb()
        
        memos = utils.get_all_stock_memos()
        
        # 종목별 그룹핑 (가장 최신 메모 1개와 해당 종목의 전체 메모 개수 표시)
        grouped_memos = []
        seen_codes = set()
        for m in memos:
            if m['code'] not in seen_codes:
                count = sum(1 for x in memos if x['code'] == m['code'])
                m_copy = dict(m)
                m_copy['count'] = count
                grouped_memos.append(m_copy)
                seen_codes.add(m['code'])
        
        config.console.print("[bold cyan][전체 종목 메모 현황 (종목별)][/bold cyan]")
        if grouped_memos:
            table = Table(box=box.HORIZONTALS, header_style="dim", border_style="dim")
            table.add_column("No.", justify="right", width=4)
            table.add_column("종목명(코드)", justify="left")
            table.add_column("건수", justify="right", style="yellow")
            table.add_column("최근 수정일", justify="center", width=19)
            table.add_column("최근 메모 요약", justify="left")
            
            for i, m in enumerate(grouped_memos):
                first_line = m['memo'].split('\n')[0][:40]
                if len(m['memo']) > len(first_line) or len(m['memo'].split('\n')[0]) > 40:
                    first_line += "..."
                name_disp = f"{m['name']} ({m['code']})"
                table.add_row(str(i+1), name_disp, f"{m['count']}건", m['updated_at'], first_line)
            config.console.print(table)
        else:
            config.console.print("[dim]저장된 메모가 없습니다.[/dim]\n")
            time.sleep(1)
            return 'back'

        config.console.print()
        if mode == 'view':
            prompt_msg = f"{mode_name_map[mode]}할 종목 번호 선택 [dim](추가: a, 삭제: d, 이전: b, 메인: q, 취소: Enter)[/dim]"
        else:
            prompt_msg = f"{mode_name_map[mode]}할 종목 번호 선택 [dim](이전: b, 메인: q, 취소: Enter)[/dim]"
            
        idx_str = Prompt.ask(prompt_msg)
        if idx_str.lower() in ['b', 'q']: return 'back'
        if idx_str == "": return 'back'
        
        if mode == 'view':
            if idx_str.lower() == 'a':
                context.USER_ACTION_BREADCRUMB.append("[메모 추가]")
                add_new_stock_memo()
                context.USER_ACTION_BREADCRUMB.pop()
                continue
            elif idx_str.lower() == 'd':
                context.USER_ACTION_BREADCRUMB.append("[메모 삭제]")
                manage_stock_memos_by_mode('delete')
                context.USER_ACTION_BREADCRUMB.pop()
                continue

        if idx_str.isdigit() and 1 <= int(idx_str) <= len(grouped_memos):
            target = grouped_memos[int(idx_str)-1]
            res = _manage_specific_stock_memos(target['code'], target['name'], mode)
            if res in ('quit_to_main', 'quit_to_menu'):
                return 'back'
            elif res == 'deleted':
                if mode == 'delete': return 'deleted'
                continue
        else:
            config.console.print("\n[red]잘못된 번호입니다.[/red]")
            time.sleep(1)

def view_watchlist():
    """현재 관심 종목 리스트 출력"""
    utils.clear_screen()
    config.console.print("\n[bold cyan]📋 [현재 감시 중인 관심 종목][/bold cyan]\n")
    
    from modules import auto_trade
    restricted_stocks = auto_trade.load_restricted_stocks()
    from modules import db_manager
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {r['code']: True for r in custom_rules}
    m_codes = utils.get_memo_codes()

    groups = {
        "stocks_kr": "🇰🇷 국내 주식",
        "etfs_kr": "🇰🇷 국내 ETF",
        "stocks_us": "🇺🇸 미국 주식",
        "etfs_us": "🇺🇸 미국 ETF"
    }
    
    has_stock = False
    for key, label in groups.items():
        stocks = config.session.stock_data.get(key, [])
        if stocks:
            has_stock = True
            config.console.print(f"[bold]{label}[/bold]")
            
            table = Table(box=box.HORIZONTALS, show_header=False, padding=(0, 2), border_style="dim")
            table.add_column("No.", justify="right", style="dim")
            table.add_column("종목명")
            table.add_column("코드", style="dim")
            table.add_column("상태")
            
            for i, s in enumerate(stocks):
                code = s['code']
                name = s['name']
                
                status_tags = []
                if code in restricted_stocks: status_tags.append("[blue]제한(-)[/]")
                if code in rules_map: status_tags.append("[magenta]개별(+)[/]")
                if code in m_codes: status_tags.append("[yellow]메모(M)[/]")
                
                tag_str = " ".join(status_tags)
                
                table.add_row(str(i+1), name, code, tag_str)
                
                if (i + 1) % 5 == 0 and (i + 1) < len(stocks):
                    table.add_section()
                
            config.console.print(table)
            config.console.print()
            
    if not has_stock:
        config.console.print("[dim]등록된 관심 종목이 없습니다.[/dim]\n")

def manage_stock_menu():
    """종목 추가 및 삭제를 통합 관리하는 메뉴"""
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 관심 종목 관리 메뉴 진입[/dim cyan]")

    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "1"
    
    while True:
        utils.clear_screen()
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        
        menu_items = [
            ("1", "관심 종목 전체 조회", "View Watchlist"),
            ("2", "관심 종목 추가", "Add Stock"), 
            ("3", "관심 종목 삭제", "Delete Stock"), 
            ("4", "관심 종목 순서 변경", "Reorder Stock"), 
            ("5", "관심 종목 메모 관리", "Manage Memo"), 
            ("9", "차트 및 데이터 캐시 초기화", "Clear Cache")
        ]
        choice = utils.show_menu("관심 종목 관리 (Watchlist Management)", menu_items, default_choice=last_choice)
        
        if choice.lower() in ['b', 'q']: return False
        if choice.lower() == 'h':
            if getattr(utils, 'show_help', None):
                utils.show_help()
                utils.pause()
            continue
        
        last_choice = choice
        menu_map = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")

        if choice == "1":
            view_watchlist()
            utils.pause()
        elif choice == "2":
            if get_current_price(mode='add') is not False: utils.pause()
        elif choice == "3":
            if delete_stock() is not False: utils.pause()
        elif choice == "4":
            if reorder_stock() is not False: utils.pause()
        elif choice == "5":
            manage_stock_memos_by_mode('view')
        elif choice == "9":
            import api
            from modules import market
            from modules import analysis 
            api.clear_chart_cache()
            market.clear_market_yf_cache()
            analysis.clear_smart_money_cache() 
            config.console.print("\n[bold green]차트 및 지수, 수급 데이터 캐시가 초기화되었습니다.[/bold green]")
            utils.pause()
