# modules/market.py
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.prompt import Prompt
import yfinance as yf
import pandas as pd
import config
import context # [추가] 상태 관리 모듈
import indicators
import api
from modules import analysis # [추가] 분석 모듈 임포트
from datetime import datetime, timedelta
import math
import logging
import time
import sys
import threading
import concurrent.futures # [추가] 병렬 처리용

logger = logging.getLogger(__name__)

# [수정] 지수 리스트 통합 관리 (순서 유지)
ALL_INDICES = [
    ("코스피", "^KS11"), ("코스피200", "^KS200"), ("코스닥", "^KQ11"), ("코스닥150", "^KQ150"),
    ("나스닥 선물", "NQ=F"), ("나스닥", "^IXIC"), ("S&P500", "^GSPC"), ("다우존스", "^DJI"), ("러셀2000", "^RUT"),
    ("금", "GC=F"), ("은", "SI=F"), ("구리", "HG=F"),
    ("브랜트유", "BZ=F"), ("WTI 원유", "CL=F"), ("가솔린 RBOB", "RB=F"),
    ("천연가스", "NG=F"), ("밀", "ZW=F"),
    ("달러인덱스", "DX-Y.NYB"), ("달러환율", "KRW=X"),
    ("VIX (변동성)", "^VIX"), ("SOX (반도체)", "^SOX"),
    ("비트코인", "BTC-USD"), ("이더리움", "ETH-USD"),
    ("Japan - 닛케이", "^N225"), ("Hong Kong - 항셍", "^HSI"), ("China - 상해종합", "000001.SS"), ("Taiwan - 대만가권", "^TWII"),
    ("Germany - 닥스40", "^GDAXI"), ("Europe - 스톡스50", "^STOXX50E")
]

# 이름 -> 티커 매핑 (기존 호환성 유지)
INDICES_MAP = dict(ALL_INDICES)

