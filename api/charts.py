"""차트 데이터 조회 — 일봉·주봉·분봉.

KIS·토스·yfinance·TradingView 중 어느 소스로 갈지 고르고, 소스마다 다른 응답을
공통 스키마로 맞춘다. 국내 일봉은 KRX 정규장 기준으로 받아야 한다(NXT 체결이 섞이면
ATR 이 부풀고 ADX 가 어긋난다).
"""
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf
import config
import constants

#  로거 이름은 분해 전(api.py)과 같은 'api' 로 둔다 — 로그 필터·레벨 설정이 이름을 보므로
#  서브모듈마다 다른 이름을 쓰면 기존 설정이 조용히 빗나간다.
logger = logging.getLogger("api")

def _api():
    """패키지 네임스페이스(api)를 돌려준다 — 다른 계층의 이름은 반드시 이걸 통해 부른다.

    분해 전에는 전부 한 모듈이었으므로 테스트의 patch.object(api, 'X') 가 모든 호출부에
    걸렸다. 서브모듈이 상대 모듈을 직접 import 하면 그 patch 가 닿지 않는다 —
    같은 규약을 쓰는 modules/auto_trade 의 _pkg() 와 같은 이유다.
    """
    import api
    return api

def get_stock_name_by_code(code, is_overseas):
    final_name = None
    if not is_overseas:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = _api().session.get(url, headers=headers, timeout=3)
            m_og = re.search(r'meta property="og:title" content="(.*?)"', r.text)
            if m_og:
                raw_title = m_og.group(1).strip()
                if "페이지를 찾을 수 없습니다" not in raw_title:
                    clean_name = re.sub(r'\s*\(\d{6}\)', '', raw_title)
                    clean_name = re.sub(r'\s*[:|-]\s*(Npay|네이버|Naver|금융|증권).*', '', clean_name, flags=re.IGNORECASE)
                    final_name = clean_name.strip()
                if final_name in ["Npay 증권", "네이버 페이 증권", "증권", "금융", "네이버 금융"]: final_name = None
            else: final_name = code
        except Exception as e:
            logger.debug(f"Naver stock name parsing error: {e}")
            final_name = code
    else:
        # 1. TradingView Screener 우선 조회 (속도 개선)
        try:
            from tradingview_screener import Query, Column
            count, df = Query().set_markets('america').select('description').where(Column('name') == code).limit(1).get_scanner_data()
            if count > 0 and not df.empty:
                final_name = df.iloc[0]['description']
        except Exception as e:
            logger.debug(f"TV screener stock name fetch error: {e}")
        
        if not final_name:
            # 2. yfinance Fallback
            try:
                with open(os.devnull, 'w') as fnull:
                    old_stderr = sys.stderr; sys.stderr = fnull
                    try:
                        ticker = yf.Ticker(code); info = ticker.info
                        if info: final_name = info.get('longName') or info.get('shortName')
                    except Exception as e:
                        logger.debug(f"yf.Ticker info fetch error: {e}")
                    finally: sys.stderr = old_stderr
            except Exception as e:
                logger.debug(f"yf.Ticker outer block error: {e}")
            
    if not final_name and code: return code
    return final_name

def _get_hourly_chart_data(code, is_overseas):
    """시봉(1시간) 데이터 조회 (yfinance 전용)"""
    targets = []
    if is_overseas:
        targets.append(code)
    else:
        # config 데이터(stock.json)를 참조하여 정확한 거래소 티커 1개만 구성
        market_suffix = None
        for key in ["stocks_kr", "etfs_kr"]:
            for item in config.session.stock_data.get(key, []):
                if item.get('code') == code and "exchange" in item:
                    if item['exchange'].upper() == "KOSDAQ":
                        market_suffix = ".KQ"
                    elif item['exchange'].upper() == "KOSPI":
                        market_suffix = ".KS"
                    break
            if market_suffix: break
            
        if market_suffix:
            targets.append(f"{code}{market_suffix}")
        else:
            # 시장 정보를 모를 경우 (직접 입력 등) 기존처럼 둘 다 시도
            targets.append(f"{code}.KS")
            targets.append(f"{code}.KQ")
    
    for t in targets:
        try:
            # 1시간 간격, 최근 3개월 (지표 계산을 위해 충분한 데이터 확보)
            df = _api().fetch_yfinance_data(t, period="3mo", interval="1h")
            if not df.empty:
                # 1. 컬럼 평탄화 및 튜플 방어
                flat_cols = []
                for col in df.columns:
                    if isinstance(col, tuple):
                        flat_cols.append(str(col[0]).lower())
                    else:
                        flat_cols.append(str(col).lower())
                df.columns = flat_cols
                
                # 2. 인덱스 리셋 (Datetime을 컬럼으로)
                df.reset_index(inplace=True)
                
                # 3. 모든 컬럼명을 소문자로 강제 변환
                df.columns = [str(c).lower() for c in df.columns]
                df = df.loc[:, ~df.columns.duplicated()].copy() # 중복 컬럼 제거 방어 로직 추가
                if 'datetime' in df.columns: df.rename(columns={'datetime': 'date'}, inplace=True)
                
                cols = ['date', 'open', 'high', 'low', 'close', 'volume']
                for c in cols:
                    if c not in df.columns: df[c] = 0
                
                df = df[cols].copy()
                # 시간대 변환 (UTC -> KST)
                if pd.api.types.is_datetime64_any_dtype(df['date']):
                    if df['date'].dt.tz is None:
                        df['date'] = df['date'].dt.tz_localize('UTC').dt.tz_convert('Asia/Seoul')
                    else:
                        df['date'] = df['date'].dt.tz_convert('Asia/Seoul')

                return df.sort_values('date', ascending=True).reset_index(drop=True)
        except Exception as e:
            logger.debug(f"yfinance hourly fetch failed for {t}: {e}")
            pass
            
    return pd.DataFrame()

