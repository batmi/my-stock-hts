# modules/manage.py
import logging
from rich.prompt import Prompt
from rich.table import Table
from rich import box
import time
import pandas as pd
import config
import context # [추가]
import api
import utils
import constants

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
        with config.console.status("[cyan]기간별 시세 데이터 조회 중...[/cyan]"):
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
                    if pd.isna(ma5_val) or pd.isna(ma20_val) or pd.isna(ma60_val) or pd.isna(ma120_val): return "white"
                    if ma_type == 5:
                        if ma5_val > ma20_val and ma5_val > ma60_val and ma5_val > ma120_val: return "red"
                        if ma5_val < ma20_val and ma5_val < ma60_val and ma5_val < ma120_val: return "blue"
                        if (ma20_val < ma5_val < ma60_val) or (ma60_val < ma5_val < ma20_val): return "yellow"
                        if (ma60_val < ma5_val < ma120_val) or (ma120_val < ma5_val < ma60_val): return "orange3"
                    elif ma_type == 20:
                        if ma20_val > ma60_val and ma20_val > ma120_val: return "red"
                        if ma20_val < ma60_val and ma20_val < ma120_val: return "blue"
                        if (ma60_val < ma20_val < ma120_val) or (ma120_val < ma20_val < ma60_val): return "yellow"
                    elif ma_type == 60:
                        if ma120_val > ma60_val and ma60_val > ma5_val and ma60_val > ma20_val: return "blue"
                        if ma120_val < ma60_val and ma60_val < ma5_val and ma60_val < ma20_val: return "red"
                        return "yellow"
                    elif ma_type == 120:
                        if ma60_val > ma120_val: return "red"
                        if ma60_val < ma120_val: return "blue"
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
        with config.console.status("[cyan]기간별 시세 데이터 조회 중...[/cyan]"):
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
                    if pd.isna(ma5_val) or pd.isna(ma20_val) or pd.isna(ma60_val) or pd.isna(ma120_val): return "white"
                    if ma_type == 5:
                        if ma5_val > ma20_val and ma5_val > ma60_val and ma5_val > ma120_val: return "red"
                        if ma5_val < ma20_val and ma5_val < ma60_val and ma5_val < ma120_val: return "blue"
                        if (ma20_val < ma5_val < ma60_val) or (ma60_val < ma5_val < ma20_val): return "yellow"
                        if (ma60_val < ma5_val < ma120_val) or (ma120_val < ma5_val < ma60_val): return "orange3"
                    elif ma_type == 20:
                        if ma20_val > ma60_val and ma20_val > ma120_val: return "red"
                        if ma20_val < ma60_val and ma20_val < ma120_val: return "blue"
                        if (ma60_val < ma20_val < ma120_val) or (ma120_val < ma20_val < ma60_val): return "yellow"
                    elif ma_type == 60:
                        if ma120_val > ma60_val and ma60_val > ma5_val and ma60_val > ma20_val: return "blue"
                        if ma120_val < ma60_val and ma60_val < ma5_val and ma60_val < ma20_val: return "red"
                        return "yellow"
                    elif ma_type == 120:
                        if ma60_val > ma120_val: return "red"
                        if ma60_val < ma120_val: return "blue"
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
    raw_input = Prompt.ask("조회할 주식 종목코드(6자리/티커) 또는 '종목명 코드' [dim](이전: q)[/dim]")
    config.console.print()
    if raw_input.lower() != 'q' and raw_input.strip():
        context.USER_ACTION_BREADCRUMB.append(f"[종목조회] {raw_input}")
        logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    if raw_input.lower() == 'q': return False
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
    with config.console.status("[cyan]현재가 시세 조회 중...[/cyan]"):
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
            cat_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "q"], default="1")
            config.console.print()
            
            if cat_choice.lower() != 'q':
                context.USER_ACTION_BREADCRUMB.append(f"[그룹선택] {cat_choice}")
                logger.info("운영자 실행: " + " - ".join(context.USER_ACTION_BREADCRUMB))
            
            if cat_choice.lower() == 'q': return False
            target_list_key = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}.get(cat_choice)

            new_item = {"name": input_name, "code": code}
            if code in config.session.exchange_cache: new_item["exchange"] = config.session.exchange_cache[code]
            
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
                pos_input = Prompt.ask("추가할 위치 번호를 입력하세요 [dim](그냥 Enter 입력 시 맨 끝에 추가, 취소: q)[/dim]", default="")
                config.console.print()
                
                if pos_input.lower() == 'q':
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
    if cat_choice.lower() != 'q':
        menu_map_dict = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{cat_choice}] {menu_map_dict[cat_choice]}")
    if cat_choice.lower() == 'q': return False

    group_map = {"1": ("stocks_kr", "국내 주식"), "2": ("etfs_kr", "국내 ETF"), "3": ("stocks_us", "미국 주식"), "4": ("etfs_us", "미국 ETF")}
    target_key, group_name = group_map[cat_choice]
    
    # [로그] 그룹 선택
    if config.FILE_DEBUG_LEVEL == "DEBUG":
        logger.debug(f"삭제 대상 그룹 선택: {group_name} ({target_key})")
        
    target_list = config.session.stock_data[target_key]
    
    if not target_list:
        config.console.print(f"[yellow]'{group_name}' 그룹에 저장된 종목이 없습니다.[/yellow]")
        return
        
    idx, item_to_del = utils.search_stock_in_list(target_list, title=f"{group_name} 목록")
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
        config.console.print(f"\n[green]삭제되었습니다.[/green]")
    else:
        return False

