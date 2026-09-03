"""차트 캐시 — 메모리 · 디스크(SQLite) · 토스 시드, 그리고 예열.

같은 일봉을 반복해서 내려받지 않도록 2단(메모리→디스크) 캐시를 두고, 관심종목
예열과 개요 화면 워머가 그 캐시를 미리 채운다. 라즈베리파이의 메모리·SD 수명이
제약이라 항목 상한과 오래된 항목 제거가 캐시 설계의 중심에 있다.
"""
import logging
import os
import pickle
import sqlite3
import threading
import time
import concurrent.futures
from contextlib import closing
from datetime import datetime, timedelta, timezone
import pandas as pd
import config

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

# ==========================================================
# [추가] 차트 데이터 인메모리 캐싱 시스템 (하이브리드 패치)
# ==========================================================
_CHART_CACHE = {}
_CHART_CACHE_LOCK = threading.RLock()
# [메모리] 차트 캐시는 DataFrame을 보관해 항목당 비용이 크다. 라즈베리파이 OOM 방어를 위해
# 항목 수를 제한하고, 초과 시 가장 오래된 항목부터 제거한다. (전체 시장 스캔 시 무제한 누적 방지)
_CHART_CACHE_MAX = 600

# [영속] 일봉 차트 디스크 캐시(SQLite). 일봉은 하루 1회만 바뀌므로, 같은 거래일 동안은
# 재시작 후에도 네트워크 재조회 없이 디스크에서 즉시 복원한다(시작 버스트·반복 조회 절감).
# 메모리 캐시(_CHART_CACHE) 미스 시 디스크를 확인하고, 과거일자 항목은 자동 정리해 크기를 제한한다.
_CHART_DISK_LOCK = threading.RLock()
_chart_disk_pruned_date = None

def _chart_disk_path():
    base = getattr(config, 'DATA_DIR', None) or getattr(config, 'JSON_DIR', '.')
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, 'chart_cache.db')

def _chart_disk_get(cache_key, today_str, is_overseas=False):
    """디스크 일봉 캐시에서 '오늘자' DataFrame을 복원한다(없거나 비활성/오류/만료 시 None).

    [Fix 2026-07-27] 종전엔 달력일(trade_date)만 맞으면 저장 시각과 무관하게 하루 종일
    돌려줬다. 복원할 때 메모리 캐시의 timestamp를 now로 다시 찍기 때문에 6시간 TTL마저
    영영 만료되지 않아, 자정 직후에 받아 둔 '어제까지의 일봉'이 그날 밤까지 재사용됐다.
    장중에는 실시간 오버레이가 당일 봉을 채워 가려지지만, 모든 장이 끝난 뒤(20:00 이후·
    오버레이 비활성)에는 직전 거래일 종가가 그대로 '현재가'로 노출된다.
      실측 2026-07-27(월) 22:40 관심종목 표: 삼성전자 249,500(-7.59%)로 표시 — 7/24(금)
      값이다(실제 7/27 종가 254,000 +1.80%). 캐시 저장 시각 00:54, 마지막 봉 20260724.
      같은 캐시로 EMA·RSI·CCI·52주 위치까지 하루 밀린 채 계산된다.
    그래서 두 조건을 모두 만족할 때만 재사용한다:
      1) 저장 시각 기준 TTL(CHART_CACHE_TTL_MINUTES) 이내
      2) 국내는 '직전 정규장 마감 이후'에 저장된 것 — 마감 전 캐시는 확정 종가가 없다
    """
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return None
    try:
        ttl_sec = max(0.0, float(getattr(config, 'CHART_CACHE_TTL_MINUTES', 360))) * 60
    except (TypeError, ValueError):
        ttl_sec = 360 * 60
    closed_at = None if is_overseas else _api()._krx_close_passed_at()
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            row = conn.execute("SELECT df_blob, ts FROM chart_cache WHERE cache_key=? AND trade_date=?", (cache_key, today_str)).fetchone()
            if row and row[0]:
                try:
                    saved_ts = float(row[1] or 0)
                except (TypeError, ValueError):
                    saved_ts = 0.0
                if ttl_sec > 0 and (time.time() - saved_ts) > ttl_sec:
                    return None
                if closed_at is not None and saved_ts < closed_at.timestamp():
                    return None
                df = pickle.loads(row[0])
                if df is not None and not df.empty:
                    return df
    except Exception as e:
        logger.debug(f"[ChartDisk] get 실패({cache_key}): {e}")
    return None

def _chart_disk_set(cache_key, df, today_str):
    """디스크 일봉 캐시에 '오늘자' DataFrame을 저장하고, 과거일자 항목은 하루 1회 정리한다."""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    global _chart_disk_pruned_date
    try:
        blob = pickle.dumps(df)
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            conn.execute("INSERT OR REPLACE INTO chart_cache (cache_key, trade_date, df_blob, ts) VALUES (?,?,?,?)",
                         (cache_key, today_str, blob, time.time()))
            # 거래일이 바뀌면 과거일자 항목 일괄 정리(디스크 무제한 누적 방지)
            if _chart_disk_pruned_date != today_str:
                conn.execute("DELETE FROM chart_cache WHERE trade_date != ?", (today_str,))
                # 만료된 토스 일봉 시드도 함께 지운다 — TTL은 '사용'만 막을 뿐이라
                # 관심종목에서 뺀 종목의 시드(종목당 약 18KB)가 계속 쌓인다.
                conn.execute(_TOSS_SEED_DDL)
                conn.execute("DELETE FROM toss_daily_seed WHERE ts < ?",
                             (time.time() - _TOSS_SEED_TTL_DAYS * 86400,))
                _chart_disk_pruned_date = today_str
    except Exception as e:
        logger.debug(f"[ChartDisk] set 실패({cache_key}): {e}")

