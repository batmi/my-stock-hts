# modules/analysis.py
from rich.table import Table
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn, DownloadColumn, TransferSpeedColumn
from rich import box
import config
import api
import logging
import indicators
import utils
import time
from datetime import datetime
import urllib.request
import zipfile
import os
import pandas as pd
import concurrent.futures
import shutil
import sqlite3
import json
from openpyxl.styles import Font

logger = logging.getLogger(__name__)

def calculate_score(price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd=None, macd_signal=None):
    """퀀트 멀티팩터 스코어링 모델 (10점 만점)"""
    score = 0
    details = []

    # 1. Trend Factor (4.0점)
    if ema20 is not None and price > ema20: 
        score += 0.5
        details.append("EMA: 현재가 > 20일선 (+0.5)")
    if ema20 is not None and ema60 is not None and ema20 > ema60: 
        score += 0.5
        details.append("EMA: 20일선 > 60일선 (+0.5)")
    if ema60 is not None and ema120 is not None and ema60 > ema120: 
        score += 0.5
        details.append("EMA: 60일선 > 120일선 (+0.5)")
    
    if macd is not None and macd_signal is not None:
        if macd > macd_signal:
            score += 1.0
            details.append("MACD: 골든크로스 (매수 우위) (+1.0)")
        if macd > 0:
            score += 0.5
            details.append("MACD: 0선 상회 (상승 국면) (+0.5)")

    if sar is not None and price > sar: 
        score += 1.0
        details.append("SAR: 주가 아래 (상승 추세) (+1.0)")
    
    # 2. Momentum Factor (2.5점)
    if rsi is not None:
        if 50 <= rsi <= 75: 
            score += 1.5
            details.append(f"RSI: {rsi:.1f} (강세 구간) (+1.5)")
        elif 30 <= rsi < 50: 
            score += 0.5
            details.append(f"RSI: {rsi:.1f} (반등/회복) (+0.5)")
    
    if cci is not None:
        if cci > 0: 
            score += 0.5
            details.append(f"CCI: {cci:.1f} (상승 추세) (+0.5)")
        if cci > 100: 
            score += 0.5
            details.append(f"CCI: {cci:.1f} (강한 상승 탄력) (+0.5)")

    # 3. Strength & Volume Factor (1.5점)
    if adx is not None and adx >= 20: 
        score += 0.5
        details.append(f"ADX: {adx:.1f} (추세 형성) (+0.5)")

    if obv_trend: 
        score += 1.0
        details.append("OBV: 이동평균 상회 (수급 양호) (+1.0)")

    # 4. Synergy Bonus (2.0점)
    # Trend Confirmation
    if (ema20 and ema60 and ema20 > ema60) and (macd is not None and macd > 0) and (adx is not None and adx >= 20):
        score += 1.0
        details.append("★ 추세 확증: 정배열+MACD양수+ADX (+1.0)")
        
    # Momentum Thrust
    if (macd is not None and macd_signal is not None and macd > macd_signal) and (rsi is not None and rsi >= 50) and obv_trend:
        score += 1.0
        details.append("★ 모멘텀 폭발: MACD골든+RSI강세+OBV (+1.0)")

    return score, details

def classify_stock_state(price, ema20, ema60, ema120, sar, rsi, prev_rsi, adx, cci, obv_trend, macd=None, macd_signal=None, thresholds=None):
    if price is None or ema60 is None or sar is None or rsi is None: return "-", "[dim]", "데이터 부족"
    
    reasons = []
    is_severe_danger = False
    
    # [수정] 위험 조건 완화: 장기(120)와 중기(60) 이평선을 모두 이탈해야 '위험'으로 간주 (변동성 감소)
    if ema120 is not None and price < ema120 and price < ema60: 
        is_severe_danger = True
        reasons.append("이평선 완전 이탈(60&120)")
    elif rsi <= (config.INDICATOR_PARAMS["RSI_LOWER"] - 10): 
        is_severe_danger = True # 위험 기준은 하한선보다 더 낮게 설정 (예: 20)
        reasons.append(f"RSI 초과매도({rsi:.1f})")
        
    # [수정] ADX 과열 중 RSI 하락은 '위험'보다는 '주의'로 이동
    if is_severe_danger: return "위험", "[blue]", ", ".join(reasons)
    
    is_caution = False
    # [수정] 주의 조건: 60일선 이탈 또는 120일선 이탈 중 하나라도 해당되면 '주의' (완충 구간)
    if price < ema60: 
        is_caution = True
        reasons.append("60일선 이탈")
    if ema120 is not None and price < ema120: 
        is_caution = True
        reasons.append("120일선 이탈")
    if sar > price: 
        is_caution = True
        reasons.append("SAR 매도신호")
    if rsi >= (config.INDICATOR_PARAMS["RSI_UPPER"] + 10): 
        is_caution = True
        reasons.append(f"RSI 과열({rsi:.1f})")
    elif rsi <= config.INDICATOR_PARAMS["RSI_LOWER"]: 
        is_caution = True
        reasons.append(f"RSI 침체({rsi:.1f})")
    elif adx is not None and prev_rsi is not None and adx >= 40 and rsi < prev_rsi: 
        is_caution = True
        reasons.append(f"ADX과열({adx:.1f})+RSI하락")

    # [추가] MACD 데드크로스 (매도/조정 신호)
    if macd is not None and macd_signal is not None and macd < macd_signal:
        is_caution = True
        reasons.append("MACD 데드크로스")
        
    if is_caution: return "주의", "[yellow]", ", ".join(reasons)
    
    score, _ = calculate_score(price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd, macd_signal)

    # [수정] config.py의 설정값을 사용하여 상태 판정
    if thresholds:
        buy_score = thresholds.get("BUY_SCORE", config.ANALYSIS_THRESHOLDS["BUY_SCORE"])
        rise_score = thresholds.get("RISE_SCORE", config.ANALYSIS_THRESHOLDS["RISE_SCORE"])
        buy_rsi_max = thresholds.get("BUY_RSI_MAX", config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"])
    else:
        buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        buy_rsi_max = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]

    if score >= buy_score and rsi < buy_rsi_max: return "매수", "[red]", "매수 조건 충족"
    elif score >= rise_score: return "상승", "[orange3]", "상승 추세 (점수 양호)"
    else: return "관망", "[white]", "방향성 탐색 구간"

def _get_db_connection():
    return sqlite3.connect(config.DB_FILE_PATH)

