"""yfinance·TradingView 시세 계층 + 초단기 마이크로 캐시.

해외 시세의 1차 폴백 경로이며, 화면 렌더링 중 같은 종목이 여러 번 조회되는 것을
막는 마이크로 캐시(TTL 수 초)도 여기 있다. yfinance 는 타임존 캐시 SQLite 에
동시 쓰기가 나면 'database is locked' 로 실패하므로 _YF_LOCK 으로 직렬화한다.
"""
import logging
import os
import threading
import time
import pandas as pd
import yfinance as yf
import caching
import config
import context

#  로거 이름은 분해 전(api.py)과 같은 'api' 로 둔다 — 로그 필터·레벨 설정이 이름을 보므로
#  서브모듈마다 다른 이름을 쓰면 기존 설정이 조용히 빗나간다.
logger = logging.getLogger("api")
# [추가] yfinance 호출 직렬화용 전역 락.
#  yfinance는 종목 timezone을 ~/.cache(또는 ~/Library/Caches)/py-yfinance 아래 SQLite에
#  캐싱하는데, 여러 스레드가 동시에 yfinance를 호출하면 이 캐시 DB에 동시 쓰기가 발생해
#  OperationalError('database is locked')로 다운로드가 실패한다(특히 모의투자처럼 해외 지수
#  fallback 의존도가 높을 때 빈번). 해외 yfinance 진입점을 이 락으로 직렬화해 경합을 차단한다.
#  (국내 지수의 KIS 조회는 락 대상이 아니므로 병렬성은 유지된다)
_YF_LOCK = threading.Lock()

def _api():
    """패키지 네임스페이스(api)를 돌려준다 — 다른 계층의 이름은 반드시 이걸 통해 부른다.

    분해 전에는 전부 한 모듈이었으므로 테스트의 patch.object(api, 'X') 가 모든 호출부에
    걸렸다. 서브모듈이 상대 모듈을 직접 import 하면 그 patch 가 닿지 않는다 —
    같은 규약을 쓰는 modules/auto_trade 의 _pkg() 와 같은 이유다.
    """
    import api
    return api

def _is_screen_output_allowed():
    """화면 출력 허용 여부 확인 (텔레그램 봇 스레드 차단) — context 공용 판정으로 위임"""
    return context.is_screen_output_allowed()

def clear_yfinance_cache():
    """yfinance 캐시 파일(.sqlite)을 강제로 삭제하여 DB Lock 문제를 해결합니다."""
    if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
        config.console.print("[dim cyan][DEBUG] yfinance 캐시 정리 시도...[/dim cyan]")
    
    possible_paths = [
        os.path.join(os.path.expanduser("~"), ".cache", "py-yfinance"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "py-yfinance"),
        os.path.join(os.path.expanduser("~"), "Library", "Caches", "py-yfinance")
    ]
    
    deleted_count = 0
    for c_path in possible_paths:
        if os.path.exists(c_path):
            try:
                for f in os.listdir(c_path):
                    if f.endswith('.sqlite') or f.endswith('.sqlite-journal'):
                        try:
                            os.remove(os.path.join(c_path, f))
                            deleted_count += 1
                        except Exception as e:
                            logger.debug(f"clear_yfinance_cache file remove error: {e}")
            except Exception as e:
                logger.debug(f"clear_yfinance_cache directory access error: {e}")
    
    if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL == "DEBUG" and deleted_count > 0:
        config.console.print(f"[dim cyan][DEBUG] 캐시 파일 {deleted_count}개 삭제 완료[/dim cyan]")