def _chart_disk_delete(cache_key):
    """디스크 일봉 캐시에서 특정 키를 제거한다(수정주가 감지 시 오염 항목 파기용).
    메모리만 지우면 다음 호출에서 디스크의 옛 df가 재적재→재파기가 반복되므로 함께 지운다."""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            conn.execute("DELETE FROM chart_cache WHERE cache_key=?", (cache_key,))
    except Exception as e:
        logger.debug(f"[ChartDisk] delete 실패({cache_key}): {e}")


# =========================================================================
# [최적화] 토스 일봉 '과거 구간' 시드 — 종목당 캔들 콜 2회를 1회로 줄인다.
#
# 토스 캔들은 호출당 200봉이 상한이고(실측: count=250은 [400] invalid-request),
# 52주 밴드(_w52_band)에 250봉이 필요해 종목당 2콜이 강제된다. 그런데 /candles 그룹은
# 서버 한도가 5 RPS(실측: X-RateLimit-Limit 헤더)라 콜 수가 그대로 표 소요를 결정한다:
#   18종목 × 2콜 ÷ 5 RPS = 7.2초 하한 (실측 7.1초 — 이미 하한에 붙어 있다)
# 워커를 늘려도 _throttle 앞에 줄만 더 설 뿐이라 이 하한은 내려가지 않는다.
#
# 두 번째 페이지가 담는 '더 오래된 봉'은 불변이므로 날짜가 바뀌어도 다시 받을 이유가 없다.
# 그래서 일자 무관 장기 보관하고, 다음 조회 때 최신 1페이지(200봉)만 받아 앞을 채운다.
#   → 종목당 1콜, 표 하한 절반.
#
# 유일한 위험은 액면분할·유상증자다(adjusted=true라 과거 값이 통째로 바뀐다). 시드와 새
# 페이지는 ~199봉이 겹치므로, 그 구간 종가가 하나라도 어긋나면 시드를 버리고 정상 페이징한다.
# 게다가 최종 tail(250)까지 살아남는 시드 구간은 겹침 바로 앞 ~50봉뿐이다.
# =========================================================================
_TOSS_SEED_MIN_OVERLAP = 20      # 수정주가 검증 표본 최소 봉 수 (미달이면 시드를 쓰지 않는다)
_TOSS_SEED_MAX_ROWS = 400        # 종목당 보관 봉 수 (2페이지 분량)
_TOSS_SEED_TTL_DAYS = 30         # 이 기간 갱신되지 않은 시드는 폐기 (장기 미조회 종목 정리)
_TOSS_SEED_DDL = ("CREATE TABLE IF NOT EXISTS toss_daily_seed "
                  "(code TEXT PRIMARY KEY, df_blob BLOB, ts REAL)")


def _toss_seed_get(code):
    """토스 일봉 시드를 읽는다(거래일 무관). 없거나 만료면 None."""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return None
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn:
            conn.execute(_TOSS_SEED_DDL)
            row = conn.execute("SELECT df_blob, ts FROM toss_daily_seed WHERE code=?", (code,)).fetchone()
        if not row or not row[0]:
            return None
        if (time.time() - float(row[1] or 0)) > _TOSS_SEED_TTL_DAYS * 86400:
            return None
        df = pickle.loads(row[0])
        return df if df is not None and not df.empty else None
    except Exception as e:
        logger.debug(f"[TossSeed] get 실패({code}): {e}")
        return None


def _toss_seed_set(code, df):
    """토스 일봉 시드를 저장한다(확정된 최근 _TOSS_SEED_MAX_ROWS봉).

    [중요] 마지막 봉은 버린다. 장중(국내는 NXT 연장 20:00까지)에 받은 당일 봉은 아직
    움직이므로, 그대로 저장하면 다음 조회의 겹침 대조에서 종가가 어긋나 시드가 매번
    폐기된다(실측: 5종목 중 3종목이 당일 봉 변동만으로 재페이징 — 콜 10→8에 그쳤다).
    시드는 '불변 구간'만 담아야 한다.
    """
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    if df is None or len(df) < 2:
        return
    try:
        blob = pickle.dumps(df.iloc[:-1].tail(_TOSS_SEED_MAX_ROWS).reset_index(drop=True))
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute(_TOSS_SEED_DDL)
            conn.execute("INSERT OR REPLACE INTO toss_daily_seed (code, df_blob, ts) VALUES (?,?,?)",
                         (code, blob, time.time()))
    except Exception as e:
        logger.debug(f"[TossSeed] set 실패({code}): {e}")


def _toss_seed_delete(code):
    """시드를 폐기한다(수정주가 등으로 과거 값이 바뀐 경우)."""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute(_TOSS_SEED_DDL)
            conn.execute("DELETE FROM toss_daily_seed WHERE code=?", (code,))
    except Exception as e:
        logger.debug(f"[TossSeed] delete 실패({code}): {e}")


