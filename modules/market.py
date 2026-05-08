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
import utils
from modules import analysis # [추가] 분석 모듈 임포트
from datetime import datetime, timedelta, timezone
import math
import logging
import time
import sys
import threading
import concurrent.futures # [추가] 병렬 처리용

logger = logging.getLogger(__name__)

# [추가] 시장 지수 (yfinance 다중 다운로드) 전용 메모리 캐시
_MARKET_YF_CACHE = {}
_MARKET_YF_CACHE_LOCK = threading.RLock()

def clear_market_yf_cache():
    with _MARKET_YF_CACHE_LOCK:
        _MARKET_YF_CACHE.clear()

# [수정] 지수 리스트 통합 관리 (순서 유지)
ALL_INDICES = [
    # 1. 국내 지수
    ("코스피", "^KS11"), ("코스피200", "^KS200"), ("코스닥", "^KQ11"), ("코스닥150", "^KQ150"),
    # 2. 글로벌 지수
    ("나스닥 선물", "NQ=F"), ("나스닥", "^IXIC"), ("S&P500", "^GSPC"), ("다우존스", "^DJI"), ("러셀2000", "^RUT"),
    ("Japan - 닛케이", "^N225"), ("Taiwan - 대만가권", "^TWII"), ("Hong Kong - 항셍", "^HSI"), ("China - 상해종합", "000001.SS"), 
    ("Germany - 닥스40", "^GDAXI"), ("Europe - 스톡스50", "^STOXX50E"),
    # 3. 섹터 및 주요 지표
    ("SOX (반도체)", "^SOX"), ("QGRD (스마트그리드)", "^QGRD"), ("NBI (바이오)", "^NBI"), ("BKX (은행)", "^BKX"), ("VIX (변동성)", "^VIX"),
    ("MSCI 신흥국", "EEM"), ("하이일드 채권", "HYG"),
    # 4. 금리 및 환율
    ("달러인덱스", "DX-Y.NYB"), ("달러환율", "KRW=X"), ("미국채 2년물 선물", "ZT=F"), ("미국채 5년물 금리", "^FVX"), ("미국채 10년물 금리", "^TNX"), ("미국채 30년물 금리", "^TYX"),
    # 5. 원자재
    ("금", "GC=F"), ("은", "SI=F"), ("구리", "HG=F"),
    ("브랜트유", "BZ=F"), ("WTI 원유", "CL=F"), ("가솔린 RBOB", "RB=F"),
    ("천연가스", "NG=F"), ("밀", "ZW=F"),
    # 6. 암호화폐
    ("비트코인", "BTC-USD"), ("이더리움", "ETH-USD"), ("솔라나", "SOL-USD"), ("리플", "XRP-USD")
]

# 이름 -> 티커 매핑 (기존 호환성 유지)
INDICES_MAP = dict(ALL_INDICES)

