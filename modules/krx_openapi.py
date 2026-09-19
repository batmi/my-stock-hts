"""KRX Open API(openapi.krx.co.kr) — 공식 일별 확정 데이터의 단일 입구.

[왜 · 2026-09-17] data.krx.co.kr 화면용 엔드포인트를 pykrx/FDR 로 긁는 것은 KRX Data
 Marketplace 약관(제10조 제2호, 자동화 수단 수집 금지)에 걸린다. 감사 배치 뒤 실제로 IP 가
 1일 차단됐고("KDM 이용 제한 안내"), 안내문이 가리키는 공식 경로가 이 Open API 다.
 여기에 붙이면 **KRX_ID/KRX_PW(웹 로그인 계정)가 필요 없다** — 인증키 하나(KRX_OPENAPI_KEY)로
 종목 일봉·지수·파생지수(V코스피200)·선물·금현물·종목기본정보를 받는다.

[계약 — 실측 2026-09-18, 샘플 엔드포인트]
 · GET https://data-dbg.krx.co.kr/svc/apis/{category}/{api_id}.json?basDd=YYYYMMDD
   헤더 AUTH_KEY: <인증키>. 응답 {"OutBlock_1": [ {...}, ... ]} — 값은 전부 문자열,
   없는 값은 빈 문자열("").
 · 조회 단위가 **기준일 하루**다. 한 호출이 그 날의 전 종목(≈2,800행)을 돌려주므로
   종목별 조회가 아니라 **날짜별 스냅샷을 로컬(SQLite)에 누적**하고 종목은 그 위에서 자른다.
   같은 이유로 관심종목 44개든 감사 유니버스 2,800개든 비용이 같다.
 · 일별 확정 데이터만 있다. **전일 데이터가 다음 영업일 08:00 에 실린다** — 당일·장중 없음.
   그래서 오늘 봉은 호출부(krx_daily)가 다른 소스로 덧대고, 여기서는 '실릴 수 있는 마지막
   날짜'(latest_available_dd) 이후는 묻지 않는다.
 · 한도: 인증키당 하루 10,000회(자정 리셋, HTTP 429). 서비스별로 이용신청·승인이 따로
   있어 미승인 API 는 HTTP 401 {"respMsg":"Unauthorized API Call"} 이다.
 · 휴장일은 빈 OutBlock_1 이다. 그래서 빈 응답을 '휴장'으로 기록해 두되(다시 묻지 않는다),
   최근 며칠의 빈 응답은 '아직 안 실림'일 수 있어 TTL 뒤 다시 묻는다.

[규약]
 · 조회 실패는 None, 데이터 없음은 빈 프레임/빈 dict — 호출부가 폴백할 수 있어야 한다
   ([[unknown-vs-empty]]).
 · 앱 안에서는 한 요청당 호출 수를 KRX_OPENAPI_MAX_INLINE_CALLS 로 막는다. 첫 적재(수년치
   × 시장 2 = 수천 콜)는 tools/krx_openapi_backfill.py 로 한 번 한다. 그 뒤 앱은 매일
   빠진 며칠만 받는다. 캡을 넘게 비어 있으면 **부분 이력을 돌려주지 않고 None** 이다 —
   구멍 난 시계열로 지표를 재면 조용히 틀린다.
 · 라즈베리파이는 이 DB 파일(data/krx_openapi.db)을 맥에서 복사해 와도 된다 — 내용은
   순수 확정 데이터라 기기 간 차이가 없다.
"""
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

import config

logger = logging.getLogger(__name__)

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
ENV_KEY = "KRX_OPENAPI_KEY"

# api_id -> (category, 표시명). 이 표에 없는 서비스는 부르지 않는다.
API = {
    "stk_bydd_trd": ("sto", "유가증권 일별매매정보"),
    "ksq_bydd_trd": ("sto", "코스닥 일별매매정보"),
    "knx_bydd_trd": ("sto", "코넥스 일별매매정보"),
    "etf_bydd_trd": ("etp", "ETF 일별매매정보"),
    "etn_bydd_trd": ("etp", "ETN 일별매매정보"),
    "stk_isu_base_info": ("sto", "유가증권 종목기본정보"),
    "ksq_isu_base_info": ("sto", "코스닥 종목기본정보"),
    "knx_isu_base_info": ("sto", "코넥스 종목기본정보"),
    "kospi_dd_trd": ("idx", "KOSPI 시리즈 일별시세정보"),
    "kosdaq_dd_trd": ("idx", "KOSDAQ 시리즈 일별시세정보"),
    "drvprod_dd_trd": ("idx", "파생상품지수 시세정보"),
    "fut_bydd_trd": ("drv", "선물 일별매매정보"),
    "gold_bydd_trd": ("gen", "금시장 일별매매정보"),
}
#  종목 일봉 한 벌 = 주권 3시장 + ETF + ETN. 관심종목·차트·검증에 ETF/ETN(예: 0080G0)도 들어오므로
#   같은 표(stock_daily)에 쌓는다 — 호출부는 코드만 알고 시장을 모른다.
STOCK_APIS = ("stk_bydd_trd", "ksq_bydd_trd", "knx_bydd_trd", "etf_bydd_trd", "etn_bydd_trd")
STOCK_API_MARKET = {"stk_bydd_trd": "KOSPI", "ksq_bydd_trd": "KOSDAQ", "knx_bydd_trd": "KONEX",
                    "etf_bydd_trd": "ETF", "etn_bydd_trd": "ETN"}