def _toss_seed_extend(code, fresh_df, need):
    """시드의 과거 구간으로 fresh_df 앞을 채운다. 쓸 수 없으면 None(→ 정상 페이징).

    fresh_df: 방금 받은 최신 페이지(오름차순, date=YYYYMMDD).
    need: 목표 봉 수. 병합 결과가 이에 못 미치면 시드로 콜을 아낄 수 없으니 None.
    """
    if fresh_df is None or fresh_df.empty:
        return None
    seed = _toss_seed_get(code)
    if seed is None or 'date' not in seed.columns or 'close' not in seed.columns:
        return None

    oldest = str(fresh_df['date'].iloc[0])
    older = seed[seed['date'].astype(str) < oldest]
    if older.empty or (len(older) + len(fresh_df)) < need:
        return None      # 시드가 짧아 어차피 2페이지가 필요하다

    # 겹침 구간 종가 대조 — 수정주가가 발생했으면 여기서 어긋난다.
    a = seed.set_index(seed['date'].astype(str))['close']
    b = fresh_df.set_index(fresh_df['date'].astype(str))['close']
    common = a.index.intersection(b.index)
    if len(common) < _TOSS_SEED_MIN_OVERLAP:
        return None      # 검증 표본 부족(신규 상장·장기 미조회) — 안전하게 정상 페이징
    try:
        diff = (a.loc[common].astype(float) - b.loc[common].astype(float)).abs()
        ref = b.loc[common].astype(float).abs().clip(lower=1e-9)
        if not bool(((diff / ref) <= 1e-6).all()):
            _toss_seed_delete(code)
            logger.debug(f"[TossSeed] 폐기({code}): 겹침 {len(common)}봉 종가 불일치(수정주가 추정)")
            return None
    except Exception as e:
        logger.debug(f"[TossSeed] 대조 실패({code}): {e}")
        return None

    merged = pd.concat([older, fresh_df], ignore_index=True)
    merged = merged.drop_duplicates(subset=['date'], keep='last')
    return merged.sort_values('date').reset_index(drop=True)


# [추가] 캐시 오버레이(get_current_price_data) 재진입 방지용 가드.
# 액면분할 보정 경로(get_current_price_data → get_chart_data → 오버레이 → get_current_price_data)에서
# 무한 재귀가 발생하지 않도록, 오버레이 진행 중 같은 스레드의 재진입 시 과거봉 캐시만 반환한다.
_OVERLAY_GUARD = threading.local()

def _chart_disk_clear():
    """디스크 일봉 캐시(SQLite)를 전부 비운다. (수동 전체 갱신/테스트 격리용)"""
    if getattr(config, 'CHART_DISK_CACHE', True) is False:
        return
    try:
        with _CHART_DISK_LOCK, closing(sqlite3.connect(_chart_disk_path())) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS chart_cache (cache_key TEXT PRIMARY KEY, trade_date TEXT, df_blob BLOB, ts REAL)")
            conn.execute("DELETE FROM chart_cache")
            # 토스 일봉 시드도 함께 비운다 — 남겨두면 '전체 갱신'이 과거 구간을 재조회하지 않는다.
            conn.execute(_TOSS_SEED_DDL)
            conn.execute("DELETE FROM toss_daily_seed")
    except Exception as e:
        logger.debug(f"[ChartDisk] clear 실패: {e}")

def clear_chart_cache():
    """모든 차트 데이터 캐시 초기화 (수동 갱신용). 디스크 영속 캐시도 함께 비운다."""
    with _CHART_CACHE_LOCK:
        _CHART_CACHE.clear()
    _chart_disk_clear()
    if _api()._is_screen_output_allowed():
        config.console.print("[bold green]차트 데이터 캐시(메모리)가 전체 초기화되었습니다.[/bold green]")
    logger.info("[Cache] 차트 데이터 캐시 수동 초기화")

def _chart_cache_key(code, is_overseas, is_index):
    """차트 캐시 키 (브로커 네임스페이스 포함).

    같은 종목이라도 브로커별로 일봉 종가가 다르다: **TOSS 국내 일봉=NXT 연장 종가,
    KIS 일봉=KRX 정규장 종가**. 브로커를 키에 넣지 않으면 mode 전환 시(예: 맥북에서
    mode 2 → mode 3) 한쪽 데이터가 다른 쪽 캐시로 새어들어 등락률/EMA가 어긋난다.
    모의(mode1)·실전(mode2)은 둘 다 KIS/KRX라 'K'로 공유한다.

    TOSS는 'T3' — 일봉의 '기준' 자체가 바뀔 때마다 네임스페이스를 올려, 이미 저장된
    옛 기준 캐시(메모리/디스크)가 재사용되지 않게 한다. 올리지 않으면 코드를 고쳐도
    당일 디스크 캐시가 그대로 반환되어 수정 전 값이 계속 보인다.
      T  → T2 : 일봉 이상치 보정(_toss_sanitize_daily_ohlc) 도입
      T2 → T3 : 국내 일봉을 KRX 정규장 기준으로 전환(_krx_daily_chart, pykrx/FDR)
    """
    broker = 'T3' if getattr(config.session, 'is_toss', False) else 'K'
    return f"{broker}_{code}_{is_overseas}_{is_index}"