def fetch_yfinance_data(tickers, period=None, start=None, end=None, interval="1d", group_by='column', _retried=False, threads=False):
    """yfinance 데이터 조회 통합 함수.

    [DB Lock 대응]
    - 전역 _YF_LOCK으로 호출을 직렬화하여 tz 캐시(SQLite) 동시 접근 경합을 차단한다.
    - 최신 yfinance는 'database is locked'를 예외로 던지지 않고 내부에서 삼킨 뒤
      빈 DataFrame을 반환하므로(예외 핸들러 우회), 결과가 비어 있으면 캐시를 정리하고
      1회 재시도한다. (_retried 플래그로 무한 재귀 방지)

    threads: 다중 티커 요청 시 yfinance 내부 병렬 다운로드 허용 여부.
      _YF_LOCK이 '외부 호출자 간' 경합은 계속 직렬화하므로, 시장 지수처럼 티커가 많은
      일괄 조회는 True로 켜면 티커당 순차 왕복(N회)이 병렬로 줄어 수 배 빨라진다.
      (결과 데이터는 동일. 빈 응답/DB Lock 재시도 시엔 안전하게 순차(False)로 폴백)
    """
    try:
        with _YF_LOCK:
            df = yf.download(tickers, period=period, start=start, end=end, interval=interval, group_by=group_by, progress=False, threads=threads)

        # [추가] 빈 결과(= tz 캐시 lock 등으로 인한 조용한 실패 가능성) → 캐시 정리 후 1회 재시도
        if not _retried and (df is None or getattr(df, 'empty', True)):
            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim yellow]yfinance 빈 응답({tickers}). 캐시 정리 후 1회 재시도합니다.[/dim yellow]")
            clear_yfinance_cache()
            time.sleep(0.5)  # 파일 잠금 해제 대기
            return fetch_yfinance_data(tickers, period, start, end, interval, group_by, _retried=True)
        return df
    except Exception as e:
        err_msg = str(e).lower()
        if not _retried and ("database" in err_msg or "lock" in err_msg):
            if _is_screen_output_allowed() and config.SCREEN_DEBUG_LEVEL in ["TRACE", "DEBUG"]:
                config.console.print(f"[dim yellow]yfinance DB Lock 감지: {e}. 캐시 정리 후 재시도합니다.[/dim yellow]")
            clear_yfinance_cache()
            time.sleep(0.5) # 파일 잠금 해제 대기
            return fetch_yfinance_data(tickers, period, start, end, interval, group_by, _retried=True)
        raise e

# ==========================================================
# [추가] 실시간 단건 API용 초단기 마이크로 캐시 (Micro-Cache)
# 화면 렌더링 중 발생하는 동일 종목의 동시다발적 중복 호출 방지 (TTL: 3~10초)
# ==========================================================
# [메모리] 항목 상한(라즈베리파이 OOM 방어). 초과 시 가장 오래된 항목부터 제거한다.
# 전체 종목(코스피+코스닥 ~2800)을 스캔해도 종목당 cp_/vol_/ob_/yf_fi_ 정도라 여유 있게 잡는다.
_MICRO_CACHE_MAX = 6000
_MICRO_CACHE = caching.TTLCache(max_size=_MICRO_CACHE_MAX)
_MICRO_CACHE_LOCK = _MICRO_CACHE._lock  # 하위 호환 별칭 (기존 코드/테스트의 with 락 사용처)

def _get_micro_cache(key, ttl=60.0): # [수정] 잦은 중복 호출 방지를 위해 기본 TTL 상향
    return _MICRO_CACHE.get(key, ttl)

def _evict_oldest(cache, max_size, time_key):
    """딕셔너리 캐시가 상한을 넘으면 가장 오래된 항목들을 제거해 90% 수준으로 낮춘다.
    (eviction 빈도를 줄이기 위해 한 번에 여유분까지 비운다. 호출자가 락을 보유한 상태여야 한다)"""
    if len(cache) <= max_size:
        return
    drop = len(cache) - int(max_size * 0.9)
    for k in sorted(cache, key=lambda k: cache[k].get(time_key, 0))[:drop]:
        cache.pop(k, None)

def _set_micro_cache(key, data):
    _MICRO_CACHE.set(key, data)  # 상한 초과 시 자동 eviction (TTLCache 내장)

