# modules/market.py
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.prompt import Prompt
import yfinance as yf
import pandas as pd
import config
config.silence_yfinance_numpy_warning()  # yfinance import 뒤에 걸어야 억제 유효
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

def _us_futures_closed_now():
    """CME 글로벡스 주말 휴장(금 17:00 ~ 일 18:00 ET) 여부."""
    try:
        et = api.now_us_eastern()
        wd = et.weekday()  # 0=월
        return wd == 5 or (wd == 6 and et.hour < 18) or (wd == 4 and et.hour >= 17)
    except Exception:
        return False

def _daily_prev_close_idx(df_daily, last_price, is_futures):
    """선물/암호화폐의 '전일 종가'로 쓸 일봉 인덱스(-1 또는 -2)를 고른다.

    마지막 봉이 오늘(UTC) 이전이면 기본은 -1(마지막 봉 종가 = 전일 종가, 월요일 아시아
    시간대처럼 새 세션 봉이 아직 없는 장중 상황). 다만 선물이 주말·휴장 중이면 현재가가
    마지막 봉 종가와 같아 등락이 0%로 굳으므로, 그 전 봉(-2)과 비교해 마지막 세션의
    등락을 표시한다. (휴장 판정: 현재가==마지막 봉 종가 또는 CME 주말 휴장 시간대)"""
    last_dt = df_daily.index[-1].date()
    utc_today = datetime.now(timezone.utc).date()
    if last_dt >= utc_today:
        return -2
    if is_futures:
        stale_price = False
        try:
            last_close = float(df_daily['close'].iloc[-1])
            stale_price = (last_price is not None and not math.isnan(float(last_price))
                           and last_close > 0
                           and abs(float(last_price) - last_close) / last_close < 1e-6)
        except Exception:
            pass
        if stale_price or _us_futures_closed_now():
            return -2
    return -1

def _k200_night_session(now=None):
    """코스피200 선물 표시 세션 판정 — 야간(CM)=18:00~익일 07:59, 주간(F)=08:00~17:59.

    주간장(08:45~15:45) 마감 후 18시 전까지는 주간 최종치를, 야간장(18:00~익일 06:00)
    마감 후 아침 8시 전까지는 야간 최종치를 유지 표시한다.
    """
    h = (now or datetime.now()).hour
    return h >= 18 or h < 8

# [수정] 지수 리스트 통합 관리 (순서 유지)
ALL_INDICES = [
    # 1. 국내 지수
    #  V코스피200(VKOSPI, 변동성지수)은 KIS 업종코드 0503 전용이며 yfinance 티커가 없다
    #  ("^VKOSPI"는 자리표시자, yfinance 다운로드에서 제외됨).
    #  코스피200선물은 KIS 선물 TR 전용 — 주간(F)/야간(CM)을 시간대로 자동 전환하며
    #  표시명이 '코스피200선물 F/CM'으로 바뀐다("^K200FUT"는 자리표시자).
    #  두 지수 모두 KIS '실전' 서버 전용(모드 2)이다: 모의서버는 해당 TR 미지원/불안정하고
    #  모의 모드에서 실전 서버는 사용하지 않으므로 토스(3)·모의(1)에서는 표시하지 않는다.
    ("코스피", "^KS11"), ("코스피200", "^KS200"), ("코스피200선물", "^K200FUT"), ("V코스피200", "^VKOSPI"), ("코스닥", "^KQ11"), ("코스닥150", "^KQ150"),
    # 2. 미국 지수
    ("나스닥 선물", "NQ=F"), ("나스닥", "^IXIC"), ("S&P500 선물", "ES=F"), ("S&P500", "^GSPC"), ("다우존스 선물", "YM=F"), ("다우존스", "^DJI"), ("러셀2000 선물", "RTY=F"), ("러셀2000", "^RUT"),
    # 3. 섹터 및 지표
    ("SOX (반도체)", "^SOX"), ("DRG (제약)", "^DRG"), ("NBI (바이오)", "^NBI"), ("BKX (은행)", "^BKX"), ("DJT (운송)", "^DJT"), ("DJU (유틸/전력)", "^DJU"), ("XAL (항공)", "^XAL"), ("XOI (에너지)", "^XOI"), ("HUI (금광)", "^HUI"), ("VIX (변동성)", "^VIX"), ("HY OAS (신용위험)", "^HYOAS"),
    ("MSCI 전세계", "ACWI"), ("MSCI 선진국", "URTH"), ("MSCI 신흥국", "EEM"),
    # 4. 금리 및 환율
    #  미국채 2년물은 야후에 현물 금리 지수 티커(^FVX류)가 없고 CBOT 금리선물(2YY=F)은 유동성
    #  고갈로 시세가 수일 지연되는 죽은 값이라 사용하지 않는다 → tvDatafeed 현물(TVC:US02Y) 전용.
    #  ("^US02Y"는 자리표시자, yfinance 다운로드에서 제외되며 현물 실패 시 수신 실패로 표시)
    ("달러인덱스", "DX-Y.NYB"), ("달러환율", "KRW=X"), ("미국채 2년물 금리", "^US02Y"), ("미국채 5년물 금리", "^FVX"), ("미국채 10년물 금리", "^TNX"), ("미국채 30년물 금리", "^TYX"),
    # 5. 글로벌 지수
    ("Japan - 닛케이", "^N225"), ("Taiwan - 대만가권", "^TWII"), ("Hong Kong - 항셍", "^HSI"), ("China - 상해종합", "000001.SS"), 
    ("UK - FTSE 100", "^FTSE"), ("France - CAC 40", "^FCHI"), ("Germany - DAX 40", "^GDAXI"), ("Europe - STOXX 50", "^STOXX50E"),
    # 6. 원자재
    ("금", "GC=F"), ("은", "SI=F"), ("구리", "HG=F"),
    ("브랜트유", "BZ=F"), ("WTI 원유", "CL=F"), ("가솔린 RBOB", "RB=F"),
    ("천연가스", "NG=F"), ("밀", "ZW=F"),
    # 7. 암호화폐
    ("비트코인", "BTC-USD"), ("이더리움", "ETH-USD"), ("솔라나", "SOL-USD"), ("리플", "XRP-USD")
]

