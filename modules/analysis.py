# modules/analysis.py
from rich.table import Table
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn, DownloadColumn, TransferSpeedColumn
from rich import box
import config
import context
import api
import logging
import indicators
import utils
import time
from datetime import datetime
import urllib.request
import sys
import zipfile
import os
import pandas as pd
import concurrent.futures
import shutil
import sqlite3
import json
import math
import re
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from modules import db_manager

logger = logging.getLogger(__name__)

def calculate_score(price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd=None, macd_signal=None, weights=None):
    """퀀트 멀티팩터 스코어링 모델 (10점 만점)"""
    if weights is None: weights = config.SCORING_WEIGHTS
    
    # 기본 가중치 대비 비율 계산 (유연한 배점 적용)
    # 기본값: TREND(4.0), MOMENTUM(2.5), STRENGTH(1.5), SYNERGY(2.0)
    r_trend = weights.get("TREND", 4.0) / 4.0
    r_mom = weights.get("MOMENTUM", 2.5) / 2.5
    r_str = weights.get("STRENGTH", 1.5) / 1.5
    r_syn = weights.get("SYNERGY", 2.0) / 2.0

    score = 0
    details = []

    # 1. Trend Factor (4.0점)
    if ema20 is not None and price > ema20: 
        s = round(0.5 * r_trend, 2)
        score += s
        details.append(f"EMA: 현재가 > 20일선 (+{s:.2f})")
    if ema20 is not None and ema60 is not None and ema20 > ema60: 
        s = round(0.5 * r_trend, 2)
        score += s
        details.append(f"EMA: 20일선 > 60일선 (+{s:.2f})")
    if ema60 is not None and ema120 is not None and ema60 > ema120: 
        s = round(0.5 * r_trend, 2)
        score += s
        details.append(f"EMA: 60일선 > 120일선 (+{s:.2f})")
    
    if macd is not None and macd_signal is not None:
        if macd > macd_signal:
            s = round(1.0 * r_trend, 2)
            score += s
            details.append(f"MACD: 골든크로스 (매수 우위) (+{s:.2f})")
        if macd > 0:
            s = round(0.5 * r_trend, 2)
            score += s
            details.append(f"MACD: 0선 상회 (상승 국면) (+{s:.2f})")

    if sar is not None and price > sar: 
        s = round(1.0 * r_trend, 2)
        score += s
        details.append(f"SAR: 주가 아래 (상승 추세) (+{s:.2f})")
    
    # 2. Momentum Factor (2.5점)
    if rsi is not None:
        if 50 <= rsi <= 75: 
            s = round(1.5 * r_mom, 2)
            score += s
            details.append(f"RSI: {rsi:.1f} (강세 구간) (+{s:.2f})")
        elif 30 <= rsi < 50: 
            s = round(0.5 * r_mom, 2)
            score += s
            details.append(f"RSI: {rsi:.1f} (반등/회복) (+{s:.2f})")
    
    if cci is not None:
        if cci > 0: 
            s = round(0.5 * r_mom, 2)
            score += s
            details.append(f"CCI: {cci:.1f} (상승 추세) (+{s:.2f})")
        if cci > 100: 
            s = round(0.5 * r_mom, 2)
            score += s
            details.append(f"CCI: {cci:.1f} (강한 상승 탄력) (+{s:.2f})")

    # 3. Strength & Volume Factor (1.5점)
    if adx is not None and adx >= 20: 
        s = round(0.5 * r_str, 2)
        score += s
        details.append(f"ADX: {adx:.1f} (추세 형성) (+{s:.2f})")

    if obv_trend: 
        s = round(1.0 * r_str, 2)
        score += s
        details.append(f"OBV: 이동평균 상회 (수급 양호) (+{s:.2f})")

    # 4. Synergy Bonus (2.0점)
    # Trend Confirmation
    if (ema20 and ema60 and ema20 > ema60) and (macd is not None and macd > 0) and (adx is not None and adx >= 20):
        s = round(1.0 * r_syn, 2)
        score += s
        details.append(f"추세 확증: 정배열+MACD양수+ADX (+{s:.2f})")
        
    # Momentum Thrust
    if (macd is not None and macd_signal is not None and macd > macd_signal) and (rsi is not None and rsi >= 50) and obv_trend:
        s = round(1.0 * r_syn, 2)
        score += s
        details.append(f"모멘텀 폭발: MACD골든+RSI강세+OBV (+{s:.2f})")

    return round(score, 2), details

def get_domestic_index_data(market_type):
    """국내 지수 데이터 조회 (KIS API -> yfinance Fallback)"""
    kis_code = "0001"
    yf_ticker = "^KS11"
    
    if market_type == "KOSDAQ": 
        kis_code = "1001"
        yf_ticker = "^KQ11"
    elif market_type == "KOSPI200": 
        kis_code = "2001"
        yf_ticker = "^KS200"
    elif market_type == "KOSDAQ150":
        kis_code = "2203"
        yf_ticker = "^KQ150"
        
    df = None
    try:
        # 1. KIS API 조회
        df = api.get_domestic_index_chart(kis_code)
        
        # [Fix] KIS API 컬럼명 표준화 및 타입 변환
        if df is not None and not df.empty:
            rename_map = {
                'stck_bsop_date': 'date',
                'bstp_nmix_prpr': 'close',
                'bstp_nmix_oprc': 'open',
                'bstp_nmix_hgpr': 'high',
                'bstp_nmix_lwpr': 'low',
                'acml_vol': 'volume'
            }
            df.rename(columns=rename_map, inplace=True)
            
            for col in ['close', 'open', 'high', 'low', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            df.attrs['source'] = 'KIS' # [추가] 데이터 소스 명시

    except Exception as e:
        logger.debug(f"[MARKET_INDEX_DEBUG] {market_type} KIS API 조회 실패: {e}")
        pass
    
    # 2. Fallback 체크
    ma_period = config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20)
    
    if df is None or df.empty or len(df) < ma_period:
        logger.debug(f"[MARKET_INDEX_DEBUG] {market_type} KIS API 데이터 부족/실패({len(df) if df is not None else 0}건) -> yfinance({yf_ticker}) Fallback 시도")
        try:
            df = api.get_chart_data(yf_ticker, is_overseas=True)
            if df is not None:
                df.attrs['source'] = 'YFINANCE' # [추가] 데이터 소스 명시
        except Exception as e:
            logger.debug(f"[MARKET_INDEX_DEBUG] {market_type} yfinance Fallback 실패: {e}")
        
    return df

def get_market_regime(market_type="KOSPI"):
    """시장 국면 판단 (Bull/Bear/Sideways)"""
    try:
        # [수정] 공통 함수를 통해 데이터 조회 (Fallback 적용)
        df = get_domestic_index_data(market_type)
        
        # [수정] 설정된 MA 기간 가져오기
        ma_period = config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20)
        
        if df is None or df.empty or len(df) < ma_period:
            return "Sideways", 0.0 # 데이터 부족 시 횡보로 가정
            
        current_price = float(df.iloc[-1]['close'])
        
        # 지표 계산
        adx_threshold = config.MARKET_REGIME_PARAMS.get("REGIME_ADX_THRESHOLD", 20)
        
        ma_val = df['close'].ewm(span=ma_period, adjust=False).mean().iloc[-1]
        
        # MA 기울기 (최근 5일)
        ma_series = df['close'].ewm(span=ma_period, adjust=False).mean()
        slope = (ma_series.iloc[-1] - ma_series.iloc[-5]) / 5
        
        # ADX 계산
        ind = indicators.calculate_indicators(df)
        adx = ind['adx']
        
        # 국면 판단 로직
        # 1. 강세장: 지수 > MA & 기울기 > 0 & ADX > 기준
        if current_price > ma_val and slope > 0 and adx >= adx_threshold:
            return "Bull", config.MARKET_REGIME_PARAMS.get("BULL_SCORE_ADJ", -1.0)
            
        # 2. 약세장: 지수 < MA
        elif current_price < ma_val:
            return "Bear", config.MARKET_REGIME_PARAMS.get("BEAR_SCORE_ADJ", 1.0)
            
        # 3. 횡보장: 그 외
        else:
            return "Sideways", config.MARKET_REGIME_PARAMS.get("SIDEWAYS_SCORE_ADJ", 0.0)
            
    except Exception as e:
        logger.error(f"시장 국면 판단 오류: {e}")
        return "Sideways", 0.0