def _get_cached_chart(code, is_overseas, is_index, fetch_func, realtime_overlay=True):
    """캐시된 차트를 반환하되, 당일 최신 캔들은 실시간 현재가로 덮어씌워 반환합니다.

    realtime_overlay=False면 캐시 적중 시 현재가 API 오버레이를 생략하고 과거봉 캐시를 그대로 반환한다.
    (호출자가 자체적으로 당일 캔들을 실시간 갱신하는 경우, 중복 현재가 호출을 막아 TPS 부담을 줄인다.)
    """
    ttl_minutes = getattr(config, 'CHART_CACHE_TTL_MINUTES', 360)
    if ttl_minutes <= 0:
        return fetch_func()

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    cache_key = _chart_cache_key(code, is_overseas, is_index)
    settled_at = None if is_overseas else _api()._krx_close_passed_at()

    with _CHART_CACHE_LOCK:
        cached = _CHART_CACHE.get(cache_key)
        if cached:
            # 날짜 변경선 감지 (자정이 지나면 무효화)
            if cached['date'] != today_str:
                del _CHART_CACHE[cache_key]
                cached = None
            # TTL 감지
            elif (now - cached['timestamp']).total_seconds() > (ttl_minutes * 60):
                del _CHART_CACHE[cache_key]
                cached = None
            # [Fix] 정규장 마감 전에 만들어진 캐시는 당일 확정 종가를 담을 수 없다.
            #  TTL(6시간) 안이라도 파기해 재조회한다 — 마감 후 이 캐시를 그대로 쓰면
            #  오버레이가 꺼지는 20:00 이후 직전 거래일 종가가 '현재가'로 보인다.
            #  (근거·실측은 _chart_disk_get 주석 참조)
            elif settled_at is not None and cached['timestamp'] < settled_at:
                del _CHART_CACHE[cache_key]
                cached = None

    # [영속] 메모리 미스 시 디스크(오늘자) 캐시를 확인해 네트워크 재조회를 피한다(재시작 내성).
    if cached is None and not is_index:
        disk_df = _chart_disk_get(cache_key, today_str, is_overseas)
        if disk_df is not None:
            cached = {'df': disk_df, 'timestamp': now, 'date': today_str}
            with _CHART_CACHE_LOCK:
                _CHART_CACHE[cache_key] = cached
                _api()._evict_oldest(_CHART_CACHE, _CHART_CACHE_MAX, 'timestamp')

    if cached:
        df = cached['df'].copy()
        # [추가] 오버레이 불필요(호출자 자체 갱신) 시 과거봉 캐시만 즉시 반환 → 중복 현재가 호출 제거
        if not realtime_overlay:
            return df
        # [추가] 재진입(분할보정 경로 등) 감지 시 오버레이/재조회 없이 과거봉 캐시만 반환 → 무한재귀 차단
        if getattr(_OVERLAY_GUARD, 'active', False):
            return df
        # [추가] 모든 장 종료 후(NXT 애프터마켓 20:00 이후·주말·휴장)에는 국내 봉을 갱신하지 않는다.
        #  이 시간대의 현재가는 마지막 NXT 체결가로 굳어 있어, 확정된 KRX 일봉 종가를 덮어쓰면
        #  지표가 통째로 흔들린다(chart_overlay_price 참조). 설정으로 끌 수 있다.
        if not _api().chart_overlay_enabled(is_overseas):
            return df
        _OVERLAY_GUARD.active = True
        try:
            # 시장 기준일(주말·휴장일이면 직전 거래일). 달력 날짜를 쓰면 비거래일에
            # 최종 종가로 '가짜 당일 봉'이 추가되어 등락률이 0으로 계산된다.
            today_ymd = _api().market_today(is_overseas)
            last_date = str(df.iloc[-1]['date'])
            
            curr, open_p, high_p, low_p, vol, prev = 0, 0, 0, 0, 0, 0

            def _safe_float(val):
                if val is None: return 0.0
                s = str(val).strip().replace(',', '')
                if not s: return 0.0
                try: return float(s)
                except Exception: return 0.0

            # 1. 가장 가벼운 현재가 API로 오늘 데이터만 가져오기
            if is_index and not is_overseas:
                res = _api().get_domestic_index_price(code)
                if res and res.get('rt_cd') == '0':
                    out = res.get('output', {})
                    curr = _safe_float(out.get('bstp_nmix_prpr', 0))
                    prev = _safe_float(out.get('bstp_nmix_prdy_clpr', 0))
                    # [Fix] 업종 현재가 TR은 당일 시가/고가/저가/누적거래량을 함께 준다.
                    #  현재가로 근사하던 종전 방식은 당일 봉의 고저를 뭉개 ATR·CCI를 왜곡했다.
                    #  (토스 경로는 이 필드들을 주지 않아 0 → 아래에서 현재가/캐시 값으로 보정)
                    open_p = _safe_float(out.get('bstp_nmix_oprc', 0)) or curr
                    high_p = _safe_float(out.get('bstp_nmix_hgpr', 0)) or curr
                    low_p = _safe_float(out.get('bstp_nmix_lwpr', 0)) or curr
                    vol = _safe_float(out.get('acml_vol', 0))
            elif is_index and is_overseas:
                # yfinance 단건 현재가 빠른 조회
                try:
                    fi = _api().get_yf_fast_info(code)
                    if fi:
                        curr = _safe_float(fi['last_price'])
                        prev = _safe_float(fi['regular_market_previous_close'])
                        open_p, high_p, low_p = curr, curr, curr
                        vol = _safe_float(fi['last_volume'])
                except Exception as e:
                    logger.debug(f"[Cache] yfinance fast_info error for {code}: {e}")
                    pass
            else:
                cp_data = _api().get_current_price_data(code, is_overseas)
                if cp_data and cp_data.get('rt_cd') == '0':
                    out = cp_data.get('output', {})
                    if is_overseas:
                        curr = _safe_float(out.get('last', 0))
                        open_p = _safe_float(out.get('open', 0)) if out.get('open') else curr
                        high_p = _safe_float(out.get('high', 0)) if out.get('high') else curr
                        low_p = _safe_float(out.get('low', 0)) if out.get('low') else curr
                        vol = _safe_float(out.get('tvol', 0) or out.get('vol', 0))
                        # 전일종가는 base 필드만 신뢰한다. (curr - diff) 방식은 last가 장외
                        # 실시간가로 덮어써지거나(KIS 프리/애프터) diff 미제공(토스) 시 어긋나
                        # 아래 수정주가 검증이 오탐→캐시 파기·재조회를 반복한다. base 없으면 0(검증 스킵).
                        prev = _safe_float(out.get('base', 0))
                    else:
                        curr = _safe_float(out.get('stck_prpr', 0))
                        open_p = _safe_float(out.get('stck_oprc', 0))
                        high_p = _safe_float(out.get('stck_hgpr', 0))
                        low_p = _safe_float(out.get('stck_lwpr', 0))
                        vol = _safe_float(out.get('acml_vol', 0))
                        prev = _safe_float(out.get('stck_prdy_clpr', 0))

            if curr > 0:
                # 2. 정합성(수정주가) 검증 로직: 전일 종가가 1.5% 이상 차이나면 오염된 캐시로 판단하고 파기
                target_prev = float(df.iloc[-2]['close']) if len(df) >= 2 else 0
                if last_date < today_ymd: target_prev = float(df.iloc[-1]['close'])
                
                if target_prev > 0 and prev > 0 and abs(target_prev - prev) / target_prev > 0.015:
                    if config.FILE_DEBUG_LEVEL == "DEBUG": logger.debug(f"[Cache] {code} 수정주가 감지 (캐시:{target_prev} != 실시간:{prev}). 파기 후 재조회.")
                    with _CHART_CACHE_LOCK:
                        if cache_key in _CHART_CACHE: del _CHART_CACHE[cache_key]
                    _chart_disk_delete(cache_key)
                    # 아래 공통 재조회 경로로 합류시켜 새 데이터가 메모리·디스크에 재캐싱되게 한다.
                    cached = None
                else:
                    # 3. 실시간 가격 패치(Patch)
                    # last_date > today_ymd(캔들 소스가 기준일보다 앞선 경우: KST 장중의 국내지수를
                    # ET 기준일로 본 경우 등)에도 최신 봉을 덮어쓴다. 새 행 추가는 기준일이
                    # 거래일로 보정되어 있으므로 실제 개장일에만 일어난다.
                    if last_date >= today_ymd:
                        # 당일 봉 덮어쓰기 (고가/저가는 캐시된 데이터와 비교하여 최대/최소 유지)
                        old_high = float(df.iloc[-1]['high'])
                        old_low = float(df.iloc[-1]['low'])
                        high_p = max(old_high, high_p, curr)
                        low_p = min(old_low, low_p, curr) if low_p > 0 else min(old_low, curr)
                        # 현재가 API가 시가/거래량을 안 주는 경우(토스 등)는 캐시된 당일 봉 값을 보존(0 덮어쓰기 방지)
                        if open_p <= 0: open_p = float(df.iloc[-1]['open'])
                        if vol <= 0: vol = float(df.iloc[-1]['volume'])
                        df.loc[df.index[-1], ['open', 'high', 'low', 'close', 'volume']] = [open_p, high_p, low_p, curr, vol]
                    else:
                        # [Fix] 국내 지수는 NXT 연장거래가 없어 09:00 개장 전 현재가 = 전일 종가다.
                        #  이때 '오늘 봉'을 추가하면 마지막 두 봉의 종가가 같아져 등락률이 0%로 굳고,
                        #  그 df가 지수 공유 캐시에 실려 하루 종일 '어제 값 + 0%'로 보이게 된다.
                        #  개장 전이고 현재가가 마지막 봉 종가와 같으면 가짜 봉을 만들지 않는다.
                        if (is_index and not is_overseas and abs(curr - float(df.iloc[-1]['close'])) < 1e-9
                                and _api()._before_krx_regular_open()):
                            return df
                        # 오늘 날짜 행이 없으면 새로 추가 (시가/고가/저가 미제공 시 현재가로 근사)
                        if open_p <= 0: open_p = curr
                        if high_p <= 0: high_p = curr
                        if low_p <= 0: low_p = curr
                        new_row = pd.DataFrame([{'date': today_ymd, 'open': open_p, 'high': high_p, 'low': low_p, 'close': curr, 'volume': vol}])
                        df = pd.concat([df, new_row], ignore_index=True)

                    return df

            # [Fix] 현재가를 못 얻은 채(오버레이 실패) 캐시의 마지막 봉이 '전일 종가 복제'
            #  당일 봉이면, 개장 후에도 등락률이 0%로 굳은 채 캐시 TTL(기본 6시간) 내내 유지된다.
            #  (실측 2026-07-28: 지수 현재가 TR 매핑 누락으로 장중 전 지수가 어제 값 + 0.00%)
            #  이 조합은 정상 상태가 아니므로 캐시를 파기하고 원본을 다시 받는다 — 개장 후의
            #  KIS 업종 일봉은 당일 봉을 실시간으로 채워 주므로 한 번의 재조회로 복구된다.
            if (curr <= 0 and is_index and not is_overseas and len(df) >= 2
                    and str(df.iloc[-1]['date']) == today_ymd
                    and abs(float(df.iloc[-1]['close']) - float(df.iloc[-2]['close'])) < 1e-9
                    and not _api()._before_krx_regular_open()):
                logger.debug(f"[Cache] 지수({code}) 당일 봉이 전일 종가와 동일 + 현재가 미확보 → 캐시 파기 후 재조회")
                with _CHART_CACHE_LOCK:
                    _CHART_CACHE.pop(cache_key, None)
                cached = None
        except Exception as e:
            logger.debug(f"[Cache] Update failed for {code}: {e}")
        finally:
            _OVERLAY_GUARD.active = False
        # 오버레이 실패(현재가 미확보·예외) 시 전체 재조회 대신 캐시된 과거봉을 그대로 반환한다.
        # 과거봉은 불변이므로 당일 봉만 잠시 덜 신선할 뿐, 무거운 전체 재다운로드보다 낫다.
        # (수정주가 파기 시에만 cached=None으로 아래 재조회 경로를 탄다)
        if cached is not None:
            return df

    df = fetch_func()
    if df is not None and not df.empty:
        with _CHART_CACHE_LOCK:
            _CHART_CACHE[cache_key] = {
                'df': df.copy(),
                'timestamp': now,
                'date': today_str
            }
            _api()._evict_oldest(_CHART_CACHE, _CHART_CACHE_MAX, 'timestamp')
        # [영속] 일봉(비지수)만 디스크에 저장해 재시작/반복 조회 시 네트워크 호출을 줄인다.
        if not is_index:
            _chart_disk_set(cache_key, df, today_str)
    return df

