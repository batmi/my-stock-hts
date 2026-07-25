"""국내 일봉을 'KRX 정규장 기준'으로 조회한다 (pykrx 1순위 / FinanceDataReader 폴백).

[왜 필요한가]
토스 캔들은 SOR 통합값이라 NXT 프리마켓(08:00~09:00)·애프터마켓(15:30~20:00) 체결이
일봉 OHLC에 그대로 섞인다. 2026-07-25 실측(삼성전자 60거래일, yfinance=KRX 공식 대조):

    토스 일봉 종가 KRX와 완전일치 3/60 (평균 괴리 1.16%)   ← NXT 거래 종목
    SK하이닉스 3/60(1.27%) / 에코프로비엠 4/60(1.29%)
    GS건설·카카오 60/60 (0.0000%)                         ← NXT 체결 없는 종목

월별 대조상 오염은 NXT 출범(2025-03)부터 시작되며 그 이전 일봉은 순수 KRX다.
지표 영향은 RSI·EMA에서는 미미하나(RSI 차이 0.3~2.1, EMA60 0.2~0.4%) ADX에서 크다
(에코프로비엠 25.1 vs 34.6 = 9.45 차이) — True Range가 장전·장후 체결로 부풀기 때문이다.

[왜 이 방식인가]
토스 분봉을 09:00~15:30으로 잘라 재구성하면 O/H/L이 KRX와 정확히 일치하지만(실측 확인),
720일치는 종목당 1,360페이지라 35종목 1회 갱신에 2.6시간(레이트리밋 5rps)이 걸리고
매매 경로와 차트 그룹 리밋을 공유해 시세 조회를 굶긴다. DB 영속화를 쓰지 않는 정책이므로
외부 KRX 소스를 쓴다 — 50종목 720일 전량이 pykrx 2.6초 / FDR 5.0초다.

[정확도] pykrx(KRX 공식) 기준 FDR은 O/H/L/C 240/240 완전일치.
         yfinance는 O/H/L은 맞지만 종가가 종목당 2~4일 어긋나 지표용으로 부적합 → 미사용.

[역할 분담] 과거 일봉은 여기서(6시간 캐시), 당일 봉은 토스 실시간 현재가가 덮어쓴다
            (api._get_cached_chart의 오버레이). pykrx·FDR은 장중 당일 값을 주지 않는다.

※ 토스 API가 KRX 기준 OHLC를 지원하면 이 모듈을 걷어내고 토스 경로로 되돌린다.
"""
import io
import logging
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta

import pandas as pd

import config

logger = logging.getLogger(__name__)

# 6시간 캐시(과거 일봉은 불변, 당일 봉은 호출부가 실시간으로 덮어쓴다).
# api._get_cached_chart의 CHART_CACHE_TTL_MINUTES(기본 360)와 같은 주기를 기본값으로 쓴다.
_CACHE = {}                       # {code: {'df': df, 'ts': epoch, 'day': 'YYYYMMDD'}}
_CACHE_LOCK = threading.RLock()
_CACHE_MAX = 300
_FAIL = {}                        # {code: 마지막 실패 시각} — 연속 실패 시 재시도 폭주 방지
_FAIL_COOLDOWN_SEC = 300

_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume']

# pykrx는 import 시 KRX 로그인 시도 로그를 stderr로 흘린다(선택적 로그인이라 조회에는 무관).
# 라즈베리파이 로그 오염을 막기 위해 import 구간만 억제한다.
_pykrx = None
_fdr = None
_import_done = False
_import_lock = threading.RLock()


def _lazy_import():
    """pykrx / FinanceDataReader를 최초 1회만 로드한다(둘 다 없으면 None으로 남는다)."""
    global _pykrx, _fdr, _import_done
    if _import_done:
        return
    with _import_lock:
        if _import_done:
            return
        buf = io.StringIO()
        try:
            with redirect_stderr(buf), redirect_stdout(buf):
                try:
                    from pykrx import stock as _s
                    _pykrx = _s
                except Exception as e:      # noqa: BLE001 - 미설치/로드 실패 모두 폴백 대상
                    logger.debug(f"[KRX] pykrx 로드 실패: {e}")
                try:
                    import FinanceDataReader as _f
                    _fdr = _f
                except Exception as e:      # noqa: BLE001
                    logger.debug(f"[KRX] FinanceDataReader 로드 실패: {e}")
        finally:
            _import_done = True
        noise = buf.getvalue().strip()
        if noise:
            logger.debug(f"[KRX] 라이브러리 로드 메시지(억제됨): {noise[:200]}")


def is_available():
    _lazy_import()
    return _pykrx is not None or _fdr is not None