def classify_stock_state(price, ema20, ema60, ema120, sar, rsi, prev_rsi, adx, cci, obv_trend, macd=None, macd_signal=None, thresholds=None):
    if price is None or ema60 is None or sar is None or rsi is None: return "-", "[dim]", "데이터 부족"
    
    # [추가] 1. 낙폭과대(역추세) 반등 조건 최우선 확인
    use_mr = thresholds.get("USE_MEAN_REVERSION", config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)) if thresholds else config.ANALYSIS_THRESHOLDS.get("USE_MEAN_REVERSION", True)
    if use_mr and ema20 is not None and prev_rsi is not None and rsi is not None:
        mr_rsi = thresholds.get("MR_RSI_MAX", config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_RSI_MAX", 40.0)
        mr_disp = thresholds.get("MR_DISPARITY_MAX", config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)) if thresholds else config.ANALYSIS_THRESHOLDS.get("MR_DISPARITY_MAX", 90.0)
        
        disparity = (price / ema20) * 100
        # 조건: RSI 침체 도달 후 전일 대비 상승(반등 확인) & 이격도 충분히 하락
        if rsi <= mr_rsi and rsi > prev_rsi and disparity <= mr_disp:
            return "역추세매수", "[magenta]", "낙폭과대 (역추세 반등 신호)"

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
    if is_severe_danger: return "매도", "[blue]", ", ".join(reasons)
    
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
    
    # [수정] 가중치 적용 점수 계산 (thresholds에 weights가 포함되어 있을 수 있음)
    weights = thresholds.get("WEIGHTS") if thresholds else None
    score, _ = calculate_score(price, ema20, ema60, ema120, sar, rsi, adx, cci, obv_trend, macd, macd_signal, weights=weights)

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

def _init_analysis_db_logic():
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

def _save_analysis_result_logic(market_type, results, params):
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

def _load_analysis_result_logic(market_type):
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

# [수정] 큐 시스템을 통한 실행 래퍼 함수들
def _init_analysis_db():
    _init_analysis_db_logic()

def _save_analysis_result(market_type, results, params):
    if hasattr(db_manager.db, 'execute_custom'):
        db_manager.db.execute_custom(_save_analysis_result_logic, market_type, results, params)
    else:
        _save_analysis_result_logic(market_type, results, params)

def _load_analysis_result(market_type):
    if hasattr(db_manager.db, 'execute_custom'):
        return db_manager.db.execute_custom(_load_analysis_result_logic, market_type)
    else:
        return _load_analysis_result_logic(market_type)