# [추가] yfinance 특수 티커를 TradingView 티커로 완벽히 1:1 매핑
YF_TO_TV_EXACT = {
    # [수정] 미국 3대 지수는 TradingView(15분 지연) 대신 yfinance(실시간)를 사용하도록 매핑 해제
    # "^IXIC": "NASDAQ:IXIC",        # 나스닥
    # "^GSPC": "SP:SPX",             # S&P 500
    # "^DJI": "DJ:DJI",              # 다우존스
    # "^RUT": "RUSSELL:RUT",         # 러셀 2000
    "KRW=X": "FX_IDC:USDKRW",      # 원/달러 환율
    "DX-Y.NYB": "TVC:DXY",         # 달러인덱스
    "CL=F": "NYMEX:CL1!",          # WTI 원유
    # [Fix] 브렌트유는 NYMEX가 아니라 ICE 유럽(ICEEUR) 상장 — 실측상 NYMEX:BRN1!은 빈 응답이고
    #  ICEEUR:BRN1!만 시세를 준다(2026-07-28). 종전 스크리너 경로가 통째로 죽어 있어 드러나지 않았다.
    "BZ=F": "ICEEUR:BRN1!",        # 브렌트유
    "GC=F": "COMEX:GC1!",          # 금
    "SI=F": "COMEX:SI1!",          # 은
    "HG=F": "COMEX:HG1!",          # 구리
    "NG=F": "NYMEX:NG1!",          # 천연가스
    "ZW=F": "CBOT:ZW1!",           # 밀
    "^VIX": "CBOE:VIX",            # 변동성 지수
    "^HYOAS": "FRED:BAMLH0A0HYM2", # 하이일드 OAS 스프레드
    "BTC-USD": "CRYPTO:BTCUSD",    # 비트코인
    "ETH-USD": "CRYPTO:ETHUSD",    # 이더리움
    "^TNX": "TVC:US10Y",           # 미국채 10년물
    "^FVX": "TVC:US05Y",           # 미국채 5년물
    "^TYX": "TVC:US30Y",           # 미국채 30년물
    "ZT=F": "CBOT:ZT1!",           # 미국채 2년물 선물
    "ZF=F": "CBOT:ZF1!",           # 미국채 5년물 선물
    "ZN=F": "CBOT:ZN1!",           # 미국채 10년물 선물
    "ZB=F": "CBOT:ZB1!"            # 미국채 30년물 선물
}

# ==========================================================
# [폴백] TV 매핑 심볼(지수·선물·환율)의 tvDatafeed 시세
# ==========================================================
#  종전에는 이 심볼들을 tradingview_screener의 Query().get_tickers()로 조회했는데,
#  설치된 tradingview-screener 3.x에는 그 메서드가 없어(2.x API) AttributeError가 나고
#  바깥 except가 조용히 삼켜, YF_TO_TV_EXACT 매핑 전체가 무효인 채 yfinance로만 동작했다.
#  스크리너는 미국 '주식' 스캔 유니버스라 set_tickers로 고쳐도 지수·선물이 나오지 않는다
#  (실측: CBOE:VIX·TVC:DXY·COMEX:GC1!·TVC:US10Y 모두 빈 응답, set_markets('index')는 404).
#  이미 미국채 현물·국내 지수에 쓰고 있는 tvDatafeed로 경로를 옮긴다.
#
#  [순서] yfinance를 1차로 두고 tvDatafeed는 폴백이다. tvDatafeed는 웹소켓이라 전역 락으로
#   직렬화되고 간헐 타임아웃이 잦아, 지수 화면의 20여 심볼을 여기에 태우면 라즈베리파이에서
#   체감 지연이 커진다. 실측상 이 심볼들은 yfinance가 평상시 정상이므로, TV는 yfinance가
#   실패한 순간에만 값을 메우는 역할이 맞다.
_TVD_QUOTE_FAIL = {}            # tv_symbol -> 마지막 실패 시각(time.time)
_TVD_QUOTE_NEG_TTL_SEC = 600    # 실패 심볼 재시도 쿨다운(웹소켓 재연결 비용 절감)