def _normalize(df, source):
    """소스별 컬럼명을 ['date','open','high','low','close','volume']로 통일한다.

    date는 KIS/토스 일봉과 동일하게 'YYYYMMDD' 문자열로 맞춘다(지표·차트 경로가 이 형식을 가정).
    """
    if df is None or getattr(df, 'empty', True):
        return None

    df = df.copy()
    rename = {'시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume',
              'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}
    df = df.rename(columns=rename)
    df.columns = [str(c).lower() for c in df.columns]

    if 'date' not in df.columns:
        df = df.reset_index()
        df = df.rename(columns={c: 'date' for c in df.columns if str(c).lower() in ('index', 'date', '날짜')})
    if 'date' not in df.columns:
        logger.debug(f"[KRX] {source} 날짜 컬럼 없음: {list(df.columns)[:6]}")
        return None

    missing = [c for c in ('open', 'high', 'low', 'close') if c not in df.columns]
    if missing:
        logger.debug(f"[KRX] {source} 필수 컬럼 누락 {missing}")
        return None
    if 'volume' not in df.columns:
        df['volume'] = 0

    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date'])
    df['date'] = df['date'].dt.strftime('%Y%m%d')

    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['open', 'high', 'low', 'close'])

    # 거래정지일 등 0원 봉은 지표를 망가뜨리므로 제거한다.
    df = df[(df['close'] > 0) & (df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0)]
    if df.empty:
        return None

    df = df[_COLUMNS].drop_duplicates(subset=['date'], keep='last')
    return df.sort_values('date').reset_index(drop=True)


def _fetch_pykrx(code, start, end):
    """pykrx(KRX 공식). adjusted=True로 수정주가 기준을 FDR과 통일한다.

    기준을 맞추지 않으면 조회 구간에 액면분할·유상증자가 있을 때 두 소스가 어긋나
    폴백 전환 시점에 EMA120·52주 밴드가 튄다.
    """
    if _pykrx is None:
        return None
    try:
        df = _pykrx.get_market_ohlcv(start, end, code, adjusted=True)
    except TypeError:
        # 구버전 pykrx는 adjusted 인자를 받지 않는다.
        df = _pykrx.get_market_ohlcv(start, end, code)
    return _normalize(df, 'pykrx')


def _fetch_fdr(code, start, end):
    if _fdr is None:
        return None
    return _normalize(_fdr.DataReader(code, start, end), 'FDR')


def get_daily(code, lookback_days=None, use_cache=True):
    """KRX 정규장 기준 일봉 DataFrame(['date','open','high','low','close','volume']).

    실패 시 None을 반환한다(호출부가 토스 캔들로 폴백). 국내 6자리 종목코드만 지원한다.
    """
    code = str(code or '').strip()
    if not code.isdigit() or len(code) != 6:
        return None
    if not is_available():
        return None

    if lookback_days is None:
        lookback_days = config.INDICATOR_PARAMS.get("CHART_LOOKBACK_DAYS", 730)

    now = time.time()
    today = datetime.now().strftime('%Y%m%d')

    lookback_days = int(lookback_days)

    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(code)
            # [중요] 캐시본의 조회 기간이 요청보다 짧으면 재사용하지 않는다.
            #  차트 경로(730일)가 먼저 캐시해 두면 백테스트(수년)가 짧은 시계열을 받아
            #  기간이 조용히 잘린 채 검증되기 때문이다. 반대로 더 긴 캐시본은 그대로 쓴다.
            if (hit and hit.get('day') == today
                    and (now - hit['ts']) < _cache_ttl_sec()
                    and hit.get('lookback', 0) >= lookback_days):
                return hit['df'].copy()
            failed_at = _FAIL.get(code, 0)
        if failed_at and (now - failed_at) < _FAIL_COOLDOWN_SEC:
            return None

    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    s, e = start.strftime('%Y%m%d'), end.strftime('%Y%m%d')

    df = None
    for name, fetch in (('pykrx', _fetch_pykrx), ('FDR', _fetch_fdr)):
        try:
            df = fetch(code, s, e)
        except Exception as ex:     # noqa: BLE001 - 어느 소스가 죽어도 다음 소스로 넘어간다
            logger.debug(f"[KRX] {name} 조회 실패({code}): {ex}")
            df = None
        if df is not None and not df.empty:
            df.attrs['source'] = name
            break

    if df is None or df.empty:
        logger.warning(f"[KRX] 일봉 조회 실패({code}) — pykrx·FDR 모두 실패, 토스 캔들로 폴백")
        with _CACHE_LOCK:
            _FAIL[code] = now
        return None

    with _CACHE_LOCK:
        _FAIL.pop(code, None)
        _CACHE[code] = {'df': df, 'ts': now, 'day': today, 'lookback': lookback_days}
        if len(_CACHE) > _CACHE_MAX:
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1]['ts'])[:len(_CACHE) - _CACHE_MAX]
            for k, _ in oldest:
                _CACHE.pop(k, None)

    return df.copy()


def _cache_ttl_sec():
    """차트 캐시와 동일 주기(기본 6시간). 0 이하면 캐시를 쓰지 않는다."""
    try:
        minutes = float(getattr(config, 'CHART_CACHE_TTL_MINUTES', 360))
    except (TypeError, ValueError):
        minutes = 360.0
    return max(0.0, minutes * 60)


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()
        _FAIL.clear()