BASE_INFO_APIS = ("stk_isu_base_info", "ksq_isu_base_info", "knx_isu_base_info")
# 앱이 지수 화면·백테스트에 쓰는 지수 — analysis 의 표기 → (api_id, 정규화한 지수명)
INDEX_KEYS = {
    "KOSPI": ("kospi_dd_trd", "코스피"),
    "KOSDAQ": ("kosdaq_dd_trd", "코스닥"),
    "KOSPI200": ("kospi_dd_trd", "코스피200"),
    "KOSDAQ150": ("kosdaq_dd_trd", "코스닥150"),
    "VKOSPI": ("drvprod_dd_trd", "코스피200변동성지수"),
}
K200_FUTURES_PROD = "코스피200선물"        # PROD_NM 정규화값
GOLD_ISU_CD = "04020000"                  # 금 99.99K (1kg)

PUBLISH_HOUR = 8            # 전일 데이터가 실리는 시각(다음 영업일 08:00) — 실측 전 가정, 아래 latest_available_dd 주석
RECENT_EMPTY_TTL_SEC = 3600  # 최근 며칠의 빈 응답('아직 안 실림'일 수 있다)을 다시 물어보기까지
RECENT_EMPTY_DAYS = 3        # 그 '최근'의 범위(평일 기준)
_COLUMNS = ["date", "open", "high", "low", "close", "volume"]

_DB_LOCK = threading.RLock()
_CALL_LOCK = threading.RLock()
_LAST_CALL_TS = [0.0]
_DISABLED_UNTIL = [0.0]     # 401/429 뒤 헛호출을 막는 쿨다운(epoch)
_DISABLED_REASON = [""]


class OpenAPIError(RuntimeError):
    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient      # 빈 본문·네트워크·5xx — 잠깐 뒤 다시 물어볼 만한 것


# ---------------------------------------------------------------------------
# 설정·상태
# ---------------------------------------------------------------------------
def api_key():
    return (os.environ.get(ENV_KEY) or "").strip()


def is_configured():
    """인증키가 있는가 — 저장소(로컬 DB)를 읽어도 되는 조건. 쿨다운과 무관하다."""
    return bool(api_key())


def is_available():
    """인증키가 있고 쿨다운(401/429)에 걸려 있지 않은가 — **네트워크 호출**이 가능한 조건.

    조회 함수(stock_daily 등)는 이걸로 막지 않는다. 429 로 자정까지 쿨다운이 걸린 날에도
    이미 받아 둔 확정분은 그대로 읽어야 한다(_ensure_or_none 이 꼬리만 잘라 준다) —
    안 그러면 한도 한 번에 지수·선물·금이 하루 종일 사라진다(스크래핑 폴백은 기본 OFF).
    """
    return is_configured() and time.time() >= _DISABLED_UNTIL[0]


def status_text():
    """기동 점검용 (사용 가능 여부, 한 줄)."""
    if not api_key():
        return False, (f"KRX Open API 미사용 — {ENV_KEY} 미설정. openapi.krx.co.kr 에서 인증키를 받아 "
                       f"~/.htsrc 에 export 하고 재기동하세요(종목 일봉·지수·금현물은 종전 소스로 폴백).")
    if time.time() < _DISABLED_UNTIL[0]:
        return False, f"KRX Open API 일시 비활성 — {_DISABLED_REASON[0]}"
    return True, "KRX Open API 사용 (종목 일봉·지수·V코스피200·코스피200선물·금현물, 전일 확정분)"


def _db_path():
    return getattr(config, "KRX_OPENAPI_DB_PATH",
                   os.path.join(getattr(config, "DATA_DIR", "data"), "krx_openapi.db"))


def _max_inline_calls():
    try:
        return int(getattr(config, "KRX_OPENAPI_MAX_INLINE_CALLS", 60))
    except (TypeError, ValueError):
        return 60


def _call_interval():
    try:
        return float(getattr(config, "KRX_OPENAPI_CALL_INTERVAL_SEC", 0.2))
    except (TypeError, ValueError):
        return 0.2