def diagnose_stock(target_code=None, target_name=None, target_is_overseas=False):
    """특정 종목에 대해 시스템 트레이딩 로직을 진단(시뮬레이션)합니다."""
    config.console.print("\n[bold cyan]=== 개별 종목 분석 (Analysis) ===[/]")
    
    code, name, is_overseas = None, None, False

    if target_code:
        code, name, is_overseas = target_code, target_name, target_is_overseas
    else:
        # [수정] 종목 선택 메뉴 확장 ([5] 직접 입력 추가 및 기본값 설정)
        config.console.print("\n[bold]분석할 종목을 선택하세요:[/bold]")
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

    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    # [추가] 개별 룰 로드 및 설정 준비
    custom_rule = db_manager.db.get_stock_strategy(code)
    rule_applied = False
    
    # 기본값 설정
    buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
    buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
    rise_score = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
    buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
    weights = config.SCORING_WEIGHTS
    
    if custom_rule:
        rule_applied = True
        buy_score = custom_rule['buy_score']
        buy_rsi = custom_rule['buy_rsi']
        if custom_rule.get('weights'):
            try:
                w_data = custom_rule['weights']
                if isinstance(w_data, str): weights = json.loads(w_data)
                elif isinstance(w_data, dict): weights = w_data
            except: pass
        if custom_rule.get('buy_vol_strength'):
            buy_vol = custom_rule['buy_vol_strength']

    foreign_rate_str = "-"
    # [추가] 적응형 임계값 적용 (시장 국면 보정)
    score_adj = 0.0
    if not is_overseas:
        try:
            # API로 시장 구분 및 외인 소진율 확인
            cp = api.get_current_price_data(code, False)
            if cp.get('rt_cd') == '0':
                foreign_rate_str = f"{cp['output'].get('hts_frgn_ehrt', '-')}%"
                
                if config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
                    market_type = "KOSDAQ" if "코스닥" in cp['output'].get('rprs_mrkt_kor_name', '') else "KOSPI"
                    regime, score_adj = get_market_regime(market_type)
                    if score_adj != 0:
                        buy_score += score_adj
        except: pass

    # [추가] 임계값 및 가중치 딕셔너리 구성
    thresholds = {
        "BUY_SCORE": buy_score,
        "BUY_RSI_MAX": buy_rsi,
        "RISE_SCORE": rise_score,
        "WEIGHTS": weights
    }

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task(f"[green]{name}({code}) 데이터 분석 중...[/]", total=None)

        # 1. 데이터 조회 (실시간 시세 반영된 일봉)
        progress.update(task, description=f"[green]{name}({code}) 차트 데이터 조회 중...[/]")
        df = api.get_chart_data(code, is_overseas=is_overseas)
        if df is None or df.empty:
            config.console.print("[red]차트 데이터를 불러올 수 없습니다.[/red]")
            return

        # 2. 지표 계산
        progress.update(task, description="[green]기술적 지표 계산 및 상태 분류 중...[/]")
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
            ind['psar'], ind['rsi'], prev_rsi, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'),
            thresholds=thresholds # [수정] 임계값 및 가중치 전달
        )
        
        score, details = calculate_score(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'),
            weights=weights
        )

        # [추가] 체결강도 조회 (국내주식인 경우만)
        vol_strength = None
        if not is_overseas:
            progress.update(task, description="[green]실시간 체결강도 조회 중...[/]")
            vol_strength = api.get_realtime_vol_strength(code)

    # 4. 결과 출력
    config.console.print()
    
    # [테이블 1] 기술적 지표 분석
    tech_title = f"기술적 지표 분석: {name} ({code})"

    table_tech = Table(title=tech_title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
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

    # ATR (변동성)
    atr_val = ind.get('atr', 0)
    vol_str = "-"
    if atr_val > 0 and current_price > 0:
        annual_vol = (atr_val / current_price) * math.sqrt(252) * 100
        vol_str = f"{annual_vol:.1f}%"
    
    table_tech.add_row("변동성 (ATR)", f"{int(atr_val):,} ({vol_str})", "연환산 변동성 (리스크)")

    # SAR
    sar_pos = "주가 아래 (상승)" if ind['psar'] < current_price else "주가 위 (하락)"
    sar_color = "[red]" if ind['psar'] < current_price else "[blue]"
    table_tech.add_row("SAR 위치", f"{sar_color}{sar_pos}[/]", "파라볼릭 추세 전환")

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

    # [추가] 외인 소진율
    if not is_overseas:
        table_tech.add_row("외인 소진율", foreign_rate_str, "외국인 보유 비중")

    config.console.print(table_tech)
    config.console.print()
    
    # [테이블 2] 시스템 트레이딩 판단 결과
    logic_title = "시스템 트레이딩 판단 결과"
    changes_summary = None
    if score_adj != 0:
        logic_title += " [bold magenta](*)[/]"
        
    if rule_applied:
        # [추가] 변경된 룰 요약
        changes = []
        
        # 전역 설정값 가져오기
        def_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        def_buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        def_buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        def_sell_score = config.SELL_STRATEGY["SELL_SCORE"]
        def_tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
        def_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]
        
        # 비교 및 요약
        if custom_rule.get('buy_score') != def_buy_score:
            changes.append(f"매수점수({def_buy_score}->{custom_rule['buy_score']})")
        if custom_rule.get('buy_rsi') != def_buy_rsi:
            changes.append(f"매수RSI({def_buy_rsi}->{custom_rule['buy_rsi']})")
        if custom_rule.get('buy_vol_strength') and custom_rule['buy_vol_strength'] != def_buy_vol:
            changes.append(f"체결강도({def_buy_vol}%->{custom_rule['buy_vol_strength']}%)")
        
        if custom_rule.get('sell_score') != def_sell_score:
            changes.append(f"매도점수({def_sell_score}->{custom_rule['sell_score']})")
        if custom_rule.get('take_profit') != def_tp:
            changes.append(f"익절({def_tp}%->{custom_rule['take_profit']}%)")
        if custom_rule.get('stop_loss') != def_sl:
            changes.append(f"손절({def_sl}%->{custom_rule['stop_loss']}%)")
            
        if custom_rule.get('weights'):
            changes.append("가중치")

        if changes:
            changes_summary = ", ".join(changes)

    table_logic = Table(title=logic_title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
    table_logic.add_column("항목", justify="center", style="cyan", width=15)
    table_logic.add_column("결과", justify="center", width=30)
    table_logic.add_column("상세 내용 / 사유", justify="left", style="dim")

    # 종합 점수
    s_color = state_color.replace('[', '').replace(']', '')
    score_str = f"[bold {s_color}]{score:.2f}점[/]"
    
    details_str = ""
    if details:
        details_str = "\n".join([f"[green]* {d}[/green]" for d in details])
    else:
        details_str = "[dim]획득한 점수가 없습니다.[/dim]"
    
    table_logic.add_row("종합 점수", score_str, details_str)
    
    # [추가] 적용 가중치 정보 출력
    w_val = f"{weights.get('TREND', 4.0):.1f} / {weights.get('MOMENTUM', 2.5):.1f} / {weights.get('STRENGTH', 1.5):.1f} / {weights.get('SYNERGY', 2.0):.1f}"
    w_desc = "추세 / 모멘텀 / 강도 / 시너지"
    if rule_applied and custom_rule.get('weights'):
        w_desc += " [magenta](개별 설정)[/]"
    else:
        w_desc += " [dim](시스템 설정)[/dim]"
    table_logic.add_row("적용 가중치", w_val, w_desc)
    
    # 상태 분류
    table_logic.add_row("상태 분류", f"[bold {s_color}]{state}[/]", state_reason)
    
    # 매수 조건 체크
    buy_score_limit = buy_score
    buy_rsi_limit = thresholds["BUY_RSI_MAX"]
    
    is_mr_state = (state == "역추세매수")
    if is_mr_state:
        buy_vol_limit = thresholds.get("MR_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0))
    else:
        buy_vol_limit = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        if rule_applied and custom_rule.get('buy_vol_strength'):
            buy_vol_limit = custom_rule['buy_vol_strength']
        
    is_buy_score = score >= buy_score_limit
    is_buy_rsi = ind['rsi'] < buy_rsi_limit
    is_safe_state = state not in ["위험", "주의"]
    is_buy_vol = True
    if vol_strength is not None:
        is_buy_vol = vol_strength >= buy_vol_limit
    
    if is_mr_state:
        buy_result = "[bold magenta]매수 가능 (역추세)[/]" if is_buy_vol else "[bold blue]매수 불가 (체결강도 미달)[/]"
    else:
        buy_result = "[bold red]매수 가능[/]" if (is_buy_score and is_buy_rsi and is_safe_state and is_buy_vol) else "[bold blue]매수 불가[/]"
    
    buy_reason_list = []
    if not is_safe_state: buy_reason_list.append(f"진입 불가 상태 ({state})")
    if not is_buy_score and not is_mr_state: # 역추세매수는 점수 무관
        if score_adj != 0:
            origin_score = round(buy_score_limit - score_adj, 2)
            buy_reason_list.append(f"점수 미달 (기준: {buy_score_limit} 이상 [설정: {origin_score}, 시장보정 {score_adj:+.1f}점])")
        else:
            buy_reason_list.append(f"점수 미달 (기준: {buy_score_limit}점 이상)")
    if not is_buy_rsi and not is_mr_state: buy_reason_list.append(f"RSI 과열 (기준: {buy_rsi_limit} 미만)")
    if not is_buy_vol: buy_reason_list.append(f"체결강도 미달 ({vol_strength:.1f}% < {buy_vol_limit}%)")
    buy_reason = ", ".join(buy_reason_list) if buy_reason_list else ("역추세 반등 확인" if is_mr_state else "모든 매수 조건 충족")
    
    table_logic.add_row("매수 판단", buy_result, buy_reason)
    
    # 매도(추세 이탈) 조건 체크
    sell_score_limit = config.SELL_STRATEGY["SELL_SCORE"]
    is_sell_signal = (score < sell_score_limit) or (state == "매도")
    
    sell_result = "[bold blue]매도(추세이탈)[/]" if is_sell_signal else "[bold green]보유(추세유지)[/]"
    
    if state == "매도":
        sell_reason = "매도 상태 진입 (필터링 조건)"
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

    # [수정] 개별 룰 적용 여부 및 상세 내용 출력
    rule_res = "[bold magenta]적용[/]" if rule_applied else "[dim]미적용[/]"
    rule_desc = f"[dim]{changes_summary}[/dim]" if changes_summary else "-"
    table_logic.add_row("개별 룰", rule_res, rule_desc)

    config.console.print(table_logic)
    
    if score_adj != 0:
        config.console.print("[dim] (*) 적응형 임계값(시장 국면 보정)이 적용된 분류 결과입니다.[/dim]")

    config.console.print()

    # [추가] 기간별 시세 30일치 출력
    _print_period_price_30(code, is_overseas)

def _diagnose_group_stock_worker(item, market_filter, restricted_stocks, rules_map):
    """(내부함수) 관심 종목 일괄 분석용 단일 워커 (병렬 처리용)"""
    try:
        code = item['code']
        name = item['name']
        
        # 1. 시장 구분 확인 (필터링이 필요한 경우)
        if market_filter:
            try:
                # 현재가 조회로 시장 구분 확인
                cp_data = api.get_current_price_data(code, is_overseas=False)
                if cp_data.get('rt_cd') != '0': return None
                
                mrkt_name = cp_data['output'].get('rprs_mrkt_kor_name', '')
                # 유가증권(KOSPI), 코스닥(KOSDAQ)
                is_kospi = "유가증권" in mrkt_name or "KOSPI" in mrkt_name
                is_kosdaq = "코스닥" in mrkt_name or "KOSDAQ" in mrkt_name
                
                if market_filter == "KOSPI" and not is_kospi: return None
                if market_filter == "KOSDAQ" and not is_kosdaq: return None
            except:
                return None

        # 2. 차트 데이터 및 지표 계산
        df = api.get_chart_data(code, is_overseas=False)
        if df is None or df.empty: return None
        
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
        
        # [추가] 개별 룰 여부 확인
        is_custom_rule = code in rules_map
        is_restricted = code in restricted_stocks
        
        return {
            'code': code, 'name': name, 'price': current_price,
            'score': score, 'state': state, 'state_color': state_color,
            'rsi': ind['rsi'], 'adx': ind['adx'], 'cci': ind['cci'],
            'vol_strength': vol_strength, 'is_custom_rule': is_custom_rule,
            'is_restricted': is_restricted
        }
    except Exception:
        return None

def diagnose_group_stocks(market_filter=None):
    """등록된 종목들에 대해 일괄 분석을 수행합니다."""
    # 대상: 국내 주식 + 국내 ETF
    targets = config.session.stock_data.get('stocks_kr', []) + config.session.stock_data.get('etfs_kr', [])
    
    if not targets:
        config.console.print("[yellow]등록된 국내 종목이 없습니다.[/yellow]")
        return
        
    # [추가] 개별 룰 로드 (전체 조회 최적화)
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {r['code']: True for r in custom_rules}

    # [추가] 트레이딩 제한 종목 로드
    from modules import auto_trade
    restricted_stocks = auto_trade.load_restricted_stocks()
    any_restricted = False

    results = []
    
    title_suffix = f" ({market_filter})" if market_filter else " (전체)"
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=config.console,
        transient=True
    ) as progress:
        task = progress.add_task(f"[green]등록된 종목 병렬 분석 중{title_suffix}...[/]", total=len(targets))
        
        # [최적화] 실전: 4개 스레드 병렬 처리, 모의: 순차 처리(단일 스레드)
        if config.session.is_simulation:
            for item in targets:
                res = _diagnose_group_stock_worker(item, market_filter, restricted_stocks, rules_map)
                if res: results.append(res)
                progress.advance(task)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(_diagnose_group_stock_worker, item, market_filter, restricted_stocks, rules_map) for item in targets]
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res: results.append(res)
                    progress.advance(task)

    # 결과 출력
    if not results:
        config.console.print(f"[yellow]해당 조건({market_filter})에 맞는 종목이 없거나 데이터를 불러올 수 없습니다.[/yellow]")
        return

    # 정렬 기준 개선: 1. 점수 높은 순, 2. RSI 낮은 순 (상승 여력)
    # RSI가 None인 경우 맨 뒤로 보내기 위해 999 처리
    results.sort(key=lambda x: (-x['score'], x['rsi'] if x['rsi'] is not None else 999))
    
    table_title = f"전체 종목 분석 결과{title_suffix}"
    
    # [추가] 적용된 가중치 정보 표시 (검증용)
    if config.SCORING_WEIGHTS:
        w = config.SCORING_WEIGHTS
        w_str = f"{w.get('TREND', 4.0)}/{w.get('MOMENTUM', 2.5)}/{w.get('STRENGTH', 1.5)}/{w.get('SYNERGY', 2.0)}"
        table_title += f" [dim](가중치: {w_str})[/dim]"

    table = Table(title=table_title, box=box.HORIZONTALS, header_style="dim", border_style="dim")
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
        score_str = f"[{s_color}]{r['score']:.2f}점[/]"
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
        
        name_display = r['name']
        if r.get('is_custom_rule'):
            name_display += "*"
        
        if r.get('is_restricted'):
            name_display += "-"
            any_restricted = True
        
        table.add_row(
            f"{name_display}({r['code']})",
            f"{int(r['price']):,}원",
            score_str,
            state_str,
            rsi_str,
            adx_str,
            cci_str
        )
        
    config.console.print(table, crop=False)
    sys.stdout.flush()
    config.console.print()
    
    if any_restricted:
        config.console.print("[dim] (-) 시스템 트레이딩 거래 제한 종목입니다.[/dim]")