def _init_analysis_db():
    try:
        with _get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_analysis_cache (
                    market_type TEXT PRIMARY KEY,
                    updated_at TEXT,
                    params TEXT,
                    data TEXT
                )
            """)
            conn.commit()
    except Exception: pass

def _save_analysis_result(market_type, results, params):
    try:
        _init_analysis_db()
        with _get_db_connection() as conn:
            cursor = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data_json = json.dumps(results, ensure_ascii=False)
            params_json = json.dumps(params, ensure_ascii=False)
            
            cursor.execute("""
                INSERT OR REPLACE INTO market_analysis_cache (market_type, updated_at, params, data)
                VALUES (?, ?, ?, ?)
            """, (market_type, now_str, params_json, data_json))
            conn.commit()
    except Exception as e:
        config.console.print(f"[dim red]분석 결과 저장 실패: {e}[/dim red]")

def _load_analysis_result(market_type):
    try:
        _init_analysis_db()
        with _get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT updated_at, params, data FROM market_analysis_cache WHERE market_type = ?", (market_type,))
            row = cursor.fetchone()
            if row:
                return {
                    'updated_at': row[0],
                    'params': json.loads(row[1]),
                    'data': json.loads(row[2])
                }
    except Exception as e:
        config.console.print(f"[dim red]분석 결과 로드 실패: {e}[/dim red]")
    return None

def diagnose_stock(target_code=None, target_name=None, target_is_overseas=False):
    """특정 종목에 대해 시스템 트레이딩 로직을 진단(시뮬레이션)합니다."""
    config.console.print("\n[bold cyan]=== 개별 종목 진단 (Diagnosis) ===[/]")
    
    code, name, is_overseas = None, None, False

    if target_code:
        code, name, is_overseas = target_code, target_name, target_is_overseas
    else:
        # [수정] 종목 선택 메뉴 확장 ([5] 직접 입력 추가 및 기본값 설정)
        config.console.print("\n[bold]진단할 종목을 선택하세요:[/bold]")
        config.console.print("[1] 국내 주식")
        config.console.print("[2] 국내 ETF")
        config.console.print("[3] 미국 주식")
        config.console.print("[4] 미국 ETF")
        config.console.print("[5] 직접 입력 (코드 검색)")
        config.console.print()
        
        choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "4", "5", "q"], default="5")
        if choice.lower() == 'q': return

        if choice == '5':
            # 직접 입력 로직
            from modules import manage
            # manage.get_current_price는 출력을 포함하므로, 여기서는 간단히 입력만 받음
            raw_input = Prompt.ask("종목코드(6자리/티커) 또는 종목명 [dim](취소: q)[/dim]")
            if not raw_input or raw_input.lower() == 'q': return
            
            # manage 모듈의 _resolve_stock 로직과 유사하게 처리하거나 utils 활용
            # 여기서는 utils가 없으므로 telegram_bot의 로직을 참고하여 간단히 구현
            if raw_input.isdigit() and len(raw_input) == 6:
                code = raw_input
                name = api.get_stock_name_by_code(code, False) or code
                is_overseas = False
            elif all(ord(c) < 128 for c in raw_input) and not raw_input.isdigit(): # 해외 티커 가정
                code = raw_input.upper()
                name = api.get_stock_name_by_code(code, True) or code
                is_overseas = True
            else:
                # 한글 종목명 검색 시도 (config.session.stock_data 활용)
                found = False
                for key in ['stocks_kr', 'etfs_kr']:
                    for item in config.session.stock_data.get(key, []):
                        if item['name'] == raw_input:
                            code, name, is_overseas = item['code'], item['name'], False
                            found = True; break
                    if found: break
                if not found:
                    for key in ['stocks_us', 'etfs_us']:
                        for item in config.session.stock_data.get(key, []):
                            if item['name'].lower() == raw_input.lower():
                                code, name, is_overseas = item['code'], item['name'], True
                                found = True; break
                        if found: break
                
                if not found:
                    config.console.print(f"[red]'{raw_input}'을(를) 찾을 수 없습니다. 코드로 입력해주세요.[/red]")
                    return
        else:
            # 리스트 선택
            key_map = {"1": "stocks_kr", "2": "etfs_kr", "3": "stocks_us", "4": "etfs_us"}
            target_key = key_map.get(choice)
            stock_list = config.session.stock_data.get(target_key, [])
            
            if not stock_list:
                config.console.print("[yellow]등록된 종목이 없습니다.[/yellow]")
                return
                
            # 간이 리스트 출력 및 선택
            for i, s in enumerate(stock_list):
                config.console.print(f"[{i+1}] {s['name']} ({s['code']})")
            
            config.console.print()
            sel = Prompt.ask("번호 선택 [dim](취소: q)[/dim]")
            if sel.lower() == 'q': return
            if sel.isdigit() and 1 <= int(sel) <= len(stock_list):
                item = stock_list[int(sel)-1]
                code, name = item['code'], item['name']
                is_overseas = (choice in ["3", "4"])
            else:
                return

    if not code: return

    logger.info(f"운영자 실행: {' - '.join(config.USER_ACTION_BREADCRUMB)}")

    with config.console.status(f"[bold green]{name}({code}) 데이터 분석 중...[/]"):
        # 1. 데이터 조회 (실시간 시세 반영된 일봉)
        df = api.get_chart_data(code, is_overseas=is_overseas)
        if df is None or df.empty:
            config.console.print("[red]차트 데이터를 불러올 수 없습니다.[/red]")
            return

        # 2. 지표 계산
        ind = indicators.calculate_indicators(df)
        
        # 전일 RSI 계산
        prev_rsi = None
        if df is not None and not df.empty and len(df) >= 16:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
            except: pass
            
        current_price = float(df.iloc[-1]['close'])
        
        # 3. 상태 분류 및 점수 계산
        state, state_color, state_reason = classify_stock_state(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
        )
        
        score, details = calculate_score(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
        )

    # [추가] 체결강도 조회 (국내주식인 경우만)
    vol_strength = None
    if not is_overseas:
        vol_strength = api.get_realtime_vol_strength(code)

    # 4. 결과 출력
    config.console.print()
    
    # [테이블 1] 기술적 지표 분석
    table_tech = Table(title=f"기술적 지표 분석: {name} ({code})", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table_tech.add_column("지표", justify="left", style="cyan", width=15)
    table_tech.add_column("값 (상태)", justify="left")
    table_tech.add_column("해석/기준", justify="left", style="dim")

    # 현재가
    curr_price_color = "[white]"
    if ind['ema_5'] and ind['ema_20'] and ind['ema_60']:
        if ind['ema_5'] > ind['ema_20'] and ind['ema_20'] > ind['ema_60']:
            if current_price > ind['ema_5']: curr_price_color = "[red]"
            elif current_price < ind['ema_60']: curr_price_color = "[blue]"
            elif current_price < ind['ema_5'] or current_price < ind['ema_20']: curr_price_color = "[dim]"
        elif ind['ema_5'] < ind['ema_20'] and ind['ema_5'] < ind['ema_60']:
            if current_price < ind['ema_5']: curr_price_color = "[blue]"
            elif current_price > ind['ema_20'] or current_price > ind['ema_60']: curr_price_color = "[orange3]"
            elif current_price > ind['ema_5']: curr_price_color = "[white]"
        else:
            if current_price < ind['ema_5']: curr_price_color = "[blue]"
            elif current_price > ind['ema_20']: curr_price_color = "[orange3]"
            elif current_price < ind['ema_20']: curr_price_color = "[white]"

    price_str = f"${current_price:,.2f}" if is_overseas else f"{current_price:,.0f}원"
    table_tech.add_row("현재가", f"{curr_price_color}{price_str}[/]", "이평선 배열 및 위치 기반")

    # RSI
    rsi_val = ind['rsi']
    rsi_str = f"{rsi_val:.2f}"
    rsi_desc = ""
    if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: 
        rsi_str = f"[magenta]{rsi_str}[/]"
        rsi_desc = "과열 (추격금지)"
    elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: 
        rsi_str = f"[red]{rsi_str}[/]"
        rsi_desc = "강세 유지"
    elif 45 <= rsi_val < 55: 
        rsi_str = f"[orange3]{rsi_str}[/]"
        rsi_desc = "강세 조정 (진입후보)"
    elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: 
        rsi_str = f"[yellow]{rsi_str}[/]"
        rsi_desc = "약세/하락전환 가능"
    else: 
        rsi_str = f"[blue]{rsi_str}[/]"
        rsi_desc = "침체 (과매도)"
    table_tech.add_row("RSI (14)", f"{rsi_str} [dim]({rsi_desc})[/dim]", "과매수(70)/과매도(30)")

    # ADX
    adx_val = ind['adx']
    adx_str = f"{adx_val:.2f}"
    adx_desc = ""
    if adx_val >= 40: 
        adx_str = f"[magenta]{adx_str}[/]" 
        adx_desc = "과열 (조정 주의)"
    elif adx_val >= 30: 
        adx_str = f"[red]{adx_str}[/]"     
        adx_desc = "강한 추세"
    elif adx_val >= 20: 
        adx_str = f"[orange3]{adx_str}[/]"
        adx_desc = "안정적 추세"
    elif adx_val >= 15: 
        adx_str = f"[yellow]{adx_str}[/]"
        adx_desc = "추세 형성 중"
    else: 
        adx_str = f"[white]{adx_str}[/]"
        adx_desc = "추세 없음 (횡보)"
    table_tech.add_row("ADX (14)", f"{adx_str} [dim]({adx_desc})[/dim]", "추세 강도 (25 이상 강세)")

    # CCI
    cci_val = ind['cci']
    cci_str = f"{cci_val:.2f}"
    cci_desc = ""
    if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: 
        cci_str = f"[red]{cci_str}[/]"
        cci_desc = "과열 (추격 금물)"
    elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: 
        cci_str = f"[orange3]{cci_str}[/]"
        cci_desc = "상승 추세"
    elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: 
        cci_str = f"[yellow]{cci_str}[/]"
        cci_desc = "반등 시도"
    else: 
        cci_str = f"[blue]{cci_str}[/]"
        cci_desc = "과매도 (저점 탐색)"
    table_tech.add_row("CCI (20)", f"{cci_str} [dim]({cci_desc})[/dim]", "추세 및 과매수/매도")

    # MACD
    macd_val = ind.get('macd')
    sig_val = ind.get('macd_signal')
    
    macd_str = "-"
    macd_desc = "추세 확인"
    if macd_val is not None and sig_val is not None:
        m_color = "[red]" if macd_val >= sig_val else "[blue]"
        macd_str = f"{m_color}{macd_val:.2f}[/] / {sig_val:.2f}"
        macd_desc = "골든크로스 (매수 우위)" if macd_val >= sig_val else "데드크로스 (매도 우위)"
            
    table_tech.add_row("MACD (12/26/9)", macd_str, macd_desc)

    # OBV
    obv_trend_str = '상승' if ind.get('obv_trend') else '하락'
    obv_color = "[red]" if ind.get('obv_trend') else "[blue]"
    table_tech.add_row("OBV 추세", f"{obv_color}{obv_trend_str}[/]", "이동평균 상회 여부")
    
    # SAR
    sar_pos = "주가 아래 (상승)" if ind['psar'] < current_price else "주가 위 (하락)"
    sar_color = "[red]" if ind['psar'] < current_price else "[blue]"
    table_tech.add_row("SAR 위치", f"{sar_color}{sar_pos}[/]", "파라볼릭 추세 전환")
    
    # 이평 배열
    ema_align = "알 수 없음"
    ema_color = "[white]"
    if ind['ema_20'] > ind['ema_60'] > ind['ema_120']: 
        ema_align = "정배열 (20>60>120)"; ema_color = "[red]"
    elif ind['ema_20'] < ind['ema_60'] < ind['ema_120']: 
        ema_align = "역배열 (20<60<120)"; ema_color = "[blue]"
    else: 
        ema_align = "혼조세"; ema_color = "[yellow]"
    table_tech.add_row("이평 배열", f"{ema_color}{ema_align}[/]", "5/20/60/120일선 배열")

    # 이격도
    d_20 = (current_price / ind['ema_20'] * 100) if ind['ema_20'] else 0
    d_60 = (current_price / ind['ema_60'] * 100) if ind['ema_60'] else 0
    d_120 = (current_price / ind['ema_120'] * 100) if ind['ema_120'] else 0
    
    def dc(val): return "[red]" if val >= 100 else "[blue]"
    
    disp_msg = f"20선({dc(d_20)}{d_20:.1f}%[/]) 60선({dc(d_60)}{d_60:.1f}%[/]) 120선({dc(d_120)}{d_120:.1f}%[/])"
    
    disp_upper = config.ANALYSIS_THRESHOLDS.get("DISPARITY_UPPER", 110)
    disp_lower = config.ANALYSIS_THRESHOLDS.get("DISPARITY_LOWER", 90)

    disp_eval = ""
    if d_20 >= disp_upper: disp_eval = "[bold red]단기 과열[/]"
    elif d_20 <= disp_lower: disp_eval = "[bold blue]과매도[/]"
    else: disp_eval = "[white]적정 범위[/]"
    
    table_tech.add_row("이격도", disp_msg, f"{disp_eval} [dim](현재가/이평선)[/dim]")

    config.console.print(table_tech)
    config.console.print()
    
    # [테이블 2] 시스템 트레이딩 판단 결과
    table_logic = Table(title="시스템 트레이딩 판단 결과", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table_logic.add_column("항목", justify="center", style="cyan", width=15)
    table_logic.add_column("결과", justify="center", width=20)
    table_logic.add_column("상세 내용 / 사유", justify="left", style="dim")

    # 종합 점수
    s_color = state_color.replace('[', '').replace(']', '')
    score_str = f"[bold {s_color}]{score}점[/]"
    
    details_str = ""
    if details:
        details_str = "\n".join([f"[green]* {d}[/green]" for d in details])
    else:
        details_str = "[dim]획득한 점수가 없습니다.[/dim]"
    
    table_logic.add_row("종합 점수", score_str, details_str)
    
    # 상태 분류
    table_logic.add_row("상태 분류", f"[bold {s_color}]{state}[/]", state_reason)
    
    # 매수 조건 체크
    buy_score_limit = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    buy_rsi_limit = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    buy_vol_limit = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
    
    is_buy_score = score >= buy_score_limit
    is_buy_rsi = ind['rsi'] < buy_rsi_limit
    is_safe_state = state not in ["위험", "주의"]
    is_buy_vol = True
    if vol_strength is not None:
        is_buy_vol = vol_strength >= buy_vol_limit
    
    buy_result = "[bold red]매수 가능[/]" if (is_buy_score and is_buy_rsi and is_safe_state and is_buy_vol) else "[bold blue]매수 불가[/]"
    
    buy_reason_list = []
    if not is_safe_state: buy_reason_list.append(f"진입 불가 상태 ({state})")
    if not is_buy_score: buy_reason_list.append(f"점수 미달 (기준: {buy_score_limit}점 이상)")
    if not is_buy_rsi: buy_reason_list.append(f"RSI 과열 (기준: {buy_rsi_limit} 미만)")
    if not is_buy_vol: buy_reason_list.append(f"체결강도 미달 ({vol_strength:.1f}% < {buy_vol_limit}%)")
    buy_reason = ", ".join(buy_reason_list) if buy_reason_list else "모든 매수 조건 충족"
    
    table_logic.add_row("매수 판단", buy_result, buy_reason)
    
    # 매도(추세 이탈) 조건 체크
    sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]
    is_sell_signal = (score < sell_score_limit) or (state == "위험")
    
    sell_result = "[bold blue]매도(추세이탈)[/]" if is_sell_signal else "[bold green]보유(추세유지)[/]"
    
    if state == "위험":
        sell_reason = "위험 상태 진입 (필터링 조건)"
    elif score < sell_score_limit:
        sell_reason = f"점수 하락 (기준: {sell_score_limit}점 미만)"
    else:
        sell_reason = "추세 유지 중 (주의/관망 상태라도 점수 유지 시 보유)"
    
    table_logic.add_row("보유 판단", sell_result, sell_reason)
    
    # [추가] 체결강도 행 추가
    vol_str = "-"
    vol_eval = ""
    if vol_strength is not None:
        v_color = "[red]" if is_buy_vol else "[blue]"
        vol_str = f"{v_color}{vol_strength:.1f}%[/]"
        vol_eval = "[bold red]양호[/]" if is_buy_vol else "[bold blue]미달[/]"
    table_logic.add_row("체결강도", vol_str, f"{vol_eval} (기준: {buy_vol_limit}% 이상)")

    config.console.print(table_logic)
    config.console.print()

    # [추가] 기간별 시세 20일치 출력
    _print_period_price_20(code, is_overseas)

def diagnose_group_stocks(market_filter=None):
    """등록된 종목들에 대해 일괄 진단을 수행합니다."""
    # 대상: 국내 주식 + 국내 ETF
    targets = config.session.stock_data.get('stocks_kr', []) + config.session.stock_data.get('etfs_kr', [])
    
    if not targets:
        config.console.print("[yellow]등록된 국내 종목이 없습니다.[/yellow]")
        return

    results = []
    
    title_suffix = f" ({market_filter})" if market_filter else " (전체)"
    
    with config.console.status(f"[bold green]등록된 종목 일괄 진단 중{title_suffix}...[/]"):
        for item in targets:
            code = item['code']
            name = item['name']
            
            # 1. 시장 구분 확인 (필터링이 필요한 경우)
            if market_filter:
                try:
                    # 현재가 조회로 시장 구분 확인
                    cp_data = api.get_current_price_data(code, is_overseas=False)
                    if cp_data.get('rt_cd') != '0': continue
                    
                    mrkt_name = cp_data['output'].get('rprs_mrkt_kor_name', '')
                    # 유가증권(KOSPI), 코스닥(KOSDAQ)
                    is_kospi = "유가증권" in mrkt_name or "KOSPI" in mrkt_name
                    is_kosdaq = "코스닥" in mrkt_name or "KOSDAQ" in mrkt_name
                    
                    if market_filter == "KOSPI" and not is_kospi: continue
                    if market_filter == "KOSDAQ" and not is_kosdaq: continue
                except:
                    continue

            # 2. 차트 데이터 및 지표 계산
            df = api.get_chart_data(code, is_overseas=False)
            if df is None or df.empty: continue
            
            ind = indicators.calculate_indicators(df)
            current_price = float(df.iloc[-1]['close'])
            
            # 전일 RSI (상태 분류용)
            prev_rsi = None
            if len(df) >= 16:
                delta = df['close'].diff()
                gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
                loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
                try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
                except: pass

            # 3. 점수 및 상태 계산
            state, state_color, state_reason = classify_stock_state(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
            )
            
            score, _ = calculate_score(
                current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
                ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
            )
            
            # [추가] 체결강도 조회
            vol_strength = api.get_realtime_vol_strength(code)
            
            results.append({
                'code': code, 'name': name, 'price': current_price,
                'score': score, 'state': state, 'state_color': state_color,
                'rsi': ind['rsi'], 'adx': ind['adx'], 'cci': ind['cci'],
                'vol_strength': vol_strength
            })
            
            # [최적화] API 호출 간격 조절은 api.py의 ThrottledSession에서 전담하므로
            # 이곳의 강제 대기(time.sleep)를 제거하여 처리 속도를 최적화합니다.

    # 결과 출력
    if not results:
        config.console.print(f"[yellow]해당 조건({market_filter})에 맞는 종목이 없거나 데이터를 불러올 수 없습니다.[/yellow]")
        return

    # 정렬 기준 개선: 1. 점수 높은 순, 2. RSI 낮은 순 (상승 여력)
    # RSI가 None인 경우 맨 뒤로 보내기 위해 999 처리
    results.sort(key=lambda x: (-x['score'], x['rsi'] if x['rsi'] is not None else 999))
    
    table = Table(title=f"전체 종목 진단 결과{title_suffix}", box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table.add_column("종목명(코드)", justify="left")
    table.add_column("현재가", justify="right")
    table.add_column("점수", justify="center")
    table.add_column("상태", justify="center")
    table.add_column("RSI", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("CCI", justify="right")
    table.add_column("체결강도", justify="right")
    
    for r in results:
        s_color = r['state_color'].replace('[', '').replace(']', '')
        score_str = f"[{s_color}]{r['score']}점[/]"
        state_str = f"[{s_color}]{r['state']}[/]"
        
        rsi_val = r['rsi']
        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
        if rsi_val is not None:
            if rsi_val >= 70: rsi_str = f"[magenta]{rsi_str}[/]"
            elif rsi_val <= 30: rsi_str = f"[blue]{rsi_str}[/]"
            
        adx_str = f"{r['adx']:.1f}" if r['adx'] is not None else "-"
        cci_str = f"{r['cci']:.1f}" if r['cci'] is not None else "-"
        
        vol_val = r.get('vol_strength')
        vol_str = f"{vol_val:.1f}%" if vol_val else "-"
        
        table.add_row(
            f"{r['name']}({r['code']})",
            f"{int(r['price']):,}원",
            score_str,
            state_str,
            rsi_str,
            adx_str,
            cci_str
        )
        
    config.console.print(table)
    config.console.print()

def get_analysis_params():
    """분석에 사용할 파라미터를 사용자로부터 입력받습니다."""
    params = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    }
    
    config.console.print("\n[bold]분석 파라미터 설정 (Enter: 기본값 사용, q: 취소)[/bold]")
    
    val = Prompt.ask(f"매수 기준 점수 (기본: {params['BUY_SCORE']})", default=str(params['BUY_SCORE']))
    if val.lower() == 'q': return None
    try: params['BUY_SCORE'] = float(val)
    except: pass
    
    val = Prompt.ask(f"매수 허용 최대 RSI (기본: {params['BUY_RSI_MAX']})", default=str(params['BUY_RSI_MAX']))
    if val.lower() == 'q': return None
    if val.isdigit(): params['BUY_RSI_MAX'] = int(val)
    
    # [추가] 체결강도 입력
    current_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
    val = Prompt.ask(f"매수 체결강도 기준(%) (기본: {current_vol}, 0: 미사용)", default=str(current_vol))
    if val.lower() == 'q': return None
    try: params['BUY_VOL_STRENGTH'] = float(val)
    except: params['BUY_VOL_STRENGTH'] = current_vol

    val = Prompt.ask(f"상승 추세 기준 점수 (기본: {params['RISE_SCORE']})", default=str(params['RISE_SCORE']))
    if val.lower() == 'q': return None
    try: params['RISE_SCORE'] = float(val)
    except: pass
    
    filter_choice = Prompt.ask("출력 대상 선택 (1: 매수, 2: 상승, 3: 매수+상승)", choices=["1", "2", "3", "q"], default="1")
    if filter_choice.lower() == 'q': return None
    if filter_choice == '1': params['OUTPUT_FILTER'] = 'BUY'
    elif filter_choice == '2': params['OUTPUT_FILTER'] = 'RISE'
    else: params['OUTPUT_FILTER'] = 'ALL'
    
    return params

def _get_master_stock_list(market_type):
    """(내부함수) 마스터 파일 다운로드 및 파싱하여 종목 리스트 반환"""
    base_dir = getattr(config, 'DATA_DIR', 'data')
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)

    if market_type == 'KOSPI':
        url = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
        filename = "kospi_code.mst"
    else:
        url = "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"
        filename = "kosdaq_code.mst"

    zip_path = os.path.join(base_dir, f"{filename}.zip")
    extract_path = os.path.join(base_dir, filename)
    
    stock_list = []

    try:
        # [수정] 파일이 존재하고 오늘 다운로드된 것이라면 다운로드 스킵
        need_download = True
        if os.path.exists(zip_path) and os.path.exists(extract_path):
            file_time = datetime.fromtimestamp(os.path.getmtime(zip_path))
            if file_time.date() == datetime.now().date():
                need_download = False
                config.console.print(f"[dim]{market_type} 마스터 파일이 최신입니다. (기존 파일 사용)[/dim]")

        if need_download:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                "•",
                DownloadColumn(),
                "•",
                TransferSpeedColumn(),
                "•",
                TimeRemainingColumn(),
                console=config.console
            ) as progress:
                task_id = progress.add_task(f"[green]{market_type} 마스터 파일 다운로드...", total=None)
                
                def report_hook(block_num, block_size, total_size):
                    progress.update(task_id, total=total_size, completed=block_num * block_size)
                
                urllib.request.urlretrieve(url, zip_path, reporthook=report_hook)

            with config.console.status(f"[green]{market_type} 데이터 압축 해제 중...[/]"):
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(base_dir)
            
        with config.console.status(f"[green]{market_type} 종목 리스트 로딩 및 파싱 중...[/]"):
            with open(extract_path, 'rb') as f:
                for line in f:
                    try:
                        code = line[0:9].decode('cp949').strip()
                        name = line[21:61].decode('cp949').strip()
                        
                        if len(code) == 6:
                            stock_list.append({'code': code, 'name': name})
                    except Exception:
                        continue
    except Exception as e:
        config.console.print(f"[red]{market_type} 마스터 파일 처리 실패: {e}[/red]")
        
    return stock_list

def _analyze_stock_worker(stock, params=None):
    """(내부함수) 단일 종목 분석 워커 (멀티스레드용)"""
    code = stock['code']
    name = stock['name']
    
    try:
        # API 호출 (api.py 내부에서 Rate Limit 처리됨)
        df = api.get_chart_data(code, is_overseas=False)
        if df is None or df.empty: return None
        
        current_price = float(df.iloc[-1]['close'])
        ind = indicators.calculate_indicators(df)
        
        # 전일 RSI 계산
        prev_rsi = None
        if len(df) >= 16:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(com=13, adjust=False).mean()
            loss = -delta.where(delta < 0, 0).ewm(com=13, adjust=False).mean()
            try: prev_rsi = (100 - (100 / (1 + gain/loss))).iloc[-2]
            except: pass

        # 상태 분류 및 점수 계산
        state, state_color, state_reason = classify_stock_state(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'),
            thresholds=params
        )
        
        if state == "-": return None # 데이터 부족

        # [추가] 초기 상태 보존 (로그 출력 시 체결강도 미달로 관망으로 변경되더라도 원본 상태 표시)
        initial_state = state
        initial_state_color = state_color

        score, _ = calculate_score(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
        )
        
        # 52주 위치 계산 (최근 250일 기준)
        w52_pos = 0.0
        if len(df) > 0:
            recent_df = df.tail(250)
            h52 = recent_df['high'].max()
            l52 = recent_df['low'].min()
            if h52 > l52:
                w52_pos = (current_price - l52) / (h52 - l52) * 100

        # [수정] 체결강도 조회 최적화: 필터 조건에 맞는 종목만 조회
        vol_strength = None
        
        # 조회 대상 상태 정의 (기본: 매수, 상승)
        target_vol_states = ["매수", "상승"]
        
        if params:
            filter_mode = params.get("OUTPUT_FILTER", "ALL")
            if filter_mode == "BUY": target_vol_states = ["매수"]
            elif filter_mode == "RISE": target_vol_states = ["상승"]
        
        # 현재 상태가 조회 대상에 포함될 때만 체결강도 API 호출
        if state in target_vol_states:
            # [추가] 조회 실패 시 재시도 로직 (최대 2회)
            for _ in range(2):
                try:
                    vol_strength = api.get_realtime_vol_strength(code)
                    if vol_strength is not None: break
                except: time.sleep(0.1)

        # [수정] 매수 또는 상승 상태일 경우 체결강도 기준 체크 (필터링)
        if state in ["매수", "상승"] and vol_strength is not None:
            try:
                if params and 'BUY_VOL_STRENGTH' in params:
                    min_vol = params['BUY_VOL_STRENGTH']
                else:
                    min_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
                
                if min_vol > 0 and vol_strength < min_vol:
                    state = "관망"
                    state_color = "[white]"
                    state_reason = f"체결강도 미달({vol_strength:.1f}% < {min_vol}%)"
            except: pass

        # 필터링 조건 확인
        is_target = False
        if params:
            filter_mode = params.get("OUTPUT_FILTER", "BUY")
            target_states = []
            if filter_mode == "BUY": target_states = ["매수"]
            elif filter_mode == "RISE": target_states = ["상승"]
            elif filter_mode == "ALL": target_states = ["매수", "상승"]
            if state in target_states:
                is_target = True
        else:
            is_target = True # params가 없으면(엑셀 저장 등) 모두 유효

        return {
            'code': code, 'name': name, 'price': current_price,
            'score': score, 'state': initial_state, 'state_color': initial_state_color, 'state_reason': state_reason,
            'rsi': ind['rsi'], 'adx': ind['adx'], 'cci': ind['cci'], 'obv_trend': ind.get('obv_trend'),
            'psar': ind['psar'], 'macd': ind.get('macd'), 'macd_signal': ind.get('macd_signal'),
            'is_target': is_target, 
            'vol_strength': vol_strength,
            'w52_pos': w52_pos
        }
    except Exception: return None

def analyze_market_stocks(market_type):
    """선택한 시장의 전체 종목을 분석하고 매수 가능 종목을 출력합니다."""
    
    # 1. DB에서 기존 분석 결과 확인
    cached_data = _load_analysis_result(market_type)
    buy_candidates = []
    params = None
    use_cache = False
    
    if cached_data:
        updated_at = cached_data['updated_at']
        c_params = cached_data['params']
        
        config.console.print(f"\n[bold cyan]기존 분석 결과가 존재합니다.[/bold cyan]")
        config.console.print(f"• 분석 일시: {updated_at}")
        config.console.print(f"• 분석 조건: 매수 {c_params.get('BUY_SCORE')}점, RSI {c_params.get('BUY_RSI_MAX')}, 체결 {c_params.get('BUY_VOL_STRENGTH', 100)}%, 상승 {c_params.get('RISE_SCORE')}점")
        
        config.console.print()
        choice = Prompt.ask("기존 결과를 보시겠습니까?", choices=["y", "n", "q"], default="y")
        if choice == 'q': return
        if choice == "y":
            buy_candidates = cached_data['data']
            params = c_params
            use_cache = True
            config.console.print(f"[dim]DB에서 {len(buy_candidates)}개의 종목 정보를 로드했습니다.[/dim]")

    # 2. 새로 분석 (캐시 미사용 시)
    if not use_cache:
        stock_list = _get_master_stock_list(market_type)
        config.console.print(f"\n[bold]{market_type} 전체 종목 수: {len(stock_list)}개[/bold]")
        
        c_buy = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        c_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        c_rise = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        c_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        config.console.print(f"현재 설정: 매수 {c_buy}점 / RSI {c_rsi} / 체결 {c_vol}% / 상승 {c_rise}점")

        config.console.print()
        # 파라미터 설정
        change_settings = Prompt.ask("분석 조건을 변경하시겠습니까?", choices=["y", "n", "q"], default="n")
        if change_settings == 'q': return

        if change_settings == 'y':
            params = get_analysis_params()
            if params is None: return
        else:
            params = {
                "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
                "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                "BUY_VOL_STRENGTH": config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0),
                "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
                "OUTPUT_FILTER": "BUY"
            }
            config.console.print(f"[dim]기본 설정으로 진행합니다. (매수: {params['BUY_SCORE']}점, RSI: {params['BUY_RSI_MAX']}, 체결: {params['BUY_VOL_STRENGTH']}%, 상승: {params['RISE_SCORE']}점)[/dim]")
        
        # 설정 백업 및 적용
        original_thresholds = config.ANALYSIS_THRESHOLDS.copy()
        config.ANALYSIS_THRESHOLDS["BUY_SCORE"] = params["BUY_SCORE"]
        config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"] = params["BUY_RSI_MAX"]
        config.ANALYSIS_THRESHOLDS["RISE_SCORE"] = params["RISE_SCORE"]
        config.ANALYSIS_THRESHOLDS["BUY_VOL_STRENGTH"] = params["BUY_VOL_STRENGTH"]

        config.console.print("\n[bold cyan]=== 전체 종목 분석 시작 (중단: Ctrl+C) ===[/bold cyan]")

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
                console=config.console
            ) as progress:
                task = progress.add_task(f"[cyan]{market_type} 분석 중...[/cyan]", total=len(stock_list))
                
                # [최적화] 멀티스레딩 적용 (API Rate Limit 고려하여 워커 수 조정)
                # 실전: 20TPS -> 워커 20개, 모의: 2TPS -> 워커 5개
                max_workers = 20 if not config.session.is_simulation else 5
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    # Future 객체 생성 및 매핑
                    futures = {executor.submit(_analyze_stock_worker, stock, params): stock for stock in stock_list}
                    
                    completed_count = 0
                    for future in concurrent.futures.as_completed(futures):
                        completed_count += 1
                        stock = futures[future]
                        try:
                            result = future.result()
                            if result:
                                rsi_val = result['rsi']
                                rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
                                adx_str = f"{result['adx']:.1f}" if result['adx'] is not None else "-"
                                cci_str = f"{result['cci']:.1f}" if result['cci'] is not None else "-"
                                obv_trend = result.get('obv_trend')
                                obv_str = "상승" if obv_trend is True else ("하락" if obv_trend is False else "-")
                                
                                # SAR 상태
                                sar_val = result.get('psar')
                                sar_str = "상승" if sar_val and result['price'] > sar_val else "하락"
                                
                                # MACD 상태
                                macd_val = result.get('macd')
                                sig_val = result.get('macd_signal')
                                macd_str = "골든" if macd_val is not None and sig_val is not None and macd_val > sig_val else "데드"
                                
                                vol_str = ""
                                if result.get('vol_strength') is not None: vol_str = f", 체결={result['vol_strength']:.0f}%"
                                
                                log_msg = f"[{completed_count}/{len(stock_list)}] [분석] {result['name']}({result['code']}): 현재가={int(result['price']):,}, 점수={result['score']}, 상태={result['state']}, RSI={rsi_str}, ADX={adx_str}, CCI={cci_str}, OBV={obv_str}, SAR={sar_str}, MACD={macd_str}{vol_str}"
                                
                                if result['is_target']:
                                    log_style = "bold green" if result['state'] == "매수" else "bold orange3"
                                    progress.console.print(f"[{log_style}]{log_msg}[/{log_style}]")
                                    buy_candidates.append(result)
                                else:
                                    progress.console.print(f"[dim]{log_msg}[/dim]")
                        except Exception: pass
                        
                        progress.advance(task)
                    
        except KeyboardInterrupt:
            config.console.print("\n[yellow]분석이 사용자에 의해 중단되었습니다.[/yellow]")
        finally:
            # 설정 복구
            config.ANALYSIS_THRESHOLDS = original_thresholds

    # 결과 테이블 출력
    if not buy_candidates:
        config.console.print("\n[yellow]조건을 만족하는 종목이 없습니다.[/yellow]")
        return

    # [추가] 선별된 종목에 대해 업종 정보 보강 (캐시에 없거나 새로 분석한 경우)
    need_sector_fetch = False
    if buy_candidates and 'sector' not in buy_candidates[0]:
        need_sector_fetch = True
        
    if need_sector_fetch:
        with config.console.status("[bold green]선별된 종목의 업종 정보를 조회 중...[/]"):
            # 병렬 처리로 업종 정보 조회
            def fetch_sector(item):
                try:
                    res = api.get_current_price_data(item['code'], is_overseas=False)
                    if res.get('rt_cd') == '0':
                        return res['output'].get('bstp_kor_isnm', '-')
                except: pass
                return '-'

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                future_to_idx = {executor.submit(fetch_sector, item): i for i, item in enumerate(buy_candidates)}
                for future in concurrent.futures.as_completed(future_to_idx):
                    buy_candidates[future_to_idx[future]]['sector'] = future.result()
        
        # 새로 분석했거나 sector 정보가 추가된 경우 DB 저장
        if not use_cache:
            _save_analysis_result(market_type, buy_candidates, params)

    # 정렬 기준 개선: 1. 점수 높은 순, 2. RSI 낮은 순 (상승 여력)
    # RSI가 None인 경우 맨 뒤로 보내기 위해 999 처리
    buy_candidates.sort(key=lambda x: (-x['score'], x['rsi'] if x['rsi'] is not None else 999))
    
    filter_mode = params.get("OUTPUT_FILTER", "BUY")
    if filter_mode == "BUY": filter_str = "매수"
    elif filter_mode == "RISE": filter_str = "상승"
    else: filter_str = "매수/상승"
    config.console.print(f"\n[bold]분석 결과: {filter_str} 종목 {len(buy_candidates)}개[/bold]")
    
    # [수정] 페이징 처리 및 컬럼 포맷 변경 (한 줄 출력, 말줄임 방지)
    # 터미널 높이에 따라 페이지 크기 자동 조절
    try:
        terminal_lines = shutil.get_terminal_size().lines
        # 테이블 헤더, 타이틀, 여백, 프롬프트 공간 등을 고려하여 제외 (약 13줄)
        page_size = max(5, terminal_lines - 13)
    except:
        page_size = 15

    total_items = len(buy_candidates)
    total_pages = (total_items + page_size - 1) // page_size
    
    for page in range(total_pages):
        start_idx = page * page_size
        end_idx = min((page + 1) * page_size, total_items)
        page_items = buy_candidates[start_idx:end_idx]
        
        table = Table(title=f"{market_type} 유망 종목 ({filter_str}) - 페이지 {page+1}/{total_pages}", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("No.", justify="right", width=4)
        table.add_column("종목명(코드)", justify="left", no_wrap=True)
        table.add_column("업종", justify="center", no_wrap=True)
        table.add_column("현재가", justify="right")
        table.add_column("52주(위치)", justify="right")
        table.add_column("점수", justify="center")
        table.add_column("상태", justify="center")
        table.add_column("RSI", justify="right")
        table.add_column("ADX", justify="right")
        table.add_column("CCI", justify="right")
        table.add_column("SAR", justify="center")
        table.add_column("MACD", justify="center")
        table.add_column("OBV", justify="center")
        table.add_column("체결강도", justify="right")
        
        for i, item in enumerate(page_items):
            rsi_str = f"{item['rsi']:.1f}" if item['rsi'] is not None else "-"
            adx_str = f"{item['adx']:.1f}" if item['adx'] is not None else "-"
            cci_str = f"{item['cci']:.1f}" if item['cci'] is not None else "-"
            
            # SAR 상태
            sar_val = item.get('psar')
            sar_str = "[red]상승[/]" if sar_val and item['price'] > sar_val else "[blue]하락[/]"
            
            # MACD 상태
            macd_val = item.get('macd')
            sig_val = item.get('macd_signal')
            macd_str = "-"
            if macd_val is not None and sig_val is not None:
                macd_str = "[red]골든[/]" if macd_val > sig_val else "[blue]데드[/]"

            s_color = item.get('state_color', '[white]').replace('[', '').replace(']', '')
            
            # 52주 위치 색상
            pos = item.get('w52_pos', 0)
            w_color = "[white]"
            if pos >= 90: w_color = "[red]"
            elif pos >= 80: w_color = "[orange3]"
            elif pos <= 20: w_color = "[blue]"
            
            obv_trend = item.get('obv_trend')
            obv_str = "-"
            if obv_trend is True: obv_str = "[red]상승[/]"
            elif obv_trend is False: obv_str = "[blue]하락[/]"
            
            vol_val = item.get('vol_strength')
            vol_str = "-"
            if vol_val is not None:
                std_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
                v_color = "[red]" if vol_val >= std_vol else "[blue]"
                vol_str = f"{v_color}{vol_val:.1f}%[/]"
            
            table.add_row(
                str(start_idx + i + 1),
                f"{item['name']} [dim]({item['code']})[/dim]",
                item.get('sector', '-'),
                f"{int(item['price']):,}원",
                f"{w_color}{pos:.1f}%[/]",
                f"[{s_color}]{item['score']}[/]",
                f"[{s_color}]{item['state']}[/]",
                rsi_str,
                adx_str,
                cci_str,
                sar_str,
                macd_str,
                obv_str,
                vol_str
            )
            
            # 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(page_items):
                table.add_section()
                
        config.console.print(table)
        
        if page < total_pages - 1:
            if Prompt.ask(f"[dim]다음 페이지를 보시겠습니까? (q: 중단)[/dim]", choices=["y", "n", "q"], default="y").lower() in ['q', 'n']:
                break

    # 상세 분석 이동 기능
    from modules import chart
    
    while True:
        config.console.print("\n[dim]개별 진단 및 상세 차트 분석을 보려면 종목 번호를 입력하세요 (Enter: 메뉴복귀)[/dim]")
        choice = Prompt.ask("선택", default="q", show_default=False)
        
        if choice.lower() == 'q':
            break
            
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(buy_candidates):
                selected = buy_candidates[idx]
                code = selected['code']
                name = selected['name']
                
                # [수정] 차트 분석 전 개별 종목 진단 결과 출력
                config.console.print(f"\n[bold green]>> {name}({code}) 개별 진단 및 차트 분석 실행[/bold green]")
                diagnose_stock(code, name, target_is_overseas=False)
                
                chart.generate_visual_chart(code, name, is_overseas=False)
            else:
                config.console.print("[red]잘못된 번호입니다. 리스트에 있는 번호를 입력해주세요.[/red]")
        else:
            config.console.print("[red]올바른 번호를 입력해주세요.[/red]")

def save_all_market_analysis():
    """코스피/코스닥 전 종목 진단 결과를 엑셀로 저장"""
    
    config.console.print("\n[bold cyan]=== 전체종목 진단결과 저장 (Excel) ===[/bold cyan]")
    config.console.print("[dim]코스피 및 코스닥 전 종목을 분석하여 파일로 저장합니다.[/dim]")
    config.console.print("[dim]시간이 오래 걸릴 수 있습니다. (중단: Ctrl+C)[/dim]\n")
    
    if Prompt.ask("진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
        return

    markets = ["KOSPI", "KOSDAQ"]
    results = {} # market -> list of dict

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=config.console
        ) as progress:
            
            for market_type in markets:
                stock_list = _get_master_stock_list(market_type)
                if not stock_list: continue
                
                results[market_type] = []
                
                # 1. 기술적 분석 (Chart Data)
                analyzed_data = []
                task = progress.add_task(f"[cyan]{market_type} 기술적 분석 중...[/cyan]", total=len(stock_list))

                # [최적화] 멀티스레딩 적용
                max_workers = 20 if not config.session.is_simulation else 5
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(_analyze_stock_worker, stock, None): stock for stock in stock_list}
                    
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result()
                            if result:
                                analyzed_data.append(result)
                        except Exception: pass
                        
                        progress.advance(task)
                
                # 2. 업종 정보 조회 (Price Data) 및 데이터 정제
                if analyzed_data:
                    task_sector = progress.add_task(f"[green]{market_type} 업종 정보 조회 및 정리 중...[/green]", total=len(analyzed_data))
                    
                    def fetch_sector_and_format(item):
                        sector = "-"
                        try:
                            res = api.get_current_price_data(item['code'], is_overseas=False)
                            if res.get('rt_cd') == '0':
                                sector = res['output'].get('bstp_kor_isnm', '-')
                        except: pass
                        
                        # 데이터 포맷팅 (소수점 1자리, 정수 등)
                        rsi = round(item['rsi'], 1) if item['rsi'] is not None else None
                        adx = round(item['adx'], 1) if item['adx'] is not None else None
                        cci = round(item['cci'], 1) if item['cci'] is not None else None
                        w52 = int(item['w52_pos']) if item['w52_pos'] is not None else 0
                        vol = round(item['vol_strength'], 1) if item.get('vol_strength') else None
                        
                        # SAR/MACD 상태
                        sar_state = "상승" if item['psar'] and item['price'] > item['psar'] else "하락"
                        
                        macd_state = "-"
                        if item.get('macd') is not None and item.get('macd_signal') is not None:
                            macd_state = "골든" if item['macd'] > item['macd_signal'] else "데드"

                        return {
                            "종목코드": item['code'],
                            "종목명": item['name'],
                            "업종": sector,
                            "현재가(원)": item['price'],
                            "52주위치(%)": w52,
                            "점수": item['score'],
                            "상태": item['state'],
                            "상태사유": item['state_reason'],
                            "RSI": rsi,
                            "ADX": adx,
                            "CCI": cci,
                            "SAR": sar_state,
                            "MACD": macd_state,
                            "OBV": "상승" if item['obv_trend'] else "하락",
                            "체결강도": vol
                        }

                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures_sector = {executor.submit(fetch_sector_and_format, item): item for item in analyzed_data}
                        
                        for future in concurrent.futures.as_completed(futures_sector):
                            try:
                                formatted_result = future.result()
                                results[market_type].append(formatted_result)
                            except Exception: pass
                            progress.advance(task_sector)

        # 엑셀 저장
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = os.path.join(config.DATA_DIR, f"market_analysis_{timestamp}.xlsx")
        
        if not any(results.values()):
            config.console.print("\n[red]저장할 데이터가 없습니다. (마스터 파일 오류 또는 분석 실패)[/red]")
            return

        with config.console.status(f"[bold green]엑셀 파일 저장 중... ({filename})[/]"):
            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                for market_type, data in results.items():
                    if data:
                        # 점수 높은 순 정렬
                        data.sort(key=lambda x: (-x['점수'], x['RSI'] if x['RSI'] is not None else 999))
                        df = pd.DataFrame(data)
                        df.to_excel(writer, sheet_name=market_type, index=False)
                        
                        # 엑셀 서식 적용 (필터, 컬럼 너비, 색상 등)
                        ws = writer.sheets[market_type]
                        ws.auto_filter.ref = ws.dimensions
                        
                        # 헤더에서 컬럼 인덱스 찾기
                        header = [c.value for c in ws[1]]
                        try:
                            col_price = header.index("현재가(원)") + 1
                            col_state = header.index("상태") + 1
                            
                            for row in range(2, ws.max_row + 1):
                                # 현재가 쉼표 포맷
                                ws.cell(row=row, column=col_price).number_format = '#,##0'
                                
                                # 상태 컬럼 색상 적용
                                cell = ws.cell(row=row, column=col_state)
                                val = cell.value
                                if val == "매수": cell.font = Font(color="FF0000", bold=True)
                                elif val == "상승": cell.font = Font(color="FF8C00", bold=True)
                                elif val == "주의": cell.font = Font(color="DAA520", bold=True)
                                elif val == "위험": cell.font = Font(color="0000FF", bold=True)
                        except ValueError: pass
        
        config.console.print(f"\n[bold green]저장 완료: {filename}[/bold green]")
        
    except KeyboardInterrupt:
        config.console.print("\n[yellow]작업이 중단되었습니다.[/yellow]")
    except Exception as e:
        config.console.print(f"\n[bold red]오류 발생: {e}[/bold red]")

def print_table(title, data_list, is_overseas=False):
    is_domestic_etf = ("ETF" in title and not is_overseas)
    use_investor_data = False
    if not is_overseas and data_list:
        test_data = api.get_investor_trend(data_list[0][1])
        if test_data:
            sample = test_data[0]
            if any(api.safe_int(sample.get(k)) != 0 for k in ['prsn_ntby_qty', 'frgn_ntby_qty', 'orgn_ntby_qty']): use_investor_data = True
    
    table = Table(title=f"\n{title}", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
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

    for i, (name, code) in enumerate(data_list):
        curr_data = api.get_current_price_data(code, is_overseas)
        chart_df = api.get_chart_data(code, is_overseas)
        
        ind = indicators.calculate_indicators(chart_df)
        w52_pos_str, per_str, pbr_str, shar_str = "-", "-", "-", "-"
        foreign_rate_str = "-"
        inv_str = "-"
        cached_ex = config.session.exchange_cache.get(code, "NAS") if is_overseas else None
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

            class_name, class_color, _ = classify_stock_state(curr, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], prev_rsi_val, ind['adx'], ind['cci'], ind.get('obv_trend'))
            
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
        
        if table.row_count % 5 == 0 and table.row_count < len(data_list):
            table.add_section()

    config.console.print(table)

def show_stock_analysis():
    config.console.print("\n[bold]분석할 종목 그룹을 선택하세요:[/bold]")
    config.console.print("[1] 국내 주식")
    config.console.print("[2] 국내 ETF")
    config.console.print("[3] 미국 주식")
    config.console.print("[4] 미국 ETF")
    config.console.print("[5] 전체 보기")
    config.console.print("[6] 개별 종목 진단")
    config.console.print("[7] 전체 종목 진단")
    valid_choices = ["1", "2", "3", "4", "5", "6", "7", "12", "34", "11", "22", "33", "44", "55", "q", "Q"]
    config.console.print()
    choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=valid_choices, default="5", show_choices=True)
    
    menu_map = {
        "1": "국내주식", "2": "국내ETF", "3": "미국주식", "4": "미국ETF", "5": "전체보기", "6": "개별진단", "7": "전체진단",
        "12": "국내전체", "34": "미국전체", 
        "11": "국내주식(반복)", "22": "국내ETF(반복)", "33": "미국주식(반복)", "44": "미국ETF(반복)", "55": "전체(반복)"
    }
    if choice in menu_map:
        config.USER_ACTION_BREADCRUMB.append(f"[{choice}] {menu_map[choice]}")

    if choice.lower() == 'q': return
    
    if choice == "6":
        diagnose_stock()
        return

    if choice == "7":
        config.console.print("\n[bold]진단할 시장을 선택하세요:[/bold]")
        config.console.print("[1] 코스피 (KOSPI)")
        config.console.print("[2] 코스닥 (KOSDAQ)")
        config.console.print("[3] 전체 종목 진단 결과 저장 (Excel)")
        config.console.print()
        sub_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "q"], default="1")
        
        if sub_choice.lower() == 'q': return
        
        if sub_choice == "3":
            save_all_market_analysis()
            return

        market_type = "KOSPI" if sub_choice == "1" else "KOSDAQ"
        config.USER_ACTION_BREADCRUMB.append(f"[시장선택] {market_type}")
        
        analyze_market_stocks(market_type)
        return

    interval = 0
    real_choice = choice
    if choice in ["11", "22", "33", "44", "55"]: interval = 60; real_choice = choice[0] 
    elif choice in ["12", "34"]: real_choice = choice

    def get_list(key):
        return [(x['name'], x['code']) for x in config.session.stock_data.get(key, [])]

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

    logger.info(f"운영자 실행: {' - '.join(config.USER_ACTION_BREADCRUMB)}")

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

def get_snapshot(code, is_overseas):
    """주문 시점의 종목 상태 스냅샷 생성 (DB 저장용)"""
    snapshot = {}
    try:
        # 1. 차트 데이터 및 지표
        df = api.get_chart_data(code, is_overseas)
        if df is not None and not df.empty:
            ind = indicators.calculate_indicators(df)
            # numpy float 등을 일반 float으로 변환하여 저장
            snapshot['indicators'] = {k: (float(v) if v is not None else None) for k, v in ind.items()}
            snapshot['price'] = float(df.iloc[-1]['close'])
        
        # 2. 환율 (해외인 경우)
        if is_overseas:
            snapshot['exchange_rate'] = utils.get_exchange_rate()
            
        snapshot['market'] = "Overseas" if is_overseas else "Domestic"
        
    except Exception as e:
        snapshot['error'] = str(e)
        
    return snapshot

def _print_period_price_20(code, is_overseas):
    """기간별 시세 20일치 출력 (5줄마다 구분선)"""
    def _fmt_vol(v):
        val = float(v)
        if val == 0: return "0"
        if val >= 1_000_000: return f"{val/1_000_000:,.1f}M"
        if val >= 1_000: return f"{val/1_000:,.0f}K"
        return f"{val:,.0f}"

    # [수정] 통합 로직: api.get_chart_data 사용 (120일선 계산을 위해 충분한 데이터 확보)
    df = api.get_chart_data(code, is_overseas)
    if df is None or df.empty: return

    # 이동평균선 계산
    for w in [5, 20, 60, 120]:
        df[f'ma{w}'] = df['close'].rolling(window=w).mean()

    # 등락폭/등락률 계산 (get_chart_data는 기본 제공하지 않음)
    df['diff'] = df['close'].diff()
    df['rate'] = df['close'].pct_change() * 100

    # 최신순 정렬 및 20개 추출
    recent_df = df.sort_values('date', ascending=False).head(20)

    title_prefix = "[해외주식]" if is_overseas else "[국내주식]"
    table = Table(title=f"{title_prefix} 기간별 시세 (최근 20일)", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("종가", justify="right")
    table.add_column("등락폭 (등락률)", justify="right")
    table.add_column("시가", justify="right")
    table.add_column("고가", justify="right")
    table.add_column("저가", justify="right")
    table.add_column("거래량", justify="right")
    table.add_column("5일선", justify="right")
    table.add_column("20일선", justify="right")
    table.add_column("60일선", justify="right")
    table.add_column("120일선", justify="right")

    for i, (idx, row) in enumerate(recent_df.iterrows()):
        date_str = str(row['date'])
        if len(date_str) == 8: date_str = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
        
        close = row['close']
        diff = row['diff'] if not pd.isna(row['diff']) else 0
        rate = row['rate'] if not pd.isna(row['rate']) else 0
        
        # 포맷팅 헬퍼
        def fmt_p(val): return f"{val:,.2f}" if is_overseas else f"{int(val):,}"
        def fmt_diff(val): return f"{val:+.2f}" if is_overseas else f"{int(val):+}"
        
        c_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
        diff_str = f"{c_color}{fmt_diff(diff)} ({rate:+.2f}%)[/]"
        
        # [수정] 이동평균선 색상 규칙 변경 (이평선 간 배열 기준)
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

        table.add_row(
            date_str, 
            fmt_p(close), 
            diff_str, 
            fmt_p(row['open']), 
            fmt_p(row['high']), 
            fmt_p(row['low']), 
            _fmt_vol(row['volume']),
            fmt_ma(ma5_val, get_ma_color(ma5_val, 5)),
            fmt_ma(ma20_val, get_ma_color(ma20_val, 20)),
            fmt_ma(ma60_val, get_ma_color(ma60_val, 60)),
            fmt_ma(ma120_val, get_ma_color(ma120_val, 120))
        )
        
        if (i + 1) % 5 == 0 and (i + 1) < len(recent_df):
            table.add_section()
    
    config.console.print(table)
    config.console.print()