# 이름 -> 티커 매핑 (기존 호환성 유지)
INDICES_MAP = dict(ALL_INDICES)

# [추가] 그룹 구분선을 시작하는 지수명 — 지수 화면(메뉴 1)과 텔레그램 시장지수가 공유한다
#  (양쪽에 따로 하드코딩되어 국채 2년물 신설 시 텔레그램만 누락됐던 문제 재발 방지)
SECTION_START_INDICES = ["나스닥 선물", "Japan - 닛케이", "SOX (반도체)", "달러인덱스", "미국채 2년물 금리", "금", "비트코인"]

def fetch_index_quote(name, code):
    """단일 지수의 경량 시세 (표시명, 현재값, 전일값)을 반환한다. 실패 시 (name, None, None).

    텔레그램 등 외부 표면용 — 소스 선택 규칙은 지수 화면(_process_index_worker)과 동일:
    코스피200선물=KIS 선물 TR(주/야간 자동 전환), 국내 지수=KIS/tvDatafeed,
    미국채=tvDatafeed 현물(실패 시 5/10/30년만 야후 현물+아시아장 선물 프록시 '(선물적용)',
    2년물은 대체 소스가 없어 실패 반환), 그 외 해외=yfinance fast_info(실패 시 일봉 차트).
    """
    display_name = name
    current = prev = None
    domestic_map = {
        "코스피": "KOSPI", "코스피200": "KOSPI200",
        "코스닥": "KOSDAQ", "코스닥150": "KOSDAQ150", "V코스피200": "VKOSPI"
    }
    try:
        if name == "코스피200선물":
            fut_div = "CM" if _k200_night_session() else "F"
            fut_iscd = api.get_k200_futures_front_code()
            fut_q = api.get_k200_futures_quote(fut_div, fut_iscd) if fut_iscd else None
            if fut_q:
                current = float(fut_q['current'])
                prev = current - float(fut_q['diff'])
                display_name = f"{name} {fut_div}"
        elif name in domestic_map:
            df = analysis.get_domestic_index_data(domestic_map[name])
            if df is not None and not df.empty:
                current = float(df.iloc[-1]['close'])
                prev = float(df.iloc[-2]['close']) if len(df) > 1 else current
        elif name in config.US_TREASURY_SPOT_SYMBOLS:
            tdf = analysis.get_us_treasury_spot_data(config.US_TREASURY_SPOT_SYMBOLS[name])
            if tdf is not None and not tdf.empty and len(tdf) >= 2:
                current = float(tdf['close'].iloc[-1])
                prev = float(tdf['close'].iloc[-2])
            elif name != "미국채 2년물 금리":
                fi = api.get_yf_fast_info(code)
                if fi and fi.get('last_price'):
                    current = float(fi['last_price'])
                    prev = float(fi.get('regular_market_previous_close', current))
                    fut_mapping = {
                        "미국채 5년물 금리": {"ticker": "ZF=F", "duration": 4.5},
                        "미국채 10년물 금리": {"ticker": "ZN=F", "duration": 7.5},
                        "미국채 30년물 금리": {"ticker": "ZB=F", "duration": 16.0}
                    }
                    fut_info = fut_mapping[name]
                    fut_fi = api.get_yf_fast_info(fut_info["ticker"])
                    if fut_fi and fut_fi.get('last_price') and fut_fi.get('regular_market_previous_close'):
                        f_curr = float(fut_fi['last_price'])
                        f_prev = float(fut_fi['regular_market_previous_close'])
                        if f_prev > 0:
                            utc_hour = datetime.now(timezone.utc).hour
                            if utc_hour < 13 or utc_hour >= 21:
                                f_rate = (f_curr - f_prev) / f_prev * 100
                                est_yield = current - (f_rate / fut_info["duration"])
                                prev = current
                                current = est_yield
                                display_name = f"{name}(선물적용)"
        else:
            try:
                fi = api.get_yf_fast_info(code)
                if fi and fi.get('last_price'):
                    current = float(fi['last_price'])
                    prev = float(fi.get('regular_market_previous_close', current))
            except Exception:
                pass
            if current is None:
                df = api.get_chart_data(code, is_overseas=True)
                if df is not None and not df.empty:
                    current = float(df.iloc[-1]['close'])
                    prev = float(df.iloc[-2]['close']) if len(df) > 1 else current
    except Exception as e:
        logger.debug(f"fetch_index_quote 실패({name}): {e}")
    return display_name, current, prev