def _process_index_worker(name, ticker, df_daily, df_intraday, market_regime_cache):
    """(내부함수) 단일 지수 분석 워커"""
    try:
        # --- A. DataFrame 준비 및 지표 계산 ---
        if not df_daily.empty:
            df_daily.columns = [c.lower() for c in df_daily.columns]
            # [이동] 데이터 정제: Close가 NaN인 행 제거 (비트코인 등 데이터 공백 방지)
            if 'close' in df_daily.columns:
                df_daily = df_daily.dropna(subset=['close'])

        if not df_intraday.empty:
            df_intraday.columns = [c.lower() for c in df_intraday.columns]

        # [수정] 국내 지수의 경우 analysis 모듈의 공통 함수를 사용하여 데이터 조회 (Fallback 포함)
        is_domestic_index = name in ["코스피", "코스닥", "코스피200", "코스닥150"]
        kis_code = ""
        is_kis_source = False
        mismatch_msg = None

        if is_domestic_index:
            logger.debug(f"[MARKET_INDEX_DEBUG] Processing {name}...")
            if name == "코스피": kis_code = "0001"; m_type = "KOSPI"
            elif name == "코스닥": kis_code = "1001"; m_type = "KOSDAQ"
            elif name == "코스피200": kis_code = "2001"; m_type = "KOSPI200"
            elif name == "코스닥150": kis_code = "2203"; m_type = "KOSDAQ150"
            
            df_fallback = analysis.get_domestic_index_data(m_type)
            if df_fallback is not None and not df_fallback.empty:
                logger.debug(f"[MARKET_INDEX_DEBUG] {name} - Data Fetched. Shape: {df_fallback.shape}")
                df_daily = df_fallback
                df_daily.columns = [c.lower() for c in df_daily.columns]
                
                # [Fix] KIS API 데이터 사용 시 Index가 DatetimeIndex가 아닌 경우 변환
                if not isinstance(df_daily.index, pd.DatetimeIndex):
                    target_col = None
                    if 'date' in df_daily.columns: target_col = 'date'
                    elif 'stck_bsop_date' in df_daily.columns: target_col = 'stck_bsop_date'
                    if target_col:
                        df_daily[target_col] = pd.to_datetime(df_daily[target_col])
                        df_daily.set_index(target_col, inplace=True)
                
                # [추가] KIS API 데이터와 yfinance 데이터 간 날짜 차이 검증
                if not df_intraday.empty:
                    try:
                        kis_last_dt = df_daily.index[-1].date()
                        yf_last_dt = df_intraday.index[-1].date()
                        if yf_last_dt > kis_last_dt:
                            mismatch_msg = f"{name}(KIS:{kis_last_dt} vs YF:{yf_last_dt})"
                    except Exception: pass
                
                # [추가] 데이터 소스 확인 (KIS API 여부)
                if df_daily.attrs.get('source') == 'KIS':
                    is_kis_source = True

        # 지표 계산
        ema5, ema20, ema60, ema120 = None, None, None, None
        val_psar, val_rsi, val_adx, val_cci, val_macd, val_macd_sig = None, None, None, None, None, None
        high_52_daily = 0.0

        if not df_daily.empty and 'close' in df_daily.columns and len(df_daily) > 10:
            df_calc = df_daily[['open', 'high', 'low', 'close', 'volume']].copy()
            df_calc.dropna(subset=['close'], inplace=True)
            ind = indicators.calculate_indicators(df_calc)
            
            ema5, ema20 = ind['ema_5'], ind['ema_20']
            ema60, ema120 = ind['ema_60'], ind['ema_120']
            val_psar = ind.get('psar')
            val_rsi = ind.get('rsi')
            val_adx = ind.get('adx')
            val_cci = ind.get('cci')
            val_macd = ind.get('macd')
            val_macd_sig = ind.get('macd_signal')
            high_52_daily = float(df_calc['close'].tail(250).max())

        # --- B. 가격 결정 로직 (Prioritize: fast_info > Intraday > Daily) ---
        current = 0.0
        prev = 0.0
        high_52 = high_52_daily
        
        is_crypto = name in ["비트코인", "이더리움"]
        
        # [추가] 국내 지수 KIS API 현재가 우선 적용
        use_kis_price = False
        if is_domestic_index:
            try:
                res = api.get_domestic_index_price(kis_code)
                if res.get('rt_cd') == '0':
                    out = res.get('output', {})
                    current = float(out.get('bstp_nmix_prpr', 0))
                    prev = float(out.get('bstp_nmix_prdy_clpr', 0)) # 전일 종가
                    use_kis_price = True
            except Exception: pass

        # 1. fast_info 시도 (가장 정확)
        use_fast_info = False
        if not use_kis_price: # KIS API 성공 시 건너뜀
            try:
                # [수정] 워커 내에서 Ticker 객체 생성
                ticker_obj = yf.Ticker(ticker)
                fi = ticker_obj.fast_info
                last_price = fi.last_price
                prev_close = fi.regular_market_previous_close # 공식 전일 종가
                
                # [추가] 암호화폐 전일 종가 보정 (UTC 00:00 기준)
                if is_crypto and not df_daily.empty and len(df_daily) >= 2:
                    try:
                        last_dt = df_daily.index[-1].date()
                        utc_today = datetime.utcnow().date()
                        target_idx = -2 if last_dt >= utc_today else -1
                        check_prev = float(df_daily['close'].iloc[target_idx])
                        if not math.isnan(check_prev):
                            prev_close = check_prev
                    except Exception: pass
                
                if (last_price is not None and prev_close is not None and 
                    not math.isnan(last_price) and not math.isnan(prev_close)):
                    
                    current = float(last_price)
                    prev = float(prev_close)
                    
                    if hasattr(fi, 'year_high') and fi.year_high is not None and not math.isnan(fi.year_high):
                        high_52 = max(high_52, float(fi.year_high))
                    else:
                        high_52 = max(high_52, current)
                    
                    use_fast_info = True
            except Exception: pass

        # 2. DataFrame 기반 Fallback
        patched_name = None
        missing_name = None
        
        if not use_fast_info and not use_kis_price:
            if df_daily.empty:
                return {'status': 'failed', 'name': name}
            
            daily_last_date = df_daily.index[-1].date()
            today = datetime.now().date()
            
            # (1) 기본값: 일봉 마지막 값
            current = float(df_daily['close'].iloc[-1])
            # 전일 종가 기본값: 데이터가 2개 이상이면 -2, 아니면 current
            if len(df_daily) >= 2:
                prev = float(df_daily['close'].iloc[-2])
                prev_date_src = df_daily.index[-2].date()
                if math.isnan(prev):
                    for i in range(3, min(10, len(df_daily) + 1)):
                        val = float(df_daily['close'].iloc[-i])
                        if not math.isnan(val):
                            prev = val
                            prev_date_src = df_daily.index[-i].date()
                            break
            else:
                prev = current
                prev_date_src = daily_last_date

            # (2) 현재가 보정: 분봉이 일봉보다 최신이면 교체
            intra_last_date = None
            if not df_intraday.empty:
                try:
                    valid_intra = df_intraday['close'].dropna()
                    if not valid_intra.empty:
                        intra_last_ts = valid_intra.index[-1]
                        intra_last_date = intra_last_ts.date()
                        if intra_last_date >= daily_last_date:
                            current = float(valid_intra.iloc[-1])
                except: pass

            # (3) Target Date 결정
            target_date = intra_last_date if (intra_last_date and intra_last_date >= daily_last_date) else daily_last_date

            # (4) 전일 종가 보정 및 Gap Check
            if daily_last_date < target_date:
                prev = float(df_daily['close'].iloc[-1])
                prev_date_src = daily_last_date
            elif daily_last_date == target_date:
                if len(df_daily) >= 2:
                    prev = float(df_daily['close'].iloc[-2])
                    prev_date_src = df_daily.index[-2].date()

            # Gap 감지
            gap_days = (target_date - prev_date_src).days
            weekday = target_date.weekday()
            is_gap = False
            
            if weekday == 0: # 월요일
                if gap_days > 3: is_gap = True
            elif weekday < 5: # 화~금
                if gap_days > 1: is_gap = True
            
            if target_date < today:
                is_gap = True

            # 분봉으로 전일 종가 찾기 시도
            patched = False
            if is_gap and not df_intraday.empty:
                try:
                    intra_dates = df_intraday.index.date
                    mask = intra_dates < target_date
                    past_intra = df_intraday.loc[mask]
                    
                    if not past_intra.empty:
                        last_past_date = past_intra.index[-1].date()
                        if last_past_date > prev_date_src:
                            for i in range(1, min(11, len(past_intra) + 1)):
                                val = float(past_intra.iloc[-i]['close'])
                                if not math.isnan(val):
                                    prev = val
                                    prev_date_src = past_intra.index[-i].date()
                                    patched = True
                                    is_gap = False
                                    break
                except: pass
            
            # 경고 메시지용
            if patched: patched_name = name
            elif is_gap: 
                if target_date < today: missing_name = f"{name}(Old:{target_date})"
                else: missing_name = f"{name}(Last:{prev_date_src})"

        # C. 결과 계산
        if math.isnan(current): current = 0.0
        if math.isnan(prev): prev = 0.0

        diff = current - prev
        rate = 0.0
        if prev != 0: rate = (diff / prev) * 100
        
        if math.isnan(diff): diff = 0.0
        if math.isnan(rate): rate = 0.0

        high_52_rate = 0.0
        if high_52 != 0: high_52_rate = ((current - high_52) / high_52) * 100
        if math.isnan(high_52_rate): high_52_rate = 0.0

        # --- 서식 ---
        diff_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
        change_str = f"{diff_color}{diff:+.2f} ({rate:+.2f}%)[/]"

        curr_price_color = "[white]"
        if ema5 and ema20 and ema60:
            if ema5 > ema20 and ema20 > ema60:
                if current > ema5: curr_price_color = "[red]"
                elif current < ema60: curr_price_color = "[blue]"
                else: curr_price_color = "[dim]"
            elif ema5 < ema20 and ema5 < ema60:
                if current < ema5: curr_price_color = "[blue]"
                elif current > ema20: curr_price_color = "[orange3]"
                else: curr_price_color = "[white]"
            else:
                if current < ema5: curr_price_color = "[blue]"
                elif current > ema20: curr_price_color = "[orange3]"
                else: curr_price_color = "[white]"
        
        curr_fmt = f"{current:,.2f}"
        if name == "달러환율": curr_fmt += "원"
        curr_str = f"{curr_price_color}{curr_fmt}[/]"

        h_color = "[white]"
        if high_52_rate > -3.0: h_color = "[red]"
        elif high_52_rate < -20.0: h_color = "[blue]"
        high_52_str = f"[dim]{high_52:,.2f}[/] ({h_color}{high_52_rate:.1f}%[/])"

        def fmt_val(val, color_tag):
            if val is None: return "-"
            s = f"{val:,.0f}" if val >= 1000 else f"{val:,.2f}"
            return f"{color_tag}{s}[/]" if color_tag else s

        # EMA Colors
        ema5_color = "[white]"
        if ema5 and ema20 and ema60 and ema120:
            if ema5 > ema20 and ema5 > ema60 and ema5 > ema120: ema5_color = "[red]"
            elif ema5 < ema20 and ema5 < ema60 and ema5 < ema120: ema5_color = "[blue]"
            elif (ema20 < ema5 < ema60) or (ema60 < ema5 < ema20): ema5_color = "[yellow]"
            elif (ema60 < ema5 < ema120) or (ema120 < ema5 < ema60): ema5_color = "[orange3]"
        
        ema20_color = "[white]"
        if ema20 and ema60 and ema120:
            if ema20 > ema60 and ema20 > ema120: ema20_color = "[red]"
            elif ema20 < ema60 and ema20 < ema120: ema20_color = "[blue]"
            elif (ema60 < ema20 < ema120) or (ema120 < ema20 < ema60): ema20_color = "[yellow]"

        ema60_color = "[yellow]"
        if ema60 and ema5 and ema20 and ema120:
            if ema120 > ema60 and ema60 > ema5 and ema60 > ema20: ema60_color = "[blue]"
            elif ema120 < ema60 and ema60 < ema5 and ema60 < ema20: ema60_color = "[red]"

        ema120_color = "[white]"
        if ema120 and ema60:
            if ema60 > ema120: ema120_color = "[red]" 
            elif ema60 < ema120: ema120_color = "[blue]"

        # 추세(S/M/O) 통합
        sar_icon = "-"
        if val_psar is not None:
            sar_icon = "[red]⬆[/]" if current > val_psar else "[blue]⬇[/]"
        
        macd_icon = "-"
        if val_macd is not None and val_macd_sig is not None:
            zero_sign = "+" if val_macd > 0 else "-"
            cross_char = "G" if val_macd > val_macd_sig else "D"
            m_color = "red" if val_macd > val_macd_sig else "blue"
            macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"
        
        obv_icon = "-" 
        trend_str = f"{sar_icon} {macd_icon} {obv_icon}"

        rsi_str = f"{val_rsi:.1f}" if val_rsi is not None else "-"
        if val_rsi is not None:
            if val_rsi >= 70: rsi_str = f"[magenta]{rsi_str}[/]"
            elif 55 <= val_rsi < 70: rsi_str = f"[red]{rsi_str}[/]"
            elif 45 <= val_rsi < 55: rsi_str = f"[orange3]{rsi_str}[/]"
            elif 30 < val_rsi < 45: rsi_str = f"[yellow]{rsi_str}[/]"
            else: rsi_str = f"[blue]{rsi_str}[/]"

        adx_str = f"{val_adx:.1f}" if val_adx is not None else "-"
        if val_adx is not None:
            if val_adx >= 40: adx_str = f"[magenta]{adx_str}[/]" 
            elif val_adx >= 30: adx_str = f"[red]{adx_str}[/]"     
            elif val_adx >= 20: adx_str = f"[orange3]{adx_str}[/]"
            elif val_adx >= 15: adx_str = f"[yellow]{adx_str}[/]"
            else: adx_str = f"[white]{adx_str}[/]"

        cci_str = f"{val_cci:.1f}" if val_cci is not None else "-"
        if val_cci is not None:
            if val_cci >= 100: cci_str = f"[red]{cci_str}[/]"
            elif 0 < val_cci < 100: cci_str = f"[orange3]{cci_str}[/]"
            elif -100 < val_cci <= 0: cci_str = f"[yellow]{cci_str}[/]"
            else: cci_str = f"[blue]{cci_str}[/]"

        display_name = name
        
        # 적응형 임계값 색상 적용 대상
        adaptive_targets = [
            "코스피", "코스닥", "코스피200", "코스닥150",
            "나스닥 선물", "나스닥", "S&P500", "다우존스", "러셀2000",
            "Japan - 닛케이", "Hong Kong - 항셍", "China - 상해종합", 
            "Taiwan - 대만가권", "Germany - 닥스40", "Europe - 스톡스50",
            "금", "은", "구리", "비트코인", "이더리움"
        ]

        if name in adaptive_targets:
            regime_override = None
            regime_key_map = {
                "코스피": "KOSPI", "코스닥": "KOSDAQ", "코스피200": "KOSPI200", "코스닥150": "KOSDAQ150"
            }
            if name in regime_key_map:
                regime_override = market_regime_cache.get(regime_key_map[name])
            
            if regime_override:
                suffix = "*" if is_kis_source else ""
                if regime_override == "Bull": display_name = f"[red]{name}{suffix}[/]"
                elif regime_override == "Bear": display_name = f"[blue]{name}{suffix}[/]"
                else: display_name = f"[white]{name}{suffix}[/]"
            else:
                # 기존 로직
                try:
                    ma_period = config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20)
                    adx_threshold = config.MARKET_REGIME_PARAMS.get("REGIME_ADX_THRESHOLD", 20)
                    
                    if not df_daily.empty and len(df_daily) >= ma_period:
                        ma_series = df_daily['close'].ewm(span=ma_period, adjust=False).mean()
                        ma_val = ma_series.iloc[-1]
                        slope = 0
                        if len(ma_series) >= 5:
                            slope = (ma_series.iloc[-1] - ma_series.iloc[-5]) / 5
                        adx_val = val_adx if val_adx is not None else 0
                        
                        if current > ma_val and slope > 0 and adx_val >= adx_threshold:
                            display_name = f"[red]{name}[/]"
                        elif current < ma_val:
                            display_name = f"[blue]{name}[/]"
                        else:
                            display_name = f"[white]{name}[/]"
                except: pass
        elif name == "SOX (반도체)":
            if high_52_rate > -5.0: display_name = f"[red]{name}[/]"
            elif -12.0 < high_52_rate <= -5.0: display_name = f"[orange3]{name}[/]"
            elif -20.0 < high_52_rate <= -12.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -25.0: display_name = f"[blue]{name}[/]"
        # ... (기타 색상 로직 생략, 기존 코드 참조)
        elif name == "VIX (변동성)":
            if current <= 20: display_name = f"[green]{name}[/]"
            elif 20 < current < 30: display_name = f"[cyan]{name}[/]"
            elif 30 <= current < 40: display_name = f"[yellow]{name}[/]"
            elif 40 <= current < 50: display_name = f"[orange3]{name}[/]"
            elif current >= 50: display_name = f"[red]{name}[/]"
        elif name == "달러인덱스":
            if current >= 120: display_name = f"[magenta]{name}[/]"
            elif 110 <= current < 120: display_name = f"[red]{name}[/]"
            elif 103 <= current < 110: display_name = f"[orange3]{name}[/]"
            elif 90 <= current < 103: display_name = f"[green]{name}[/]"
            elif 80 <= current < 90: display_name = f"[yellow]{name}[/]"
            elif current < 80: display_name = f"[blue]{name}[/]"
        elif name == "달러환율":
            if current >= 1600: display_name = f"[magenta]{name}[/]"
            elif 1500 <= current < 1600: display_name = f"[red]{name}[/]"
            elif 1400 <= current < 1500: display_name = f"[orange3]{name}[/]"
            elif 1300 <= current < 1400: display_name = f"[yellow]{name}[/]"
            elif 1200 <= current < 1300: display_name = f"[green]{name}[/]"
            elif 1100 <= current < 1200: display_name = f"[cyan]{name}[/]"
            elif current < 1100: display_name = f"[blue]{name}[/]"
        elif name == "WTI 원유":
            if current >= 120: display_name = f"[magenta]{name}[/]"
            elif 100 <= current < 120: display_name = f"[red]{name}[/]"
            elif 80 <= current < 100: display_name = f"[orange3]{name}[/]"
            elif 60 <= current < 80: display_name = f"[green]{name}[/]"
            elif 40 <= current < 60: display_name = f"[yellow]{name}[/]"
            elif current < 40: display_name = f"[blue]{name}[/]"
        elif name == "브랜트유":
            if current >= 125: display_name = f"[magenta]{name}[/]"
            elif 105 <= current < 125: display_name = f"[red]{name}[/]"
            elif 85 <= current < 105: display_name = f"[orange3]{name}[/]"
            elif 65 <= current < 85: display_name = f"[green]{name}[/]"
            elif 45 <= current < 65: display_name = f"[yellow]{name}[/]"
            elif current < 45: display_name = f"[blue]{name}[/]"
        elif name == "가솔린 RBOB":
            if current >= 4.00: display_name = f"[magenta]{name}[/]"
            elif 3.20 <= current < 4.00: display_name = f"[red]{name}[/]"
            elif 2.60 <= current < 3.20: display_name = f"[orange3]{name}[/]"
            elif 2.10 <= current < 2.60: display_name = f"[green]{name}[/]"
            elif 1.60 <= current < 2.10: display_name = f"[yellow]{name}[/]"
            elif current < 1.60: display_name = f"[blue]{name}[/]"
        elif name == "천연가스":
            if current >= 10: display_name = f"[magenta]{name}[/]"
            elif 6 <= current < 10: display_name = f"[red]{name}[/]"
            elif 4 <= current < 6: display_name = f"[orange3]{name}[/]"
            elif 2.5 <= current < 4: display_name = f"[green]{name}[/]"
            elif 1.5 <= current < 2.5: display_name = f"[yellow]{name}[/]"
            elif current < 1.5: display_name = f"[blue]{name}[/]"
        elif name == "밀":
            if current >= 900: display_name = f"[magenta]{name}[/]"
            elif 750 <= current < 900: display_name = f"[red]{name}[/]"
            elif 650 <= current < 750: display_name = f"[orange3]{name}[/]"
            elif 500 <= current < 650: display_name = f"[green]{name}[/]"
            elif 400 <= current < 500: display_name = f"[yellow]{name}[/]"
            elif current < 400: display_name = f"[blue]{name}[/]"

        return {
            'status': 'success',
            'row_data': [display_name, curr_str, change_str, high_52_str, fmt_val(ema5, ema5_color), fmt_val(ema20, ema20_color), fmt_val(ema60, ema60_color), fmt_val(ema120, ema120_color), trend_str, rsi_str, adx_str, cci_str],
            'patched_name': patched_name,
            'missing_name': missing_name,
            'mismatch_msg': mismatch_msg,
            'is_kis_source': is_kis_source
        }
    except Exception as e:
        return {'status': 'error', 'name': name, 'error': e}