def reorder_stock():
    """관심 종목 순서 재배치"""
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 종목 순서 변경 메뉴 진입[/dim cyan]")

    menu_items = [("1", "국내 주식", "Domestic Stock"), ("2", "국내 ETF", "Domestic ETF"), ("3", "미국 주식", "US Stock"), ("4", "미국 ETF", "US ETF")]
    cat_choice = utils.show_menu("어떤 그룹의 순서를 변경하시겠습니까?", menu_items, default_choice="1")
    if cat_choice.lower() != 'q':
        menu_map_dict = dict((k, v) for k, v, _ in menu_items)
        context.USER_ACTION_BREADCRUMB.append(f"[{cat_choice}] {menu_map_dict[cat_choice]}")
    if cat_choice.lower() == 'q': return False

    group_map = {"1": ("stocks_kr", "국내 주식"), "2": ("etfs_kr", "국내 ETF"), "3": ("stocks_us", "미국 주식"), "4": ("etfs_us", "미국 ETF")}
    target_key, group_name = group_map[cat_choice]
    
    target_list = config.session.stock_data[target_key]
    
    if len(target_list) < 2:
        config.console.print(f"[yellow]'{group_name}' 그룹에 순서를 변경할 만큼 종목이 충분하지 않습니다.[/yellow]")
        return
        
    from_idx, target_stock = utils.search_stock_in_list(target_list, title=f"{group_name} 목록")
    if not target_stock: return False
    context.USER_ACTION_BREADCRUMB.append(f"[이동대상] {target_stock['name']}")
    
    config.console.print()
    to_idx_str = Prompt.ask(f"'{target_stock['name']}' 종목을 몇 번 위치로 이동하시겠습니까? (1~{len(target_list)}) [dim](취소: q)[/dim]")
    config.console.print()
    if to_idx_str.lower() != 'q':
        context.USER_ACTION_BREADCRUMB.append(f"[목표위치] {to_idx_str}")
    if to_idx_str.lower() == 'q' or not to_idx_str.isdigit(): return False
    
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

def manage_stock_menu():
    """종목 추가 및 삭제를 통합 관리하는 메뉴"""
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print(f"[dim cyan][TRACE] 관심 종목 관리 메뉴 진입[/dim cyan]")

    menu_items = [("1", "종목 추가", "Add Stock"), ("2", "종목 삭제", "Delete Stock"), ("3", "종목 순서 변경", "Reorder Stock"), ("4", "차트 캐시 수동 갱신", "Clear Cache")]
    choice = utils.show_menu("관심 종목 관리 (Watchlist Management)", menu_items, default_choice="1")
    
    menu_map = {"1": "종목 추가", "2": "종목 삭제", "3": "종목 순서 변경", "4": "차트 캐시 갱신"}
    if choice in menu_map:
        context.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")

    if choice.lower() == 'q': return False

    if choice == "1":
        if get_current_price(mode='add') is False: return False
    elif choice == "2":
        if delete_stock() is False: return False
    elif choice == "3":
        if reorder_stock() is False: return False
    elif choice == "4":
        import api
        from modules import market
        api.clear_chart_cache()
        market.clear_market_yf_cache()
