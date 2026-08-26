"""지수와 선물 — 코스피/코스닥 지수, K200 선물.

지수는 모드에 따라 소스가 갈린다(KIS · 토스 모드에서는 tvDatafeed). 시장 필터와
국면 판정의 입력이라, 소스가 바뀌어도 같은 스키마로 나가야 한다.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import config
from core import constants
from brokers import toss_api

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

# KIS 지수 코드 → yfinance 티커 (토스 모드 폴백용)
_INDEX_KIS_TO_YF = {"0001": "^KS11", "1001": "^KQ11", "2001": "^KS200", "2203": "^KQ150"}

# KIS 지수 코드 → 토스 시장지표 심볼 (토스 API 1.2.4 /market-indicators).
#  KRX 공식 지수를 그대로 주므로 토스 모드의 1순위 소스다. 코스피200(2001)·코스닥150(2203)은
#  토스 심볼 카탈로그에 없어(400 unsupported-symbol) 종전 폴백(tvDatafeed→yfinance)을 그대로 쓴다.
_INDEX_KIS_TO_TOSS = {"0001": "KOSPI", "1001": "KOSDAQ"}


def _toss_index_chart_data(symbol, target=260):
    """토스 시장지표 일봉 → ['date','open','high','low','close','volume'] DataFrame.

    date=YYYYMMDD 문자열, 오름차순(KIS 지수 일봉과 동일 스키마). 실패/무응답 시 빈 DF.
    호출당 최대 200봉이라 nextBefore 커서로 EMA120·52주에 충분한 분량을 모은다.
    """
    candles = []
    before = None
    prev_cursor = None
    try:
        for _ in range(3):
            res = toss_api.get_market_indicator_candles(symbol, interval="1d",
                                                        count=200, before=before)
            batch = (res or {}).get('candles', []) or []
            if not batch:
                break
            candles.extend(batch)
            if len(candles) >= target:
                break
            nb = (res or {}).get('nextBefore')
            oldest = min((str(c.get('timestamp', '')) for c in batch if c.get('timestamp')),
                         default=None)
            before = nb or oldest
            if not before or before == prev_cursor:   # 커서 정체 → 무한루프 방지
                break
            prev_cursor = before
    except toss_api.TossApiError as e:
        logger.warning(f"[Toss] 지수({symbol}) 일봉 조회 실패: {e}")
        return pd.DataFrame()

    if not candles:
        logger.warning(f"[Toss] 지수({symbol}) 일봉 조회 결과 없음")
        return pd.DataFrame()

    rows = [{
        'date': str(c.get('timestamp', ''))[:10].replace('-', ''),
        'open': _api()._toss_float(c.get('openPrice')),
        'high': _api()._toss_float(c.get('highPrice')),
        'low': _api()._toss_float(c.get('lowPrice')),
        'close': _api()._toss_float(c.get('closePrice')),
        'volume': _api()._toss_float(c.get('volume')),
    } for c in candles]
    df = pd.DataFrame(rows)
    df = df[df['date'] != ''].drop_duplicates(subset=['date'])
    return df.sort_values('date', ascending=True).reset_index(drop=True).tail(250)


def get_domestic_index_chart(code):
    """업종/지수 기간별 시세(일봉) 조회 (KIS API / 토스 모드는 시장지표 API·yfinance)"""
    # [추가] 토스: KIS 미사용. 코스피·코스닥은 토스 시장지표(1.2.4), 그 외는 yfinance 티커 매핑.
    if config.session.is_toss:
        toss_symbol = _INDEX_KIS_TO_TOSS.get(str(code))
        if toss_symbol:
            # KIS 경로와 동일하게 캐시+당일봉 실시간 오버레이를 태운다(오버레이는 토스 지수 현재가 사용).
            return _api()._get_cached_chart(code, is_overseas=False, is_index=True,
                                     fetch_func=lambda: _toss_index_chart_data(toss_symbol))
        yf_ticker = _INDEX_KIS_TO_YF.get(str(code))
        if not yf_ticker:
            return pd.DataFrame()
        try:
            df = _api().get_chart_data(yf_ticker, is_overseas=True)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.debug(f"[Toss] 지수 차트 yfinance 조회 실패({code}): {e}")
            return pd.DataFrame()

    def fetch_func():
        # 지수/업종 차트 조회 URL 및 TR_ID (실전/모의 동일)
        url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_CHART"]
        tr_id = "FHKUP03500100"

        now = datetime.now()
        today = now.strftime("%Y%m%d")
        start_date = (now - timedelta(days=730)).strftime("%Y%m%d") # 2년치 조회

        def _fetch_pages(api_caller, log_fail=True):
            """기간 분할 페이지네이션 공통 루프 (api_caller: params → 응답 dict)"""
            all_items = []
            current_end_date = today
            retry_count = 0
            while len(all_items) < 300 and retry_count < 10:
                params = {
                    "FID_COND_MRKT_DIV_CODE": "U",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": start_date,
                    "FID_INPUT_DATE_2": current_end_date,
                    "FID_PERIOD_DIV_CODE": "D"
                }
                data = api_caller(params)
                if data.get('rt_cd') == '0':
                    # 빈 행/None 응답 방어
                    items = [it for it in (data.get('output2') or []) if it.get('stck_bsop_date')]
                    if items:
                        all_items.extend(items)
                        last_date = items[-1]['stck_bsop_date']
                        current_end_date = (datetime.strptime(last_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                        retry_count += 1
                        time.sleep(0.1)
                    else:
                        break
                elif data.get('msg_cd') == 'EGW00201':
                    time.sleep(0.5)
                    retry_count += 1
                else:
                    if not all_items and log_fail:
                        logger.warning(f"[API] 지수({code}) 조회 실패: {data.get('msg1')} (Code: {data.get('msg_cd')})")
                    break
            return all_items

        # 모의서버 업종 TR은 'MCI전송 오류(OPSQ0008)' 등으로 간헐 실패한다. 모의 모드에서
        # 실전 서버는 사용하지 않으며(운영 방침), 실패 시 상위 폴백 체인(tvDatafeed→yfinance)이
        # 코스피·코스닥·코스피200·코스닥150을 받쳐준다 (VKOSPI는 폴백이 없어 모드 1 목록 제외).
        all_items = _fetch_pages(
            lambda p: _api().call_api(url_path, "domestic", "quotations", "index_chart", params=p, tr_id=tr_id, retries=0)
        )

        if all_items:
            df = pd.DataFrame(all_items)
            df.drop_duplicates(subset=['stck_bsop_date'], inplace=True)
            df = df[['stck_bsop_date', 'bstp_nmix_prpr', 'bstp_nmix_oprc', 'bstp_nmix_hgpr', 'bstp_nmix_lwpr', 'acml_vol']].copy()
            df.columns = ['date', 'close', 'open', 'high', 'low', 'volume']
            df = df.astype({'close': float, 'open': float, 'high': float, 'low': float, 'volume': float})
            df = df.sort_values('date', ascending=True).reset_index(drop=True)

            # [Fix] 개장 전에는 아직 당일 세션이 없는데도 KIS 업종 일봉은 당일 행을 전일 종가
            #  그대로(거래량 0) 채워 내려준다. 이 행을 남기면 마지막 두 봉의 종가가 같아져
            #  등락률이 0%로 굳고, EMA·RSI·CCI가 같은 종가를 한 번 더 먹어 지표까지 틀어진다.
            #  (실측 2026-07-28 KOSPI: EMA5 6,813→6,794, CCI -85.9→-74.0)
            #  게다가 그 df가 지수 공유 캐시에 실리면 장중 내내 '어제 값 + 0%'로 보인다.
            #  국내 지수는 NXT 연장거래가 없어 개장 전 당일 행은 언제나 가짜다 → 제거한다.
            if len(df) >= 2 and _api()._before_krx_regular_open() and str(df.iloc[-1]['date']) == _api().market_today(False):
                df = df.iloc[:-1].reset_index(drop=True)
            return df

        return pd.DataFrame()

    return _api()._get_cached_chart(code, is_overseas=False, is_index=True, fetch_func=fetch_func)

def get_domestic_index_price(code):
    """업종/지수 현재가 조회 (KIS API / 토스 모드는 시장지표 API·yfinance)"""
    # [추가] 토스: KIS 미사용. 코스피·코스닥은 토스 시장지표(1.2.4) 실시간 지수를 KIS 형태로 반환.
    if config.session.is_toss:
        toss_symbol = _INDEX_KIS_TO_TOSS.get(str(code))
        if toss_symbol:
            cache_key = f"toss_idx_price_{toss_symbol}"
            cached = _api()._get_micro_cache(cache_key)
            if cached:
                return cached
            try:
                row = toss_api.get_market_indicator_price(toss_symbol)
                curr = _api()._toss_float((row or {}).get('lastPrice'))
                if curr > 0:
                    # 토스 시장지표는 전일 종가를 주지 않는다 → 0으로 두어 상위의 수정주가 검증을
                    # 건너뛰게 한다(임의 값을 넣으면 정상 캐시를 오탐 파기한다).
                    res = {'rt_cd': '0', 'output': {
                        'bstp_nmix_prpr': str(curr),
                        'bstp_nmix_prdy_clpr': '0',
                    }}
                    _api()._set_micro_cache(cache_key, res)
                    return res
            except Exception as e:
                logger.debug(f"[Toss] 지수 현재가 조회 실패({toss_symbol}): {e}")
            # 토스 시장지표 실패 시 yfinance로 폴백 (아래 공통 경로)

        yf_ticker = _INDEX_KIS_TO_YF.get(str(code))
        if not yf_ticker:
            return {'rt_cd': '9999'}
        try:
            fi = _api().get_yf_fast_info(yf_ticker)
            if not fi:
                return {'rt_cd': '9999'}
            curr = fi.get('last_price')
            prev = fi.get('regular_market_previous_close')
            if curr is None:
                return {'rt_cd': '9999'}
            return {'rt_cd': '0', 'output': {
                'bstp_nmix_prpr': str(curr),
                'bstp_nmix_prdy_clpr': str(prev if prev is not None else curr),
            }}
        except Exception as e:
            logger.debug(f"[Toss] 지수 현재가 yfinance 조회 실패({code}): {e}")
            return {'rt_cd': '9999'}

    cache_key = f"idx_price_{code}"
    cached = _api()._get_micro_cache(cache_key)
    if cached: return cached

    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["INDEX_PRICE"]
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": code
    }
    res = _api().call_api(url, "domestic", "quotations", "index_price", params=params)
    if res.get('rt_cd') == '0':
        # [Fix] 업종 현재가 TR은 '전일 종가'를 직접 주지 않는다(현재가 - 전일대비로만 구할 수 있다).
        #  호출측(차트 오버레이의 수정주가 검증, 서킷브레이커 등락률, 토스 경로)이 공통으로
        #  bstp_nmix_prdy_clpr를 읽으므로 여기서 계산해 채운다. 비워 두면 전일 종가 0으로
        #  등락률 산출이 조용히 실패한다.
        out = res.get('output') or {}
        try:
            prpr = float(str(out.get('bstp_nmix_prpr', '')).replace(',', '') or 0)
            vrss = float(str(out.get('bstp_nmix_prdy_vrss', '')).replace(',', '') or 0)
            if prpr > 0 and not out.get('bstp_nmix_prdy_clpr'):
                out['bstp_nmix_prdy_clpr'] = f"{prpr - vrss:.2f}"
        except (TypeError, ValueError):
            pass
        _api()._set_micro_cache(cache_key, res)
    return res

# ==========================================================
# [추가] 코스피200 선물 (주간 F / 야간 CM) 시세 — KIS 전용
#  - 종목코드는 주간/야간 공통(예: A01609), 시장분류코드로 세션을 가른다.
#  - 야간(KRX 야간파생시장, 18:00~익일 06:00)은 FID_COND_MRKT_DIV_CODE='CM'.
# ==========================================================
def get_k200_futures_front_code(now=None):
    """코스피200 선물 근월물 종목코드(예: 'A01609')를 계산한다.

    결제월은 3/6/9/12월, 만기(최종거래일)는 결제월 두 번째 목요일.
    만기일 주간장 마감(15:45) 이후에는 차근월물로 롤오버한다.
    (마스터파일 fo_cme_code.mst의 코드 체계: 'A01' + 연도 끝자리 + 결제월 2자리)
    """
    if now is None:
        now = datetime.now()
    d = now.date()
    y, m = d.year, d.month
    for _ in range(8):
        qm = ((m - 1) // 3 + 1) * 3  # 3/6/9/12월로 올림
        first_wd = datetime(y, qm, 1).weekday()
        expiry = datetime(y, qm, 1 + (3 - first_wd) % 7 + 7).date()  # 두 번째 목요일
        if d < expiry or (d == expiry and now.hour < 16):
            return f"A01{y % 10}{qm:02d}"
        # 만기 경과 → 다음 분기월로 이동
        m = qm + 1
        if m > 12:
            m = 1
            y += 1
    return None

def _call_k200_futures_api(url_path, action, tr_id, params):
    """국내선물옵션 시세 TR 호출 (조회 전용, 실전 모드 전용).

    모의투자 서버는 선물 TR을 지원하지 않는다(현재가 HTTP 500, 차트 'MCI전송 오류').
    모의 모드에서 실전 서버를 사용하지 않는다는 운영 방침에 따라 우회 없이 실패를 반환하며,
    코스피200선물 지수는 표시 계층(market)에서 모드 1/토스 시 목록에서 제외된다.
    """
    return _api().call_api(url_path, "domestic", "quotations", action, params=params, tr_id=tr_id, retries=0)

def get_k200_futures_quote(mrkt_div_code, iscd):
    """코스피200 선물 현재가/전일대비/등락률 조회 (FHMIF10000000).

    mrkt_div_code: 'F'(주간) / 'CM'(야간). 야간 등락률은 주간 종가 대비(KIS 제공값 그대로).
    성공 시 {'current','diff','rate'} dict, 실패 시 None.
    """
    if config.session.is_toss:
        return None
    url = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["FUT_PRICE"]
    params = {"FID_COND_MRKT_DIV_CODE": mrkt_div_code, "FID_INPUT_ISCD": iscd}
    data = _call_k200_futures_api(url, "fut_price", "FHMIF10000000", params)
    if data.get('rt_cd') == '0':
        out = data.get('output1') or {}
        try:
            current = float(out.get('futs_prpr'))
            diff = float(out.get('futs_prdy_vrss'))
            rate = float(out.get('futs_prdy_ctrt'))
            if current > 0:
                return {'current': current, 'diff': diff, 'rate': rate}
        except (TypeError, ValueError):
            pass
    else:
        logger.debug(f"[API] K200선물 시세({mrkt_div_code}/{iscd}) 조회 실패: {data.get('msg1')}")
    return None

def get_k200_futures_chart(mrkt_div_code, iscd):
    """코스피200 선물 일봉 조회 (FHKIF03020100, 1콜 최대 100건 → 기간 분할로 최대 ~300봉).

    mrkt_div_code: 'F'(주간) / 'CM'(야간). 반환 스키마는 지수 차트와 동일:
    ['date','open','high','low','close','volume'] (오름차순, attrs['source']='KIS').
    근월물 상장기간이 짧으면 확보되는 만큼만 반환한다.
    """
    if config.session.is_toss:
        return pd.DataFrame()

    url_path = constants.API_URLS["DOMESTIC"]["QUOTATIONS"]["FUT_CHART"]
    now = datetime.now()
    all_items = []
    current_end_date = now.strftime("%Y%m%d")
    retry_count = 0
    while len(all_items) < 300 and retry_count < 10:
        params = {
            "FID_COND_MRKT_DIV_CODE": mrkt_div_code,
            "FID_INPUT_ISCD": iscd,
            "FID_INPUT_DATE_1": (now - timedelta(days=730)).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": current_end_date,
            "FID_PERIOD_DIV_CODE": "D"
        }
        data = _call_k200_futures_api(url_path, "fut_chart", "FHKIF03020100", params)
        if data.get('rt_cd') == '0':
            items = data.get('output2', [])
            # 빈 행(과거 미상장 구간) 제거
            items = [it for it in items if it.get('stck_bsop_date')]
            if items:
                all_items.extend(items)
                last_date = items[-1]['stck_bsop_date']
                current_end_date = (datetime.strptime(last_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                retry_count += 1
                time.sleep(0.1)
            else:
                break
        elif data.get('msg_cd') == 'EGW00201':
            time.sleep(0.5)
            retry_count += 1
        else:
            if not all_items:
                logger.warning(f"[API] K200선물 차트({mrkt_div_code}/{iscd}) 조회 실패: {data.get('msg1')} (Code: {data.get('msg_cd')})")
            break

    if not all_items:
        return pd.DataFrame()

    df = pd.DataFrame(all_items)
    df.drop_duplicates(subset=['stck_bsop_date'], inplace=True)
    rename_map = {
        'stck_bsop_date': 'date',
        'futs_prpr': 'close',
        'futs_oprc': 'open',
        'futs_hgpr': 'high',
        'futs_lwpr': 'low',
        'acml_vol': 'volume'
    }
    df.rename(columns=rename_map, inplace=True)
    for col in ['close', 'open', 'high', 'low', 'volume']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        else:
            df[col] = 0.0
    df = df[['date', 'open', 'high', 'low', 'close', 'volume']].dropna(subset=['close'])
    df = df.sort_values('date', ascending=True).reset_index(drop=True)
    df.attrs['source'] = 'KIS'
    return df