def _process_index_worker(name, ticker, df_daily, df_intraday):
    """(내부함수) 단일 지수 분석 워커"""
    try:
        is_domestic_index = name in ["코스피", "코스닥", "코스피200", "코스닥150"]

        # [수정] 국내 지수는 yfinance 데이터를 사용하지 않고 KIS API 데이터만 사용
        if is_domestic_index:
            df_daily = pd.DataFrame()
            df_intraday = pd.DataFrame()

        # A. DataFrame 준비
        if not df_daily.empty:
            df_daily.columns = [c.lower() for c in df_daily.columns]
            if 'close' in df_daily.columns:
                df_daily = df_daily.dropna(subset=['close'])
        if not df_intraday.empty:
            df_intraday.columns = [c.lower() for c in df_intraday.columns]

        is_kis_source = False
        mismatch_msg = None

        if is_domestic_index:
            if name == "코스피": m_type = "KOSPI"
            elif name == "코스닥": m_type = "KOSDAQ"
            elif name == "코스피200": m_type = "KOSPI200"
            elif name == "코스닥150": m_type = "KOSDAQ150"
            
            df_fallback = analysis.get_domestic_index_data(m_type)
            if df_fallback is not None and not df_fallback.empty:
                df_daily = df_fallback
                df_daily.columns = [c.lower() for c in df_daily.columns]
                
                if not isinstance(df_daily.index, pd.DatetimeIndex):
                    target_col = None
                    if 'date' in df_daily.columns: target_col = 'date'
                    elif 'stck_bsop_date' in df_daily.columns: target_col = 'stck_bsop_date'
                    if target_col:
                        df_daily[target_col] = pd.to_datetime(df_daily[target_col])
                        df_daily.set_index(target_col, inplace=True)
                
                if not df_intraday.empty:
                    try:
                        kis_last_dt = df_daily.index[-1].date()
                        yf_last_dt = df_intraday.index[-1].date()
                        if yf_last_dt > kis_last_dt:
                            mismatch_msg = f"{name}(KIS:{kis_last_dt} vs YF:{yf_last_dt})"
                    except Exception: pass
                
                if df_daily.attrs.get('source') == 'KIS':
                    is_kis_source = True
                    # [Fix] KIS API 사용 시, yfinance 분봉 데이터는 무시하여 데이터 불일치 방지
                    # yfinance 분봉의 최신 날짜와 KIS 일봉의 최신 날짜가 다를 경우, 불필요한 '데이터 누락' 경고가 발생함
                    df_intraday = pd.DataFrame()

        # B. 가격 결정
        high_52_daily = 0.0
        if not df_daily.empty and 'close' in df_daily.columns:
            high_52_daily = float(df_daily['close'].tail(250).max())
            
        current = 0.0
        prev = 0.0
        high_52 = high_52_daily
        
        is_crypto = name in ["비트코인", "이더리움", "솔라나", "리플"]
        is_futures = name in ["나스닥 선물", "미국채 2년물 선물", "금", "은", "구리", "브랜트유", "WTI 원유", "가솔린 RBOB", "천연가스", "밀"]
        is_proxy_yield = False # [추가] 금리 추정 여부 플래그
        chart_calc_price = None # [추가] 지표 계산용 원본 가격 보존
        
        use_fast_info = False
        if not is_domestic_index:
            try:
                fi = api.get_yf_fast_info(ticker)
                if fi:
                    last_price = fi.get('last_price')
                    prev_close = fi.get('regular_market_previous_close')
                    
                    # [수정] 선물/암호화폐는 yfinance의 전일 종가 대신 일봉 데이터의 종가를 사용 (정확도 향상)
                    if (is_crypto or is_futures) and not df_daily.empty and len(df_daily) >= 2:
                        try:
                            last_dt = df_daily.index[-1].date()
                            utc_today = datetime.now(timezone.utc).date()
                            target_idx = -2 if last_dt >= utc_today else -1
                            check_prev = float(df_daily['close'].iloc[target_idx])
                            if not math.isnan(check_prev):
                                prev_close = check_prev
                        except Exception: pass
                    
                    if (last_price is not None and prev_close is not None and 
                        not math.isnan(last_price) and not math.isnan(prev_close)):
                        
                        current = float(last_price)
                        prev = float(prev_close)
                        chart_calc_price = current # 프록시 적용 전 원본 가격(실제 지수) 저장
                        
                        yh = fi.get('year_high')
                        if yh is not None and not math.isnan(yh):
                            high_52 = max(high_52, float(yh))
                        else:
                            high_52 = max(high_52, current)
                        
                        use_fast_info = True

                # --- [수정] 미국채 금리 아시아장 실시간 추정 (선물 연동) ---
                fut_mapping = {
                    "미국채 5년물 금리": {"ticker": "ZF=F", "duration": 4.5},
                    "미국채 10년물 금리": {"ticker": "ZN=F", "duration": 7.5},
                    "미국채 30년물 금리": {"ticker": "ZB=F", "duration": 16.0}
                }
                if name in fut_mapping:
                    fut_info = fut_mapping[name]
                    try:
                        fut_fi = api.get_yf_fast_info(fut_info["ticker"])
                        if fut_fi:
                            f_curr = fut_fi.get('last_price')
                            f_prev = fut_fi.get('regular_market_previous_close')
                            if f_curr and f_prev and not math.isnan(f_curr) and not math.isnan(f_prev) and f_prev > 0:
                                utc_hour = datetime.now(timezone.utc).hour
                                    # 미국 정규장 및 프리마켓 일부 제외 시간대 (utc_hour < 13 or >= 21)
                                if utc_hour < 13 or utc_hour >= 21:
                                    f_rate = (f_curr - f_prev) / f_prev * 100
                                    est_yield = current - (f_rate / fut_info["duration"])
                                    
                                    # 선물처럼 정규장 종가(current)를 prev로 삼아 오버나이트 등락을 표시
                                    prev = current
                                    current = est_yield
                                    is_proxy_yield = True
                    except: pass
            except Exception: pass

        patched_name = None
        missing_name = None
        is_delayed = False
        
        if not use_fast_info:
            if not is_domestic_index:
                is_delayed = True

            if df_daily.empty:
                return {'status': 'failed', 'name': name}
            
            daily_last_date = df_daily.index[-1].date()
            today = datetime.now().date()
            
            current = float(df_daily['close'].iloc[-1])
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

            target_date = intra_last_date if (intra_last_date and intra_last_date >= daily_last_date) else daily_last_date

            if daily_last_date < target_date:
                prev = float(df_daily['close'].iloc[-1])
                prev_date_src = daily_last_date
            elif daily_last_date == target_date:
                if len(df_daily) >= 2:
                    prev = float(df_daily['close'].iloc[-2])
                    prev_date_src = df_daily.index[-2].date()

            gap_days = (target_date - prev_date_src).days
            weekday = target_date.weekday()
            is_gap = False

            # [Fix] 해외 지수에 대해서만 데이터 갭(주말, 휴장일 등)을 체크하고, 국내 지수는 휴장일이 있으므로 체크하지 않습니다.
            if not is_domestic_index:
                if weekday == 0 and gap_days > 3: is_gap = True
                elif weekday < 5 and gap_days > 1: is_gap = True

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
                except Exception as e:
                    logger.debug(f"[MARKET_GAP_DEBUG] [{name}] Patching failed: {e}")
            
            if patched: patched_name = name
            elif is_gap: 
                if target_date < today: missing_name = f"{name}(Old:{target_date})"
                else: missing_name = f"{name}(Last:{prev_date_src})"
                
        if chart_calc_price is None:
            chart_calc_price = current
            
        # [추가] 지표 및 상태 판별용 오리지널 가격 (프록시 적용 시 원본 가격 유지)
        eval_price = chart_calc_price if is_proxy_yield else current

        # C. 실시간 가격 패치
        ema5, ema20, ema60, ema120 = None, None, None, None
        val_psar, val_rsi, val_adx, val_cci, val_macd, val_macd_sig = None, None, None, None, None, None
        df_calc = pd.DataFrame()

        if not df_daily.empty and 'close' in df_daily.columns and len(df_daily) > 10:
            df_calc = df_daily[['open', 'high', 'low', 'close', 'volume']].copy()
            df_calc.dropna(subset=['close'], inplace=True)
            
            # [수정] 프록시 추정값은 지표 연산에서 철저히 배제 (원본 차트 가격 유지)
            if not math.isnan(chart_calc_price) and chart_calc_price > 0 and not is_proxy_yield:
                df_calc.iloc[-1, df_calc.columns.get_loc('close')] = chart_calc_price
                if chart_calc_price > df_calc.iloc[-1]['high']: df_calc.iloc[-1, df_calc.columns.get_loc('high')] = chart_calc_price
                if chart_calc_price < df_calc.iloc[-1]['low']: df_calc.iloc[-1, df_calc.columns.get_loc('low')] = chart_calc_price
            
            ind = indicators.calculate_indicators(df_calc)
            
            ema5, ema20 = ind['ema_5'], ind['ema_20']
            ema60, ema120 = ind['ema_60'], ind['ema_120']
            val_psar = ind.get('psar')
            val_rsi = ind.get('rsi')
            val_adx = ind.get('adx')
            val_cci = ind.get('cci')
            val_macd = ind.get('macd')
            val_macd_sig = ind.get('macd_signal')

        # D. 결과 포맷팅
        if math.isnan(current): current = 0.0
        if math.isnan(prev): prev = 0.0
        if math.isnan(eval_price): eval_price = 0.0

        diff = current - prev
        rate = 0.0
        if prev != 0: rate = (diff / prev) * 100
        
        if math.isnan(diff): diff = 0.0
        if math.isnan(rate): rate = 0.0

        high_52_rate = 0.0
        if high_52 != 0: high_52_rate = ((eval_price - high_52) / high_52) * 100
        if math.isnan(high_52_rate): high_52_rate = 0.0

        is_invalid_data = False
        if current == 0.0:
            is_invalid_data = True
        elif is_domestic_index and df_daily.empty:
            is_invalid_data = True

        if is_invalid_data:
            curr_str = "[dim]-[/]"
            change_str = "[dim]-[/]"
            high_52_str = "[dim]-[/]"
        else:
            diff_color = "[red]" if diff > 0 else ("[blue]" if diff < 0 else "[white]")
            
            if "미국채" in name and "선물" not in name:
                change_str = f"{diff_color}{diff:+.2f}p ({rate:+.2f}%)[/]"
                curr_fmt = f"{current:,.2f}%"
            else:
                change_str = f"{diff_color}{diff:+.2f} ({rate:+.2f}%)[/]"
                curr_fmt = f"{current:,.2f}"
                if name == "달러환율": curr_fmt += "원"

            # [수정] 현재가 색상 규칙 단순화 (추세와 현재가 관계 중심)
            curr_price_color = "[white]" # 기본값
            if ema20 and ema60:
                is_bullish_trend = ema20 > ema60
                is_bearish_trend = ema20 < ema60
                
                if is_bullish_trend:
                    # 강세장: 현재가가 20일선 위에 있으면 강세(red), 아래면 눌림목(white)
                    curr_price_color = "[red]" if eval_price > ema20 else "[white]"
                elif is_bearish_trend:
                    # 약세장: 현재가가 20일선 아래에 있으면 약세(blue), 위면 반등 시도(orange3)
                    curr_price_color = "[blue]" if eval_price < ema20 else "[orange3]"
                # 혼조세(ema20 == ema60)는 기본값 white 유지
            
            curr_str = f"{curr_price_color}{curr_fmt}[/]"

            h_color = "[white]"
            if high_52_rate > -3.0: h_color = "[red]"
            elif high_52_rate < -20.0: h_color = "[blue]"
            
            if "미국채" in name and "선물" not in name:
                high_52_str = f"[dim]{high_52:,.2f}%[/] ({h_color}{high_52_rate:.1f}%[/])"
            else:
                high_52_str = f"[dim]{high_52:,.2f}[/] ({h_color}{high_52_rate:.1f}%[/])"

        def fmt_val(val, color_tag):
            if val is None or math.isnan(val): return "-"
            if "미국채" in name and "선물" not in name:
                s = f"{val:,.2f}%"
            else:
                s = f"{val:,.0f}" if val >= 1000 else f"{val:,.2f}"
            return f"{color_tag}{s}[/]" if color_tag else s

        # [수정] 이평선 색상 규칙 단순화 (계층적 분석)
        ema5_color = "[white]"
        if ema5 is not None and ema20 is not None:
            ema5_color = "[red]" if ema5 > ema20 else "[blue]"

        ema20_color = "[white]"
        if ema20 is not None and ema60 is not None:
            ema20_color = "[red]" if ema20 > ema60 else "[blue]"

        ema60_color = "[white]"
        if ema60 is not None and ema120 is not None:
            ema60_color = "[red]" if ema60 > ema120 else "[blue]"

        ema120_color = "[white]"
        if not df_calc.empty and len(df_calc) > 121:
            try:
                ema120_series = df_calc['close'].ewm(span=120, adjust=False).mean()
                if ema120_series.iloc[-1] > ema120_series.iloc[-2]:
                    ema120_color = "[red]"
                else:
                    ema120_color = "[blue]"
            except: pass

        sar_icon = "-"
        if val_psar is not None:
            sar_icon = "[red]⬆[/]" if eval_price > val_psar else "[blue]⬇[/]"
        
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
        
        adaptive_targets = [
            "코스피", "코스닥", "코스피200", "코스닥150",
            "나스닥 선물", "나스닥", "S&P500", "다우존스", "러셀2000",
            "Japan - 닛케이", "Hong Kong - 항셍", "China - 상해종합", 
            "Taiwan - 대만가권", "Germany - 닥스40", "Europe - 스톡스50",
            "미국채 2년물 선물",
            "MSCI 신흥국", "하이일드 채권"
        ]

        if name in adaptive_targets:
            try:
                ma_period = config.MARKET_REGIME_PARAMS.get("REGIME_MA_PERIOD", 20)
                adx_threshold = config.MARKET_REGIME_PARAMS.get("REGIME_ADX_THRESHOLD", 20)
                
                regime_state = "Sideways"
                    
                target_df = df_calc if not df_calc.empty else df_daily
                if not target_df.empty and len(target_df) >= ma_period:
                    ma_series = target_df['close'].ewm(span=ma_period, adjust=False).mean()
                    ma_val = ma_series.iloc[-1]
                    
                    slope = 0
                    if len(ma_series) >= 5:
                        slope = (ma_series.iloc[-1] - ma_series.iloc[-5]) / 5
                    
                    adx_val = val_adx if val_adx is not None else 0
                    
                    if eval_price > ma_val and slope > 0 and adx_val >= adx_threshold:
                        regime_state = "Bull"
                    elif eval_price < ma_val:
                        regime_state = "Bear"
                
                if regime_state == "Bull": display_name = f"[red]{name}[/]"
                elif regime_state == "Bear": display_name = f"[blue]{name}[/]"
                else: display_name = f"[yellow]{name}[/]"
            except: pass
        elif name == "미국채 10년물 금리":
            if eval_price >= 4.80: display_name = f"[magenta]{name}[/]"
            elif 4.50 <= eval_price < 4.80: display_name = f"[red]{name}[/]"
            elif 4.20 <= eval_price < 4.50: display_name = f"[orange3]{name}[/]"
            elif 3.80 <= eval_price < 4.20: display_name = f"[green]{name}[/]"
            elif 3.30 <= eval_price < 3.80: display_name = f"[yellow]{name}[/]"
            elif eval_price < 3.30: display_name = f"[blue]{name}[/]"
        elif name == "미국채 5년물 금리":
            if eval_price >= 4.50: display_name = f"[red]{name}[/]"
            elif 4.00 <= eval_price < 4.50: display_name = f"[orange3]{name}[/]"
            elif 3.50 <= eval_price < 4.00: display_name = f"[green]{name}[/]"
            elif eval_price < 3.50: display_name = f"[blue]{name}[/]"
        elif name == "미국채 30년물 금리":
            if eval_price >= 5.00: display_name = f"[magenta]{name}[/]"
            elif 4.60 <= eval_price < 5.00: display_name = f"[red]{name}[/]"
            elif 4.00 <= eval_price < 4.60: display_name = f"[green]{name}[/]"
            elif eval_price < 4.00: display_name = f"[blue]{name}[/]"
        elif name == "SOX (반도체)":
            if high_52_rate >= -5.0: display_name = f"[red]{name}[/]"
            elif -10.0 <= high_52_rate < -5.0: display_name = f"[orange3]{name}[/]"
            elif -20.0 <= high_52_rate < -10.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -20.0: display_name = f"[blue]{name}[/]"
        elif name == "QGRD (스마트그리드)":
            if high_52_rate >= -5.0: display_name = f"[red]{name}[/]"
            elif -10.0 <= high_52_rate < -5.0: display_name = f"[orange3]{name}[/]"
            elif -20.0 <= high_52_rate < -10.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -20.0: display_name = f"[blue]{name}[/]"
        elif name == "NBI (바이오)":
            if high_52_rate >= -5.0: display_name = f"[red]{name}[/]"
            elif -15.0 <= high_52_rate < -5.0: display_name = f"[orange3]{name}[/]"
            elif -25.0 <= high_52_rate < -15.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -25.0: display_name = f"[blue]{name}[/]"
        elif name == "BKX (은행)":
            if high_52_rate >= -5.0: display_name = f"[red]{name}[/]"
            elif -10.0 <= high_52_rate < -5.0: display_name = f"[orange3]{name}[/]"
            elif -20.0 <= high_52_rate < -10.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -20.0: display_name = f"[blue]{name}[/]"
        elif name in ["비트코인", "이더리움", "솔라나", "리플"]:
            if high_52_rate >= -10.0: display_name = f"[red]{name}[/]"
            elif -25.0 <= high_52_rate < -10.0: display_name = f"[orange3]{name}[/]"
            elif -40.0 <= high_52_rate < -25.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -40.0: display_name = f"[blue]{name}[/]"
        elif name == "금":
            if high_52_rate >= -3.0: display_name = f"[red]{name}[/]"
            elif -8.0 <= high_52_rate < -3.0: display_name = f"[orange3]{name}[/]"
            elif -15.0 <= high_52_rate < -8.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -15.0: display_name = f"[blue]{name}[/]"
        elif name in ["은", "구리"]:
            if high_52_rate >= -5.0: display_name = f"[red]{name}[/]"
            elif -15.0 <= high_52_rate < -5.0: display_name = f"[orange3]{name}[/]"
            elif -25.0 <= high_52_rate < -15.0: display_name = f"[yellow]{name}[/]"
            elif high_52_rate < -25.0: display_name = f"[blue]{name}[/]"
        elif name == "VIX (변동성)":
            if current < 15: display_name = f"[green]{name}[/]"
            elif 15 <= current < 20: display_name = f"[yellow]{name}[/]"
            elif 20 <= current < 30: display_name = f"[orange3]{name}[/]"
            elif 30 <= current < 40: display_name = f"[red]{name}[/]"
            elif current >= 40: display_name = f"[magenta]{name}[/]"
        elif name == "달러인덱스":
            if current >= 115: display_name = f"[magenta]{name}[/]"
            elif 110 <= current < 115: display_name = f"[red]{name}[/]"
            elif 105 <= current < 110: display_name = f"[orange3]{name}[/]"
            elif 95 <= current < 105: display_name = f"[green]{name}[/]"
            elif current < 95: display_name = f"[blue]{name}[/]"
        elif name == "달러환율":
            if current >= 1500: display_name = f"[magenta]{name}[/]"
            elif 1450 <= current < 1500: display_name = f"[red]{name}[/]"
            elif 1400 <= current < 1450: display_name = f"[orange3]{name}[/]"
            elif 1300 <= current < 1400: display_name = f"[green]{name}[/]"
            elif 1200 <= current < 1300: display_name = f"[cyan]{name}[/]"
            elif current < 1200: display_name = f"[blue]{name}[/]"
        elif name == "WTI 원유":
            if current >= 100: display_name = f"[magenta]{name}[/]"
            elif 90 <= current < 100: display_name = f"[red]{name}[/]"
            elif 80 <= current < 90: display_name = f"[orange3]{name}[/]"
            elif 65 <= current < 80: display_name = f"[green]{name}[/]"
            elif 55 <= current < 65: display_name = f"[yellow]{name}[/]"
            elif current < 55: display_name = f"[blue]{name}[/]"
        elif name == "브랜트유":
            if current >= 105: display_name = f"[magenta]{name}[/]"
            elif 95 <= current < 105: display_name = f"[red]{name}[/]"
            elif 85 <= current < 95: display_name = f"[orange3]{name}[/]"
            elif 70 <= current < 85: display_name = f"[green]{name}[/]"
            elif 60 <= current < 70: display_name = f"[yellow]{name}[/]"
            elif current < 60: display_name = f"[blue]{name}[/]"
        elif name == "가솔린 RBOB":
            if current >= 4.00: display_name = f"[magenta]{name}[/]"
            elif 3.20 <= current < 4.00: display_name = f"[red]{name}[/]"
            elif 2.60 <= current < 3.20: display_name = f"[orange3]{name}[/]"
            elif 2.10 <= current < 2.60: display_name = f"[green]{name}[/]"
            elif 1.60 <= current < 2.10: display_name = f"[yellow]{name}[/]"
            elif current < 1.60: display_name = f"[blue]{name}[/]"
        elif name == "천연가스":
            if current >= 6.0: display_name = f"[magenta]{name}[/]"
            elif 4.0 <= current < 6.0: display_name = f"[red]{name}[/]"
            elif 3.0 <= current < 4.0: display_name = f"[orange3]{name}[/]"
            elif 2.0 <= current < 3.0: display_name = f"[green]{name}[/]"
            elif 1.5 <= current < 2.0: display_name = f"[yellow]{name}[/]"
            elif current < 1.5: display_name = f"[blue]{name}[/]"
        elif name == "밀":
            if current >= 800: display_name = f"[magenta]{name}[/]"
            elif 700 <= current < 800: display_name = f"[red]{name}[/]"
            elif 600 <= current < 700: display_name = f"[orange3]{name}[/]"
            elif 500 <= current < 600: display_name = f"[green]{name}[/]"
            elif 400 <= current < 500: display_name = f"[yellow]{name}[/]"
            elif current < 400: display_name = f"[blue]{name}[/]"

        if is_proxy_yield:
            display_name += " [dim](선물적용)[/dim]"

        return {
            'status': 'success',
            'row_data': [display_name, curr_str, change_str, high_52_str, fmt_val(ema5, ema5_color), fmt_val(ema20, ema20_color), fmt_val(ema60, ema60_color), fmt_val(ema120, ema120_color), trend_str, rsi_str, adx_str, cci_str],
            'patched_name': patched_name,
            'missing_name': missing_name,
            'mismatch_msg': mismatch_msg,
            'is_kis_source': is_kis_source,
            'is_delayed': is_delayed
        }
    except Exception as e:
        return {'status': 'error', 'name': name, 'error': e}