def _process_index_worker(name, ticker, df_daily, df_intraday):
    """(내부함수) 단일 지수 분석 워커"""
    try:
        is_domestic_index = name in ["코스피", "코스닥", "코스피200", "코스닥150", "V코스피200", "코스피200선물"]

        # [수정] 국내 지수는 yfinance 데이터를 사용하지 않고 KIS API 데이터만 사용
        if is_domestic_index:
            df_daily = pd.DataFrame()
            df_intraday = pd.DataFrame()

        # A. DataFrame 준비
        if not df_daily.empty:
            df_daily.columns = [c.lower() for c in df_daily.columns]
            # [Fix] yfinance 특정 지수(QGRD 등) 조회 시 Volume 컬럼 누락으로 인한 KeyError 방어
            for c in ['open', 'high', 'low', 'close', 'volume']:
                if c not in df_daily.columns: df_daily[c] = 0.0
                
            if 'close' in df_daily.columns:
                df_daily = df_daily.dropna(subset=['close'])
        if not df_intraday.empty:
            df_intraday.columns = [c.lower() for c in df_intraday.columns]
            for c in ['open', 'high', 'low', 'close', 'volume']:
                if c not in df_intraday.columns: df_intraday[c] = 0.0

        is_kis_source = False
        mismatch_msg = None
        fut_div = None    # [추가] 코스피200선물 세션 ('F'=주간 / 'CM'=야간)
        fut_quote = None  # [추가] 선물 시세 TR 결과 (현재가/전일대비/등락률)

        if is_domestic_index:
            if name == "코스피": m_type = "KOSPI"
            elif name == "코스닥": m_type = "KOSDAQ"
            elif name == "코스피200": m_type = "KOSPI200"
            elif name == "코스닥150": m_type = "KOSDAQ150"
            elif name == "V코스피200": m_type = "VKOSPI"
            elif name == "코스피200선물":
                fut_div = "CM" if _k200_night_session() else "F"
                m_type = f"K200FUT_{fut_div}"

            # [추가] V코스피200·코스피200선물은 KIS 실전 전용 — 토스는 대체 소스가 없고,
            #  모의서버는 해당 TR 미지원/불안정 + 모의 모드에서 실전 서버 미사용(운영 방침) → 스킵
            #  (표시 자체는 _show_market_indices_core에서 모드 2로 제한되며, 여기는 방어 로직)
            if (config.session.is_toss or config.session.is_simulation) and m_type in ("VKOSPI", "K200FUT_F", "K200FUT_CM"):
                return {'status': 'skipped', 'name': name}

            df_fallback = analysis.get_domestic_index_data(m_type)

            # [추가] 코스피200선물: 현재가/등락률은 시세 TR 값으로 보정한다.
            #  야간 등락률은 야간 전일봉 대비가 아닌 '주간 종가 대비'가 관행이므로
            #  차트 봉 차분 대신 KIS futs_prdy_vrss/ctrt를 그대로 쓴다.
            if fut_div:
                try:
                    fut_iscd = api.get_k200_futures_front_code()
                    if fut_iscd:
                        fut_quote = api.get_k200_futures_quote(fut_div, fut_iscd)
                except Exception:
                    fut_quote = None
            # [수정] 토스 모드: 코스피200·코스닥150은 TradingView(tvDatafeed)로 조회하며,
            #  그마저 실패하면 대체 소스가 없으므로 스킵 → '-' 표시(수신 실패 오탐 방지).
            if config.session.is_toss and m_type in ("KOSPI200", "KOSDAQ150") and (df_fallback is None or df_fallback.empty):
                return {'status': 'skipped', 'name': name}
            if df_fallback is not None and not df_fallback.empty:
                # [Fix] get_domestic_index_data는 공유 캐시 객체를 반환하므로 복사 후 사용.
                #  아래 set_index(inplace=True)가 캐시 df의 'date' 컬럼을 제거해
                #  개별 지수 분석(기간별 시세)이 KeyError로 실패하던 문제 방지.
                df_daily = df_fallback.copy()
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

            # [Fix] 토스 모드 코스피/코스닥은 yfinance(^KS11/^KQ11)로 받는데, 최신 거래일 종가가
            #  아직 미집계면 close=NaN인 후행 행이 붙어온다(fast_info도 None). 이 행이 남으면
            #  current=close.iloc[-1]=NaN→0이 되어 지수·등락률·52주가 '-'로만 표시된다(지표는
            #  dropna 후 계산돼 정상). 후행 NaN 종가 행을 제거해 마지막 유효 종가를 현재가로 쓴다.
            if 'close' in df_daily.columns:
                df_daily = df_daily[df_daily['close'].notna()]

        # B. 가격 결정
        high_52_daily = 0.0
        if not df_daily.empty:
            if 'high' in df_daily.columns and pd.notna(df_daily['high'].max()) and float(df_daily['high'].max()) > 0:
                high_52_daily = float(df_daily['high'].tail(250).max())
            elif 'close' in df_daily.columns:
                high_52_daily = float(df_daily['close'].tail(250).max())
            
        current = 0.0
        prev = 0.0
        high_52 = high_52_daily
        
        is_crypto = name in ["비트코인", "이더리움", "솔라나", "리플"]
        is_futures = name in ["나스닥 선물", "S&P500 선물", "다우존스 선물", "러셀2000 선물", "금", "은", "구리", "브랜트유", "WTI 원유", "가솔린 RBOB", "천연가스", "밀"]
        is_proxy_yield = False # [추가] 금리 추정 여부 플래그
        chart_calc_price = None # [추가] 지표 계산용 원본 가격 보존
        
        use_fast_info = False
        is_treasury_spot = False  # [추가] 미국채 '현물' 금리(TVC:USxxY) 소스 적용 여부

        # [추가] 미국채 금리: 현물(TVC:USxxY)을 tvDatafeed로 1차 조회한다. 현물 금리는
        #  아시아장에도 거의 24시간 갱신되어 선물 프록시 추정 없이 실제 호가를 표시할 수 있다.
        #  성공 시 현재가·전일대비·52주고·지표를 모두 현물 일봉으로 계산한다. 폴백은
        #  5/10/30년만 기존 경로(^FVX류+아시아장 선물 프록시 (F)) 사용 — 2년물은 대체 소스가
        #  없어(야후 현물 지수 부재, 2YY=F 선물은 유동성 고갈로 수일 지연되는 죽은 시세)
        #  죽은 값을 표시하는 대신 수신 실패로 처리한다.
        if name in config.US_TREASURY_SPOT_SYMBOLS:
            try:
                tv_df = analysis.get_us_treasury_spot_data(config.US_TREASURY_SPOT_SYMBOLS[name])
                if tv_df is not None and not tv_df.empty and len(tv_df) >= 2:
                    df_daily = tv_df.copy()
                    df_daily['date'] = pd.to_datetime(df_daily['date'])
                    df_daily.set_index('date', inplace=True)
                    current = float(df_daily['close'].iloc[-1])
                    prev = float(df_daily['close'].iloc[-2])
                    chart_calc_price = current
                    # 52주 고점은 다른 지수와 동일하게 일중 고가 기준(고가 미제공 시 종가)
                    hi_max = float(df_daily['high'].tail(250).max() or 0)
                    high_52 = hi_max if hi_max > 0 else float(df_daily['close'].tail(250).max())
                    use_fast_info = True
                    is_treasury_spot = True
            except Exception as e:
                logger.debug(f"{name} 현물(TV) 조회 실패: {e}")
            if name == "미국채 2년물 금리" and not is_treasury_spot:
                return {'status': 'failed', 'name': name, 'src': 'TradingView'}

        if name == "HY OAS (신용위험)":
            try:
                tv_df = analysis.get_fred_data("BAMLH0A0HYM2")
                if tv_df is not None and not tv_df.empty and len(tv_df) >= 2:
                    df_daily = tv_df.copy()
                    df_daily['date'] = pd.to_datetime(df_daily['date'])
                    df_daily.set_index('date', inplace=True)
                    current = float(df_daily['close'].iloc[-1])
                    prev = float(df_daily['close'].iloc[-2])
                    chart_calc_price = current
                    hi_max = float(df_daily['high'].tail(250).max() or 0)
                    high_52 = hi_max if hi_max > 0 else float(df_daily['close'].tail(250).max())
                    use_fast_info = True
                    is_treasury_spot = True  # 동일한 플래그로 yfinance 폴백 생략
            except Exception as e:
                logger.debug(f"HY OAS 조회 실패: {e}")

        if not is_domestic_index and not is_treasury_spot:
            try:
                fi = api.get_yf_fast_info(ticker)
                if fi:
                    last_price = fi.get('last_price')
                    prev_close = fi.get('regular_market_previous_close')
                    
                    # [수정] 선물/암호화폐는 yfinance의 전일 종가 대신 일봉 데이터의 종가를 사용 (정확도 향상)
                    if (is_crypto or is_futures) and not df_daily.empty and len(df_daily) >= 2:
                        try:
                            target_idx = _daily_prev_close_idx(df_daily, last_price, is_futures)
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
                    except Exception: pass
            except Exception: pass

        patched_name = None
        missing_name = None
        is_delayed = False

        if not use_fast_info:
            if not is_domestic_index:
                is_delayed = True

            if df_daily.empty:
                # [수정] 국내 지수는 KIS 계열 소스라 'yfinance 응답 없음' 문구가 오해를 줌 → 소스 구분 전달
                return {'status': 'failed', 'name': name, 'src': 'KIS' if is_domestic_index else 'yfinance'}

            # [최적화] 분봉(5m)은 fast_info 실패(지연) 시에만 단건 지연조회한다.
            # (평상시 불필요한 bulk 5m 다운로드를 제거하여 콜드스타트 지연을 줄임)
            if df_intraday.empty and not is_domestic_index:
                try:
                    i_raw = api.fetch_yfinance_data(ticker, period="5d", interval="5m", group_by='ticker')
                    if i_raw is not None and not i_raw.empty:
                        if isinstance(i_raw.columns, pd.MultiIndex):
                            if ticker in i_raw.columns.get_level_values(0):
                                i_raw = i_raw[ticker].copy()
                            else:
                                i_raw = pd.DataFrame()
                        if not i_raw.empty:
                            df_intraday = i_raw
                            df_intraday.columns = [str(c).lower() for c in df_intraday.columns]
                            for c in ['open', 'high', 'low', 'close', 'volume']:
                                if c not in df_intraday.columns: df_intraday[c] = 0.0
                except Exception:
                    pass

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
                except Exception: pass

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
                
        # [추가] 코스피200선물: 시세 TR 성공 시 현재가/기준가를 KIS 제공값으로 교체
        #  (prev = 현재가 - 전일대비 → 아래 D단계 diff/rate 계산이 KIS 등락률과 일치)
        if fut_quote:
            current = fut_quote['current']
            prev = current - fut_quote['diff']

        if chart_calc_price is None:
            chart_calc_price = current

        # [추가] 지표 및 상태 판별용 오리지널 가격 (프록시 적용 시 원본 가격 유지)
        eval_price = chart_calc_price if is_proxy_yield else current

        # C. 실시간 가격 패치
        ema5, ema20, ema60, ema120 = None, None, None, None
        val_psar, val_rsi, val_adx, val_cci, val_macd, val_macd_sig = None, None, None, None, None, None
        val_plus_di, val_minus_di = None, None
        df_calc = pd.DataFrame()
        ind = {}

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
            val_plus_di = ind.get('plus_di')
            val_minus_di = ind.get('minus_di')

        # D. 결과 포맷팅
        if math.isnan(current): current = 0.0
        if math.isnan(prev): prev = 0.0
        if math.isnan(eval_price): eval_price = 0.0

        diff = current - prev
        rate = 0.0
        if prev != 0: rate = (diff / prev) * 100

        # [장전 폴백] 국내 지수는 NXT 연장거래가 없어 KRX 개장(09:00) 전엔 현재가=전일 종가라
        #  등락률이 0%로 굳는다. 이 구간엔 직전 정규장 최종 등락률(전일 vs 전전일 종가)을 표시한다.
        #  (일봉 마지막 봉이 장전 placeholder(오늘)면 전전일=-3, 오늘 봉이 없으면 전전일=-2)
        if is_domestic_index and fut_div is None and diff == 0 and not df_daily.empty and len(df_daily) >= 2 \
           and api._before_krx_regular_open():
            try:
                today_d = datetime.strptime(utils.market_today(False), '%Y%m%d').date()
                last_d = df_daily.index[-1].date()
                pp_idx = -3 if last_d >= today_d else -2
                if len(df_daily) >= abs(pp_idx):
                    pp = float(df_daily['close'].iloc[pp_idx])
                    if not math.isnan(pp) and pp > 0:
                        prev = pp
                        diff = current - prev
                        rate = (diff / prev) * 100
            except Exception: pass

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
                change_str = f"{diff_color}{diff:+.3f}p ({rate:+.2f}%)[/]"
                curr_fmt = f"{current:,.3f}%"
            else:
                change_str = f"{diff_color}{diff:+.2f} ({rate:+.2f}%)[/]"
                curr_fmt = f"{current:,.2f}"
                if name == "달러환율": curr_fmt += "원"

            # [통일] 현재가 색상은 종목 표와 동일 규칙 — analysis.price_trend_color 단일 소스
            #  자산 종류와 무관하게 '값 자체의 방향'만 나타낸다(등락률·52주 고점대비와 동일 문법).
            curr_price_color = analysis.price_trend_color(eval_price, ema20, ema60)
            curr_str = f"{curr_price_color}{curr_fmt}[/]"

            h_color = "[white]"
            if high_52_rate > -3.0: h_color = "[red]"
            elif high_52_rate < -20.0: h_color = "[blue]"
            
            if "미국채" in name and "선물" not in name:
                high_52_str = f"[dim]{high_52:,.3f}%[/] ({h_color}{high_52_rate:.1f}%[/])"
            else:
                h52_fmt = f"{high_52:,.0f}" if high_52 >= 1000 else f"{high_52:,.2f}"
                high_52_str = f"[dim]{h52_fmt}[/] ({h_color}{high_52_rate:.1f}%[/])"

        def fmt_val(val, color_tag):
            if val is None or math.isnan(val): return "[dim]-[/dim]"
            if "미국채" in name and "선물" not in name:
                s = f"{val:,.3f}%"
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
            except Exception: pass

        sar_icon = "[dim]-[/dim]"
        if val_psar is not None:
            sar_icon = "[red]⬆[/]" if eval_price > val_psar else "[blue]⬇[/]"
        
        macd_icon = "[dim]-[/dim]"
        if val_macd is not None and val_macd_sig is not None:
            zero_sign = "+" if val_macd > 0 else "-"
            cross_char = "G" if val_macd > val_macd_sig else "D"
            m_color = "red" if val_macd > val_macd_sig else "blue"
            macd_icon = f"[{m_color}]{zero_sign}{cross_char}[/]"
        
        obv_trend = ind.get('obv_trend')
        obv_val = ind.get('obv')
        vol_sum = df_calc['volume'].tail(5).sum() if not df_calc.empty and 'volume' in df_calc.columns else 0
        
        if vol_sum == 0 or obv_trend is None or obv_val is None or math.isnan(obv_val):
            obv_icon = "[dim]-[/dim]"
            obv_disp = "[dim]-[/dim]"
        else:
            obv_icon = "[red]▲[/]" if obv_trend else "[blue]▼[/]"
            obv_c = "red" if obv_trend else "blue"
            abs_val = abs(obv_val)
            if abs_val >= 999_950_000_000: obv_str = f"{obv_val/1_000_000_000_000:,.1f}T"
            elif abs_val >= 999_950_000: obv_str = f"{obv_val/1_000_000_000:,.1f}B"
            elif abs_val >= 999_500: obv_str = f"{obv_val/1_000_000:,.1f}M"
            elif abs_val >= 999.5: obv_str = f"{obv_val/1_000:,.0f}K"
            else: obv_str = f"{obv_val:,.0f}"
            obv_disp = f"[{obv_c}]{obv_str}[/]"
            
        trend_str = f"{sar_icon} {macd_icon} {obv_icon}"

        # [통일] RSI 임계값은 종목 표·도움말과 같은 config 단일 소스를 쓴다
        #  (상수로 두면 사용자가 RSI_UPPER/LOWER를 바꿔도 지수 표만 옛 기준으로 남는다)
        _rsi_up = config.INDICATOR_PARAMS["RSI_UPPER"]
        _rsi_low = config.INDICATOR_PARAMS["RSI_LOWER"]
        rsi_str = f"{val_rsi:.1f}" if val_rsi is not None else "[dim]-[/dim]"
        if val_rsi is not None:
            if val_rsi >= _rsi_up: rsi_str = f"[magenta]{rsi_str}[/]"
            elif 55 <= val_rsi < _rsi_up: rsi_str = f"[red]{rsi_str}[/]"
            elif 45 <= val_rsi < 55: rsi_str = f"[orange3]{rsi_str}[/]"
            elif _rsi_low < val_rsi < 45: rsi_str = f"[yellow]{rsi_str}[/]"
            else: rsi_str = f"[blue]{rsi_str}[/]"

        # ADX 값 뒤에 DMI 우위 방향(▲/▼/●)을 함께 표기 (표기 규칙은 analysis 단일 소스)
        adx_str = analysis.format_adx_cell(val_adx, val_plus_di, val_minus_di)

        # [통일] CCI 임계값도 config 단일 소스 (RSI와 동일한 이유)
        _cci_up = config.INDICATOR_PARAMS["CCI_UPPER"]
        _cci_low = config.INDICATOR_PARAMS["CCI_LOWER"]
        cci_str = f"{val_cci:.1f}" if val_cci is not None else "[dim]-[/dim]"
        if val_cci is not None:
            if val_cci >= _cci_up: cci_str = f"[red]{cci_str}[/]"
            elif 0 < val_cci < _cci_up: cci_str = f"[orange3]{cci_str}[/]"
            elif _cci_low < val_cci <= 0: cci_str = f"[yellow]{cci_str}[/]"
            else: cci_str = f"[blue]{cci_str}[/]"

        display_name = name

        # [추가] 코스피200선물: 현재 표시 중인 세션을 지수명에 병기 (F=주간 / CM=야간)
        if fut_div:
            display_name = f"{name} {fut_div}"

        # [통일] 방향성 자산의 지수명은 모두 국면 룰(이중 EMA + 추종 확인)로 색을 입힌다.
        #  섹터 지수·귀금속/구리·암호화폐는 과거 '52주 낙폭 구간' 룰을 썼으나,
        #  낙폭은 가격 수준일 뿐 추세 방향이 아니라 고점 직후 꺾임을 강세(빨강)로 오인했다.
        #  10년 일봉 검증에서 위험 조기경보 성능은 두 룰이 동등(J≈0)했고, 색 전환 빈도는
        #  국면 룰이 일관되게 낮아(원자재 20 vs 24회/년, 코인 27 vs 33, 섹터 21 vs 25)
        #  화면 안정성과 색 문법 일관성 모두 국면 룰이 우세해 통합했다.
        #  (52주 낙폭 수치 자체는 '52주 고점' 컬럼에 그대로 표시되므로 정보 손실 없음)
        #  절대 밴드 자산(VIX·미국채·달러·유가/가스/밀)은 수준 자체가 매크로 의미라 제외.
        adaptive_targets = [
            "코스피", "코스닥", "코스피200", "코스닥150", "코스피200선물",
            "나스닥 선물", "나스닥", "S&P500 선물", "S&P500", "다우존스 선물", "다우존스", "러셀2000 선물", "러셀2000",
            "Japan - 닛케이", "Hong Kong - 항셍", "China - 상해종합",
            "Taiwan - 대만가권", "UK - FTSE 100", "France - CAC 40",
            "Germany - DAX 40", "Europe - STOXX 50",
            "MSCI 전세계", "MSCI 선진국", "MSCI 신흥국",
            "SOX (반도체)", "NBI (바이오)", "BKX (은행)", "DJU (유틸/전력)", "DRG (제약)",
            "DJT (운송)", "XAL (항공)", "XOI (에너지)", "HUI (금광)",
            "금", "은", "구리",
            "비트코인", "이더리움", "솔라나", "리플",
        ]

        if name in adaptive_targets:
            try:
                # [동기화] 자동매매(analysis.get_market_regime)와 동일한 이중 EMA 교차 + 추종 확인 규칙.
                #  판정 로직이 양쪽에 중복되어 한쪽만 바뀌던 문제를 공통 함수로 통일했다.
                #  실시간 화면은 장중 현재가(eval_price)를 마지막 종가에 덮어써 즉시 반영한다.
                target_df = df_calc if not df_calc.empty else df_daily
                regime_state = "Sideways"

                if not target_df.empty and eval_price:
                    live_df = target_df.copy()
                    live_df.iloc[-1, live_df.columns.get_loc('close')] = eval_price
                    regime_state = analysis.classify_regime_from_df(live_df)['regime']

                # [수정] name 대신 display_name에 색을 입힌다 — 코스피200선물의 'F/CM' 접미사 유지
                _, regime_color = analysis.REGIME_DISPLAY.get(regime_state, ("", "yellow"))
                display_name = f"[{regime_color}]{display_name}[/]"
            except Exception: pass
        elif name in config.US_TREASURY_YIELD_BANDS:
            # [수정] 밴드 정의는 config.US_TREASURY_YIELD_BANDS 단일 소스 사용
            #  (상태 문구는 theme_analysis, 도움말은 main.show_help가 같은 소스를 공유)
            for band in config.US_TREASURY_YIELD_BANDS[name]["bands"]:
                thr, color = band[0], band[1]
                if thr is None or eval_price >= thr:
                    display_name = f"[{color}]{name}[/]"
                    break
        # [통일] 섹터 지수(SOX/NBI/BKX/DJU/DRG/DJT/XAL/XOI/HUI)·금/은/구리·암호화폐의
        #  52주 낙폭 색상 분기는 제거했다 — 위 adaptive_targets의 국면 룰로 일원화(유럽 지수와 동일).
        elif name in ["VIX (변동성)", "V코스피200"]:
            if current < 15: display_name = f"[green]{name}[/]"
            elif 15 <= current < 20: display_name = f"[yellow]{name}[/]"
            elif 20 <= current < 30: display_name = f"[orange3]{name}[/]"
            elif 30 <= current < 40: display_name = f"[red]{name}[/]"
            elif current >= 40: display_name = f"[magenta]{name}[/]"
        elif name == "HY OAS (신용위험)":
            if current >= 8.0: display_name = f"[magenta]{name}[/]"
            elif 5.0 <= current < 8.0: display_name = f"[red]{name}[/]"
            elif 4.0 <= current < 5.0: display_name = f"[orange3]{name}[/]"
            elif current < 4.0: display_name = f"[green]{name}[/]"
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
            # [통일] 국면 표시(PendDown)와 같은 하늘색 사용 — cyan(ANSI 6)은 터미널 테마마다
            #  렌더링이 달라져 하늘색과 구분이 흐려지므로 256색으로 고정한다
            elif 1200 <= current < 1300: display_name = f"[sky_blue3]{name}[/]"
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
            display_name += " [dim](F)[/dim]"

        return {
            'status': 'success',
            'row_data': [display_name, curr_str, change_str, high_52_str, fmt_val(ema5, ema5_color), fmt_val(ema20, ema20_color), fmt_val(ema60, ema60_color), fmt_val(ema120, ema120_color), trend_str, adx_str, rsi_str, cci_str, obv_disp],
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

    # [최적화] 과거에는 매 조회마다 마이크로 캐시를 강제 초기화해 100% 실시간 가격을 받았으나,
    # 이로 인해 fast_info(단건 시세)를 ~47개 지수마다 매번 재요청(yfinance 폴백은 _YF_LOCK 직렬화)하여
    # 결과 출력이 크게 지연되었다. 이제 fast_info의 짧은 TTL(기본 60초) 캐시를 그대로 활용한다.
    # (실시간성은 최대 TTL 만큼만 지연되며, 반복(@) 조회 시 체감 속도가 크게 향상된다.)

    indices_map = INDICES_MAP.copy()

    # [수정] 토스 모드에서도 코스피200·코스닥150을 출력한다.
    #  토스 API는 이 지수 시세를 제공하지 않으므로 analysis._fetch_domestic_index_data가
    #  TradingView(tvDatafeed)로 보강한다(모드 무관 출력). 조회 대상에서 더 이상 제외하지 않는다.

    # [추가] V코스피200(업종코드 0503)·코스피200선물(선물 TR)은 KIS 실전 전용 — 대체 소스
    #  (yfinance/tvDatafeed)가 없고, 모의서버는 해당 TR 미지원/불안정(MCI 오류)이며 모의 모드에서
    #  실전 서버는 사용하지 않는다(운영 방침) → 토스(3)·모의(1) 모드에서는 목록에서 제외(모드 2 전용).
    if config.session.is_toss or config.session.is_simulation:
        indices_map.pop("V코스피200", None)
        indices_map.pop("코스피200선물", None)

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
    failed_srcs = set()   # [추가] 실패 지수의 실제 소스 (하단 경고 문구용 — yfinance 하드코딩 방지)
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

                # [추가] 코스닥150·V코스피200·코스피200선물·미국채2년·HYOAS는 yfinance를 호출하지 않도록 필터링 (야후 미제공 티커)
                yf_t_list = [t for t in t_list if t not in ("^KQ150", "^VKOSPI", "^K200FUT", "^US02Y", "^HYOAS")]
                if not yf_t_list:
                    continue

                tickers_to_fetch = []
                now = datetime.now()
                ttl_seconds = getattr(config, 'CHART_CACHE_TTL_MINUTES', 360) * 60

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
                                # [최적화] 분봉(5m) bulk 다운로드 제거. 평상시 fast_info가 성공하면 분봉은 쓰이지 않으므로,
                                # 일봉(1y)만 받고 분봉은 fast_info 실패(지연) 종목에 한해 워커에서 단건 지연조회한다.
                                # threads=True: 그룹 내 티커들을 yfinance 내부에서 병렬 수신(순차 N왕복 → 동시, 데이터 동일)
                                d = api.fetch_yfinance_data(tickers_str, period="1y", interval="1d", group_by='ticker', threads=True)
                                result_container['daily'] = d
                                result_container['intra'] = pd.DataFrame()
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
                                except Exception: pass

                                try:
                                    if not i_data.empty:
                                        if isinstance(i_data.columns, pd.MultiIndex):
                                            if t_fetch in i_data.columns.get_level_values(0): 
                                                i_df = i_data[t_fetch].copy()
                                        elif 'Close' in i_data.columns: i_df = i_data.copy()
                                        elif 'close' in i_data.columns: i_df = i_data.copy()
                                except Exception: pass
                                
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
                        _missing_yf = [t for t in tickers_to_fetch if t not in success_tickers]
                        if _missing_yf and attempt < 1:
                            tickers_to_fetch = _missing_yf
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
        table.add_column("ADX", justify="right")
        table.add_column("RSI", justify="right")
        table.add_column("CCI", justify="right")
        table.add_column("OBV", justify="right")

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
                if name in SECTION_START_INDICES:
                    table.add_section()

                res = results_dict.get(name)
                if res:
                    if res['status'] == 'success':
                        table.add_row(*res['row_data'])
                        if res.get('patched_name'): patched_tickers.append(res['patched_name'])
                        if res.get('missing_name'): missing_tickers.append(res['missing_name'])
                        if res.get('mismatch_msg'): mismatch_tickers.append(res['mismatch_msg'])
                        if res.get('is_delayed'): delayed_tickers.append(name)
                    elif res['status'] == 'skipped':
                        # [추가] 토스 모드 코스닥150 등 미지원 지수: 재시도 없이 '-'만 표시
                        table.add_row(name, "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]")
                    elif res['status'] == 'failed':
                        fail_src = res.get('src', 'yfinance')
                        table.add_row(name, "[red]수신 실패[/]", f"[dim]{fail_src} 응답 없음[/]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]")
                        failed_tickers.append(name)
                        failed_srcs.add(fail_src)
                    else:
                        if config.SCREEN_DEBUG_LEVEL in ["DEBUG", "TRACE"]:
                            config.console.print(f"[bold red][DEBUG] 에러 발생({name}): {res.get('error')}[/bold red]")
                        table.add_row(name, "Error", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]")
        
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
        # [수정] 실패 소스를 그대로 안내 (국채 현물은 TradingView — yfinance로 오인되지 않도록)
        srcs = ", ".join(sorted(failed_srcs)) or "yfinance"
        config.console.print(f"[dim][red] ⚠️ 데이터 수신 실패: {targets} - {srcs} 서버 장애 또는 일시적 통신 오류일 수 있습니다. 잠시 후 다시 시도하세요.[/red][/dim]")

    return failed_tickers

def show_market_indices(interval=0):
    logger.info(f"운영자 실행: {' - '.join(context.USER_ACTION_BREADCRUMB)}")
    
    base_breadcrumb_len = len(context.USER_ACTION_BREADCRUMB)
    last_choice = "9"

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
                    
            menu_items.append(("8", "개별 지수 분석", "Individual Index Analysis"))
            menu_items.append(("9", "전체 지수", "All Indices"))
            
            try:
                sel = utils.show_menu("시장 지수 조회 (Market Indices)", menu_items, default_choice=last_choice, custom_prompt="번호 입력 [dim](예: 1,3 또는 12 / 반복: 1@)[/dim]")
            except StopIteration:
                # 테스트 프레임워크(Mock)에서 사이드 이펙트 입력이 소진되었을 때 안전하게 탈출
                return False
                
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
                
                if '8' in keys:
                    indices_list = ALL_INDICES
                    # [추가] KIS 실전 전용 지수는 토스·모의 모드 목록에서 제외 (지수 화면과 동일 정책)
                    if config.session.is_toss or config.session.is_simulation:
                        indices_list = [(n, c) for n, c in indices_list if n not in ("V코스피200", "코스피200선물")]
                    dict_list = [{'name': n, 'code': c} for n, c in indices_list]
                    idx, item = utils.search_stock_in_list(dict_list, title="개별 지수 분석 대상 선택")
                    if item:
                        target_name, target_code = item['name'], item['code']

                        is_overseas = True
                        domestic_map = {
                            "코스피": "KOSPI", "코스피200": "KOSPI200",
                            "코스닥": "KOSDAQ", "코스닥150": "KOSDAQ150",
                            "V코스피200": "VKOSPI"
                        }
                        if target_name == "코스피200선물":
                            # [Fix] 자리표시자(^K200FUT)가 yfinance로 넘어가 분석이 실패하던 문제 —
                            #  지수 화면과 동일하게 세션(주간 F/야간 CM)별 KIS 선물 차트로 분석한다.
                            target_code = f"K200FUT_{'CM' if _k200_night_session() else 'F'}"
                            is_overseas = False
                        elif target_name in domestic_map:
                            target_code = domestic_map[target_name]
                            is_overseas = False
                            
                        context.USER_ACTION_BREADCRUMB.append(f"[개별분석] {target_name}")
                        config.console.print(f"\n[bold green]>> {target_name}({target_code}) 개별 지수 심층 분석 실행[/bold green]")
                        # [Fix] 분석 중 예외가 바깥 입력 파싱 except에 잡혀 '잘못된 입력입니다.'로
                        #  잘못 안내되지 않도록 별도 처리하고 트레이스백을 로그에 남긴다.
                        try:
                            analysis.diagnose_stock(target_code=target_code, target_name=target_name, target_is_overseas=is_overseas)
                        except Exception:
                            logger.exception(f"개별 지수 분석 실패: {target_name}({target_code})")
                            config.console.print(f"[red]{target_name} 분석 중 오류가 발생했습니다. 로그(logs/mystock.log)를 확인하세요.[/red]")
                    
                    last_choice = sel
                    utils.pause()
                    continue

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
            except Exception:
                logger.exception(f"시장 지수 메뉴 입력 처리 오류: {sel!r}")
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
                        try:
                            if Prompt.ask(f"[yellow]⚠️ 조회 실패한 {len(failed_list)}개 지수를 다시 시도하시겠습니까?[/yellow]", choices=["y", "n"], default="y") == "y":
                                config.console.print()
                                # [추가] 국채 현물(TVC) 음성 캐시·회로차단 해제 — 해제 없이는
                                #  음성 캐시(600s) 동안 재시도가 즉시 실패해 무의미하다.
                                if any(n in config.US_TREASURY_SPOT_SYMBOLS for n in failed_list):
                                    analysis.reset_us_treasury_spot_failures()
                                target_indices = failed_list
                                continue
                        except StopIteration:
                            pass
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