def prefetch_multiple_current_prices(codes, is_overseas=False, include_investor=True, progress_updater=None, prefer_ws=False, skip_if_fresh_sec=None):
    """[최적화] 다중 종목 실시간 데이터 일괄 조회 (Micro-Cache 사전 예열)

    prefer_ws=True면 WS 실시간 피드가 이미 신선한 현재가를 보유한 종목은 현재가 REST 예열을
    생략한다(모의투자 2 TPS 절감). 시스템 트레이딩처럼 이후 경로가 현재가 값만 필요한 곳에서 쓴다.
    skip_if_fresh_sec(해외 전용): 전 종목의 fast_info 마이크로 캐시가 지정 초 이내로 신선하면
    TV 재조회 자체를 생략한다. 백그라운드 워머(OverviewWarmer)가 방금 예열한 경우 임계경로의
    네트워크 왕복을 제거하는 용도이며, 워머 자신은 이 인자를 쓰면 안 된다(갱신이 영구 생략됨).
    """
    if not codes: return

    if is_overseas:
        # 0. [최적화] 워머가 예열해 둔 캐시가 전 종목 신선하면 라이브 조회 생략
        if skip_if_fresh_sec:
            #  skip_if_fresh_sec 은 화면 경로만 넘긴다(워머는 쓰면 안 된다 — 위 주석).
            #  그래서 이 인자가 왔다는 것은 '사람이 개요를 보고 있다'는 뜻이고,
            #  워머는 그 신호를 근거로 유휴 시간에 스스로 멈춘다(start_overview_warmer).
            note_warm_consumer()
            if all(_api()._get_micro_cache(f"yf_fi_{c}", ttl=skip_if_fresh_sec) for c in codes):
                if progress_updater:
                    for _ in codes: progress_updater()
                return

        # 1. TradingView 일괄 조회 (가장 빠름, 단 1회의 HTTP 요청으로 모두 해결)
        tv_success_codes = set()
        try:
            from tradingview_screener import Query
            _, df = Query().set_markets('america').select('name', 'close', 'change_abs', 'volume', 'High.52Week', 'premarket_close', 'postmarket_close').get_tickers(codes)
            
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    ticker = row.get('ticker')
                    if not ticker: continue
                    
                    close_p = row.get('close')
                    change_abs = row.get('change_abs')
                    
                    pre_close = row.get('premarket_close')
                    post_close = row.get('postmarket_close')
                    is_extended = False
                    if pd.notna(post_close) and post_close > 0:
                        close_p = post_close
                        is_extended = True
                    elif pd.notna(pre_close) and pre_close > 0:
                        close_p = pre_close
                        is_extended = True

                    if pd.isna(close_p): continue

                    prev_close = None
                    if pd.notna(row.get('close')) and pd.notna(change_abs):
                        prev_close = row.get('close') - change_abs

                    data = {
                        'last_price': close_p,
                        'regular_market_previous_close': prev_close,
                        'last_volume': row.get('volume', 0),
                        'year_high': row.get('High.52Week'),
                        'src': 'tv',
                        'is_extended': is_extended
                    }
                    _api()._set_micro_cache(f"yf_fi_{ticker}", data)
                    tv_success_codes.add(ticker)
                    if progress_updater: progress_updater()
        except Exception as e:
            logger.debug(f"TV Screener prefetch error: {e}")
            pass
            
        # 2. TV 조회에 실패한 종목들만 yfinance 병렬 워커로 Fallback
        remaining_codes = [c for c in codes if c not in tv_success_codes]
        if remaining_codes:
            def fetch_yf_worker(code):
                try: _api().get_yf_fast_info(code)
                except Exception: pass
                if progress_updater: progress_updater()

            max_w = 5
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
                futures = [executor.submit(fetch_yf_worker, c) for c in remaining_codes]
                concurrent.futures.wait(futures)
    else:
        # WS가 신선한 현재가를 이미 가진 종목은 현재가 REST 예열을 생략(TPS 절감)하기 위한 피드 핸들
        _ws_feed = None
        if prefer_ws and getattr(config, 'USE_WEBSOCKET', True) and not config.session.is_toss:
            try:
                from brokers import realtime
                _ws_feed = realtime.get_feed()
            except Exception:
                _ws_feed = None
        _ws_ttl = getattr(config, 'WS_DATA_TTL_SEC', 3.0)

        def fetch_worker(code):
            ws_has_price = False
            if _ws_feed is not None:
                try:
                    p = _ws_feed.get_price(code, max_age=_ws_ttl)
                    ws_has_price = bool(p and p > 0)
                except Exception:
                    ws_has_price = False
            if not ws_has_price:
                try: _api().get_current_price_data(code, False)
                except Exception: pass
            if include_investor:
                try: _api().get_investor_trend(code)
                except Exception: pass
            # 체결강도는 get_realtime_vol_strength가 내부적으로 WS를 먼저 확인하므로 WS 커버 시 REST 미발생
            try: _api().get_realtime_vol_strength(code)
            except Exception: pass

        max_w = 10
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_w) as executor:
            futures = [executor.submit(fetch_worker, c) for c in codes]
            for future in concurrent.futures.as_completed(futures):
                if progress_updater: progress_updater()