def _fast_info_via_tvdatafeed(tv_symbol):
    """'EXCHANGE:SYMBOL' 일봉 마지막 2봉으로 (현재가, 전일종가)를 만든다. 실패 시 None.

    tvDatafeed 일봉은 장중에도 마지막 봉이 갱신되므로 현재가로 쓸 수 있다.
    다만 프리/애프터 세션가는 제공하지 않으므로 src는 스크리너('tv')와 구분해 'tvd'로 둔다
    (해외 종목 장외 폴백 _overseas_tv_fallback_price가 src=='tv'만 채택한다).
    """
    if not tv_symbol or ':' not in tv_symbol:
        return None
    last_fail = _TVD_QUOTE_FAIL.get(tv_symbol)
    if last_fail and (time.time() - last_fail) < _TVD_QUOTE_NEG_TTL_SEC:
        return None
    try:
        from modules import analysis as _analysis
        from tvDatafeed import Interval
    except Exception:
        return None
    tv = _analysis._get_tvdatafeed()
    if tv is None:
        return None

    exchange, symbol = tv_symbol.split(':', 1)
    df = None
    try:
        with _analysis._TVDATAFEED_LOCK:
            df = tv.get_hist(symbol=symbol, exchange=exchange,
                             interval=Interval.in_daily, n_bars=5)
    except Exception as e:
        logger.debug(f"[TVDATAFEED] {tv_symbol} 시세 조회 오류: {e}")

    if df is None or df.empty or len(df) < 2:
        _TVD_QUOTE_FAIL[tv_symbol] = time.time()
        return None
    try:
        last_price = float(df['close'].iloc[-1])
        prev_close = float(df['close'].iloc[-2])
        if not (last_price > 0):
            raise ValueError("invalid close")
        year_high = float(df['high'].max()) if 'high' in df.columns else None
    except Exception as e:
        logger.debug(f"[TVDATAFEED] {tv_symbol} 시세 변환 실패: {e}")
        _TVD_QUOTE_FAIL[tv_symbol] = time.time()
        return None

    _TVD_QUOTE_FAIL.pop(tv_symbol, None)
    return {
        'last_price': last_price,
        'regular_market_previous_close': prev_close,
        'last_volume': 0,
        # n_bars=5의 최고가라 52주 고점이 아니다 → None으로 두어 호출부가 일봉 기준을 쓰게 한다
        'year_high': None,
        'src': 'tvd',
        'is_extended': False,
    }


# yfinance·TV가 모두 실패했을 때 직전 성공값을 재사용하는 유예 시간(stale-if-error).
#  지수 화면은 ~47개 심볼이 각자 fast_info를 호출하고 _YF_LOCK으로 직렬화되어, 야후의 순간
#  스로틀 하나로 특정 지수만 '실시간 시세 지연' 경고가 뜨곤 했다(실측: DRG). 몇 분 전 값은
#  마지막 확정 종가로 되돌아가는 것보다 훨씬 최신이므로, 유예 안에서는 직전 값을 재사용한다.
_YF_FI_STALE_GRACE_SEC = 900