def get_analysis_params():
    """분석에 사용할 파라미터를 사용자로부터 입력받습니다."""
    params = {
        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"],
        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"],
        "WEIGHTS": config.SCORING_WEIGHTS.copy() # [추가] 가중치 포함 (복사본 사용)
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

    # [추가] 가중치 설정 입력
    config.console.print("\n[스코어링 가중치 설정]")
    curr_weights = params['WEIGHTS'].copy()
    while True:
        config.console.print("[dim]순서: 추세 / 모멘텀 / 강도 / 시너지 (합계 10점 권장)[/dim]")
        
        try:
            def ask_w(key, desc, default_v):
                v = Prompt.ask(f"{desc} [dim](현재: {default_v})[/dim]", default=str(default_v))
                if v.lower() == 'q': raise ValueError("quit")
                return float(v)

            w_trend = ask_w("TREND", "추세 (TREND)", curr_weights.get('TREND', 4.0))
            w_mom = ask_w("MOMENTUM", "모멘텀 (MOMENTUM)", curr_weights.get('MOMENTUM', 2.5))
            w_str = ask_w("STRENGTH", "강도 (STRENGTH)", curr_weights.get('STRENGTH', 1.5))
            w_syn = ask_w("SYNERGY", "시너지 (SYNERGY)", curr_weights.get('SYNERGY', 2.0))
            
            total_score = w_trend + w_mom + w_str + w_syn
            
            if abs(total_score - 10.0) > 0.01:
                config.console.print(f"\n[bold red]경고: 가중치 합계가 {total_score:.1f}점입니다. (권장: 10.0점)[/bold red]")
                config.console.print("[yellow]합계가 10점이 되도록 다시 입력해주세요.[/yellow]")
                curr_weights = {"TREND": w_trend, "MOMENTUM": w_mom, "STRENGTH": w_str, "SYNERGY": w_syn}
                continue
            
            params['WEIGHTS'] = {"TREND": w_trend, "MOMENTUM": w_mom, "STRENGTH": w_str, "SYNERGY": w_syn}
            break
        except ValueError as e:
            if str(e) == "quit": return None
            config.console.print("[red]잘못된 입력입니다. 숫자를 입력해주세요.[/red]")
            continue

    filter_choice = Prompt.ask("\n출력 대상 선택 (1: 매수, 2: 상승, 3: 매수+상승)", choices=["1", "2", "3", "q"], default="1")
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

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=config.console,
                transient=True
            ) as progress:
                progress.add_task(f"[green]{market_type} 데이터 압축 해제 중...[/]", total=None)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(base_dir)
        
        # [수정] 파일 파싱 시 Progress Bar 적용 (파일 크기 기준)
        file_size = os.path.getsize(extract_path)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task(f"[green]{market_type} 종목 리스트 파싱 중...[/]", total=file_size)
            
            with open(extract_path, 'rb') as f:
                for line in f:
                    progress.advance(task, advance=len(line))
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
    is_custom_rule = stock.get('is_custom_rule', False) # [추가]
    
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

        # [수정] 사용자 설정 가중치 적용
        weights = params.get('WEIGHTS') if params else None
        score, _ = calculate_score(
            current_price, ind['ema_20'], ind['ema_60'], ind['ema_120'], 
            ind['psar'], ind['rsi'], ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal')
            , weights=weights
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
        target_vol_states = ["매수", "역추세매수", "상승"]
        
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

        # [수정] 매수(역추세포함) 또는 상승 상태일 경우 체결강도 기준 체크 (필터링)
        if state in ["매수", "역추세매수", "상승"] and vol_strength is not None:
            try:
                if state == "역추세매수":
                    min_vol = params.get("MR_VOL_STRENGTH", config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)) if params else config.ANALYSIS_THRESHOLDS.get("MR_VOL_STRENGTH", 120.0)
                elif params and 'BUY_VOL_STRENGTH' in params:
                    min_vol = params.get('BUY_VOL_STRENGTH', 100.0)
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
            if filter_mode == "BUY": target_states = ["매수", "역추세매수"]
            elif filter_mode == "RISE": target_states = ["상승"]
            elif filter_mode == "ALL": target_states = ["매수", "역추세매수", "상승"]
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
            'w52_pos': w52_pos,
            'is_custom_rule': is_custom_rule # [추가]
        }
    except Exception: return None