def _show_market_indices_core(target_indices=None):
    # [변경] config.DEBUG_LEVEL 참조
    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print("[dim][TRACE] show_market_indices() 호출[/dim]")

    # [추가] 지수 조회 시에도 100% 실시간 최신 가격을 가져오도록 마이크로 캐시 강제 초기화
    if hasattr(api, 'clear_micro_cache'):
        api.clear_micro_cache()

    indices_map = INDICES_MAP.copy()
    if target_indices:
        indices_map = {k: v for k, v in indices_map.items() if k in target_indices}
        
    if not indices_map:
        return []

    data_storage = {}
    yf_tickers = None
    
    # [Fix] 예외 발생 시 참조 오류(UnboundLocalError) 방지를 위해 변수 초기화 상단 이동
    patched_tickers = []
    missing_tickers = []
    mismatch_tickers = []
    failed_tickers = []
    delayed_tickers = []

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

            task_dl = progress.add_task("[cyan]지수 데이터 수신 준비 중...[/cyan]", total=None)

            # 그룹별 순차 요청
            for group_name, t_list in groups_to_fetch:
                if not t_list: continue

                # [추가] 코스닥150은 yfinance를 호출하지 않도록 필터링
                yf_t_list = [t for t in t_list if t != "^KQ150"]
                if not yf_t_list:
                    continue

                tickers_to_fetch = []
                now = datetime.now()
                ttl_seconds = getattr(config, 'CHART_CACHE_TTL_MINUTES', 180) * 60

                # [추가] 지수 캐시 적중(Hit) 검사
                with _MARKET_YF_CACHE_LOCK:
                    for t in yf_t_list:
                        cached = _MARKET_YF_CACHE.get(t)
                        if cached and ttl_seconds > 0 and (now - cached['time']).total_seconds() < ttl_seconds:
                            # 날짜가 같을 때만 유효 (자정 무효화)
                            if cached['date'] == now.strftime("%Y-%m-%d"):
                                data_storage[t] = cached['data']
                                continue
                        tickers_to_fetch.append(t)

                # 모든 티커가 캐시에 있으면 다운로드 생략
                if not tickers_to_fetch:
                    if config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                        config.console.print(f"[dim cyan][TRACE] Cache Hit (All) for {group_name}[/dim cyan]")
                    continue
                
                tickers_str = " ".join(tickers_to_fetch)
                disp_group = group_name.split(" (")[0] if " (" in group_name else group_name
                progress.update(task_dl, description=f"[cyan]지수 데이터 수신 중 ({disp_group})...[/cyan]")

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

                        # [수정] 받아온 데이터 파싱 후 누락 검증 (yfinance 다중 요청 시 일부 누락 방지)
                        success_tickers = []
                        with _MARKET_YF_CACHE_LOCK:
                            for t_fetch in tickers_to_fetch:
                                d_df = pd.DataFrame()
                                i_df = pd.DataFrame()
                                
                                try:
                                    if not d_data.empty:
                                        if isinstance(d_data.columns, pd.MultiIndex):
                                            # levels[0] 대신 get_level_values 사용으로 안정성 확보
                                            if t_fetch in d_data.columns.get_level_values(0): 
                                                d_df = d_data[t_fetch].copy()
                                        elif 'Close' in d_data.columns: d_df = d_data.copy()
                                        elif 'close' in d_data.columns: d_df = d_data.copy()
                                except: pass

                                try:
                                    if not i_data.empty:
                                        if isinstance(i_data.columns, pd.MultiIndex):
                                            if t_fetch in i_data.columns.get_level_values(0): 
                                                i_df = i_data[t_fetch].copy()
                                        elif 'Close' in i_data.columns: i_df = i_data.copy()
                                        elif 'close' in i_data.columns: i_df = i_data.copy()
                                except: pass
                                
                                # 유효성 검사: 모든 값이 NaN으로 돌아온 경우 캐시하지 않음
                                is_valid = False
                                if not d_df.empty:
                                    close_col = next((c for c in d_df.columns if str(c).lower() == 'close'), None)
                                    if close_col and not d_df[close_col].dropna().empty:
                                        is_valid = True

                                if is_valid:
                                    stored_data = {'daily': d_df, 'intra': i_df}
                                    data_storage[t_fetch] = stored_data
                                    success_tickers.append(t_fetch)
                                    
                                    if not d_df.empty:
                                        _MARKET_YF_CACHE[t_fetch] = {
                                            'data': stored_data,
                                            'time': now,
                                            'date': now.strftime("%Y-%m-%d")
                                        }
                                        
                        # 누락된 티커 추출 후 재시도
                        missing_tickers = [t for t in tickers_to_fetch if t not in success_tickers]
                        if missing_tickers and attempt < 1:
                            tickers_to_fetch = missing_tickers
                            tickers_str = " ".join(tickers_to_fetch)
                            time.sleep(0.5)
                            continue # 다음 attempt로 즉시 이동하여 누락분만 재다운로드
                            
                        break # 모두 성공했거나 최대 재시도 도달 시 루프 종료
                    except KeyboardInterrupt:
                        raise # 상위 핸들러로 전파
                    except Exception as e:
                        if "database" in str(e).lower(): api.clear_yfinance_cache()
                        else: break
                
                # 그룹 처리 사이 인터럽트 감지 기회 제공
                time.sleep(0.1)

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
            # [최적화] 통신과 연산의 작업 시간 편차로 인한 프로그레스 바 불규칙 증가를 해결하기 위해,
            # 예열(Prefetch) 단계와 연산 단계를 하나로 통합하고 병렬 스레드 내에서 순차 처리하도록 리팩토링합니다.
            task = progress.add_task("[cyan]지수 실시간 데이터 수집 및 지표 연산 중...[/cyan]", total=len(indices_map))

            # [수정] 지수 지표 분석 루프 병렬화 (ThreadPoolExecutor)
            results_dict = {}
            # 야후 API 동시 호출 차단을 방지하고 부드러운 진행을 위해 max_workers를 5로 조정
            max_w = 5
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                futures = {}
                for name, ticker in indices_map.items():
                    stored = data_storage.get(ticker, {'daily': pd.DataFrame(), 'intra': pd.DataFrame()})
                    # DataFrame의 복사본을 전달하여 스레드 안전성 확보
                    df_daily = stored['daily'].copy() if not stored['daily'].empty else pd.DataFrame()
                    df_intraday = stored['intra'].copy() if not stored['intra'].empty else pd.DataFrame()
                    futures[executor.submit(_process_index_worker, name, ticker, df_daily, df_intraday)] = name
                    
                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    try:
                        results_dict[name] = future.result()
                    except Exception as e:
                        results_dict[name] = {'status': 'error', 'name': name, 'error': e}
                    finally:
                        progress.advance(task)

            for name, ticker in indices_map.items():
                if name in ["나스닥 선물", "Japan - 닛케이", "SOX (반도체)", "달러인덱스", "미국채 2년물 선물", "금", "비트코인"]: 
                    table.add_section()

                res = results_dict.get(name)
                if res:
                    if res['status'] == 'success':
                        table.add_row(*res['row_data'])
                        if res.get('patched_name'): patched_tickers.append(res['patched_name'])
                        if res.get('missing_name'): missing_tickers.append(res['missing_name'])
                        if res.get('mismatch_msg'): mismatch_tickers.append(res['mismatch_msg'])
                        if res.get('is_delayed'): delayed_tickers.append(name)
                    elif res['status'] == 'failed':
                        table.add_row(name, "[red]수신 실패[/]", "[dim]yfinance 응답 없음[/]", "-", "-", "-", "-", "-", "-", "-", "-", "-")
                        failed_tickers.append(name)
                    else:
                        if config.SCREEN_DEBUG_LEVEL in ["DEBUG", "TRACE"]:
                            config.console.print(f"[bold red][DEBUG] 에러 발생({name}): {res.get('error')}[/bold red]")
                        table.add_row(name, "Error", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-")
        
        # 테이블 출력 (Progress Context 밖에서 실행)
        try:
            config.console.print(table, crop=False)
            sys.stdout.flush()

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

    if delayed_tickers:
        targets = ", ".join(delayed_tickers)
        config.console.print(f"[dim][yellow] ⚠️ 실시간 시세 지연: {targets} - 실시간 단건 조회(fast_info)가 불가하여 최신 차트 데이터를 기준으로 표시했습니다.[/yellow][/dim]")

    if failed_tickers:
        targets = ", ".join(failed_tickers)
        config.console.print(f"[dim][red] ⚠️ 데이터 수신 실패: {targets} - yfinance 서버 장애 또는 일시적 통신 오류일 수 있습니다. 잠시 후 다시 시도하세요.[/red][/dim]")
        
    return failed_tickers

def show_market_indices(interval=0):
    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
    
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "9"
    
    # [추가] 런타임에 3번 그룹(섹터 및 주요 지표)에 BKX (은행) 지수를 동적으로 편입
    if hasattr(config, 'INDICES_GROUPS') and "3" in config.INDICES_GROUPS:
        grp3_idx = config.INDICES_GROUPS["3"].get('indices', [])
        if "BKX (은행)" not in grp3_idx:
            if "NBI (바이오)" in grp3_idx:
                grp3_idx.insert(grp3_idx.index("NBI (바이오)") + 1, "BKX (은행)")
            else:
                grp3_idx.append("BKX (은행)")

    while True:
        utils.clear_screen()
        context.USER_ACTION_BREADCRUMB = context.USER_ACTION_BREADCRUMB[:base_breadcrumb_len]
        target_indices = None
        
        if interval == 0:
            menu_items = []
            for key, info in config.INDICES_GROUPS.items():
                name = info['name']
                if " (" in name:
                    parts = name.split(" (", 1)
                    menu_items.append((key, parts[0], parts[1].replace(")", "")))
                else:
                    menu_items.append((key, name, ""))
                    
            menu_items.append(("9", "전체 지수", "All Indices"))
            sel = utils.show_menu("시장 지수 조회 (Market Indices)", menu_items, default_choice=last_choice, custom_prompt="번호 입력 [dim](예: 1,3 또는 12 / 반복: 1@)[/dim]")
            if sel.lower() in ['b', 'q']: return False
            if sel.lower() == 'h':
                if getattr(utils, 'show_help', None):
                    utils.show_help()
                    utils.pause()
                continue
            
            
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
                
                if '9' in keys:
                    target_indices = None
                else:
                    target_indices = []
                    for k in keys:
                        if k in config.INDICES_GROUPS:
                            target_indices.extend(config.INDICES_GROUPS[k]['indices'])
                    
                    if not target_indices:
                        config.console.print("[red]선택된 그룹이 없습니다.[/red]")
                        time.sleep(1)
                        continue
            except:
                config.console.print("[red]잘못된 입력입니다.[/red]")
                time.sleep(1)
                continue

            # [수정] 입력값 검증이 끝난 후 정상 처리 시에만 마지막 선택값 및 트래킹 갱신
            last_choice = sel
            
            sel_clean = sel.replace('@', '')
            menu_map_dict = dict((k, v) for k, v, _ in menu_items)
            if sel_clean in menu_map_dict:
                context.USER_ACTION_BREADCRUMB.append(f"[{sel_clean}] {menu_map_dict[sel_clean]}")
            else:
                context.USER_ACTION_BREADCRUMB.append(f"[선택] {sel_clean}")

        try:
            while True:
                if interval > 0:
                    now_str = datetime.now().strftime("%H:%M:%S")
                    config.console.print(f"\n[dim]조회 시간: {now_str}[/dim]")

                failed_list = _show_market_indices_core(target_indices)

                if interval <= 0:
                    if failed_list:
                        if Prompt.ask(f"[yellow]⚠️ 조회 실패한 {len(failed_list)}개 지수를 다시 시도하시겠습니까?[/yellow]", choices=["y", "n"], default="y") == "y":
                            config.console.print()
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
            
        if interval > 0:
            return False
        else:
            utils.pause()