def prefetch_watchlists_async():
    """백그라운드에서 관심 종목의 차트 데이터를 캐싱(Warming)합니다."""
    def worker():
        try:
            # [수정] 1. 글로벌 지수 데이터 백그라운드 예열 (로직 개선)
            logger.info("[Cache] 글로벌 지수 데이터 백그라운드 예열 시작")
            from modules import market, analysis
            
            # 국내 지수 이름 집합
            domestic_indices_names = { "코스피": "KOSPI", "코스피200": "KOSPI200", "코스닥": "KOSDAQ", "코스닥150": "KOSDAQ150" }
            
            def _prefetch_worker(name, ticker):
                try:
                    if name in domestic_indices_names:
                        # 국내 지수는 KIS API 우선 조회 로직을 태움
                        analysis.get_domestic_index_data(domestic_indices_names[name])
                    else:
                        # 해외 지수는 yfinance 조회 로직을 태움 (내부 캐싱 활용)
                        _api().get_chart_data(ticker, is_overseas=True)
                except Exception as e:
                    logger.debug(f"[Cache] Index pre-fetch failed for {name}: {e}")

            # 병렬 실행
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(_prefetch_worker, name, ticker) for name, ticker in market.ALL_INDICES]
                concurrent.futures.wait(futures)

            import config
            stocks = []
            for key in ["stocks_kr", "etfs_kr", "stocks_us", "etfs_us"]:
                stocks.extend([(s['code'], 'us' in key) for s in config.session.stock_data.get(key, [])])

            if not stocks: return
            
            # 중복 제거
            unique_stocks = []
            seen = set()
            for c, ovs in stocks:
                if c not in seen:
                    seen.add(c)
                    unique_stocks.append((c, ovs))
            
            logger.info(f"[Cache] 백그라운드 예열(Warming) 시작: 총 {len(unique_stocks)}종목")
            
            # 모의투자는 시스템 트레이딩 API 호출 방해를 피하기 위해 여유를 둠 (실전:0.1초, 모의:1.0초)
            delay = 0.1
            
            for code, is_overseas in unique_stocks:
                try:
                    # 캐시 적중 시 API 호출 생략 처리 로직은 _get_cached_chart 안에 이미 포함됨
                    # [Fix 2026-08-09] realtime=False. 이 호출은 반환값을 쓰지 않는다 —
                    #  목적은 일봉 캐시를 채우는 것뿐인데, 기본값(realtime=True)이면 캐시가
                    #  적중해도 종목마다 현재가 오버레이 API를 1건씩 더 부르고 그 결과를 버렸다.
                    #  해외는 데이마켓 세션 중 거래소 후보 순회(BAQ→NAS 등)까지 겹쳐 종목당
                    #  2콜이 된다. delay=0.1과 맞물려 '아무것도 안 한 상태'에서 8 TPS가 나갔다.
                    #  당일 봉은 실제 조회 시점에 오버레이되므로 신선도 손실이 없다.
                    _api().get_chart_data(code, is_overseas=is_overseas, realtime=False)
                except Exception as e:
                    logger.debug(f"[Cache] 예열 중 오류({code}): {e}")
                
                time.sleep(delay)
                
            logger.info("[Cache] 백그라운드 예열 완료")
        except Exception as e:
            logger.error(f"[Cache] 예열 워커 오류: {e}")

    t = threading.Thread(target=worker, daemon=True, name="CacheWarmer")
    t.start()
    return t # [수정] 테스트 코드에서 제어할 수 있도록 스레드 객체 반환