def analyze_market_stocks(market_type):
    """선택한 시장의 전체 종목을 분석하고 매수 가능 종목을 출력합니다."""
    
    # 1. DB에서 기존 분석 결과 확인
    cached_data = _load_analysis_result(market_type)
    buy_candidates = []
    params = None
    use_cache = False
    
    # [추가] 개별 룰 로드
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {r['code']: True for r in custom_rules}
    
    # [추가] 트레이딩 제한 종목 로드
    from modules import auto_trade
    restricted_stocks = auto_trade.load_restricted_stocks()
    
    if cached_data:
        updated_at = cached_data['updated_at']
        c_params = cached_data['params']
        
        config.console.print(f"\n[bold cyan]기존 분석 결과가 존재합니다.[/bold cyan]")
        config.console.print(f"• 분석 일시: {updated_at}")
        
        w = c_params.get('WEIGHTS', config.SCORING_WEIGHTS)
        w_str = f"{w.get('TREND', 4.0)}/{w.get('MOMENTUM', 2.5)}/{w.get('STRENGTH', 1.5)}/{w.get('SYNERGY', 2.0)}"
        
        # [수정] 매수 점수 표시 (보정 정보 포함)
        buy_score_val = c_params.get('BUY_SCORE')
        buy_score_str = f"{buy_score_val}점"
        if c_params.get('SCORE_ADJ'):
            buy_score_str += f" (시장보정 {c_params['SCORE_ADJ']:+.1f}점)"

        config.console.print(f"• 분석 조건: 매수 {buy_score_str}, RSI {c_params.get('BUY_RSI_MAX')}, 체결 {c_params.get('BUY_VOL_STRENGTH', 100)}%, 상승 {c_params.get('RISE_SCORE')}점, 가중치 {w_str}")
        
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
        
        # [추가] stock_list에 is_custom_rule 정보 주입
        for s in stock_list:
            s['is_custom_rule'] = s['code'] in rules_map
        
        c_buy = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
        c_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
        c_rise = config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
        c_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
        
        w = config.SCORING_WEIGHTS
        w_str = f"{w.get('TREND', 4.0)}/{w.get('MOMENTUM', 2.5)}/{w.get('STRENGTH', 1.5)}/{w.get('SYNERGY', 2.0)}"

        config.console.print(f"현재 설정: 매수 {c_buy}점 / RSI {c_rsi} / 체결 {c_vol}% / 상승 {c_rise}점 / 가중치 {w_str}")

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
                "OUTPUT_FILTER": "BUY",
                "WEIGHTS": config.SCORING_WEIGHTS # [추가] 가중치 전달
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
                
                # [최적화] 실전: 4개 스레드 병렬 처리, 모의: 순차 처리(단일 스레드)
                completed_count = 0
                
                def _process_result(stock_info, res_data):
                    if res_data:
                        rsi_val = res_data['rsi']
                        rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
                        adx_str = f"{res_data['adx']:.1f}" if res_data['adx'] is not None else "-"
                        cci_str = f"{res_data['cci']:.1f}" if res_data['cci'] is not None else "-"
                        obv_trend = res_data.get('obv_trend')
                        obv_str = "상승" if obv_trend is True else ("하락" if obv_trend is False else "-")
                        
                        sar_val = res_data.get('psar')
                        sar_str = "상승" if sar_val and res_data['price'] > sar_val else "하락"
                        
                        macd_val = res_data.get('macd')
                        sig_val = res_data.get('macd_signal')
                        macd_str = "골든" if macd_val is not None and sig_val is not None and macd_val > sig_val else "데드"
                        
                        vol_str = ""
                        if res_data.get('vol_strength') is not None: vol_str = f", 체결={res_data['vol_strength']:.0f}%"
                        
                        log_msg = f"[{completed_count}/{len(stock_list)}] [분석] {res_data['name']}({res_data['code']}): 현재가={int(res_data['price']):,}, 점수={res_data['score']:.2f}, 상태={res_data['state']}, RSI={rsi_str}, ADX={adx_str}, CCI={cci_str}, OBV={obv_str}, SAR={sar_str}, MACD={macd_str}{vol_str}"
                        
                        if res_data['is_target']:
                            log_style = "bold green" if res_data['state'] in ["매수", "역추세매수"] else "bold orange3"
                            progress.console.print(f"[{log_style}]{log_msg}[/{log_style}]")
                            buy_candidates.append(res_data)
                        else:
                            progress.console.print(f"[dim]{log_msg}[/dim]")
                    else:
                        progress.console.print(f"[dim red][{completed_count}/{len(stock_list)}] [실패] {stock_info['name']}({stock_info['code']}) - 데이터 부족 또는 API 응답 없음[/dim red]")

                if config.session.is_simulation:
                    for stock in stock_list:
                        completed_count += 1
                        try:
                            result = _analyze_stock_worker(stock, params)
                            _process_result(stock, result)
                        except Exception as e:
                            progress.console.print(f"[dim red][{completed_count}/{len(stock_list)}] [오류] {stock['name']}({stock['code']}) - {e}[/dim red]")
                        
                        progress.advance(task)
                else:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                        futures = {executor.submit(_analyze_stock_worker, stock, params): stock for stock in stock_list}
                        for future in concurrent.futures.as_completed(futures):
                            completed_count += 1
                            stock = futures[future]
                            try:
                                result = future.result()
                                _process_result(stock, result)
                            except Exception as e:
                                progress.console.print(f"[dim red][{completed_count}/{len(stock_list)}] [오류] {stock['name']}({stock['code']}) - {e}[/dim red]")
                            
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
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task("[green]선별된 종목의 업종 정보를 조회 중...[/]", total=len(buy_candidates))
            
            # 병렬 처리로 업종 정보 조회
            def fetch_sector(item):
                try:
                    res = api.get_current_price_data(item['code'], is_overseas=False)
                    if res.get('rt_cd') == '0':
                        return res['output'].get('bstp_kor_isnm', '-')
                except: pass
                return '-'

            if config.session.is_simulation:
                for i, item in enumerate(buy_candidates):
                    buy_candidates[i]['sector'] = fetch_sector(item)
                    progress.advance(task)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    future_to_idx = {executor.submit(fetch_sector, item): i for i, item in enumerate(buy_candidates)}
                    for future in concurrent.futures.as_completed(future_to_idx):
                        buy_candidates[future_to_idx[future]]['sector'] = future.result()
                        progress.advance(task)
        
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
        any_restricted_in_page = False
        
        table = Table(title=f"{market_type} 유망 종목 ({filter_str}) - 페이지 {page+1}/{total_pages}", box=box.HORIZONTALS, header_style="dim", border_style="dim")
        table.add_column("No.", justify="right", width=4)
        table.add_column("종목명(코드)", justify="left", no_wrap=True)
        table.add_column("업종", justify="center", no_wrap=True)
        table.add_column("현재가", justify="right")
        table.add_column("52주(위치)", justify="right")
        table.add_column("점수", justify="center")
        table.add_column("상태", justify="center")
        table.add_column("추세SMO", justify="center")
        table.add_column("RSI", justify="right")
        table.add_column("ADX", justify="right")
        table.add_column("CCI", justify="right")
        table.add_column("체결강도", justify="right")
        
        for i, item in enumerate(page_items):
            rsi_val = item['rsi']
            rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "-"
            if rsi_val is not None:
                if rsi_val >= config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[magenta]{rsi_str}[/]"
                elif 55 <= rsi_val < config.INDICATOR_PARAMS["RSI_UPPER"]: rsi_str = f"[red]{rsi_str}[/]"
                elif 45 <= rsi_val < 55: rsi_str = f"[orange3]{rsi_str}[/]"
                elif config.INDICATOR_PARAMS["RSI_LOWER"] < rsi_val < 45: rsi_str = f"[yellow]{rsi_str}[/]"
                else: rsi_str = f"[blue]{rsi_str}[/]"

            adx_val = item['adx']
            adx_str = f"{adx_val:.1f}" if adx_val is not None else "-"
            if adx_val is not None:
                if adx_val >= 40: adx_str = f"[magenta]{adx_str}[/]" 
                elif adx_val >= 30: adx_str = f"[red]{adx_str}[/]"     
                elif adx_val >= 20: adx_str = f"[orange3]{adx_str}[/]"
                elif adx_val >= 15: adx_str = f"[yellow]{adx_str}[/]"
                else: adx_str = f"[white]{adx_str}[/]"

            cci_val = item['cci']
            cci_str = f"{cci_val:.1f}" if cci_val is not None else "-"
            if cci_val is not None:
                if cci_val >= config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[red]{cci_str}[/]"
                elif 0 < cci_val < config.INDICATOR_PARAMS["CCI_UPPER"]: cci_str = f"[orange3]{cci_str}[/]"
                elif config.INDICATOR_PARAMS["CCI_LOWER"] < cci_val <= 0: cci_str = f"[yellow]{cci_str}[/]"
                else: cci_str = f"[blue]{cci_str}[/]"
            
            # SAR 상태
            sar_val = item.get('psar')
            sar_icon = "[red]⬆[/]" if sar_val and item['price'] > sar_val else "[blue]⬇[/]"
            
            # MACD 상태
            macd_val = item.get('macd')
            sig_val = item.get('macd_signal')
            macd_icon = "-"
            if macd_val is not None and sig_val is not None:
                zero_sign = "+" if macd_val > 0 else "-"
                cross_char = "G" if macd_val > sig_val else "D"
                m_color = "red" if macd_val > sig_val else "blue"
                macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"

            s_color = item.get('state_color', '[white]').replace('[', '').replace(']', '')
            
            # 52주 위치 색상
            pos = item.get('w52_pos', 0)
            w_color = "[white]"
            if pos >= 90: w_color = "[red]"
            elif pos >= 80: w_color = "[orange3]"
            elif pos <= 20: w_color = "[blue]"
            
            obv_trend = item.get('obv_trend')
            obv_icon = "-"
            if obv_trend is True: obv_icon = "[red]▲[/]"
            elif obv_trend is False: obv_icon = "[blue]▼[/]"
            
            trend_str = f"{sar_icon} {macd_icon} {obv_icon}"
            
            vol_val = item.get('vol_strength')
            vol_str = "-"
            if vol_val is not None:
                std_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
                v_color = "[red]" if vol_val >= std_vol else "[blue]"
                vol_str = f"{v_color}{vol_val:.1f}%[/]"
            
            name_display = item['name']
            if item['code'] in restricted_stocks:
                name_display += "-"
                any_restricted_in_page = True

            table.add_row(
                str(start_idx + i + 1),
                f"{name_display} [dim]({item['code']})[/dim]",
                item.get('sector', '-'),
                f"{int(item['price']):,}원",
                f"{w_color}{pos:.1f}%[/]",
                f"[{s_color}]{item['score']}[/]",
                f"[{s_color}]{item['state']}[/]",
                trend_str,
                rsi_str,
                adx_str,
                cci_str,
                vol_str
            )
            
            # 5개마다 실선 추가
            if (i + 1) % 5 == 0 and (i + 1) < len(page_items):
                table.add_section()
                
        config.console.print(table, crop=False)
        sys.stdout.flush()
        
        if any_restricted_in_page:
            config.console.print("[dim] (-) 시스템 트레이딩 거래 제한 종목입니다.[/dim]")

        if page < total_pages - 1:
            if Prompt.ask(f"[dim]다음 페이지를 보시겠습니까? (q: 중단)[/dim]", choices=["y", "n", "q"], default="y").lower() in ['q', 'n']:
                break

    # 상세 분석 이동 기능
    from modules import chart
    
    while True:
        config.console.print("\n[dim]개별 분석 및 상세 차트 분석을 보려면 종목 번호를 입력하세요 (Enter: 메뉴복귀)[/dim]")
        choice = Prompt.ask("선택", default="q", show_default=False)
        
        if choice.lower() == 'q':
            break
            
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(buy_candidates):
                selected = buy_candidates[idx]
                code = selected['code']
                name = selected['name']
                
                # [수정] 차트 분석 전 개별 종목 분석 결과 출력
                config.console.print(f"\n[bold green]>> {name}({code}) 개별 분석 및 차트 분석 실행[/bold green]")
                diagnose_stock(code, name, target_is_overseas=False)
                
                chart.generate_visual_chart(code, name, is_overseas=False)
            else:
                config.console.print("[red]잘못된 번호입니다. 리스트에 있는 번호를 입력해주세요.[/red]")
        else:
            config.console.print("[red]올바른 번호를 입력해주세요.[/red]")