def _show_market_indices_core(target_indices=None):
    # [변경] config.DEBUG_LEVEL 참조
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print("[dim][TRACE] show_market_indices() 호출[/dim]")

    # [추가] KOSPI/KOSDAQ 시장 국면 미리 조회 (도움말 화면과 데이터 동기화)
    # KIS API 데이터를 사용하는 analysis.get_market_regime 결과와 yfinance 데이터를 사용하는 현재 화면의 불일치 해소
    market_regime_cache = {}
    
    indices_map = INDICES_MAP.copy()
    if target_indices:
        indices_map = {k: v for k, v in indices_map.items() if k in target_indices}
        
    if not indices_map:
        return []

    # [수정] KIS API 국면 분석 대상 확대 (신규 지수 포함)
    regime_map = {
        "코스피": "KOSPI", "코스닥": "KOSDAQ", "코스피200": "KOSPI200", "코스닥150": "KOSDAQ150"
    }
    targets_regime = [m_type for k_name, m_type in regime_map.items() if k_name in indices_map]

    try:
        # [수정] console.status -> Progress (Bar 포함, Percentage 제외)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            if targets_regime:
                progress.add_task("[green]지수 데이터 수신 중(KIS API - 국내 지수)...[/green]", total=None)
                for m_type in targets_regime:
                    regime, _ = analysis.get_market_regime(m_type)
                    market_regime_cache[m_type] = regime
    except Exception:
        pass

    data_storage = {}
    yf_tickers = None
    any_kis_used = False
    
    # [Fix] 예외 발생 시 참조 오류(UnboundLocalError) 방지를 위해 변수 초기화 상단 이동
    patched_tickers = []
    missing_tickers = []
    mismatch_tickers = []
    failed_tickers = []

    try:
        # [변경] 1. 히스토리 데이터 다운로드 (그룹별 순차 요청 - Progress Bar 복원)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=config.console,
            transient=True
        ) as progress:
            
            groups_to_fetch = []
            remaining_indices = set(indices_map.keys())
            
            # config.INDICES_GROUPS 순서대로 그룹핑
            if hasattr(config, 'INDICES_GROUPS'):
                for key, info in config.INDICES_GROUPS.items():
                    group_name = info['name']
                    # indices_map에 포함된 지수만 필터링
                    group_targets = [idx for idx in info['indices'] if idx in remaining_indices]
                    
                    if group_targets:
                        tickers = [indices_map[idx] for idx in group_targets]
                        groups_to_fetch.append((group_name, tickers))
                        
                        for idx in group_targets:
                            remaining_indices.remove(idx)
            
            # 미분류 지수 처리
            if remaining_indices:
                other_tickers = [indices_map[idx] for idx in remaining_indices]
                if other_tickers:
                    groups_to_fetch.append(("기타", other_tickers))

            task_dl = progress.add_task("[green]지수 데이터 수신 준비 중...[/green]", total=None)

            # 그룹별 순차 요청
            for group_name, t_list in groups_to_fetch:
                if not t_list: continue
                
                progress.update(task_dl, description=f"[green]지수 데이터 수신 중 (yfinance - {group_name})...[/green]")
                tickers_str = " ".join(t_list)

                for attempt in range(2):
                    try:
                        if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                            config.console.print(f"[dim cyan][TRACE] REQ ({group_name}) | Attempt: {attempt+1} | Tickers: {tickers_str}[/dim cyan]")

                        # [수정] 스레드를 이용한 데이터 수신 (Ctrl+C 즉시 반응 지원)
                        result_container = {}
                        
                        def _fetch_worker():
                            try:
                                d = api.fetch_yfinance_data(tickers_str, period="1y", interval="1d", group_by='ticker')
                                i = api.fetch_yfinance_data(tickers_str, period="5d", interval="5m", group_by='ticker')
                                result_container['daily'] = d
                                result_container['intra'] = i
                            except Exception as e:
                                result_container['error'] = e

                        t = threading.Thread(target=_fetch_worker, daemon=True)
                        t.start()
                        
                        while t.is_alive():
                            try:
                                t.join(0.1)
                            except KeyboardInterrupt:
                                raise KeyboardInterrupt

                        if 'error' in result_container:
                            raise result_container['error']

                        d_data = result_container.get('daily', pd.DataFrame())
                        i_data = result_container.get('intra', pd.DataFrame())

                        if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                            d_shape = d_data.shape if not d_data.empty else "Empty"
                            i_shape = i_data.shape if not i_data.empty else "Empty"
                            config.console.print(f"[dim magenta][TRACE] RES ({group_name}) | Daily: {d_shape}, Intra: {i_shape}[/dim magenta]")

                        for t in t_list:
                            d_df = pd.DataFrame()
                            i_df = pd.DataFrame()
                            
                            try:
                                if not d_data.empty:
                                    if isinstance(d_data.columns, pd.MultiIndex):
                                        if t in d_data.columns.levels[0]: d_df = d_data[t].copy()
                                    elif 'Close' in d_data.columns: d_df = d_data.copy()
                            except: pass

                            try:
                                if not i_data.empty:
                                    if isinstance(i_data.columns, pd.MultiIndex):
                                        if t in i_data.columns.levels[0]: i_df = i_data[t].copy()
                                    elif 'Close' in i_data.columns: i_df = i_data.copy()
                            except: pass
                            
                            data_storage[t] = {'daily': d_df, 'intra': i_df}
                        break
                    except KeyboardInterrupt:
                        raise # 상위 핸들러로 전파
                    except Exception as e:
                        if "database" in str(e).lower(): api.clear_yfinance_cache()
                        else: break
                
                # 그룹 처리 사이 인터럽트 감지 기회 제공
                time.sleep(0.1)

        # 2. Tickers 객체 생성 (fast_info 접근용)
        all_tickers_list = list(indices_map.values())
        yf_tickers = yf.Tickers(" ".join(all_tickers_list))

        # 테이블 생성
        table = Table(title="\n지수 기술적 분석", box=box.HORIZONTALS, show_header=True, header_style="dim", border_style="dim")
        table.add_column("지수명", justify="left", style="white")
        table.add_column("지수", justify="right")
        table.add_column("등락폭 (등락률)", justify="right")
        table.add_column("52주 고점", justify="right")
        table.add_column("EMA(5)", justify="right")
        table.add_column("EMA(20)", justify="right")
        table.add_column("EMA(60)", justify="right")
        table.add_column("EMA(120)", justify="right")
        table.add_column("추세SMO", justify="center")
        table.add_column("RSI", justify="right")
        table.add_column("ADX", justify="right")
        table.add_column("CCI", justify="right")

        # [변경] 3. 지표 분석 및 테이블 구성 (Progress 분리: Percentage 포함)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=config.console,
            transient=True
        ) as progress:
            task = progress.add_task("[cyan]지수 지표 분석 중...[/cyan]", total=len(indices_map))

            for name, ticker in indices_map.items():
                is_kis_source = False # [추가] KIS API 사용 여부 초기화
                if name in ["나스닥 선물", "금", "달러인덱스", "VIX (변동성)", "비트코인", "Japan - 닛케이"]: 
                    table.add_section()

                try:
                    # --- A. DataFrame 준비 및 지표 계산 ---
                    stored = data_storage.get(ticker, {'daily': pd.DataFrame(), 'intra': pd.DataFrame()})
                    df_daily = stored['daily']
                    df_intraday = stored['intra']
                    
                    if not df_daily.empty:
                        df_daily.columns = [c.lower() for c in df_daily.columns]
                        # [이동] 데이터 정제: Close가 NaN인 행 제거 (비트코인 등 데이터 공백 방지)
                        if 'close' in df_daily.columns:
                            df_daily = df_daily.dropna(subset=['close'])

                    if not df_intraday.empty:
                        df_intraday.columns = [c.lower() for c in df_intraday.columns]

                    # [수정] 국내 지수의 경우 analysis 모듈의 공통 함수를 사용하여 데이터 조회 (Fallback 포함)
                    is_domestic_index = name in ["코스피", "코스닥", "코스피200", "코스닥150"]
                    kis_code = ""
                    
                    if is_domestic_index:
                        logger.debug(f"[MARKET_INDEX_DEBUG] Processing {name}...")
                        if name == "코스피": kis_code = "0001"; m_type = "KOSPI"
                        elif name == "코스닥": kis_code = "1001"; m_type = "KOSDAQ"
                        elif name == "코스피200": kis_code = "2001"; m_type = "KOSPI200"
                        elif name == "코스닥150": kis_code = "2203"; m_type = "KOSDAQ150"
                        
                        df_fallback = analysis.get_domestic_index_data(m_type)
                        if df_fallback is not None and not df_fallback.empty:
                            logger.debug(f"[MARKET_INDEX_DEBUG] {name} - Data Fetched. Shape: {df_fallback.shape}")
                            logger.debug(f"[MARKET_INDEX_DEBUG] {name} - Columns: {list(df_fallback.columns)}")
                            df_daily = df_fallback
                            df_daily.columns = [c.lower() for c in df_daily.columns]
                            
                            # [Fix] KIS API 데이터 사용 시 Index가 DatetimeIndex가 아닌 경우 변환 (int has no attribute date 에러 방지)
                            if not isinstance(df_daily.index, pd.DatetimeIndex):
                                target_col = None
                                if 'date' in df_daily.columns: target_col = 'date'
                                elif 'stck_bsop_date' in df_daily.columns: target_col = 'stck_bsop_date'
                                if target_col:
                                    df_daily[target_col] = pd.to_datetime(df_daily[target_col])
                                    df_daily.set_index(target_col, inplace=True)
                            
                            # [추가] KIS API 데이터와 yfinance 데이터 간 날짜 차이 검증
                            if not df_intraday.empty:
                                try:
                                    kis_last_dt = df_daily.index[-1].date()
                                    yf_last_dt = df_intraday.index[-1].date()
                                    
                                    if yf_last_dt > kis_last_dt:
                                        mismatch_tickers.append(f"{name}(KIS:{kis_last_dt} vs YF:{yf_last_dt})")
                                except Exception: pass
                            
                            # [추가] 데이터 소스 확인 (KIS API 여부)
                            if df_daily.attrs.get('source') == 'KIS':
                                is_kis_source = True
                        else:
                            logger.debug(f"[MARKET_INDEX_DEBUG] {name} - Data Fetch Failed or Empty.")

                    if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                        config.console.print(f"[dim cyan][DEBUG] >> Data Check: {name} ({ticker})[/dim cyan]")
                        
                        if not df_daily.empty:
                            cols_to_show = [c for c in ['open', 'close'] if c in df_daily.columns]
                            if cols_to_show:
                                tail_d = df_daily[cols_to_show].tail(2)
                                tail_str = tail_d.to_string(header=True, index=True).replace('\n', '\n[DEBUG]      ')
                                config.console.print(f"[dim][DEBUG]    [Daily Tail]\n[DEBUG]      {tail_str}[/dim]")
                        else:
                            config.console.print(f"[dim red][DEBUG]    [Daily] Empty[/dim red]")
                        
                        if not df_intraday.empty:
                            cols_to_show = [c for c in ['open', 'close'] if c in df_intraday.columns]
                            if cols_to_show:
                                tail_i = df_intraday[cols_to_show].tail(2)
                                tail_str = tail_i.to_string(header=True, index=True).replace('\n', '\n[DEBUG]      ')
                                config.console.print(f"[dim][DEBUG]    [Intra Tail]\n[DEBUG]      {tail_str}[/dim]")
                        else:
                            config.console.print(f"[dim red][DEBUG]    [Intra] Empty[/dim red]")

                    # 지표 계산
                    ema5, ema20, ema60, ema120 = None, None, None, None
                    val_psar, val_rsi, val_adx, val_cci, val_macd, val_macd_sig = None, None, None, None, None, None
                    high_52_daily = 0.0

                    if not df_daily.empty and 'close' in df_daily.columns and len(df_daily) > 10:
                        df_calc = df_daily[['open', 'high', 'low', 'close', 'volume']].copy()
                        df_calc.dropna(subset=['close'], inplace=True)
                        ind = indicators.calculate_indicators(df_calc)
                        
                        ema5, ema20 = ind['ema_5'], ind['ema_20']
                        ema60, ema120 = ind['ema_60'], ind['ema_120']
                        val_psar = ind.get('psar')
                        val_rsi = ind.get('rsi')
                        val_adx = ind.get('adx')
                        val_cci = ind.get('cci')
                        val_macd = ind.get('macd')
                        val_macd_sig = ind.get('macd_signal')
                        high_52_daily = float(df_calc['close'].tail(250).max())

                    # --- B. 가격 결정 로직 (Prioritize: fast_info > Intraday > Daily) ---
                    current = 0.0
                    prev = 0.0
                    high_52 = high_52_daily
                    
                    # [추가] 디버그 대상 확인 (비트코인, 이더리움)
                    is_target_debug = name in ["비트코인", "이더리움"]
                    is_crypto = name in ["비트코인", "이더리움"]
                    debug_tag = "[MARKET_INDEX_DEBUG]"
                    
                    # [추가] 국내 지수 KIS API 현재가 우선 적용
                    use_kis_price = False
                    if is_domestic_index:
                        try:
                            res = api.get_domestic_index_price(kis_code)
                            if res.get('rt_cd') == '0':
                                out = res.get('output', {})
                                current = float(out.get('bstp_nmix_prpr', 0))
                                prev = float(out.get('bstp_nmix_prdy_clpr', 0)) # 전일 종가
                                
                                # 52주 고가 갱신 (일봉 데이터 기준과 비교)
                                # API 출력에 52주 고가가 없으므로 일봉 데이터의 max값 사용 유지
                                
                                use_kis_price = True
                                if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                                    config.console.print(f"[dim green][DEBUG]    -> Result: Cur={current:,.2f} Prev={prev:,.2f} (Source: KIS API)[/dim green]")
                        except Exception as e:
                            if config.SCREEN_DEBUG_LEVEL == "DEBUG": config.console.print(f"[dim red]KIS Price Error: {e}[/dim red]")

                    # 1. fast_info 시도 (가장 정확)
                    use_fast_info = False
                    if not use_kis_price: # KIS API 성공 시 건너뜀
                        try:
                            ticker_obj = yf_tickers.tickers[ticker]
                            fi = ticker_obj.fast_info
                            last_price = fi.last_price
                            prev_close = fi.regular_market_previous_close # 공식 전일 종가
                            
                            # [추가] fast_info 값 로깅
                            if is_target_debug:
                                logger.debug(f"{debug_tag} {name} fast_info: last={last_price}, prev={prev_close}")
                            
                            # [추가] 암호화폐 전일 종가 보정 (UTC 00:00 기준)
                            # fast_info의 prev_close가 NaN이거나 불명확할 수 있으므로 일봉 데이터(UTC기준)로 강제 고정
                            if is_crypto and not df_daily.empty and len(df_daily) >= 2:
                                try:
                                    last_dt = df_daily.index[-1].date()
                                    utc_today = datetime.utcnow().date()
                                    
                                    # 일봉 마지막이 오늘(UTC)이면 -2번째가 전일 종가, 아니면 -1번째
                                    target_idx = -2 if last_dt >= utc_today else -1
                                    check_prev = float(df_daily['close'].iloc[target_idx])
                                    
                                    if not math.isnan(check_prev):
                                        prev_close = check_prev
                                        if is_target_debug: logger.debug(f"{debug_tag} {name} prev_close fixed to UTC 00:00: {prev_close}")
                                except Exception: pass
                            
                            if (last_price is not None and prev_close is not None and 
                                not math.isnan(last_price) and not math.isnan(prev_close)):
                                
                                current = float(last_price)
                                prev = float(prev_close)
                                
                                if hasattr(fi, 'year_high') and fi.year_high is not None and not math.isnan(fi.year_high):
                                    high_52 = max(high_52, float(fi.year_high))
                                else:
                                    high_52 = max(high_52, current)
                                
                                use_fast_info = True
                                if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                                    config.console.print(f"[dim green][DEBUG]    -> Result: Cur={current:,.2f} Prev={prev:,.2f} (Source: fast_info)[/dim green]")
                            else:
                                if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                                    config.console.print(f"[dim red][DEBUG]    fast_info rejected: nan values detected (Cur={last_price}, Prev={prev_close})[/dim red]")
                                if is_target_debug:
                                    logger.debug(f"{debug_tag} {name} fast_info rejected (NaN/None)")

                        except Exception as e:
                            if is_target_debug:
                                logger.debug(f"{debug_tag} {name} fast_info error: {e}")
                            if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                                config.console.print(f"[dim red][DEBUG]    fast_info error: {e}[/dim red]")

                    # 2. DataFrame 기반 Fallback (fast_info 실패 또는 NaN 시)
                    if not use_fast_info and not use_kis_price:
                        if is_target_debug:
                            logger.debug(f"{debug_tag} {name} entering fallback. Daily len: {len(df_daily)}")
                        
                        if df_daily.empty:
                            table.add_row(name, "[red]수신 실패[/]", "[dim]yfinance 응답 없음[/]", "-", "-", "-", "-", "-", "-", "-", "-", "-")
                            failed_tickers.append(name)
                            progress.advance(task)
                            continue
                        
                        daily_last_date = df_daily.index[-1].date()
                        today = datetime.now().date()
                        
                        # (1) 기본값: 일봉 마지막 값
                        current = float(df_daily['close'].iloc[-1])
                        # 전일 종가 기본값: 데이터가 2개 이상이면 -2, 아니면 current
                        if len(df_daily) >= 2:
                            prev = float(df_daily['close'].iloc[-2])
                            prev_date_src = df_daily.index[-2].date()
                            
                            # [추가] prev가 NaN인 경우 과거 데이터 탐색 (안전장치)
                            if math.isnan(prev):
                                for i in range(3, min(10, len(df_daily) + 1)):
                                    val = float(df_daily['close'].iloc[-i])
                                    if not math.isnan(val):
                                        prev = val
                                        prev_date_src = df_daily.index[-i].date()
                                        break
                        else:
                            prev = current
                            prev_date_src = daily_last_date

                        if is_target_debug:
                            logger.debug(f"{debug_tag} {name} fallback daily init: current={current}, prev={prev}")

                        # (2) 현재가 보정: 분봉이 일봉보다 최신이면 교체
                        intra_last_date = None
                        if not df_intraday.empty:
                            try:
                                valid_intra = df_intraday['close'].dropna()
                                if not valid_intra.empty:
                                    intra_last_ts = valid_intra.index[-1]
                                    intra_last_date = intra_last_ts.date()
                                    if intra_last_date >= daily_last_date:
                                        current = float(valid_intra.iloc[-1])
                            except: pass

                        # (3) Target Date 결정 (현재가가 기준이 되는 날짜)
                        target_date = intra_last_date if (intra_last_date and intra_last_date >= daily_last_date) else daily_last_date

                        # (4) 전일 종가 보정 및 Gap Check
                        # 일봉 마지막 날짜가 타겟 날짜보다 이전인 경우 (오늘 데이터 없음)
                        if daily_last_date < target_date:
                            # 현재가가 분봉에서 왔다면(오늘자), daily_last_date(어제)가 prev가 됨
                            prev = float(df_daily['close'].iloc[-1])
                            prev_date_src = daily_last_date
                        elif daily_last_date == target_date:
                            # 일봉이 오늘자까지 갱신됨
                            if len(df_daily) >= 2:
                                prev = float(df_daily['close'].iloc[-2])
                                prev_date_src = df_daily.index[-2].date()

                        # Gap 감지 (평일 기준 2일 이상 차이 시 누락 의심)
                        # target_date(오늘) - prev_date_src(전일종가일)
                        gap_days = (target_date - prev_date_src).days
                        weekday = target_date.weekday()
                        is_gap = False
                        
                        if weekday == 0: # 월요일이면 3일(금) 차이는 정상, 4일 이상이면 Gap
                            if gap_days > 3: is_gap = True
                        elif weekday < 5: # 화~금이면 1일 차이는 정상, 2일 이상이면 Gap
                            if gap_days > 1: is_gap = True
                        
                        # [강화] 오늘 날짜가 아닌 경우 강제로 Gap/Stale로 처리 (사용자 요청 반영)
                        if target_date < today:
                            is_gap = True

                        # 분봉으로 전일 종가 찾기 시도
                        patched = False
                        if is_gap and not df_intraday.empty:
                            try:
                                # 타겟 날짜보다 이전인 데이터 검색
                                intra_dates = df_intraday.index.date
                                mask = intra_dates < target_date
                                past_intra = df_intraday.loc[mask]
                                
                                if not past_intra.empty:
                                    last_past = past_intra.iloc[-1]
                                    last_past_date = past_intra.index[-1].date()
                                    
                                    # 일봉 전일보다 분봉 과거가 더 최신이면 교체
                                    if last_past_date > prev_date_src:
                                        # [수정] NaN이 아닌 유효한 값을 찾을 때까지 역순 탐색 (최대 10개 봉)
                                        for i in range(1, min(11, len(past_intra) + 1)):
                                            val = float(past_intra.iloc[-i]['close'])
                                            if not math.isnan(val):
                                                prev = val
                                                prev_date_src = past_intra.index[-i].date()
                                                patched = True
                                                is_gap = False # 보정 성공
                                                break
                            except: pass
                        
                        # 경고 리스트 추가
                        if patched: 
                            patched_tickers.append(name)
                        elif is_gap: 
                            if target_date < today:
                                missing_tickers.append(f"{name}(Old:{target_date})")
                            else:
                                missing_tickers.append(f"{name}(Last:{prev_date_src})")

                        if config.SCREEN_DEBUG_LEVEL == "DEBUG":
                            config.console.print(f"[dim magenta][DEBUG]    -> Result: Cur={current:,.2f} Prev={prev:,.2f} (Source: Fallback DF, Date: {target_date} vs {prev_date_src})[/dim magenta]")

                        if is_target_debug:
                            logger.debug(f"{debug_tag} {name} fallback final: current={current}, prev={prev}")

                    # C. 결과 계산
                    if math.isnan(current): current = 0.0
                    if math.isnan(prev): prev = 0.0

                    diff = current - prev
                    rate = 0.0
                    if prev != 0: rate = (diff / prev) * 100
                    
                    if math.isnan(diff): diff = 0.0
                    if math.isnan(rate): rate = 0.0

                    high_52_rate = 0.0
                    if high_52 != 0: high_52_rate = ((current - high_52) / high_52) * 100
                    if math.isnan(high_52_rate): high_52_rate = 0.0

                    # --- 서식 ---
                    diff_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
                    change_str = f"{diff_color}{diff:+.2f} ({rate:+.2f}%)[/]"

                    curr_price_color = "[white]"
                    if ema5 and ema20 and ema60:
                        if ema5 > ema20 and ema20 > ema60:
                            if current > ema5: curr_price_color = "[red]"
                            elif current < ema60: curr_price_color = "[blue]"
                            else: curr_price_color = "[dim]"
                        elif ema5 < ema20 and ema5 < ema60:
                            if current < ema5: curr_price_color = "[blue]"
                            elif current > ema20: curr_price_color = "[orange3]"
                            else: curr_price_color = "[white]"
                        else:
                            if current < ema5: curr_price_color = "[blue]"
                            elif current > ema20: curr_price_color = "[orange3]"
                            else: curr_price_color = "[white]"
                    
                    curr_fmt = f"{current:,.2f}"
                    if name == "달러환율": curr_fmt += "원"
                    curr_str = f"{curr_price_color}{curr_fmt}[/]"

                    h_color = "[white]"
                    if high_52_rate > -3.0: h_color = "[red]"
                    elif high_52_rate < -20.0: h_color = "[blue]"
                    high_52_str = f"[dim]{high_52:,.2f}[/] ({h_color}{high_52_rate:.1f}%[/])"

                    def fmt_val(val, color_tag):
                        if val is None: return "-"
                        s = f"{val:,.0f}" if val >= 1000 else f"{val:,.2f}"
                        return f"{color_tag}{s}[/]" if color_tag else s

                    # EMA Colors
                    ema5_color = "[white]"
                    if ema5 and ema20 and ema60 and ema120:
                        if ema5 > ema20 and ema5 > ema60 and ema5 > ema120: ema5_color = "[red]"
                        elif ema5 < ema20 and ema5 < ema60 and ema5 < ema120: ema5_color = "[blue]"
                        elif (ema20 < ema5 < ema60) or (ema60 < ema5 < ema20): ema5_color = "[yellow]"
                        elif (ema60 < ema5 < ema120) or (ema120 < ema5 < ema60): ema5_color = "[orange3]"
                    
                    ema20_color = "[white]"
                    if ema20 and ema60 and ema120:
                        if ema20 > ema60 and ema20 > ema120: ema20_color = "[red]"
                        elif ema20 < ema60 and ema20 < ema120: ema20_color = "[blue]"
                        elif (ema60 < ema20 < ema120) or (ema120 < ema20 < ema60): ema20_color = "[yellow]"

                    ema60_color = "[yellow]"
                    if ema60 and ema5 and ema20 and ema120:
                        if ema120 > ema60 and ema60 > ema5 and ema60 > ema20: ema60_color = "[blue]"
                        elif ema120 < ema60 and ema60 < ema5 and ema60 < ema20: ema60_color = "[red]"

                    ema120_color = "[white]"
                    if ema120 and ema60:
                        if ema60 > ema120: ema120_color = "[red]" 
                        elif ema60 < ema120: ema120_color = "[blue]"

                    # 추세(S/M/O) 통합
                    # S (SAR)
                    sar_icon = "-"
                    if val_psar is not None:
                        sar_icon = "[red]⬆[/]" if current > val_psar else "[blue]⬇[/]"
                    
                    # M (MACD)
                    macd_icon = "-"
                    if val_macd is not None and val_macd_sig is not None:
                        zero_sign = "+" if val_macd > 0 else "-"
                        cross_char = "G" if val_macd > val_macd_sig else "D"
                        m_color = "red" if val_macd > val_macd_sig else "blue"
                        macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"
                    
                    # O (OBV) - 지수는 거래량 데이터가 부정확할 수 있어 생략하거나 '-' 처리
                    obv_icon = "-" 
                    
                    trend_str = f"{sar_icon} {macd_icon} {obv_icon}"

                    rsi_str = f"{val_rsi:.1f}" if val_rsi is not None else "-"
                    if val_rsi is not None:
                        if val_rsi >= 70: rsi_str = f"[magenta]{rsi_str}[/]"
                        elif 55 <= val_rsi < 70: rsi_str = f"[red]{rsi_str}[/]"
                        elif 45 <= val_rsi < 55: rsi_str = f"[orange3]{rsi_str}[/]"
                        elif 30 < val_rsi < 45: rsi_str = f"[yellow]{rsi_str}[/]"
                        else: rsi_str = f"[blue]{rsi_str}[/]"

                    adx_str = f"{val_adx:.1f}" if val_adx is not None else "-"
                    if val_adx is not None:
                        if val_adx >= 40: adx_str = f"[magenta]{adx_str}[/]" 
                        elif val_adx >= 30: adx_str = f"[red]{adx_str}[/]"     
                        elif val_adx >= 20: adx_str = f"[orange3]{adx_str}[/]"
                        elif val_adx >= 15: adx_str = f"[yellow]{adx_str}[/]"
                        else: adx_str = f"[white]{adx_str}[/]"

                    cci_str = f"{val_cci:.1f}" if val_cci is not None else "-"
                    if val_cci is not None:
                        if val_cci >= 100: cci_str = f"[red]{cci_str}[/]"
                        elif 0 < val_cci < 100: cci_str = f"[orange3]{cci_str}[/]"
                        elif -100 < val_cci <= 0: cci_str = f"[yellow]{cci_str}[/]"
                        else: cci_str = f"[blue]{cci_str}[/]"

                    display_name = name
                    
                    # [수정] 적응형 임계값 색상 적용 대상 확대
                    adaptive_targets = [
                        "코스피", "코스닥", "코스피200", "코스닥150",
                        "나스닥 선물", "나스닥", "S&P500", "다우존스", "러셀2000",
                        "Japan - 닛케이", "Hong Kong - 항셍", "China - 상해종합", 
                        "Taiwan - 대만가권", "Germany - 닥스40", "Europe - 스톡스50",
                        "금", "은", "구리", "비트코인", "이더리움"
                    ]

                    used_kis_regime = False
                    if name in adaptive_targets:
                        # [추가] KOSPI/KOSDAQ은 캐시된 국면 정보 우선 사용 (데이터 정합성 보장)
                        regime_override = None
                        
                        # [수정] 신규 지수 매핑 추가
                        regime_key_map = {
                            "코스피": "KOSPI", "코스닥": "KOSDAQ", "코스피200": "KOSPI200", "코스닥150": "KOSDAQ150"
                        }
                        if name in regime_key_map:
                            regime_override = market_regime_cache.get(regime_key_map[name])
                        
                        if regime_override:
                            used_kis_regime = True
                            
                            # [수정] KIS API 데이터 소스일 때만 * 표시 및 하단 안내 활성화
                            suffix = "*" if is_kis_source else ""
                            if is_kis_source: any_kis_used = True

                            if regime_override == "Bull": display_name = f"[red]{name}{suffix}[/]"
                            elif regime_override == "Bear": display_name = f"[blue]{name}{suffix}[/]"
                            else: display_name = f"[white]{name}{suffix}[/]"
                        else:
                            # 기존 로직 (yfinance 데이터 기반 계산)
                            try:
                                ma_period = config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20)
                                adx_threshold = config.MARKET_REGIME_PARAMS.get("REGIME_ADX_THRESHOLD", 20)
                                
                                if not df_daily.empty and len(df_daily) >= ma_period:
                                    ma_series = df_daily['close'].ewm(span=ma_period, adjust=False).mean()
                                    ma_val = ma_series.iloc[-1]
                                    
                                    slope = 0
                                    if len(ma_series) >= 5:
                                        slope = (ma_series.iloc[-1] - ma_series.iloc[-5]) / 5
                                    
                                    adx_val = val_adx if val_adx is not None else 0
                                    
                                    if current > ma_val and slope > 0 and adx_val >= adx_threshold:
                                        display_name = f"[red]{name}[/]"
                                    elif current < ma_val:
                                        display_name = f"[blue]{name}[/]"
                                    else:
                                        display_name = f"[white]{name}[/]"
                            except: pass
                    elif name == "SOX (반도체)":
                        if high_52_rate > -5.0: display_name = f"[red]{name}[/]"
                        elif -12.0 < high_52_rate <= -5.0: display_name = f"[orange3]{name}[/]"
                        elif -20.0 < high_52_rate <= -12.0: display_name = f"[yellow]{name}[/]"
                        elif high_52_rate < -25.0: display_name = f"[blue]{name}[/]"
                    elif name == "VIX (변동성)":
                        if current <= 20: display_name = f"[green]{name}[/]"
                        elif 20 < current < 30: display_name = f"[cyan]{name}[/]"
                        elif 30 <= current < 40: display_name = f"[yellow]{name}[/]"
                        elif 40 <= current < 50: display_name = f"[orange3]{name}[/]"
                        elif current >= 50: display_name = f"[red]{name}[/]"
                    elif name == "달러인덱스":
                        if current >= 120: display_name = f"[magenta]{name}[/]"
                        elif 110 <= current < 120: display_name = f"[red]{name}[/]"
                        elif 103 <= current < 110: display_name = f"[orange3]{name}[/]"
                        elif 90 <= current < 103: display_name = f"[green]{name}[/]"
                        elif 80 <= current < 90: display_name = f"[yellow]{name}[/]"
                        elif current < 80: display_name = f"[blue]{name}[/]"
                    elif name == "달러환율":
                        if current >= 1600: display_name = f"[magenta]{name}[/]"
                        elif 1500 <= current < 1600: display_name = f"[red]{name}[/]"
                        elif 1400 <= current < 1500: display_name = f"[orange3]{name}[/]"
                        elif 1300 <= current < 1400: display_name = f"[yellow]{name}[/]"
                        elif 1200 <= current < 1300: display_name = f"[green]{name}[/]"
                        elif 1100 <= current < 1200: display_name = f"[cyan]{name}[/]"
                        elif current < 1100: display_name = f"[blue]{name}[/]"
                    elif name == "WTI 원유":
                        if current >= 120: display_name = f"[magenta]{name}[/]"
                        elif 100 <= current < 120: display_name = f"[red]{name}[/]"
                        elif 80 <= current < 100: display_name = f"[orange3]{name}[/]"
                        elif 60 <= current < 80: display_name = f"[green]{name}[/]"
                        elif 40 <= current < 60: display_name = f"[yellow]{name}[/]"
                        elif current < 40: display_name = f"[blue]{name}[/]"
                    elif name == "브랜트유":
                        if current >= 125: display_name = f"[magenta]{name}[/]"
                        elif 105 <= current < 125: display_name = f"[red]{name}[/]"
                        elif 85 <= current < 105: display_name = f"[orange3]{name}[/]"
                        elif 65 <= current < 85: display_name = f"[green]{name}[/]"
                        elif 45 <= current < 65: display_name = f"[yellow]{name}[/]"
                        elif current < 45: display_name = f"[blue]{name}[/]"
                    elif name == "가솔린 RBOB":
                        if current >= 4.00: display_name = f"[magenta]{name}[/]"
                        elif 3.20 <= current < 4.00: display_name = f"[red]{name}[/]"
                        elif 2.60 <= current < 3.20: display_name = f"[orange3]{name}[/]"
                        elif 2.10 <= current < 2.60: display_name = f"[green]{name}[/]"
                        elif 1.60 <= current < 2.10: display_name = f"[yellow]{name}[/]"
                        elif current < 1.60: display_name = f"[blue]{name}[/]"
                    elif name == "천연가스":
                        if current >= 10: display_name = f"[magenta]{name}[/]"
                        elif 6 <= current < 10: display_name = f"[red]{name}[/]"
                        elif 4 <= current < 6: display_name = f"[orange3]{name}[/]"
                        elif 2.5 <= current < 4: display_name = f"[green]{name}[/]"
                        elif 1.5 <= current < 2.5: display_name = f"[yellow]{name}[/]"
                        elif current < 1.5: display_name = f"[blue]{name}[/]"
                    elif name == "밀":
                        if current >= 900: display_name = f"[magenta]{name}[/]"
                        elif 750 <= current < 900: display_name = f"[red]{name}[/]"
                        elif 650 <= current < 750: display_name = f"[orange3]{name}[/]"
                        elif 500 <= current < 650: display_name = f"[green]{name}[/]"
                        elif 400 <= current < 500: display_name = f"[yellow]{name}[/]"
                        elif current < 400: display_name = f"[blue]{name}[/]"

                    table.add_row(display_name, curr_str, change_str, high_52_str, fmt_val(ema5, ema5_color), fmt_val(ema20, ema20_color), fmt_val(ema60, ema60_color), fmt_val(ema120, ema120_color), trend_str, rsi_str, adx_str, cci_str)
                    progress.advance(task)

                except Exception as e:
                    if name in ["코스피", "코스닥", "코스피200", "코스닥150"]:
                        logger.error(f"[MARKET_INDEX_DEBUG] Error processing {name}: {e}", exc_info=True)
                    if config.SCREEN_DEBUG_LEVEL in ["DEBUG", "TRACE"]:
                        config.console.print(f"[bold red][DEBUG] 에러 발생({name}): {e}[/bold red]")
                    table.add_row(name, "Error", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-")
                    progress.advance(task)
        
        # 테이블 출력 (Progress Context 밖에서 실행)
        try:
            config.console.print(table, crop=False)
            sys.stdout.flush()

            if any_kis_used:
                config.console.print("[dim] (*) KIS API 를 사용한 데이터가 적용된 결과입니다.[/dim]")
        except Exception as e:
            logger.error(f"테이블 출력 중 오류(tmux 리사이즈 등): {e}")
            config.console.print(f"[red]테이블 출력 실패: {e}[/red]")

    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error(f"지수 분석 중 치명적 오류: {e}")
        config.console.print(f"\n[bold red]지수 분석 중 오류 발생: {e}[/bold red]")
    
    # [하단 경고 출력]
    if patched_tickers:
        targets = ", ".join(patched_tickers)
        config.console.print(f"[dim][yellow] 알림: {targets} 등 일부 지수의 전일 일봉 데이터가 지연되어 분봉 데이터를 기준으로 등락폭을 계산했습니다.[/yellow][/dim]")
    
    if missing_tickers:
        targets = ", ".join(missing_tickers)
        config.console.print(f"[dim][yellow] 주의: 일부 지수[{targets}]의 전일 데이터가 누락되어(보정 실패) 등락폭이 정확하지 않습니다.[/yellow][/dim]")

    if mismatch_tickers:
        targets = ", ".join(mismatch_tickers)
        config.console.print(f"[dim][yellow] ⚠️ 데이터 불일치 경고: {targets} - KIS API 데이터가 yfinance보다 과거입니다. 지표 분석에 주의하세요.[/yellow][/dim]")

    if failed_tickers:
        targets = ", ".join(failed_tickers)
        config.console.print(f"[dim][red] ⚠️ 데이터 수신 실패: {targets} - yfinance 서버 장애 또는 일시적 통신 오류일 수 있습니다. 잠시 후 다시 시도하세요.[/red][/dim]")
        
    return failed_tickers

def show_market_indices(interval=0):
    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
    
    target_indices = None
    
    if interval == 0:
        config.console.print("\n[bold]조회할 지수 그룹을 선택하세요 (쉼표로 구분):[/bold]")
        
        for key, info in config.INDICES_GROUPS.items():
            config.console.print(f"[{key}] {info['name']}")
        config.console.print("[8] 전체 지수")
        
        config.console.print()
        sel = Prompt.ask("번호 입력 [dim](예: 1,3 또는 12 / 반복: 1@ / 취소: q)[/dim]", default="8")
        if sel.lower() == 'q': return
        
        try:
            if sel.endswith('@'):
                interval = 60
                sel = sel.rstrip('@')

            raw_keys = [k.strip() for k in sel.split(',') if k.strip()]
            keys = []
            for k in raw_keys:
                if k.isdigit() and len(k) > 1:
                    keys.extend(list(k))
                else:
                    keys.append(k)
            
            if '8' in keys:
                target_indices = None
            else:
                target_indices = []
                for k in keys:
                    if k in config.INDICES_GROUPS:
                        target_indices.extend(config.INDICES_GROUPS[k]['indices'])
                
                if not target_indices:
                    config.console.print("[red]선택된 그룹이 없습니다.[/red]")
                    return
        except:
            config.console.print("[red]잘못된 입력입니다.[/red]")
            return

    try:
        while True:
            if interval > 0:
                now_str = datetime.now().strftime("%H:%M:%S")
                config.console.print(f"\n[dim]조회 시간: {now_str}[/dim]")

            failed_list = _show_market_indices_core(target_indices)

            if interval <= 0:
                if failed_list:
                    config.console.print()
                    if Prompt.ask(f"[yellow]⚠️ 조회 실패한 {len(failed_list)}개 지수를 다시 시도하시겠습니까?[/yellow]", choices=["y", "n"], default="y") == "y":
                        target_indices = failed_list
                        continue
                break
            
            config.console.print() 
            try:
                for remaining in range(interval, -1, -1):
                    config.console.print(f"[bold yellow]다음 조회까지 {remaining}초 대기 중입니다. (중단: Ctrl+C)[/]   ", end="\r")
                    time.sleep(1)
            except KeyboardInterrupt:
                config.console.print("\n[yellow]반복 조회를 중단하고 메뉴로 돌아갑니다.[/yellow]")
                break
    except KeyboardInterrupt:
        config.console.print("\n[yellow]작업이 취소되었습니다.[/yellow]")
    except Exception as e:
        logger.error(f"지수 분석 중 치명적 오류: {e}")
        config.console.print(f"\n[bold red]지수 분석 중 오류 발생: {e}[/bold red]")