def _resample_weekly(df):
    """일봉 DataFrame(date=YYYYMMDD 문자열)을 주봉으로 리샘플링한다(주 마감=금요일 기준).
    KIS 네이티브 주봉이 없는 경로(토스 개별종목)에서 사용한다.
    OHLCV 집계: 시가=주 첫 거래일, 고가=최댓값, 저가=최솟값, 종가=주 마지막, 거래량=합계."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    d = df.copy()
    d['_dt'] = pd.to_datetime(d['date'].astype(str), format='%Y%m%d', errors='coerce')
    d = d.dropna(subset=['_dt']).sort_values('_dt').set_index('_dt')
    agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    w = d.resample('W-FRI').agg(agg).dropna(subset=['close'])
    w = w.reset_index()
    w['date'] = w['_dt'].dt.strftime('%Y%m%d')  # 주봉 라벨 = 해당 주 마감(금)일
    return w[['date', 'open', 'high', 'low', 'close', 'volume']].reset_index(drop=True)

def _fetch_kis_weekly_domestic(code, lookback_days=1100):
    """KIS 국내 주봉(FID_PERIOD_DIV_CODE='W'). 날짜 구간을 뒤로 페이징하며 ~3년치를 모은다."""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=lookback_days)).strftime("%Y%m%d")
    url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["CHART"]
    all_items = []
    current_end_date = today
    retry_count = 0
    while retry_count < 10:
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                  "FID_INPUT_DATE_1": start_date_origin, "FID_INPUT_DATE_2": current_end_date,
                  "FID_PERIOD_DIV_CODE": "W", "FID_ORG_ADJ_PRC": "0"}
        data = _api().call_api(url_path, "domestic", "quotations", "chart", params=params, timeout=3)
        if data.get('rt_cd') == '0':
            items = data.get('output2')
            if items:
                all_items.extend(items)
                temp_dates = sorted([x['stck_bsop_date'] for x in items if x.get('stck_bsop_date')])
                if not temp_dates or temp_dates[0] <= start_date_origin:
                    break
                current_end_date = (datetime.strptime(temp_dates[0], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            else:
                break
            retry_count += 1
        elif data.get('msg_cd') == 'EGW00201':
            time.sleep(0.5)
            retry_count += 1
        else:
            time.sleep(0.2)
            break

    if not all_items:
        return pd.DataFrame()
    df = pd.DataFrame(all_items).drop_duplicates(subset=['stck_bsop_date'])
    df = df[df['stck_bsop_date'] >= start_date_origin]
    df = df[['stck_bsop_date', 'stck_clpr', 'stck_oprc', 'stck_hgpr', 'stck_lwpr', 'acml_vol']].copy()
    df.columns = ['date', 'close', 'open', 'high', 'low', 'volume']
    df = df.astype({'close': float, 'open': float, 'high': float, 'low': float, 'volume': float})
    return df.sort_values('date', ascending=True).reset_index(drop=True).tail(160)

def _fetch_kis_weekly_overseas(code, lookback_days=1100):
    """KIS 해외 주봉(GUBN='1'). 거래소 후보를 순회하며 날짜 구간을 뒤로 페이징한다."""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=lookback_days)).strftime("%Y%m%d")
    cached_ex = config.session.exchange_cache.get(code)
    exchanges = []
    if cached_ex: exchanges.append(cached_ex)
    for e in ["NAS", "NYS", "AMS", "NASD", "NYSE", "AMEX"]:
        if e not in exchanges: exchanges.append(e)
    url_path = constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["CHART"]

    for excd in exchanges:
        all_items = []
        next_bymd = today
        retry_count = 0
        while retry_count < 10:
            params = {"AUTH": "", "EXCD": excd, "SYMB": code, "GUBN": "1", "BYMD": next_bymd, "MODP": "1", "KEYB": code}
            data = _api().call_api(url_path, "overseas", "quotations", "chart", params=params, timeout=3)
            if data.get('rt_cd') == '0':
                items = data.get('output2')
                if items:
                    if not all_items and cached_ex != excd:
                        config.session.update_cache_and_save(code, excd)
                    all_items.extend(items)
                    last = items[-1]['xymd']
                    if last <= start_date_origin:
                        break
                    next_bymd = (datetime.strptime(last, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                else:
                    break
                retry_count += 1
            elif data.get('msg_cd') == 'EGW00201':
                time.sleep(0.5)
                retry_count += 1
            else:
                time.sleep(0.1)
                break

        if all_items:
            df = pd.DataFrame(all_items).drop_duplicates(subset=['xymd'])
            df.rename(columns={'xymd': 'date', 'clos': 'close', 'open': 'open', 'high': 'high', 'low': 'low'}, inplace=True)
            if 'tvol' in df.columns: df['volume'] = df['tvol']
            elif 'tovol' in df.columns: df['volume'] = df['tovol']
            elif 'vol' in df.columns: df['volume'] = df['vol']
            else: df['volume'] = 0
            df = df[df['date'] >= start_date_origin]
            for c in ['close', 'open', 'high', 'low', 'volume']: df[c] = df[c].astype(float)
            return df.sort_values('date', ascending=True).reset_index(drop=True).tail(160)
    return pd.DataFrame()

# ==========================================================
# [추가] 지수 전용 소스 라우팅 (지수 화면 ↔ 차트 분석 공유)
# ==========================================================
#  지수 화면(메뉴 1)은 국내 지수를 모드별 소스 체인(KIS/토스/tvDatafeed/yfinance)으로,
#  미국채 현물·HY OAS를 tvDatafeed 전용 소스로 조회한다. 차트 분석(메뉴 3-5)이 목록의
#  yfinance 티커를 그대로 쓰면 같은 지수인데 다른 값이 나오거나(코스피200·코스닥150),
#  자리표시자 티커(^VKOSPI·^K200FUT·^US02Y·^HYOAS·^KRXGOLD)는 아예 조회가 실패한다.
#  → get_chart_data가 코드만 보고 같은 소스를 고르도록 여기서 한 번에 판정한다.

# 국내 지수 내부 코드 (market.resolve_index_source가 돌려주는 값) → get_domestic_index_data
DOMESTIC_INDEX_SOURCE_CODES = (
    "KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150", "VKOSPI", "K200FUT_F", "K200FUT_CM",
)


def index_source_kind(code):
    """지수 전용 소스가 필요한 코드인지 판정한다.

    Returns: 'domestic'(국내 지수 소스 체인) | 'tv_spot'(미국채 현물) | 'fred'
             | 'krx_gold'(KRX 금현물, 네이버) | None(일반 경로)
    """
    if not code:
        return None
    if code in DOMESTIC_INDEX_SOURCE_CODES:
        return 'domestic'
    if code in config.US_TREASURY_SPOT_TICKERS:
        return 'tv_spot'
    if code in config.FRED_INDEX_TICKERS:
        return 'fred'
    if code in config.KRX_GOLD_TICKERS:
        return 'krx_gold'
    return None


def _to_chart_schema(df, start_date=None, max_rows=250):
    """지수 소스(analysis 계열) DataFrame을 차트 스키마로 변환한다.

    date는 datetime(tvDatafeed·토스)일 수도 YYYYMMDD 문자열(KIS)일 수도 있어 문자열로 통일한다.
    호출부가 공유 캐시 객체를 그대로 넘기므로 반드시 복사본을 만든다(원본 오염 방지).
    """
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    if 'date' not in d.columns:
        d = d.reset_index()
        d.columns = [str(c).lower() for c in d.columns]
        if 'date' not in d.columns:
            for cand in ('index', 'datetime', 'stck_bsop_date'):
                if cand in d.columns:
                    d = d.rename(columns={cand: 'date'})
                    break
    if 'date' not in d.columns:
        return pd.DataFrame()

    d['date'] = d['date'].apply(lambda x: x.strftime('%Y%m%d') if hasattr(x, 'strftime') else str(x).replace('-', '')[:8])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        if c not in d.columns:
            d[c] = 0.0
    d = d[['date', 'open', 'high', 'low', 'close', 'volume']].copy()
    if start_date:
        d = d[d['date'] >= start_date]
    return d.sort_values('date', ascending=True).reset_index(drop=True).tail(max_rows)


def _index_source_chart_data(code, kind, period_type='daily'):
    """지수 전용 소스에서 차트 데이터를 조회한다(일봉 / 주봉=일봉 리샘플링).

    네 소스 모두 일봉만 제공하므로 시봉·분봉은 빈 DataFrame을 돌려준다
    (호출부 chart.generate_visual_chart가 사전에 안내하고 차단한다).
    """
    if period_type in ('hourly', 'intraday'):
        return pd.DataFrame()

    from modules import analysis
    if kind == 'domestic':
        src = analysis.get_domestic_index_data(code)
    elif kind == 'tv_spot':
        src = analysis.get_us_treasury_spot_data(config.US_TREASURY_SPOT_TICKERS[code])
    elif kind == 'krx_gold':
        src = analysis.get_krx_gold_data(config.KRX_GOLD_TICKERS[code])
    else:
        src = analysis.get_fred_data(config.FRED_INDEX_TICKERS[code])

    if period_type == 'weekly':
        # 지수 소스는 네이티브 주봉이 없다 → 확보된 일봉(최대 ~300봉)을 주 단위로 리샘플링한다.
        #  (토스 개별종목 주봉과 동일한 방식. 기간은 야후 주봉 5년보다 짧다)
        return _resample_weekly(_to_chart_schema(src, max_rows=400))

    now = datetime.now()
    start_date = (now - timedelta(days=config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"])).strftime("%Y%m%d")
    return _to_chart_schema(src, start_date=start_date)


def _get_weekly_chart_data(code, is_overseas):
    """주봉 차트 데이터. KIS 네이티브 주봉(국내 W / 해외 GUBN=1)으로 ~3년치를 조회하고,
    KIS 주봉이 없는 경로(지수·환율·원자재는 yfinance 1wk, 토스 개별종목은 일봉 리샘플링)로 보강한다."""
    # [추가] 국내 지수·미국채 현물·HY OAS·KRX 금은 지수 화면과 동일한 전용 소스(일봉 리샘플링)를 쓴다.
    kind = index_source_kind(code)
    if kind:
        return _index_source_chart_data(code, kind, 'weekly')

    is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X')
                or code == 'DX-Y.NYB' or '-USD' in code or code.endswith('.SS') or code.endswith('.IL'))
    if is_index:
        def _fetch_yf_index_weekly():
            try:
                df = _api().fetch_yfinance_data(code, period="5y", interval="1wk")
                if df is None or df.empty:
                    return pd.DataFrame()
                flat_cols = [str(col[0]).lower() if isinstance(col, tuple) else str(col).lower() for col in df.columns]
                df.columns = flat_cols
                df.reset_index(inplace=True)
                df.columns = [str(c).lower() for c in df.columns]
                df = df.loc[:, ~df.columns.duplicated()].copy()
                cols = ['date', 'open', 'high', 'low', 'close', 'volume']
                for c in cols:
                    if c not in df.columns: df[c] = 0
                df = df[cols].copy()
                df['date'] = df['date'].apply(lambda x: x.strftime('%Y%m%d') if hasattr(x, 'strftime') else str(x).replace('-', '')[:8])
                return df.sort_values('date', ascending=True).reset_index(drop=True).tail(160)
            except Exception as e:
                logger.debug(f"yfinance weekly index fetch error: {e}")
                return pd.DataFrame()

        # [최적화] 지수 주봉도 메모리 캐싱한다. 일봉과 키가 겹치지 않게 '_W' 접미사를 붙이고,
        # 당일 '일봉' 패치용 오버레이는 주봉 캔들(주 시작일 date)에 맞지 않으므로 끈다.
        return _api()._get_cached_chart(f"{code}_W", is_overseas=True, is_index=True,
                                 fetch_func=_fetch_yf_index_weekly, realtime_overlay=False)

    # 토스 개별종목: 토스 캔들은 일/분봉만 제공 → 일봉을 주 단위로 리샘플링
    if config.session.is_toss:
        return _resample_weekly(get_chart_data(code, is_overseas, 'daily'))

    if not is_overseas:
        return _fetch_kis_weekly_domestic(code)
    return _fetch_kis_weekly_overseas(code)

def get_chart_data(code, is_overseas=False, period_type='daily', realtime=True):
    """
    기술적 분석을 위한 차트 데이터를 조회합니다.
    period_type: 'weekly' (주봉), 'daily' (일봉), 'hourly' (시봉), 'intraday' (분봉)
    realtime=False: 일봉 캐시 적중 시 현재가 오버레이를 생략한다(호출자가 직접 당일 캔들을 갱신하는 대량 조회용).
    """
    if period_type == 'weekly':
        return _get_weekly_chart_data(code, is_overseas)

    # [추가] 지수 전용 소스(국내 지수·미국채 현물·HY OAS·KRX 금)는 모드/티커와 무관하게 지수 화면과
    #  같은 소스로 조회한다. 일반 경로로 흘리면 KIS 종목 차트 TR·yfinance 자리표시자로 넘어가
    #  조회가 실패하거나 표와 다른 값이 나온다.
    _idx_kind = index_source_kind(code)
    if _idx_kind:
        return _index_source_chart_data(code, _idx_kind, period_type)

    # [추가] 토스: yfinance 대상(지수/원자재/환율 등)이 아닌 개별 종목은 토스 캔들로 조회
    _yf_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X')
                 or code == 'DX-Y.NYB' or '-USD' in code or code.endswith('.SS') or code.endswith('.IL'))
    if config.session.is_toss and not _yf_index:
        # [최적화] 일봉은 KIS 경로와 동일하게 _get_cached_chart로 6시간/디스크 캐싱한다.
        #  과거 일봉은 불변이므로 반복 조회(메뉴2 등) 시 토스 캔들 페이지네이션(최대 4콜)을 제거한다.
        #  당일 봉은 오버레이가 실시간 현재가로 갱신한다. 시봉/분봉은 KIS와 동일하게 캐시 제외.
        if period_type == 'daily':
            return _api()._get_cached_chart(
                code, is_overseas, is_index=False,
                fetch_func=lambda: _api()._toss_daily_chart_with_tv_fallback(code, is_overseas),
                realtime_overlay=realtime,
            )
        return _api()._toss_chart_data(code, period_type, is_overseas)

    if period_type == 'intraday':
        return _get_intraday_chart_data(code, is_overseas)
    
    if period_type == 'hourly':
        return _get_hourly_chart_data(code, is_overseas)

    now = datetime.now()
    today = now.strftime("%Y%m%d")
    start_date_origin = (now - timedelta(days=config.INDICATOR_PARAMS["CHART_LOOKBACK_DAYS"])).strftime("%Y%m%d")
    
    is_index = (code.startswith('^') or code.endswith('=F') or code.endswith('=X') or code == 'DX-Y.NYB' or '-USD' in code or code.endswith('.SS') or code.endswith('.IL'))
    if is_index:
        # [수정] yfinance에서 조회가 불가능한 국내 핵심 지수(코스피200, 코스닥150)는 
        # analysis 모듈을 통해 모드별(KIS/Toss/tvDatafeed) 적합한 소스에서 우회 조회한다.
        if code in ['^KS200', '^KQ150']:
            # 야후 티커로 들어와도 내부 코드와 같은 소스를 타게 한다(차트 분석은 이미
            #  market.resolve_index_source가 'KOSPI200'/'KOSDAQ150'으로 바꿔 넘긴다).
            m_type = "KOSPI200" if code == '^KS200' else "KOSDAQ150"
            return _index_source_chart_data(m_type, 'domestic', period_type)

        def _fetch_yf_index_daily():
            try:
                df = _api().fetch_yfinance_data(code, period="2y")
                if df is None or df.empty: return pd.DataFrame()

                # 1. 컬럼 평탄화 및 튜플 방어
                flat_cols = []
                for col in df.columns:
                    if isinstance(col, tuple):
                        flat_cols.append(str(col[0]).lower())
                    else:
                        flat_cols.append(str(col).lower())
                df.columns = flat_cols

                df.reset_index(inplace=True)
                df.columns = [str(c).lower() for c in df.columns]
                df = df.loc[:, ~df.columns.duplicated()].copy() # 중복 컬럼 제거 방어 로직 추가

                cols = ['date', 'open', 'high', 'low', 'close', 'volume']
                for c in cols:
                    if c not in df.columns: df[c] = 0
                df = df[cols].copy()
                df['date'] = df['date'].apply(lambda x: x.strftime('%Y%m%d') if hasattr(x, 'strftime') else str(x).replace('-', '')[:8])
                df = df[df['date'] >= start_date_origin]
                return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
            except Exception as e:
                logger.debug(f"yfinance 2y index fetch error: {e}")
                return pd.DataFrame()

        # [최적화] 지수/원자재/환율 일봉도 종목과 동일하게 메모리 캐싱한다(is_index=True → 디스크 제외).
        # 모두 yfinance 티커이므로 당일 봉 오버레이가 fast_info 경로를 타도록 is_overseas=True로 고정한다.
        return _api()._get_cached_chart(code, is_overseas=True, is_index=True,
                                 fetch_func=_fetch_yf_index_daily, realtime_overlay=realtime)

    if not is_overseas:
        # [최적화] 일봉 과거 데이터는 불변이므로 _get_cached_chart로 캐싱(당일 봉만 실시간 오버레이).
        # 반복 조회(메뉴2 등) 시 250봉 페이지네이션(~3콜/종목)을 제거한다.
        def _fetch_domestic_daily():
            url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["CHART"]
            all_items = []
            current_end_date = today
            current_start_date = start_date_origin

            retry_count = 0
            seen_dates = set()
            while len(all_items) < 250 and retry_count < 10:
                params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": current_start_date, "FID_INPUT_DATE_2": current_end_date, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"}
                data = _api().call_api(url_path, "domestic", "quotations", "chart", params=params, timeout=3)
                if data.get('rt_cd') == '0':
                    items = data.get('output2')
                    if items:
                        before = len(seen_dates)
                        seen_dates.update(x.get('stck_bsop_date') for x in items)
                        all_items.extend(items)
                        # [추가] 새 거래일이 하나도 없으면(같은 구간을 반복 응답) 더 받을 과거 봉이 없다.
                        #  종전에는 중복만 돌아와도 페이지 예산(10회)을 끝까지 소진했다.
                        #  상장 이력이 짧은 종목·ETF에서 헛도는 호출이 모의투자 2 TPS와 겹쳐
                        #  '데이터 수신' 단계가 특정 종목에서 오래 멈추는 원인이 됐다.
                        if len(seen_dates) == before:
                            break
                        temp_dates = sorted([x['stck_bsop_date'] for x in items])
                        next_end = (datetime.strptime(temp_dates[0], "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                        # [추가] 커서가 조회 시작일 이전으로 넘어가면 받을 것이 남아 있지 않다
                        if next_end < current_start_date:
                            break
                        current_end_date = next_end
                    else:
                        break
                    retry_count += 1
                elif data.get('msg_cd') == 'EGW00201':
                    time.sleep(0.5)
                    retry_count += 1
                else:
                    time.sleep(0.2)
                    break

            if not all_items: return pd.DataFrame()
            df = pd.DataFrame(all_items).drop_duplicates(subset=['stck_bsop_date'])
            df = df[df['stck_bsop_date'] >= start_date_origin]
            df = df[['stck_bsop_date', 'stck_clpr', 'stck_oprc', 'stck_hgpr', 'stck_lwpr', 'acml_vol']].copy()
            df.columns = ['date', 'close', 'open', 'high', 'low', 'volume']
            df = df.astype({'close': float, 'open': float, 'high': float, 'low': float, 'volume': float})
            return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)

        return _api()._get_cached_chart(code, is_overseas=False, is_index=False, fetch_func=_fetch_domestic_daily, realtime_overlay=realtime)

    else:
        def _fetch_overseas_daily():
            cached_ex = config.session.exchange_cache.get(code)
            # [수정] NASD/NYSE/AMEX는 NAS/NYS/AMS와 같은 거래소의 다른 표기라 6개를 다 도는 것은
            #  같은 곳을 두 번씩 묻는 낭비였다. 표준 코드 3개로 정규화해 최악 탐색 시간을 절반으로 줄인다.
            exchanges = _api().us_excd_probe_list(cached_ex)

            url_path = constants.API_URLS["OVERSEAS"]["QUOTATIONS"]["CHART"]

            for excd in exchanges:
                all_items = []
                seen_dates = set()
                next_bymd = today

                retry_count = 0
                while len(all_items) < 250 and retry_count < 10:
                    params = {"AUTH": "", "EXCD": excd, "SYMB": code, "GUBN": "0", "BYMD": next_bymd, "MODP": "1", "KEYB": code}
                    data = _api().call_api(url_path, "overseas", "quotations", "chart", params=params, timeout=3)
                    if data.get('rt_cd') == '0':
                        items = data.get('output2')
                        if items:
                            if not all_items:
                                if cached_ex != excd: config.session.update_cache_and_save(code, excd)
                            before = len(seen_dates)
                            seen_dates.update(i.get('xymd') for i in items)
                            all_items.extend(items)
                            # [추가] 새 거래일이 하나도 없으면(같은 구간을 반복 응답) 더 받을 과거 봉이 없다.
                            #  종전에는 중복만 돌아와도 페이지 예산(10회)을 끝까지 소진했다.
                            if len(seen_dates) == before:
                                break
                            last = items[-1]['xymd']
                            next_bymd = (datetime.strptime(last, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                            # [추가] 커서가 조회 시작일 이전으로 넘어가면 받을 것이 남아 있지 않다
                            if next_bymd < start_date_origin:
                                break
                        else:
                            break
                        retry_count += 1
                    elif data.get('msg_cd') == 'EGW00201':
                        time.sleep(0.5)
                        retry_count += 1
                    else:
                        time.sleep(0.1)
                        break

                if all_items:
                    df = pd.DataFrame(all_items).drop_duplicates(subset=['xymd'])
                    df.rename(columns={'xymd': 'date', 'clos': 'close', 'open': 'open', 'high': 'high', 'low': 'low'}, inplace=True)
                    if 'tvol' in df.columns: df['volume'] = df['tvol']
                    elif 'tovol' in df.columns: df['volume'] = df['tovol']
                    elif 'vol' in df.columns: df['volume'] = df['vol']
                    else: df['volume'] = 0
                    df = df[df['date'] >= start_date_origin]
                    numeric_cols = ['close', 'open', 'high', 'low', 'volume']
                    for c in numeric_cols: df[c] = df[c].astype(float)
                    return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)
            return pd.DataFrame()

        return _api()._get_cached_chart(code, is_overseas=True, is_index=False, fetch_func=_fetch_overseas_daily, realtime_overlay=realtime)

def _get_intraday_yfinance(code, is_overseas):
    """yfinance 1분봉 폴백. 해외/지수, 또는 국내라도 장전 등으로 KIS 당일분봉이 빌 때 사용.
    5일치를 받아 최근 390개(≈정규장 1세션)만 유지 → 장전이면 직전 거래일 세션이 된다."""
    try:
        # 국내 종목이 폴백을 탈 경우를 대비해 stock.json을 참조해 정확한 티커(.KS / .KQ) 생성
        target_ticker = code
        if not is_overseas and not code.startswith('^'):
            market_suffix = None
            for key in ["stocks_kr", "etfs_kr"]:
                for item in config.session.stock_data.get(key, []):
                    if item.get('code') == code and "exchange" in item:
                        if item['exchange'].upper() == "KOSDAQ":
                            market_suffix = ".KQ"
                        elif item['exchange'].upper() == "KOSPI":
                            market_suffix = ".KS"
                        break
                if market_suffix: break

            if market_suffix:
                target_ticker = f"{code}{market_suffix}"
            else:
                target_ticker = f"{code}.KS" # 기본값 코스피

        logger.info(f"[API] '{target_ticker}' yfinance 분봉 조회 시도 (Fallback)...")
        # yfinance는 1분봉 최대 7일, 5분봉 최대 60일 지원
        df = _api().fetch_yfinance_data(target_ticker, period="5d", interval="1m")
        if df is not None and not df.empty:
            # 1. 컬럼 평탄화 및 튜플 방어
            flat_cols = []
            for col in df.columns:
                if isinstance(col, tuple):
                    flat_cols.append(str(col[0]).lower())
                else:
                    flat_cols.append(str(col).lower())
            df.columns = flat_cols

            df.reset_index(inplace=True)

            # 2. 소문자 변환
            df.columns = [str(c).lower() for c in df.columns]
            df = df.loc[:, ~df.columns.duplicated()].copy() # 중복 컬럼 제거 방어 로직 추가
            if 'datetime' in df.columns: df.rename(columns={'datetime': 'date'}, inplace=True)

            # [추가] yfinance 시간대 변환 (UTC/현지시간 -> 한국 시간 KST)
            if 'date' in df.columns and pd.api.types.is_datetime64_any_dtype(df['date']):
                if df['date'].dt.tz is not None:
                    df['date'] = df['date'].dt.tz_convert('Asia/Seoul')

            cols = ['date', 'open', 'high', 'low', 'close', 'volume']
            for c in cols:
                if c not in df.columns: df[c] = 0

            df = df[cols].copy().sort_values('date', ascending=True)

            # 최근 390개 (약 6시간 30분 = 1일 장 운영 시간) 데이터만 유지
            if len(df) > 390:
                df = df.iloc[-390:]

            return df.reset_index(drop=True)
    except Exception as e:
        logger.error(f"[API] yfinance 분봉 조회 실패: {e}")
    return pd.DataFrame()


def _get_intraday_chart_data(code, is_overseas):
    """분봉(1분) 데이터 조회 (KIS API 사용, 해외/지수는 yfinance Fallback)"""

    # 1. KIS API 미지원 대상 확인 (해외주식, 지수 등)
    use_fallback = is_overseas
    if code.startswith('^') or (code.startswith('0001') and len(code) == 4):
        use_fallback = True

    # Fallback 로직 (yfinance)
    if use_fallback:
        return _get_intraday_yfinance(code, is_overseas)

    # 국내 주식 KIS API 1분봉 조회
    # URL: /uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice
    # TR_ID: FHKST03010200
    url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["TIME_CHART"]
    tr_id = "FHKST03010200"
    
    all_items = []
    current_time_key = "" # 빈 문자열 = 현재시간(최신)
    
    # [수정] 하루치(381분) 전체 조회를 위해 반복 횟수 및 최대 건수 상향 (3회 -> 20회)
    for i in range(20):
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": current_time_key,
            "FID_PW_DIV_CODE": "1", # 1분봉
            "FID_PW_DATA_INCU_YN": "N" # [추가] 필수 입력 필드 (데이터 포함 여부)
        }
        
        # [디버그] 요청 상세 로그 (태그 포함)
        logger.debug(f"[API_DEBUG] 분봉 조회 요청({i+1}): {code} | TimeKey: {current_time_key} | Params: {params}")
        
        # [추가] 모의투자 환경일 경우 TR ID 변경 (실전: FHKST03010200, 모의: 없음/지원안함 가능성 체크)
        # KIS 모의투자 API 문서를 보면 주식분봉조회는 실전/모의 동일하게 FHKST03010200을 사용하는 경우가 많으나,
        # 모의투자 서버의 경우 데이터가 없거나 다른 TR일 수 있음. 일단 실전용 TR 사용.
        res = _api().call_api(url_path, "domestic", "quotations", "time_chart", params=params, tr_id=tr_id)
        
        if res.get('rt_cd') == '0':
            items = res.get('output2', [])
            if items:
                all_items.extend(items)
                # 다음 페이징을 위해 마지막 데이터의 시간 사용
                last_item = items[-1]
                current_time_key = last_item.get('stck_cntg_hour')
                
                # [수정] 하루 장 운영 시간(09:00~15:30) 커버를 위해 420건으로 상향
                if len(all_items) >= 420: break
            else:
                logger.debug(f"[API_DEBUG] 분봉 조회 결과 없음 (반복 중단): {res.get('msg1')}")
                break
        else:
            logger.error(f"[API] 분봉 조회 실패: {res.get('msg1')} (Code: {res.get('msg_cd')})")
            break
            
        time.sleep(0.1) # Rate Limit
    
    if not all_items:
        # 장 시작 전/휴장 등으로 당일 분봉이 없으면 빈 값 반환 (호출부에서 장전 안내 처리)
        return pd.DataFrame()

    df = pd.DataFrame(all_items)

    # 컬럼 매핑 및 정제
    # stck_bsop_date: 일자, stck_cntg_hour: 시간
    # stck_prpr: 현재가, stck_oprc: 시가, stck_hgpr: 고가, stck_lwpr: 저가, cntg_vol: 체결량
    cols_map = {
        'stck_bsop_date': 'date_str',
        'stck_cntg_hour': 'time_str',
        'stck_prpr': 'close',
        'stck_oprc': 'open',
        'stck_hgpr': 'high',
        'stck_lwpr': 'low',
        'cntg_vol': 'volume'
    }
    
    # 필요한 컬럼만 존재 시 이름 변경
    df.rename(columns=cols_map, inplace=True)
    df = df[list(cols_map.values())].copy()
    
    # 날짜+시간 병합 (YYYYMMDD + HHMMSS)
    df['date'] = pd.to_datetime(df['date_str'] + df['time_str'], format='%Y%m%d%H%M%S')
    
    # 수치형 변환
    numeric_cols = ['close', 'open', 'high', 'low', 'volume']
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c])
        
    df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
    
    # 시간순 정렬 (과거 -> 현재)
    return df.sort_values('date', ascending=True).reset_index(drop=True)