def save_all_market_analysis():
    """코스피/코스닥 전 종목 분석 결과를 엑셀로 저장"""
    
    config.console.print("\n[bold cyan]=== 전체종목 분석결과 저장 (Excel) ===[/bold cyan]")
    config.console.print("[dim]코스피 및 코스닥 전 종목을 분석하여 파일로 저장합니다.[/dim]")
    config.console.print("[dim]시간이 오래 걸릴 수 있습니다. (중단: Ctrl+C)[/dim]\n")
    
    if Prompt.ask("진행하시겠습니까?", choices=["y", "n"], default="n") != "y":
        return

    # [추가] 개별 룰 로드 (전체 조회 최적화)
    custom_rules = db_manager.db.get_all_stock_strategies()
    rules_map = {r['code']: r for r in custom_rules}

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

                # [최적화] 실전: 멀티스레드 병렬 처리, 모의: 순차 처리(단일 스레드)
                if config.session.is_simulation:
                    for stock in stock_list:
                        try:
                            result = _analyze_stock_worker(stock, None)
                            if result:
                                analyzed_data.append(result)
                        except Exception: pass
                        progress.advance(task)
                else:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
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

                        name_display = item['name']
                        if item.get('is_custom_rule'):
                            name_display += "*"

                        # [추가] 비고 (개별 룰 요약)
                        note = ""
                        if item['code'] in rules_map:
                            rule = rules_map[item['code']]
                            changes = []
                            
                            # 전역 설정값
                            def_buy_score = config.ANALYSIS_THRESHOLDS["BUY_SCORE"]
                            def_buy_rsi = config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"]
                            def_buy_vol = config.ANALYSIS_THRESHOLDS.get("BUY_VOL_STRENGTH", 100.0)
                            def_sell_score = config.SELL_STRATEGY["SELL_SCORE"]
                            def_tp = config.SELL_STRATEGY["TAKE_PROFIT_RATE"]
                            def_sl = config.SELL_STRATEGY["STOP_LOSS_RATE"]

                            # 비교
                            if rule.get('buy_score') != def_buy_score: changes.append(f"매수점수({rule['buy_score']})")
                            if rule.get('buy_rsi') != def_buy_rsi: changes.append(f"매수RSI({rule['buy_rsi']})")
                            if rule.get('buy_vol_strength') and rule['buy_vol_strength'] != def_buy_vol: changes.append(f"체결({rule['buy_vol_strength']}%)")
                            if rule.get('sell_score') != def_sell_score: changes.append(f"매도점수({rule['sell_score']})")
                            if rule.get('take_profit') != def_tp: changes.append(f"익절({rule['take_profit']}%)")
                            if rule.get('stop_loss') != def_sl: changes.append(f"손절({rule['stop_loss']}%)")
                            if rule.get('weights'): changes.append("가중치")
                            
                            if changes:
                                note = f"개별룰: {', '.join(changes)}"
                            else:
                                note = "개별룰 적용"

                        return {
                            "종목코드": item['code'],
                            "종목명": name_display,
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
                            "체결강도": vol,
                            "비고": note # [추가]
                        }

                    if config.session.is_simulation:
                        for item in analyzed_data:
                            try:
                                formatted_result = fetch_sector_and_format(item)
                                results[market_type].append(formatted_result)
                            except Exception: pass
                            progress.advance(task_sector)
                    else:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
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

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task(f"[green]엑셀 파일 저장 중... ({os.path.basename(filename)})[/]", total=len(results))
            
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
                            col_score = header.index("점수") + 1
                            
                            # [수정] 모든 컬럼 너비 자동 조절
                            for i, col_name in enumerate(header):
                                col_idx = i + 1
                                col_letter = get_column_letter(col_idx)
                                
                                # 헤더 텍스트 길이 고려
                                s_header = str(col_name)
                                max_width = len(s_header) + sum(0.7 for c in s_header if ord(c) > 127)
                                
                                for row in range(2, ws.max_row + 1):
                                    val = ws.cell(row=row, column=col_idx).value
                                    if val:
                                        s_val = str(val)
                                        length = len(s_val) + sum(0.7 for c in s_val if ord(c) > 127)
                                        if length > max_width: max_width = length
                                
                                limit = 100 if col_name == "비고" else 60
                                ws.column_dimensions[col_letter].width = min(max_width * 1.2, limit)

                            for row in range(2, ws.max_row + 1):
                                # 현재가 쉼표 포맷
                                ws.cell(row=row, column=col_price).number_format = '#,##0'
                                
                                # 점수 소수점 2자리 포맷
                                ws.cell(row=row, column=col_score).number_format = '0.00'
                                
                                # 상태 컬럼 색상 적용
                                cell = ws.cell(row=row, column=col_state)
                                val = cell.value
                                if val == "매수": cell.font = Font(color="FF0000", bold=True)
                                elif val == "상승": cell.font = Font(color="FF8C00", bold=True)
                                elif val == "주의": cell.font = Font(color="DAA520", bold=True)
                                elif val == "매도": cell.font = Font(color="0000FF", bold=True)
                        except ValueError: pass
                    
                    progress.advance(task)
        
        config.console.print(f"\n[bold green]저장 완료: {filename}[/bold green]")
        
    except KeyboardInterrupt:
        config.console.print("\n[yellow]작업이 중단되었습니다.[/yellow]")
    except Exception as e:
        config.console.print(f"\n[bold red]오류 발생: {e}[/bold red]")