# ---------------------------------------------------------------------------
# 저장소
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetched (
    api_id TEXT NOT NULL, bas_dd TEXT NOT NULL, n INTEGER NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (api_id, bas_dd));
CREATE TABLE IF NOT EXISTS stock_daily (
    bas_dd TEXT NOT NULL, code TEXT NOT NULL, market TEXT, name TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL, marcap REAL, shares REAL,
    PRIMARY KEY (bas_dd, code));
CREATE INDEX IF NOT EXISTS ix_stock_daily_code ON stock_daily (code, bas_dd);
CREATE TABLE IF NOT EXISTS index_daily (
    bas_dd TEXT NOT NULL, api_id TEXT NOT NULL, name TEXT NOT NULL, cls TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL, marcap REAL,
    PRIMARY KEY (bas_dd, api_id, name));
CREATE INDEX IF NOT EXISTS ix_index_daily_name ON index_daily (api_id, name, bas_dd);
CREATE TABLE IF NOT EXISTS futures_daily (
    bas_dd TEXT NOT NULL, prod TEXT NOT NULL, session TEXT NOT NULL, isu_cd TEXT NOT NULL, name TEXT,
    open REAL, high REAL, low REAL, close REAL, spot REAL, settle REAL, volume REAL, value REAL, oi REAL,
    PRIMARY KEY (bas_dd, session, isu_cd));
CREATE INDEX IF NOT EXISTS ix_futures_prod ON futures_daily (prod, session, bas_dd);
CREATE TABLE IF NOT EXISTS gold_daily (
    bas_dd TEXT NOT NULL, isu_cd TEXT NOT NULL, name TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL,
    PRIMARY KEY (bas_dd, isu_cd));
CREATE TABLE IF NOT EXISTS isu_base (
    code TEXT NOT NULL PRIMARY KEY, isin TEXT, name TEXT, abbrv TEXT, market TEXT, secugrp TEXT,
    kind TEXT, list_dd TEXT, parval TEXT, shares REAL, bas_dd TEXT);