def get_yf_fast_info(code, ttl=60.0):
    """단건 시세 조회 (캐싱 포함).

    순서: 마이크로 캐시 → TV 스크리너(미국 주식) → yfinance fast_info
          → tvDatafeed(TV 매핑 심볼) → 유예시간 내 직전 성공값(stale-if-error).
    """
    cache_key = f"yf_fi_{code}"
    cached = _get_micro_cache(cache_key, ttl=ttl) # [수정] 상황에 맞게 TTL 조절 가능토록 인자 추가
    if cached: return cached

    tv_exact_symbol = YF_TO_TV_EXACT.get(code)
    is_special_ticker = any(c in code for c in ['^', '=', '-', '.'])

    # 1. TradingView Screener 조회 — 미국 개별 주식 전용.
    #    (매핑 심볼(지수·선물·환율)은 스캐너 유니버스에 없어 아래 tvDatafeed 폴백이 담당한다)
    if not is_special_ticker:
        try:
            from tradingview_screener import Query, Column
            _, df = Query().set_markets('america').select('close', 'change_abs', 'volume', 'High.52Week', 'premarket_close', 'postmarket_close').where(Column('name') == code).limit(1).get_scanner_data()

            if df is not None and not df.empty:
                row = df.iloc[0]
                close_p = row.get('close')
                change_abs = row.get('change_abs')
                
                # [추가] 장외(프리/애프터마켓) 가격이 존재할 경우 실시간 가격으로 우선 적용
                pre_close = row.get('premarket_close')
                post_close = row.get('postmarket_close')

                is_extended = False
                if pd.notna(post_close) and post_close > 0:
                    close_p = post_close
                    is_extended = True
                elif pd.notna(pre_close) and pre_close > 0:
                    close_p = pre_close
                    is_extended = True

                prev_close = None
                if pd.notna(row.get('close')) and pd.notna(change_abs):
                    prev_close = row.get('close') - change_abs

                data = {
                    'last_price': close_p,
                    'regular_market_previous_close': prev_close,
                    'last_volume': row.get('volume', 0),
                    'year_high': row.get('High.52Week'),
                    'src': 'tv',            # [추가] 소스 구분 (해외주식 현재가 폴백은 TV만 허용)
                    'is_extended': is_extended  # [추가] 장외(프리/애프터) 세션 가격 여부
                }
                _set_micro_cache(cache_key, data)
                return data
        except Exception:
            pass

    # 2. yfinance
    try:
        # [수정] tz 캐시(SQLite) 동시 접근 경합 방지를 위해 yfinance 호출을 전역 락으로 직렬화
        with _YF_LOCK:
            fi = yf.Ticker(code).fast_info

        # [수정] regular_market_previous_close가 없는 지수(달러인덱스 등)를 위한 Fallback
        prev_close = getattr(fi, 'regular_market_previous_close', None)
        if prev_close is None or pd.isna(prev_close):
            prev_close = getattr(fi, 'previous_close', None)

        last_price = getattr(fi, 'last_price', None)
        if last_price is None or pd.isna(last_price):
            raise ValueError("fast_info last_price 없음")

        data = {
            'last_price': last_price,
            'regular_market_previous_close': prev_close,
            'last_volume': getattr(fi, 'last_volume', 0),
            'year_high': getattr(fi, 'year_high', None),
            'src': 'yf',           # [추가] yfinance fast_info는 정규장가만 제공 (장외 시세 병합에 사용 금지)
            'is_extended': False
        }
        if prev_close is not None and not pd.isna(prev_close):
            _set_micro_cache(cache_key, data)
            return data
        # 전일 종가가 없으면 호출부가 실시간으로 인정하지 않는다(market.py) → TV가 온전한
        #  값을 주면 그쪽을 쓰고, TV도 실패하면 이 반쪽 값이라도 종전처럼 돌려준다.
        yf_partial = data
    except Exception as e:
        logger.debug(f"get_yf_fast_info Error ({code}): {e}")
        yf_partial = None

    # 3. tvDatafeed 폴백 (TV 매핑이 있는 지수·선물·환율)
    if tv_exact_symbol:
        data = _fast_info_via_tvdatafeed(tv_exact_symbol)
        if data:
            _set_micro_cache(cache_key, data)
            return data

    if yf_partial:
        _set_micro_cache(cache_key, yf_partial)
        return yf_partial

    # 4. stale-if-error — 유예 시간 안이면 직전 성공값을 재사용한다.
    #    (한 번의 순간 실패로 '실시간 시세 지연' 경고가 뜨고 확정 종가로 되돌아가는 것을 막는다)
    stale = _get_micro_cache(cache_key, ttl=_YF_FI_STALE_GRACE_SEC)
    if stale:
        logger.debug(f"get_yf_fast_info({code}): 조회 실패 → 직전 값 재사용(stale)")
        return dict(stale, is_stale=True)
    return None