def _print_table_worker(item, title, is_overseas, use_investor_data, restricted_stocks, rules_map, market_regime_adj):
    """(내부함수) print_table용 단일 종목 데이터 조회 및 가공 워커"""
    try:
        name, code = item
        curr_data = api.get_current_price_data(code, is_overseas)
        chart_df = api.get_chart_data(code, is_overseas)
        
        ind = indicators.calculate_indicators(chart_df)
        w52_pos_str, per_str, pbr_str, shar_str = "-", "-", "-", "-"
        foreign_rate_str = "-"
        inv_str = "-"
        cached_ex = config.session.exchange_cache.get(code, "NAS") if is_overseas else None
        strength_display = ""

        # [수정] 타이틀 기반으로 주식/ETF 컨텍스트 정확히 구분 (데이터 처리 및 컬럼 매칭용)
        # 기존: 코드 형태(숫자 여부)로만 판단하여 ETF(QQQ 등)를 주식으로 오인하는 문제 해결
        is_us_stock_context = is_overseas and ("주식" in title)
        is_us_etf_context = is_overseas and ("ETF" in title)

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
                        if rt_strength >= 150: s_color = "[magenta]"
                        elif rt_strength >= 120: s_color = "[red]"
                        elif rt_strength > 100: s_color = "[orange3]"
                        elif rt_strength == 100: s_color = "[white]"
                        elif rt_strength >= 80: s_color = "[yellow]"
                        else: s_color = "[blue]"
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
            
            # [추가] 원인 분석을 위한 디버그 로그 (상장주수 0.00 등 데이터 확인용)
            if config.FILE_DEBUG_LEVEL == "DEBUG":
                logger.debug(f"[ANALYSIS_DEBUG] {code} Detail: {detail} | StockCtx:{is_us_stock_context} EtfCtx:{is_us_etf_context}")

            if detail:
                if is_us_stock_context: 
                    per_str = detail.get('perx', '-')
                    pbr_str = detail.get('pbrx', '-') if detail.get('pbrx') != '-' else '-'
                if is_us_etf_context:
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

            # 적응형 임계값 적용
            thresholds = None
            if market_regime_adj and not is_overseas:
                mrkt_name = curr_data['output'].get('rprs_mrkt_kor_name', '')
                score_adj = 0.0
                if "코스닥" in mrkt_name:
                    score_adj = market_regime_adj.get("KOSDAQ", 0.0)
                else:
                    score_adj = market_regime_adj.get("KOSPI", 0.0)
                
                if score_adj != 0:
                    thresholds = {
                        "BUY_SCORE": config.ANALYSIS_THRESHOLDS["BUY_SCORE"] + score_adj,
                        "BUY_RSI_MAX": config.ANALYSIS_THRESHOLDS["BUY_RSI_MAX"],
                        "RISE_SCORE": config.ANALYSIS_THRESHOLDS["RISE_SCORE"]
                    }

            class_name, class_color, _ = classify_stock_state(curr, ind['ema_20'], ind['ema_60'], ind['ema_120'], ind['psar'], ind['rsi'], prev_rsi_val, ind['adx'], ind['cci'], ind.get('obv_trend'), ind.get('macd'), ind.get('macd_signal'), thresholds=thresholds)
            
            def fmt(v): return f"{v:,.2f}" if is_overseas else f"{int(v):,}"
            def fmt_idx(val): return f"{int(val):,}" if val is not None else "-"

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

            # SAR 상태
            sar_val = ind.get('psar')
            if sar_val is not None:
                sar_icon = "[red]⬆[/]" if curr > sar_val else "[blue]⬇[/]"
            else:
                sar_icon = "-"
            
            # MACD 상태
            macd_val = ind.get('macd')
            sig_val = ind.get('macd_signal')
            macd_icon = "-"
            if macd_val is not None and sig_val is not None:
                zero_sign = "+" if macd_val > 0 else "-"
                cross_char = "G" if macd_val > sig_val else "D"
                m_color = "red" if macd_val > sig_val else "blue"
                macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"

            # OBV 상태
            obv_trend = ind.get('obv_trend')
            if obv_trend is not None:
                obv_icon = "[red]▲[/]" if obv_trend else "[blue]▼[/]"
            else:
                obv_icon = "-"
            
            trend_str = f"{sar_icon} {macd_icon} {obv_icon}"

            # OBV Value
            obv_val = ind.get('obv')
            obv_disp = "-"
            if obv_val:
                obv_c = "red" if ind.get('obv_trend') else "blue"
                if abs(obv_val) >= 100_000_000:
                    obv_str = f"{int(obv_val/1_000_000):,}M"
                else:
                    obv_str = f"{int(obv_val/1000):,}K"
                obv_disp = f"[{obv_c}]{obv_str}[/]"

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
            
            # 제한 종목 표시
            is_restricted = False
            if code in restricted_stocks:
                final_name_str += "-"
                is_restricted = True

            # 개별 룰 적용 종목 표시
            is_custom_rule = False
            if code in rules_map:
                final_name_str += "+"
                is_custom_rule = True

            row_data = [final_name_str, f"{code}", f"{class_color}{class_name}[/]", curr_str, rate_str, ema_5_str, ema_20_str, ema_60_str, ema_120_str, trend_str, rsi_str, adx_str, cci_str]
            if not is_overseas:
                row_data.append(w52_pos_str)
                if use_investor_data: row_data.append(inv_str)
                else: row_data.append(obv_disp)
            else:
                row_data.append(w52_pos_str)
                if is_us_stock_context: row_data.extend([per_str, pbr_str])
                elif is_us_etf_context: row_data.append(shar_str)
            return row_data, is_restricted, is_custom_rule
        else:
            return [name, code, "-", "실패", *["-"] * (14 if not is_overseas else (12 if is_us_stock_context else 11))], False, False
    except Exception as e:
        logger.error(f"[{code}] 분석 오류: {e}")
        return [name, code, "[red]Error[/]", "-", *["-"] * (14 if not is_overseas else (12 if is_us_stock_context else 11))], False, False