_OVERVIEW_WARMER_STARTED = False
#  개요 예열 결과를 화면이 마지막으로 쓴 시각. 기동 시각으로 시작해, 아무도 개요를
#  열지 않으면 워머가 조용해진다(첫 화면의 이점은 그대로 두기 위해 0 이 아니라 지금이다).
_WARM_LAST_CONSUMED = [time.time()]


def note_warm_consumer():
    """화면이 예열 결과를 소비했다 — 워머를 다시 깨우는 신호."""
    _WARM_LAST_CONSUMED[0] = time.time()


def warm_idle_seconds():
    """개요 예열 결과가 마지막으로 쓰인 뒤 흐른 시간(초)."""
    return time.time() - _WARM_LAST_CONSUMED[0]

def start_overview_warmer():
    """[최적화] 해외 종목 시세/시장 지수를 백그라운드에서 주기적으로 마이크로 캐시에 예열한다.

    '시장 지수 조회'/'종목 시세 분석'(해외) 개요 화면이 임계경로에서 무거운 조회 없이
    예열된 캐시를 즉시 읽도록 하여 체감 지연을 줄인다. (국내 종목 현재가·체결강도는 KRX/NXT
    시간대 무관하게 매 실행마다 라이브로 조회하므로 예열하지 않는다.)
    모의투자(2 TPS)는 시스템 트레이딩과 TPS를 다투므로 기본 비활성화한다.
    """
    global _OVERVIEW_WARMER_STARTED
    if _OVERVIEW_WARMER_STARTED:
        return None
    if not getattr(config, 'OVERVIEW_WARM_ENABLED', True):
        return None
    if config.session.is_toss:
        return None  # 토스 모드는 별도 캐시 경로 사용

    interval = max(5, int(getattr(config, 'OVERVIEW_WARM_INTERVAL_SEC', 15)))
    #  [Fix 2026-09-04] 예열은 '사람이 개요 화면을 볼 때'만 값이 있다. 종전에는 기동하면
    #   프로세스가 사는 내내 15초마다 돌았다 — 장이 닫힌 새벽에도, 아무도 화면을 보지
    #   않는 동안에도 하루 5,760 사이클씩 TradingView 일괄 조회와 yfinance fast_info(지수
    #   여러 개, 워커 4)를 냈다. 램 1GB 라즈베리파이에서 순수 낭비이고, 외부 소스의
    #   레이트리밋을 공짜로 갉아먹는다(레이트리밋에 걸리면 조용한 폴백이 생긴다).
    #   달력을 보지 않고 '쓰이는가'로 가른다 — 유휴면 조회를 건너뛰고, 화면이 예열 결과를
    #   읽는 순간(note_warm_consumer) 다음 순회부터 즉시 되살아난다. 유휴 중 화면을 열면
    #   그 화면이 자기 예열을 인라인으로 돌리므로(종전 폴백 경로) 결과는 같고 조금 느릴 뿐이다.
    idle_after = max(interval, int(getattr(config, 'OVERVIEW_WARM_IDLE_SEC', 600)))

    def worker():
        logger.info(f"[Warm] 개요 백그라운드 예열 시작 (주기 {interval}s · 유휴 {idle_after}s 후 대기)")
        idle_logged = False
        while True:
            if warm_idle_seconds() > idle_after:
                if not idle_logged:
                    logger.info("[Warm] 개요를 보는 사람이 없어 예열을 멈춘다(화면을 열면 재개).")
                    idle_logged = True
                time.sleep(interval)
                continue
            if idle_logged:
                logger.info("[Warm] 개요 예열 재개")
                idle_logged = False
            try:
                stock_data = config.session.stock_data or {}
                ovs_codes = []
                seen = set()
                for key in ["stocks_us", "etfs_us"]:
                    for s in stock_data.get(key, []):
                        c = s.get('code')
                        if c and c not in seen:
                            seen.add(c); ovs_codes.append(c)

                # 해외: TradingView 일괄(HTTP 1회, TPS 무관) 예열은 저비용이므로 항상 수행
                if ovs_codes:
                    try:
                        prefetch_multiple_current_prices(ovs_codes, is_overseas=True)
                    except Exception as e:
                        logger.debug(f"[Warm] 해외 예열 오류: {e}")

                # 시장 지수(메뉴1): 해외/지표 지수의 fast_info 예열. 60초 TTL이 네트워크를 자체 제한하므로
                # 매 사이클 호출해도 대부분 캐시 적중이라 저비용이다. (국내 지수는 KIS 경로라 제외)
                try:
                    from modules import market as _market
                    _domestic = {"코스피", "코스닥", "코스피200", "코스닥150"}
                    idx_tickers = [tk for nm, tk in _market.ALL_INDICES if nm not in _domestic]
                    if idx_tickers:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _ex:
                            list(_ex.map(lambda tk: _api().get_yf_fast_info(tk), idx_tickers))
                except Exception as e:
                    logger.debug(f"[Warm] 지수 예열 오류: {e}")
            except Exception as e:
                logger.error(f"[Warm] 개요 예열 루프 오류: {e}")
            time.sleep(interval)

    t = threading.Thread(target=worker, daemon=True, name="OverviewWarmer")
    t.start()
    _OVERVIEW_WARMER_STARTED = True
    return t