"""


def _connect():
    path = _db_path()
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    return conn


def _num(text):
    """KRX 문자열 숫자 → float. 빈 문자열·'-' 는 None."""
    if text is None:
        return None
    s = str(text).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_name(text):
    return "".join(str(text or "").split())


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------
def _throttle():
    interval = _call_interval()
    with _CALL_LOCK:
        wait = _LAST_CALL_TS[0] + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL_TS[0] = time.time()


def _disable(seconds, reason):
    _DISABLED_UNTIL[0] = time.time() + float(seconds)
    _DISABLED_REASON[0] = reason
    logger.warning(f"[KRX-OPENAPI] {reason} — {int(seconds)}초 동안 호출을 멈춘다")


def _seconds_until_midnight():
    now = datetime.now()
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return max(60.0, (nxt - now).total_seconds())


TRANSIENT_RETRIES = 2          # 빈 본문·네트워크 오류·5xx 는 잠깐의 것이라 몇 번 더 묻는다
TRANSIENT_RETRY_WAIT_SEC = 1.5


def _call(api_id, bas_dd):
    """한 (서비스, 기준일)을 받는다 → 행 리스트. 실패는 OpenAPIError.

    [왜 재시도 · 2026-09-19] 백필 5,350콜째에 knx_bydd_trd 20190426 이 본문 '' 로 왔다(바로
    다시 부르니 200·47KB). 한 번의 빈 응답이 수천 콜 배치를 멈추게 하면 안 된다 — 일시적인
    것(빈 본문·네트워크·5xx)만 짧게 재시도하고, 401/403/429·구조 이상은 그대로 올린다.
    """
    last = None
    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            return _call_once(api_id, bas_dd)
        except OpenAPIError as e:
            if not e.transient or attempt == TRANSIENT_RETRIES:
                raise
            last = e
            logger.debug(f"[KRX-OPENAPI] 일시 실패 재시도 {attempt + 1}/{TRANSIENT_RETRIES}: {e}")
            time.sleep(TRANSIENT_RETRY_WAIT_SEC)
    raise last      # pragma: no cover - 루프가 반드시 return/raise 한다


def _call_once(api_id, bas_dd):
    if api_id not in API:
        raise OpenAPIError(f"모르는 서비스: {api_id}")
    key = api_key()
    if not key:
        raise OpenAPIError(f"{ENV_KEY} 미설정")
    if time.time() < _DISABLED_UNTIL[0]:
        raise OpenAPIError(f"쿨다운 중: {_DISABLED_REASON[0]}")
    import requests
    cat = API[api_id][0]
    url = f"{BASE_URL}/{cat}/{api_id}.json"
    _throttle()
    try:
        resp = requests.get(url, params={"basDd": str(bas_dd)},
                            headers={"AUTH_KEY": key, "User-Agent": "my-stock-hts"}, timeout=30)
    except Exception as e:      # noqa: BLE001 - 네트워크 실패는 한 종류로 올린다
        raise OpenAPIError(f"요청 실패 {api_id} {bas_dd}: {e}", transient=True) from e
    if resp.status_code == 429:
        _disable(_seconds_until_midnight(), "일 호출 한도(10,000회) 초과")
        raise OpenAPIError("일 호출 한도 초과", status=429)
    if resp.status_code in (401, 403):
        _disable(600, f"HTTP {resp.status_code} ({API[api_id][1]}) — 인증키 미승인이거나 이 서비스의 "
                      f"이용신청이 승인되지 않았다(openapi.krx.co.kr 마이페이지 확인)")
        raise OpenAPIError(f"인증 거부 HTTP {resp.status_code} {api_id}", status=resp.status_code)
    if resp.status_code != 200:
        raise OpenAPIError(f"HTTP {resp.status_code} {api_id} {bas_dd}: {resp.text[:120]}",
                           status=resp.status_code, transient=resp.status_code >= 500)
    try:
        data = resp.json()
    except ValueError as e:
        raise OpenAPIError(f"JSON 아님 {api_id} {bas_dd}: {resp.text[:120]!r}", transient=True) from e
    if isinstance(data, dict) and "OutBlock_1" in data:
        rows = data.get("OutBlock_1") or []
    elif isinstance(data, dict) and str(data.get("respCode", "")).startswith("4"):
        raise OpenAPIError(f"{api_id} {bas_dd}: {data.get('respMsg')}", status=int(data["respCode"]))
    else:
        raise OpenAPIError(f"응답 구조 이상 {api_id} {bas_dd}: {list(data)[:5] if isinstance(data, dict) else type(data)}")
    return [r for r in rows if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------
def _store(conn, api_id, bas_dd, rows):
    bas_dd = str(bas_dd)
    if api_id in STOCK_APIS:
        market = STOCK_API_MARKET.get(api_id, "")
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(bas_dd, str(r.get("ISU_CD", "")).strip(), (r.get("MKT_NM") or market).strip().upper(), r.get("ISU_NM"),
              _num(r.get("TDD_OPNPRC")), _num(r.get("TDD_HGPRC")), _num(r.get("TDD_LWPRC")),
              _num(r.get("TDD_CLSPRC")), _num(r.get("ACC_TRDVOL")), _num(r.get("ACC_TRDVAL")),
              _num(r.get("MKTCAP")), _num(r.get("LIST_SHRS")))
             for r in rows if r.get("ISU_CD")])
    elif api_id in ("kospi_dd_trd", "kosdaq_dd_trd", "drvprod_dd_trd"):
        conn.executemany(
            "INSERT OR REPLACE INTO index_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(bas_dd, api_id, _norm_name(r.get("IDX_NM")), r.get("IDX_CLSS"),
              _num(r.get("OPNPRC_IDX")), _num(r.get("HGPRC_IDX")), _num(r.get("LWPRC_IDX")),
              _num(r.get("CLSPRC_IDX")), _num(r.get("ACC_TRDVOL")), _num(r.get("ACC_TRDVAL")),
              _num(r.get("MKTCAP")))
             for r in rows if r.get("IDX_NM")])
    elif api_id == "fut_bydd_trd":
        conn.executemany(
            "INSERT OR REPLACE INTO futures_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(bas_dd, _norm_name(r.get("PROD_NM")), str(r.get("MKT_NM") or "").strip(),
              str(r.get("ISU_CD", "")).strip(), r.get("ISU_NM"),
              _num(r.get("TDD_OPNPRC")), _num(r.get("TDD_HGPRC")), _num(r.get("TDD_LWPRC")),
              _num(r.get("TDD_CLSPRC")), _num(r.get("SPOT_PRC")), _num(r.get("SETL_PRC")),
              _num(r.get("ACC_TRDVOL")), _num(r.get("ACC_TRDVAL")), _num(r.get("ACC_OPNINT_QTY")))
             for r in rows if r.get("ISU_CD")])
    elif api_id == "gold_bydd_trd":
        conn.executemany(
            "INSERT OR REPLACE INTO gold_daily VALUES (?,?,?,?,?,?,?,?,?)",
            [(bas_dd, str(r.get("ISU_CD", "")).strip(), r.get("ISU_NM"),
              _num(r.get("TDD_OPNPRC")), _num(r.get("TDD_HGPRC")), _num(r.get("TDD_LWPRC")),
              _num(r.get("TDD_CLSPRC")), _num(r.get("ACC_TRDVOL")), _num(r.get("ACC_TRDVAL")))
             for r in rows if r.get("ISU_CD")])
    elif api_id in BASE_INFO_APIS:
        conn.executemany(
            "INSERT OR REPLACE INTO isu_base VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(str(r.get("ISU_SRT_CD", "")).strip(), r.get("ISU_CD"), r.get("ISU_NM"), r.get("ISU_ABBRV"),
              str(r.get("MKT_TP_NM") or "").strip().upper(), r.get("SECUGRP_NM"),
              r.get("KIND_STKCERT_TP_NM"), r.get("LIST_DD"), r.get("PARVAL"), _num(r.get("LIST_SHRS")),
              bas_dd)
             for r in rows if r.get("ISU_SRT_CD")])
    conn.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?,?)", (api_id, bas_dd, len(rows), time.time()))


# ---------------------------------------------------------------------------
# 날짜
# ---------------------------------------------------------------------------
def _weekdays(start_dd, end_dd):
    """[start, end] 의 평일 'YYYYMMDD' 목록(오름차순). 휴장일은 빈 응답으로 걸러진다."""
    s = datetime.strptime(str(start_dd), "%Y%m%d").date()
    e = datetime.strptime(str(end_dd), "%Y%m%d").date()
    out = []
    d = s
    while d <= e:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def _prev_weekday(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def latest_available_dd(now=None):
    """지금 물어도 되는 가장 최근 기준일.

    KRX 는 전일 데이터를 다음 영업일 PUBLISH_HOUR 에 싣는다(안내상 08:00 — 실측으로 확정
    되면 상수를 고칠 것). 그 전에는 그 전 평일까지만 묻는다. 휴장일은 빈 응답으로 기록될 뿐
    해가 되지 않으므로 달력은 평일만 본다(공휴일 API 를 여기서 부르지 않는다).
    """
    now = now or datetime.now()
    d = _prev_weekday(now.date())
    if now.hour < PUBLISH_HOUR:
        d = _prev_weekday(d)
    return d.strftime("%Y%m%d")


def _is_recent(bas_dd, now=None):
    """'아직 안 실렸을 수 있는' 최근 평일인가(빈 응답을 영구 휴장으로 굳히지 않는 범위)."""
    now = now or datetime.now()
    d = now.date()
    for _ in range(RECENT_EMPTY_DAYS):
        d = _prev_weekday(d)
    return str(bas_dd) >= d.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 보장(ensure) — 빠진 날짜를 받아 채운다
# ---------------------------------------------------------------------------
def missing_days(api_ids, start_dd, end_dd, now=None):
    """아직 안 받은 (api_id, bas_dd) 목록 — 최신 날짜부터."""
    days = _weekdays(start_dd, end_dd)
    if not days:
        return []
    now_ts = time.time()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT api_id, bas_dd, n, ts FROM fetched WHERE bas_dd BETWEEN ? AND ?",
            (days[0], days[-1])).fetchall()
    done = {}
    for api_id, dd, n, ts in rows:
        # 최근 며칠의 빈 응답은 TTL 뒤 다시 묻는다(전일 데이터가 아직 안 실렸을 수 있다).
        if n == 0 and _is_recent(dd, now) and (now_ts - ts) > RECENT_EMPTY_TTL_SEC:
            continue
        done[(api_id, dd)] = True
    out = []
    for dd in reversed(days):
        for api_id in api_ids:
            if (api_id, dd) not in done:
                out.append((api_id, dd))
    return out


def ensure(api_ids, start_dd, end_dd, max_calls=None, now=None, progress=None, workers=1):
    """[start, end] 의 빠진 날짜를 최신부터 받아 채운다.

    반환 (남은 결손 수, 이번에 한 호출 수). max_calls 를 넘기면 거기서 멈춘다 — 남은 결손이
    0이 아니면 호출부는 그 구간을 **완전하다고 보면 안 된다**.
    workers>1 이면 호출만 병렬(백필 도구용 — 응답 한 건이 2초 남짓이라 직렬은 수천 콜에 몇 시간이다).
    저장은 한 스레드씩 한다. 앱 안(인라인)은 1 이다.
    """
    api_ids = [a for a in api_ids if a in API]
    todo = missing_days(api_ids, start_dd, end_dd, now=now)
    if not todo:
        return 0, 0
    cap = _max_inline_calls() if max_calls is None else int(max_calls)
    batch = todo[:cap]
    calls = 0

    def _one(item):
        api_id, dd = item
        return api_id, dd, _call(api_id, dd)

    if int(workers) <= 1:
        results = map(_one, batch)
        pool = None
    else:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=int(workers))
        results = pool.map(_one, batch)
    try:
        for api_id, dd, rows in results:      # 실패는 그대로 올린다(부분 적재는 무해 — fetched 에 안 적힌다)
            calls += 1
            with _DB_LOCK, _connect() as conn:
                _store(conn, api_id, dd, rows)
            if progress:
                progress(api_id, dd, len(rows), calls, len(todo))
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    remaining = len(todo) - calls
    if remaining > 0:
        logger.warning(
            f"[KRX-OPENAPI] {start_dd}~{end_dd} 결손 {len(todo)}건 중 {calls}건만 받았다(호출 캡 {cap}). "
            f"남은 {remaining}건은 tools/krx_openapi_backfill.py 로 한 번에 받을 것")
    return remaining, calls


def _start_dd(lookback_days, end_dd):
    e = datetime.strptime(end_dd, "%Y%m%d").date()
    return (e - timedelta(days=int(lookback_days))).strftime("%Y%m%d")


STORED_TAIL_MAX_DAYS = 3      # 쿨다운 중 잘라 낼 수 있는 꼬리(평일) 최대 — 연휴 뒤 첫날 정도


def _stored_span(api_ids, start_dd, end_dd, now=None):
    """네트워크 없이 저장소만으로 완전한 구간 (start, end') — 결손이 꼬리(최신 쪽)에만 있으면
    그 앞까지로 끝을 당긴다. 결손이 중간에 있거나 남는 날이 없으면 None."""
    todo = missing_days(api_ids, start_dd, end_dd, now=now)
    if not todo:
        return start_dd, end_dd
    first_missing = min(dd for _, dd in todo)
    days = _weekdays(start_dd, end_dd)
    kept = [d for d in days if d < first_missing]
    # 잘라도 되는 건 '아직 못 받은 최근 며칠'뿐이다 — 그보다 깊은 결손은 창 자체가 짧아져
    #  지표(EMA·52주)가 틀어지므로 부분 이력 대신 None.
    if not kept or len(days) - len(kept) > STORED_TAIL_MAX_DAYS:
        return None
    return start_dd, kept[-1]


def _ensure_or_none(api_ids, lookback_days, max_calls, now=None):
    """구간을 채우고 완전하면 (start, end), 아니면 None.

    네트워크를 못 쓰는 동안(쿨다운·호출 실패)은 저장소만으로 완전한 구간을 돌려준다 — 최신
    하루가 비어 있을 뿐이면 그 전날까지. 호출부(krx_daily._fetch_openapi)가 그 뒤는 FDR 로
    덧대므로 오늘 봉이 빠지지 않는다. 중간에 구멍이 있으면 부분 이력 대신 None(모듈 규약).
    """
    end_dd = latest_available_dd(now)
    start_dd = _start_dd(lookback_days, end_dd)
    if time.time() < _DISABLED_UNTIL[0]:
        return _stored_span(api_ids, start_dd, end_dd, now=now)
    try:
        remaining, _ = ensure(api_ids, start_dd, end_dd, max_calls=max_calls, now=now)
    except OpenAPIError as e:
        logger.warning(f"[KRX-OPENAPI] 적재 실패({','.join(api_ids)}): {e} — 받아 둔 확정분만 쓴다")
        return _stored_span(api_ids, start_dd, end_dd, now=now)
    if remaining > 0:
        return None
    return start_dd, end_dd


def _finish(rows, source):
    if not rows:
        return None
    out = pd.DataFrame(rows, columns=_COLUMNS)
    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]
    if out.empty:
        return None
    for col in ("open", "high", "low"):
        # 빈 값(None) = 그 지수엔 시·고·저가 없다 → 종가로 평탄화(krx_data._finish 와 같다).
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(out["close"].astype(float))
    # 0 원 = 거래정지일(실측: 삼성전자 2018-04-30~05-03 분할 정지, 시·고·저·거래량 0 에 종가만).
    #  0 원 봉은 True Range 를 터뜨리므로 버린다 — krx_daily._normalize 와 같은 기준.
    out = out[(out["open"] > 0) & (out["high"] > 0) & (out["low"] > 0)]
    if out.empty:
        return None
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out = out.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)
    out.attrs["source"] = source
    return out


# ---------------------------------------------------------------------------
# 공개 조회
# ---------------------------------------------------------------------------
def stock_daily(code, lookback_days, max_calls=None, now=None):
    """종목 일봉 ['date','open','high','low','close','volume'] (확정분, 최신 = 전일).

    구간이 완전하지 않으면 None — 부분 이력은 돌려주지 않는다(모듈 독스트링 규약).
    상장 전·폐지 후 날짜는 그냥 없다(폐지 종목도 마지막 봉까지 그대로 나온다 —
    [[backtest-data-end-exit]] 가 다루는 바로 그 표본이다).
    """
    if not is_configured():
        return None
    code = str(code or "").strip()
    span = _ensure_or_none(STOCK_APIS, lookback_days, max_calls, now)
    if span is None:
        return None
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT bas_dd, open, high, low, close, volume, shares FROM stock_daily "
            "WHERE code=? AND bas_dd BETWEEN ? AND ? ORDER BY bas_dd", (code, span[0], span[1])).fetchall()
    df = _finish([r[:6] for r in adjust_splits(rows)], "OPENAPI")
    return df if df is not None else pd.DataFrame(columns=_COLUMNS)


SPLIT_MIN_RATIO = 1.5     # 상장주식수가 이 배수 이상 변하고
SPLIT_GAP_TOL = 0.15      # 직전 종가/당일 시가가 같은 배수와 15% 안에서 맞으면 분할·병합으로 본다


def adjust_splits(rows):
    """원주가 → 수정주가. rows = [(bas_dd, open, high, low, close, volume, shares), ...] 오름차순.

    [왜] Open API 일별매매는 **그 날의 원주가**다(pykrx adjusted=True·FDR 은 수정주가).
     액면분할(삼성전자 2018-05-04 50:1)·병합이 조회 창 안에 있으면 시계열이 한 번에 1/50 로
     꺾여 EMA120·52주 밴드·ATR 이 전부 틀린다. 폴백 소스와 기준을 맞추려면 여기서 고쳐야 한다.
    [어떻게] 분할·병합은 상장주식수(LIST_SHRS)가 r 배 되면서 가격이 1/r 이 되는 날이다.
     유상증자·전환은 주식수만 늘고 가격은 그만큼 뛰지 않으므로 **두 조건이 같이 맞을 때만**
     보정한다(오탐이 나면 없는 분할을 만들어 낸다 — 두 조건 결합이 그 방어다).
     보정은 그 날 **이전** 봉의 가격을 1/r, 거래량을 r 배 한다(수정주가 관행).
    """
    rows = [list(r) for r in rows]
    if len(rows) < 2:
        return rows
    factors = []          # (index, price_mult) — 이 index 이전 봉에 곱한다
    for i in range(1, len(rows)):
        sh_prev, sh = rows[i - 1][6], rows[i][6]
        c_prev, o = rows[i - 1][4], rows[i][1]
        if not sh_prev or not sh or not c_prev or not o:
            continue
        r = sh / sh_prev
        if r >= SPLIT_MIN_RATIO or r <= 1.0 / SPLIT_MIN_RATIO:
            gap = c_prev / o           # 분할이면 ≈ r
            if abs(gap / r - 1.0) <= SPLIT_GAP_TOL:
                factors.append((i, 1.0 / r))
    if not factors:
        return rows
    mult = 1.0
    j = len(factors) - 1
    for i in range(len(rows) - 1, -1, -1):
        while j >= 0 and factors[j][0] > i:
            mult *= factors[j][1]
            j -= 1
        if mult != 1.0:
            for k in (1, 2, 3, 4):
                if rows[i][k] is not None:
                    rows[i][k] = rows[i][k] * mult
            if rows[i][5] is not None:
                rows[i][5] = rows[i][5] / mult
    return rows


def index_daily(market_type, lookback_days, max_calls=None, now=None):
    """지수 일봉 — analysis 표기('KOSPI'/'KOSDAQ'/'KOSPI200'/'KOSDAQ150'/'VKOSPI')."""
    if not is_configured():
        return None
    spec = INDEX_KEYS.get(str(market_type or "").upper())
    if not spec:
        return None
    api_id, name = spec
    span = _ensure_or_none([api_id], lookback_days, max_calls, now)
    if span is None:
        return None
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT bas_dd, open, high, low, close, volume FROM index_daily "
            "WHERE api_id=? AND name=? AND bas_dd BETWEEN ? AND ? ORDER BY bas_dd",
            (api_id, name, span[0], span[1])).fetchall()
    if not rows:
        # 이름이 안 맞으면 조용히 빈 값이 된다 — 무엇이 있는지 로그에 남겨 표를 고칠 수 있게 한다
        with _DB_LOCK, _connect() as conn:
            names = [r[0] for r in conn.execute(
                "SELECT DISTINCT name FROM index_daily WHERE api_id=? AND bas_dd=?",
                (api_id, span[1])).fetchall()]
        logger.warning(f"[KRX-OPENAPI] 지수 '{name}' 이 {api_id} 응답에 없다 — 있는 이름: {names[:30]}")
        return pd.DataFrame(columns=_COLUMNS)
    return _finish(rows, "OPENAPI")


def k200_futures_daily(session="F", lookback_days=400, max_calls=None, now=None):
    """코스피200 선물 근월물 일봉. session='F'(정규/주간) | 'CM'(야간).

    근월물은 **그 날 정규장 미결제약정이 가장 큰 계약**이다(만기 연속 시계열의 관행).
    야간 세션은 같은 계약의 야간 봉을 쓴다.
    """
    if not is_configured():
        return None
    want = "야간" if str(session).upper() in ("CM", "야간", "NIGHT") else "정규"
    span = _ensure_or_none(["fut_bydd_trd"], lookback_days, max_calls, now)
    if span is None:
        return None
    with _DB_LOCK, _connect() as conn:
        front = conn.execute(
            "SELECT bas_dd, isu_cd FROM futures_daily WHERE prod=? AND session='정규' AND close>0 "
            "AND bas_dd BETWEEN ? AND ? ORDER BY bas_dd, oi DESC, volume DESC",
            (K200_FUTURES_PROD, span[0], span[1])).fetchall()
        pick = {}
        for dd, isu in front:
            pick.setdefault(dd, isu)
        if not pick:
            return pd.DataFrame(columns=_COLUMNS)
        rows = conn.execute(
            "SELECT bas_dd, isu_cd, open, high, low, close, volume FROM futures_daily "
            "WHERE prod=? AND session=? AND bas_dd BETWEEN ? AND ?",
            (K200_FUTURES_PROD, want, span[0], span[1])).fetchall()
    out = [(dd, o, h, l, c, v) for dd, isu, o, h, l, c, v in rows if pick.get(dd) == isu]
    df = _finish(out, "OPENAPI")
    return df if df is not None else pd.DataFrame(columns=_COLUMNS)


def gold_daily(lookback_days=400, max_calls=None, now=None):
    """KRX 금현물(금 99.99K, 원/g) 일봉 — 시·고·저·거래량이 실제 값이다."""
    if not is_configured():
        return None
    span = _ensure_or_none(["gold_bydd_trd"], lookback_days, max_calls, now)
    if span is None:
        return None
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT bas_dd, open, high, low, close, volume FROM gold_daily "
            "WHERE isu_cd=? AND bas_dd BETWEEN ? AND ? ORDER BY bas_dd",
            (GOLD_ISU_CD, span[0], span[1])).fetchall()
    df = _finish(rows, "OPENAPI")
    return df if df is not None else pd.DataFrame(columns=_COLUMNS)


def listing_map(max_calls=None, now=None):
    """{code: {'name','marcap','market','list_dd'}} — 종목기본정보 + 최신 일별매매의 시총. 실패 시 None.

    기본정보는 스냅샷이라 최신 기준일 하루만 받는다(2콜). 시총은 그 날의 일별매매에서 온다.
    """
    if not is_configured():
        return None
    end_dd = latest_available_dd(now)
    fresh = False
    if time.time() >= _DISABLED_UNTIL[0]:
        try:
            remaining, _ = ensure(list(BASE_INFO_APIS) + list(STOCK_APIS), end_dd, end_dd,
                                  max_calls=max_calls, now=now)
            fresh = remaining == 0
        except OpenAPIError as e:
            logger.warning(f"[KRX-OPENAPI] 상장목록 적재 실패: {e} — 받아 둔 최신 스냅샷을 쓴다")
    if not fresh:
        # 쿨다운·실패 — 목록은 하루 묵어도 무해하다(시장 판정·종목명). 마지막으로 받은 날로 대신한다.
        with _DB_LOCK, _connect() as conn:
            row = conn.execute("SELECT MAX(bas_dd) FROM fetched WHERE api_id='stk_bydd_trd' AND n>0").fetchone()
        if not row or not row[0]:
            return None
        end_dd = row[0]
    with _DB_LOCK, _connect() as conn:
        base = conn.execute("SELECT code, abbrv, name, market, list_dd, secugrp, kind FROM isu_base").fetchall()
        daily = conn.execute("SELECT code, name, market, marcap FROM stock_daily WHERE bas_dd=?",
                             (end_dd,)).fetchall()
    if not base and not daily:
        return None
    caps = {code: marcap for code, _n, _m, marcap in daily}
    out = {}
    for code, abbrv, name, market, list_dd, secugrp, kind in base:
        out[code] = {"name": (abbrv or name or "").strip(), "marcap": float(caps.get(code) or 0.0),
                     "market": (market or "").upper(), "list_dd": list_dd or "",
                     "secugrp": secugrp or "", "kind": kind or ""}
    # 종목기본정보는 주권만이다 — ETF/ETN 은 그 날 일별매매의 이름·시장으로 보탠다(종목명 검증용).
    for code, name, market, marcap in daily:
        out.setdefault(code, {"name": (name or "").strip(), "marcap": float(marcap or 0.0),
                              "market": (market or "").upper(), "list_dd": "", "secugrp": market or "", "kind": ""})
    return out


def coverage():
    """저장소 상태 요약 — 백필 도구·기동 점검용. {api_id: (첫날, 마지막날, 일수)}"""
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT api_id, MIN(bas_dd), MAX(bas_dd), COUNT(*) FROM fetched WHERE n>0 GROUP BY api_id").fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}