def print_table(title, data_list, is_overseas=False):
    is_domestic_etf = ("ETF" in title and not is_overseas)
    use_investor_data = False
    if not is_overseas and data_list:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            progress.add_task("[bold green]수급 데이터 확인 중 (KIS API)...[/]", total=None)
            test_data = api.get_investor_trend(data_list[0][1])
            if test_data:
                sample = test_data[0]
                if any(api.safe_int(sample.get(k)) != 0 for k in ['prsn_ntby_qty', 'frgn_ntby_qty', 'orgn_ntby_qty']): use_investor_data = True
    
    # [이동] 적응형 임계값 준비 (테이블 생성 전으로 이동)
    market_regime_adj = {}
    use_adaptive = False
    if not is_overseas and config.MARKET_REGIME_PARAMS.get("USE_ADAPTIVE_THRESHOLD", True):
        use_adaptive = True
        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                console=config.console,
                transient=True
            ) as progress:
                progress.add_task("[bold green]시장 국면 분석 중 (KIS API)...[/]", total=None)
                _, kospi_adj = get_market_regime("KOSPI")
                _, kosdaq_adj = get_market_regime("KOSDAQ")
                market_regime_adj["KOSPI"] = kospi_adj
                market_regime_adj["KOSDAQ"] = kosdaq_adj
        except:
            use_adaptive = False

    display_title = f"\n{title}" + (" [bold magenta](*)[/]" if use_adaptive else "")
    table = Table(title=display_title, box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
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
    table.add_column("추세SMO", justify="center")
    table.add_column("RSI", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("CCI", justify="right")
    
    is_us_stock = is_overseas and ("주식" in title)
    is_us_etf = is_overseas and ("ETF" in title)
    
    # [추가] 개별 룰 로드
    rules_map = {}
    if not is_overseas:
        custom_rules = db_manager.db.get_all_stock_strategies()
        rules_map = {r['code']: True for r in custom_rules}
    any_custom_rule = False
    
    # [추가] 트레이딩 제한 종목 로드
    from modules import auto_trade
    restricted_stocks = auto_trade.load_restricted_stocks()
    any_restricted = False
    
    if not is_overseas:
        table.add_column("52주", justify="right")
        if use_investor_data: table.add_column("수급(개/외/기)", justify="center")
        else: table.add_column("OBV", justify="right")
    else:
        table.add_column("52주", justify="right")
        if is_us_stock:
            table.add_column("PER", justify="right", style="dim")
            table.add_column("PBR", justify="right", style="dim") 
        elif is_us_etf:
            table.add_column("상장주수", justify="right", style="dim")

    # [최적화] 실전: 4개 스레드 병렬 처리, 모의: 순차 처리(단일 스레드)
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task(f"[cyan]{title}[/cyan]", total=len(data_list))
            results = [None] * len(data_list)

            if config.session.is_simulation:
                for idx, item in enumerate(data_list):
                    try:
                        results[idx] = _print_table_worker(
                            item, title, is_overseas, use_investor_data, restricted_stocks, rules_map, market_regime_adj
                        )
                    except Exception as e:
                        logger.error(f"Print table sequential worker error: {e}")
                        name, code = data_list[idx]
                        results[idx] = ([name, code, "-", "실패", *["-"] * (14 if not is_overseas else (12 if is_us_stock else 11))], False, False)
                    progress.advance(task)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [
                        executor.submit(
                            _print_table_worker, 
                            item, 
                            title, # [수정] title 전달
                            is_overseas, 
                            use_investor_data, 
                            restricted_stocks, 
                            rules_map, 
                            market_regime_adj
                        ) for item in data_list
                    ]
                    
                    # 순서 보장을 위해 인덱스 매핑 후 as_completed 사용
                    future_to_idx = {f: i for i, f in enumerate(futures)}
                    
                    for future in concurrent.futures.as_completed(futures):
                        idx = future_to_idx[future]
                        try:
                            results[idx] = future.result()
                        except Exception as e:
                            logger.error(f"Print table worker error: {e}")
                            name, code = data_list[idx]
                            results[idx] = ([name, code, "-", "실패", *["-"] * (14 if not is_overseas else (12 if is_us_stock else 11))], False, False)
                        
                        progress.advance(task)
            
            # 결과 테이블 추가
            for result_item in results:
                if not result_item: continue
                
                row_data, is_res, is_cust = result_item
                
                if is_res: any_restricted = True
                if is_cust: any_custom_rule = True
                
                table.add_row(*row_data)
                if table.row_count % 5 == 0 and table.row_count < len(data_list):
                    table.add_section()
                    
    except Exception as e:
        logger.error(f"데이터 분석 중 오류: {e}")

    try:
        config.console.print(table, crop=False)
        sys.stdout.flush()
        
        if use_adaptive:
            config.console.print("[dim] (*) 적응형 임계값(시장 국면 보정)이 적용된 분류 결과입니다.[/dim]")

        if any_restricted:
            config.console.print("[dim] (-) 시스템 트레이딩 거래 제한 종목입니다.[/dim]")

        if any_custom_rule:
            config.console.print("[dim] (+) 시스템 트레이딩 시 개별 룰이 적용된 종목입니다.[/dim]")
    except Exception as e:
        logger.error(f"테이블 출력 중 오류(tmux 리사이즈 등): {e}")
        config.console.print(f"[red]테이블 출력 실패: {e}[/red]")

def show_stock_analysis():
    config.console.print("\n[bold]분석할 종목 그룹을 선택하세요 (쉼표로 구분):[/bold]")
    config.console.print("[1] 국내 주식")
    config.console.print("[2] 국내 ETF")
    config.console.print("[3] 미국 주식")
    config.console.print("[4] 미국 ETF")
    config.console.print("[5] 전체 보기")
    config.console.print("[6] 개별 종목 분석")
    config.console.print("[7] 전체 종목 분석")
    config.console.print()
    
    choice_str = Prompt.ask("번호 입력 [dim](예: 1,3 또는 12 / 반복: 1@ / 취소: q)[/dim]", default="5")
    if choice_str.lower() == 'q': return

    interval = 0
    if choice_str.endswith('@'):
        interval = 60
        choice_str = choice_str.rstrip('@')

    raw_choices = [c.strip() for c in choice_str.split(',') if c.strip()]
    choices = []
    for c in raw_choices:
        if c.isdigit() and len(c) > 1:
            choices.extend(list(c))
        else:
            choices.append(c)

    if not choices: return

    if '6' in choices:
        context.USER_ACTION_BREADCRUMB.append("[6] 개별분석")
        diagnose_stock()
        return

    if '7' in choices:
        context.USER_ACTION_BREADCRUMB.append("[7] 전체분석")
        config.console.print("\n[bold]분석할 시장을 선택하세요:[/bold]")
        config.console.print("[1] 코스피 (KOSPI)")
        config.console.print("[2] 코스닥 (KOSDAQ)")
        config.console.print("[3] 전체 종목 분석 결과 저장 (Excel)")
        config.console.print()
        sub_choice = Prompt.ask("선택 [dim](취소: q)[/dim]", choices=["1", "2", "3", "q"], default="1")
        
        if sub_choice.lower() == 'q': return
        
        if sub_choice == "3":
            save_all_market_analysis()
            return

        market_type = "KOSPI" if sub_choice == "1" else "KOSDAQ"
        context.USER_ACTION_BREADCRUMB.append(f"[시장선택] {market_type}")
        
        analyze_market_stocks(market_type)
        return

    selected_groups = set()
    group_names = []
    
    for c in choices:
        if c == '1': 
            selected_groups.add('stocks_kr')
            group_names.append("국내주식")
        elif c == '2': 
            selected_groups.add('etfs_kr')
            group_names.append("국내ETF")
        elif c == '3': 
            selected_groups.add('stocks_us')
            group_names.append("미국주식")
        elif c == '4': 
            selected_groups.add('etfs_us')
            group_names.append("미국ETF")
        elif c == '5': 
            selected_groups.update(['stocks_kr', 'etfs_kr', 'stocks_us', 'etfs_us'])
            group_names.append("전체보기")
    
    if not selected_groups:
        config.console.print("[red]잘못된 입력입니다.[/red]")
        return

    context.USER_ACTION_BREADCRUMB.append(f"[{choice_str}] {','.join(group_names)}")

    target_list = []
    order_map = [
        ('stocks_kr', "국내 주식 기술적 분석", False),
        ('etfs_kr', "국내 ETF 기술적 분석", False),
        ('stocks_us', "미국 주식 기술적 분석", True),
        ('etfs_us', "미국 ETF 기술적 분석", True)
    ]

    for key, title, is_ovs in order_map:
        if key in selected_groups:
            d_list = [(x['name'], x['code']) for x in config.session.stock_data.get(key, [])]
            target_list.append((title, d_list, is_ovs))

    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")

    try:
        while True:
            if interval > 0:
                now_str = datetime.now().strftime("%H:%M:%S")
                config.console.print(f"\n[dim]조회 시간: {now_str}[/dim]")
            
            try:
                for title, d_list, is_ovs in target_list:
                    if d_list: print_table(title, d_list, is_ovs)
            except Exception as e:
                logger.error(f"분석 루프 실행 중 오류: {e}")
                config.console.print(f"[red]분석 중 오류 발생: {e}[/red]")
            
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
    except Exception as e:
        logger.error(f"분석 기능 실행 중 치명적 오류: {e}")
        config.console.print(f"\n[bold red]오류 발생: {e}[/bold red]")

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

def _print_period_price_common(code, is_overseas, limit=20):
    """기간별 시세 출력 공통 함수"""
    def _fmt_vol(v):
        val = float(v)
        if val == 0: return "0"
        if val >= 1_000_000: return f"{val/1_000_000:,.1f}M"
        if val >= 1_000: return f"{val/1_000:,.0f}K"
        return f"{val:,.0f}"

    # [수정] 단순 조회이므로 status 사용
    df = None
    investor_map = {} # [추가]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        console=config.console,
        transient=True
    ) as progress:
        progress.add_task("[bold green]기간별 시세 데이터 조회 중...[/]", total=None)
        df = api.get_chart_data(code, is_overseas)
        # [추가] 수급 데이터 조회
        if not is_overseas:
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
            except: pass

    if df is None or df.empty: return

    # 이동평균선 계산
    for w in [5, 20, 60, 120]:
        df[f'ma{w}'] = df['close'].rolling(window=w).mean()

    # 등락폭/등락률 계산 (get_chart_data는 기본 제공하지 않음)
    df['diff'] = df['close'].diff()
    df['rate'] = df['close'].pct_change() * 100

    # 최신순 정렬 및 limit 적용
    df_sorted = df.sort_values('date', ascending=False)
    if limit:
        recent_df = df_sorted.head(limit)
    else:
        recent_df = df_sorted

    title_prefix = "[해외주식]" if is_overseas else "[국내주식]"
    period_str = f"(최근 {limit}일)" if limit else "(전체)"
    table = Table(title=f"{title_prefix} 기간별 시세 {period_str}", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
    table.add_column("일자", justify="center")
    table.add_column("종가", justify="right")
    table.add_column("등락폭 (등락률)", justify="right")
    table.add_column("시가", justify="right")
    table.add_column("고가", justify="right")
    table.add_column("저가", justify="right")
    table.add_column("5일선", justify="right")
    table.add_column("20일선", justify="right")
    table.add_column("60일선", justify="right")
    table.add_column("120일선", justify="right")
    table.add_column("거래량", justify="right") # [이동]
    if not is_overseas:
        table.add_column("외인률", justify="right") # [추가]
        table.add_column("수급(개/외/기)", justify="center") # [수정]

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

        # [추가] 수급 데이터 포맷팅
        inv_str = "-"
        foreign_rate_str = "-"
        if not is_overseas:
            d_key = str(row['date']).replace('-', '')[:8]
            if d_key in investor_map:
                item = investor_map[d_key]
                p = api.safe_int(item.get('prsn_ntby_qty'))
                f = api.safe_int(item.get('frgn_ntby_qty'))
                o = api.safe_int(item.get('orgn_ntby_qty'))
                
                # 외인률(외국인 소진율) 파싱 복구
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

        row_data = [
            date_str, 
            fmt_p(close), 
            diff_str, 
            fmt_p(row['open']), 
            fmt_p(row['high']), 
            fmt_p(row['low']), 
            fmt_ma(ma5_val, get_ma_color(ma5_val, 5)),
            fmt_ma(ma20_val, get_ma_color(ma20_val, 20)),
            fmt_ma(ma60_val, get_ma_color(ma60_val, 60)),
            fmt_ma(ma120_val, get_ma_color(ma120_val, 120)),
            _fmt_vol(row['volume'])
        ]
        if not is_overseas:
            row_data.append(foreign_rate_str)
            row_data.append(inv_str)

        table.add_row(*row_data)
        
        if (i + 1) % 5 == 0 and (i + 1) < len(recent_df):
            table.add_section()
    
    config.console.print(table)

def _print_period_price_30(code, is_overseas):
    """기간별 시세 30일치 출력"""
    _print_period_price_common(code, is_overseas, limit=30)
    config.console.print()
