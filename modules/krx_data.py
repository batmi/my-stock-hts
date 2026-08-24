"""data.krx.co.kr 공식 시세 — 국내 지수·KRX 금현물·파생(코스피200 선물·변동성지수).

[왜 이 모듈이 있나 · 2026-08-25]
 아래 넷은 여태 KRX 가 아닌 경로로 받고 있었고, 그 경로들이 각각 데이터를 깎아먹었다.

   금현물        네이버 원자재 API  → **종가만** 온다(시·고·저가 0). 모든 봉을 종가로 평탄화하니
                                     True Range 가 종가 차분이 되어 ATR·ADX 가 왜곡되고,
                                     거래량 이력이 없어 OBV 는 '-' 로 남았다.
   코스피200      tvDatafeed         → 익명 웹소켓이라 간헐 빈 응답. 거래량을 안 주므로 OBV 불가.
   코스닥150      tvDatafeed         → 위와 같고, **무료 대체 소스가 아예 없다**(yfinance·FDR 에
                                     티커 자체가 없어 404). 즉 단일 실패점이었다.
   V코스피200·선물 KIS 전용           → 모드 3(토스)·모드 1(모의)에서는 목록에서 아예 빠졌다.

 data.krx.co.kr 회원 로그인(KRX_ID/KRX_PW)이 생기면서 넷 다 KRX 공식으로 받을 수 있게 됐다.
 자격증명이 없으면 모든 함수가 None 을 돌려주고 호출부가 종전 경로로 폴백한다 — 즉 이 모듈이
 꺼져도 동작은 종전과 같다([[reimplementation-parity-gaps]] 의 수급 경로와 같은 규약).

[검증 근거 — 붙이기 전에 잰 것]
 · 금현물 : 네이버 종가와 겹치는 60일 **불일치 0**. 비로그인 요청은 HTTP 400 'LOGOUT'.
 · 지수   : 코스피·코스피200·코스닥 종가가 FDR 과 **399/399 완전일치**.
 · 선물   : 한 계약이 1콜에 229 거래일(주간 + 야간 각각). 날짜 문자열에 세션이 붙어 온다.
 · V코스피200 : 선물 응답의 SPOT_PRC 가 현물 지수다. 계약이 달라도 같은 값임을 겹치는 88일에서
              **불일치 0** 으로 확인했다(계약을 통해 읽을 뿐 값은 계약과 무관하다).

[한계 — 호출부가 반드시 알아야 한다]
 · **마감 후 확정 봉만 준다.** 장중 현재가는 없다. 지수 화면처럼 현재값이 필요한 곳은
   이 모듈로 '이력'을 받고 당일 값은 실시간 소스가 덮어야 한다(국내 일봉의 krx_daily +
   오버레이와 같은 구조).
 · V코스피200 은 **종가만** 있다(선물 응답에 현물 OHLC 가 없다). 금현물과 달리 여기서는
   평탄화가 불가피하다 — 지수 표시에는 충분하지만 그 지표는 반쪽이다.
 · 파생 시계열 조회는 **기간 2년 상한**이 있다(넘기면 빈 응답). _clamp_range 가 잘라 준다.

[pykrx 내부 의존] 금·파생은 pykrx 에 래퍼가 없어 bld 를 직접 POST 한다. 로그인 세션만
 pykrx 에서 빌려 쓰므로(`webio.get_session`) 그 지점을 _session() 하나로 격리했다 —
 pykrx 가 내부 구조를 바꾸면 여기만 고치면 된다.
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

_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"

# bld — KRX 정보데이터시스템의 화면 식별자.
_BLD_GOLD_DAILY = "dbms/MDC/STAT/standard/MDCSTAT15001"     # 금시장 일별추이
_BLD_DRV_ALL = "dbms/MDC/STAT/standard/MDCSTAT12501"        # 파생 전종목시세(그날 살아있는 계약)
_BLD_DRV_SERIES = "dbms/MDC/STAT/standard/MDCSTAT12601"     # 파생 개별계약 일별추이

# 파생 상품코드
PROD_K200_FUTURES = "KRDRVFUK2I"
PROD_VKOSPI_FUTURES = "KRDRVFUVKI"

# KRX 금 현물 99.99_1kg. 네이버 심볼(M04020000)과는 다른 체계다.
GOLD_ISU_CD = "KRD040200002"

# 지수 market_type → **KRX** 지수 티커.
#  ※ 함정: KIS 지수코드와 숫자가 겹치는데 뜻이 다르다.
#     KIS "2001" = 코스피200 / KRX "2001" = 코스닥.
#     그래서 두 표를 절대 공유하지 않는다(analysis._fetch_domestic_index_data 의 kis_code 와 별개).
INDEX_TICKERS = {
    "KOSPI": "1001",
    "KOSDAQ": "2001",
    "KOSPI200": "1028",
    "KOSDAQ150": "2203",
}

_COLUMNS = ["date", "open", "high", "low", "close", "volume"]

_MAX_RANGE_DAYS = 720          # 파생 시계열 기간 상한(2년) — 넘기면 KRX 가 빈 응답을 준다
_FAIL_COOLDOWN_SEC = 300
_CACHE_MAX = 60

_pykrx_webio = None
_import_done = False
_import_lock = threading.RLock()

_CACHE = {}                    # key -> {'df': df, 'ts': epoch, 'day': 'YYYYMMDD'}
_CACHE_LOCK = threading.RLock()
_FAIL = {}                     # key -> 마지막 실패 시각


def silence_pykrx_banner():
    """pykrx 가 stdout 으로 흘리는 로그인 배너(계정 ID 포함)를 로거로 돌린다.

    [왜 redirect_stdout 을 쓰면 안 되나 — 2026-08-25 장애]
     `sys.stdout` 은 **프로세스 전역**이고 rich Console 은 쓰기 시점에 `sys.stdout` 을 다시
     읽는다(`Console.file` 이 프로퍼티다). 그래서 워커 스레드가 `redirect_stdout` 을 잡고
     있는 동안 **메인 스레드의 화면 출력이 통째로 StringIO 로 들어가 사라진다.**
     KRX 조회는 초 단위라 그 창이 넓고, 두 스레드의 진입/이탈이 겹치면 sys.stdout 이 죽은
     StringIO 로 남아 화면이 영구히 멎는다 — 모드 2·3 모두 기동 직후 정지로 실제 발생했다.

     그래서 전역을 건드리지 않고 **그 모듈의 `print` 이름만** 갈아끼운다. 모듈 전역에
     `print` 를 심으면 그 모듈의 print 호출만 이쪽으로 오고 다른 코드는 영향이 없다.
     한 번 심으면 영구적이라 스레드 경합도 없다.

    ※ 첫 import 때 도는 로그인 배너 한 번은 이 함수보다 먼저 실행된다(pykrx 패키지
      __init__ 이 webio 를 끌어와 즉시 로그인한다). 그 한 줄만 _lazy_import 가 처리한다.
    """
    try:
        from pykrx.website.comm import auth as _auth
    except Exception:       # noqa: BLE001
        return
    if getattr(_auth, "_hts_silenced", False):
        return

    def _to_log(*args, **_kwargs):
        msg = " ".join(str(a) for a in args).strip()
        if not msg or "로그인 ID" in msg:      # 계정 ID 는 로그에도 남기지 않는다
            return
        logger.debug(f"[pykrx] {msg}")

    _auth.print = _to_log
    _auth._hts_silenced = True


def _lazy_import():
    """pykrx 의 인증 세션 모듈을 최초 1회만 로드한다.

    pykrx 는 **import 시점에** KRX 로그인을 시도하며 성공/실패와 로그인 ID 를 stdout 으로
    흘린다. 이 한 번만 redirect 로 삼키고(그 뒤로는 silence_pykrx_banner 가 맡는다),
    **이후 어떤 조회 경로도 sys.stdout 을 건드리지 않는다** — 이유는 위 함수 주석 참조.

    이 import 는 기동 시 메인 스레드의 지수 워밍업에서 먼저 일어난다(백그라운드 스레드가
    뜨기 전이다). 그래서 이 한 번의 redirect 는 다른 스레드의 출력을 삼킬 수 없다.
    """
    global _pykrx_webio, _import_done
    if _import_done:
        return
    with _import_lock:
        if _import_done:
            return
        buf = io.StringIO()
        try:
            with redirect_stderr(buf), redirect_stdout(buf):
                from pykrx.website.comm import webio as _w
                _pykrx_webio = _w
        except Exception as e:      # noqa: BLE001 - 미설치/구조변경 모두 '조회 불가'로 둔다
            logger.debug(f"[KRXDATA] pykrx 세션 모듈 로드 실패: {e}")
            _pykrx_webio = None
        finally:
            _import_done = True
        silence_pykrx_banner()      # 이후의 세션 만료·재로그인 배너를 로거로 돌린다


def has_credentials():
    """KRX_ID/KRX_PW 가 둘 다 설정돼 있는가. **값은 절대 로그에 남기지 않는다**."""
    import os
    return bool(os.environ.get("KRX_ID")) and bool(os.environ.get("KRX_PW"))


def is_available():
    """이 모듈로 조회를 시도할 수 있는가(라이브러리 + 자격증명).

    자격증명 없이 열리는 것은 개별 종목 일봉뿐이다 — 지수·금·파생은 전부 로그인 게이트라
    미리 걸러야 헛호출과 오해할 만한 실패 로그가 안 생긴다.
    """
    if not has_credentials():
        return False
    _lazy_import()
    return _pykrx_webio is not None


def _session():
    """pykrx 가 관리하는 인증 세션(만료 시 자동 재로그인). 실패 시 None."""
    if not is_available():
        return None
    try:
        # sys.stdout 을 건드리지 않는다 — 만료 재로그인 배너는 silence_pykrx_banner 가 맡는다.
        return _pykrx_webio.get_session()
    except Exception as e:      # noqa: BLE001
        logger.debug(f"[KRXDATA] 인증 세션 획득 실패: {e}")
        return None


def _post(bld, **params):
    """bld 에 파라미터를 실어 POST 하고 output 리스트를 돌려준다. 실패/무응답 시 None.

    비로그인 상태에서 KRX 는 본문 'LOGOUT' 과 함께 HTTP 400 을 준다 — JSON 이 아니므로
    아래 startswith('{') 검사에서 걸러진다.
    """
    sess = _session()
    if sess is None:
        return None
    try:
        res = sess.post(_JSON_URL, data={"bld": bld, **params}, timeout=20)
        text = (res.text or "").strip()
        if not text.startswith("{"):
            logger.debug(f"[KRXDATA] {bld} 비정상 응답: {text[:40]}")
            return None
        return res.json().get("output") or []
    except Exception as e:      # noqa: BLE001
        logger.debug(f"[KRXDATA] {bld} 조회 실패: {e}")
        return None


def _num(text):
    """KRX 표기('1,099.70' · '-' · '')를 float 으로. 값이 없으면 None."""
    if text is None:
        return None
    s = str(text).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _date8(text):
    """'2026/08/21' 또는 '2026/08/21 (야간)' → '20260821'. 파싱 불가 시 None."""
    s = str(text or "").strip()
    if not s:
        return None
    head = s.split(" ")[0].replace("/", "").replace("-", "")
    return head if len(head) == 8 and head.isdigit() else None


def _session_tag(text):
    """'2026/08/21 (야간)' → '야간'. 표기가 없으면 ''."""
    s = str(text or "")
    if "(" in s and ")" in s:
        return s[s.rfind("(") + 1:s.rfind(")")].strip()
    return ""


def _clamp_range(days):
    """(strtDd, endDd) — 파생 조회의 2년 상한 안으로 자른다."""
    end = datetime.now()
    start = end - timedelta(days=min(int(days), _MAX_RANGE_DAYS))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _cache_ttl_sec():
    """차트 캐시와 같은 주기(기본 6시간). 과거 확정 봉은 불변이라 길게 잡아도 된다."""
    try:
        minutes = float(getattr(config, "CHART_CACHE_TTL_MINUTES", 360))
    except (TypeError, ValueError):
        minutes = 360.0
    return max(0.0, minutes * 60)


def _cache_get(key):
    now = time.time()
    today = datetime.now().strftime("%Y%m%d")
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit["day"] == today and (now - hit["ts"]) < _cache_ttl_sec():
            return hit["df"].copy()
        failed_at = _FAIL.get(key, 0)
    if failed_at and (now - failed_at) < _FAIL_COOLDOWN_SEC:
        return False        # 음성 캐시 — None(조회 결과 없음)과 구분한다
    return None


def _cache_put(key, df):
    now = time.time()
    with _CACHE_LOCK:
        if df is None or df.empty:
            _FAIL[key] = now
            return
        _FAIL.pop(key, None)
        _CACHE[key] = {"df": df, "ts": now, "day": datetime.now().strftime("%Y%m%d")}
        if len(_CACHE) > _CACHE_MAX:
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1]["ts"])[:len(_CACHE) - _CACHE_MAX]
            for k, _ in oldest:
                _CACHE.pop(k, None)


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()
        _FAIL.clear()


def _finish(rows, source):
    """[{date, open, high, low, close, volume}] → 공통 스키마 DataFrame(오름차순).

    종가가 없는 날(휴장·미체결)은 버린다 — 0 원 봉이 지표를 망가뜨리기 때문이다
    (krx_daily._normalize 와 같은 기준).
    """
    if not rows:
        return None
    out = pd.DataFrame(rows)
    if out.empty or "close" not in out.columns:
        return None
    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]
    if out.empty:
        return None
    for col in ("open", "high", "low"):
        # 시·고·저가 없는 소스(V코스피200)는 종가로 평탄화한다.
        out[col] = out[col].fillna(out["close"]) if col in out.columns else out["close"]
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out = out[_COLUMNS].drop_duplicates(subset=["date"], keep="last")
    out = out.sort_values("date").reset_index(drop=True)
    out.attrs["source"] = source
    return out


# ---------------------------------------------------------------------------
# KRX 금현물
# ---------------------------------------------------------------------------
def get_gold_daily(days=400, use_cache=True):
    """KRX 금현물(원/g) 일봉 — ['date','open','high','low','close','volume'], attrs['source']='KRX'.

    네이버 경로와 달리 **시·고·저와 거래량이 실제 값**이다. 조회 불가 시 None(호출부가 폴백).
    """
    key = ("gold", int(days))
    if use_cache:
        hit = _cache_get(key)
        if hit is False:
            return None
        if hit is not None:
            return hit

    start, end = _clamp_range(days)
    raw = _post(_BLD_GOLD_DAILY, isuCd=GOLD_ISU_CD, strtDd=start, endDd=end,
                mktId="CMD", money="1", csvxls_isNo="false")
    rows = []
    for r in raw or []:
        d = _date8(r.get("TRD_DD"))
        if not d:
            continue
        rows.append({"date": d,
                     "open": _num(r.get("TDD_OPNPRC")), "high": _num(r.get("TDD_HGPRC")),
                     "low": _num(r.get("TDD_LWPRC")), "close": _num(r.get("TDD_CLSPRC")),
                     "volume": _num(r.get("ACC_TRDVOL")) or 0.0})
    df = _finish(rows, "KRX")
    _cache_put(key, df)
    return df


# ---------------------------------------------------------------------------
# 국내 지수 (코스피 / 코스닥 / 코스피200 / 코스닥150)
# ---------------------------------------------------------------------------
def get_index_daily(market_type, days=400, use_cache=True):
    """KRX 공식 지수 일봉. 거래량을 함께 주므로 지수 OBV 가 성립한다.

    market_type 은 analysis 의 표기('KOSPI'/'KOSDAQ'/'KOSPI200'/'KOSDAQ150')를 그대로 받는다.
    지원하지 않는 지수(V코스피200·선물)는 None — 그건 전용 함수가 따로 있다.
    """
    ticker = INDEX_TICKERS.get(market_type)
    if not ticker or not is_available():
        return None

    key = ("index", market_type, int(days))
    if use_cache:
        hit = _cache_get(key)
        if hit is False:
            return None
        if hit is not None:
            return hit

    start, end = _clamp_range(days)
    df = None
    try:
        from pykrx import stock
        raw = stock.get_index_ohlcv(start, end, ticker)
        if raw is not None and not raw.empty:
            out = raw.reset_index()
            date_col = out.columns[0]
            rows = []
            for r in out.to_dict("records"):
                d = pd.to_datetime(r[date_col], errors="coerce")
                if pd.isna(d):
                    continue
                rows.append({"date": d.strftime("%Y%m%d"),
                             "open": _num(r.get("시가")), "high": _num(r.get("고가")),
                             "low": _num(r.get("저가")), "close": _num(r.get("종가")),
                             "volume": _num(r.get("거래량")) or 0.0})
            df = _finish(rows, "KRX")
    except Exception as e:      # noqa: BLE001
        logger.debug(f"[KRXDATA] 지수 조회 실패({market_type}): {e}")
        df = None

    _cache_put(key, df)
    return df


# ---------------------------------------------------------------------------
# 파생 — 계약 찾기
# ---------------------------------------------------------------------------
def _live_contracts(prod_id, trade_day):
    """그 날짜에 상장돼 있던 선물 계약 [(ISU_CD, ISU_NM, 거래량)]. 스프레드(SP)는 뺀다."""
    raw = _post(_BLD_DRV_ALL, trdDd=trade_day, prodId=prod_id, mktId="DRV",
                money="1", csvxls_isNo="false")
    out = []
    for r in raw or []:
        name = str(r.get("ISU_NM") or "")
        if " F " not in name:          # 'SP'(스프레드)·옵션 표기를 배제한다
            continue
        out.append((r.get("ISU_CD"), name, _num(r.get("ACC_TRDVOL")) or 0.0))
    return out


def _recent_trade_day(back=10):
    """최근 거래일 후보를 오늘부터 거슬러 훑는다(휴장일이면 빈 응답이라 다음 날짜로).

    KRX 휴장 달력을 따로 받지 않고 '응답이 있는 날'을 거래일로 삼는다 — 달력을 하나 더
    들여오는 것보다 실패에 강하다.
    """
    day = datetime.now()
    for _ in range(back):
        yield day.strftime("%Y%m%d")
        day -= timedelta(days=1)


def _front_contract(prod_id):
    """근월물 (ISU_CD, 조회에 쓴 거래일). 거래량이 가장 많은 계약을 근월물로 본다.

    만기 문자열을 파싱해 최소 만기를 고르는 방법도 있지만, 이름 표기가 바뀌면 조용히
    틀린 계약을 고른다. 거래량은 표기 변화에 영향받지 않는다.
    """
    for day in _recent_trade_day():
        cons = _live_contracts(prod_id, day)
        if cons:
            cons.sort(key=lambda c: c[2], reverse=True)
            return cons[0][0], day
    return None, None


def _contract_series(isu_cd, prod_id, days):
    """계약 하나의 일별추이 원본 행. 세션(주간/야간)이 섞여 온다."""
    start, end = _clamp_range(days)
    return _post(_BLD_DRV_SERIES, isuCd=isu_cd, strtDd=start, endDd=end,
                 prodId=prod_id, secugrpId="FUTR", money="1", csvxls_isNo="false") or []


# ---------------------------------------------------------------------------
# 코스피200 선물 (주간 F / 야간 CM)
# ---------------------------------------------------------------------------
def get_k200_futures_daily(session="F", days=400, use_cache=True):
    """코스피200 선물 근월물 일봉. session='F'(주간) / 'CM'(야간).

    KRX 는 날짜 문자열에 세션을 붙여 두 세션을 한 응답에 담아 준다
    ('2026/08/21 (주간)' / '2026/08/21 (야간)') — 여기서 요청한 세션만 골라낸다.
    """
    want = "야간" if str(session).upper() in ("CM", "야간", "NIGHT") else "주간"
    key = ("k200fut", want, int(days))
    if use_cache:
        hit = _cache_get(key)
        if hit is False:
            return None
        if hit is not None:
            return hit

    isu, _day = _front_contract(PROD_K200_FUTURES)
    rows = []
    if isu:
        for r in _contract_series(isu, PROD_K200_FUTURES, days):
            if _session_tag(r.get("TRD_DD")) != want:
                continue
            d = _date8(r.get("TRD_DD"))
            if not d:
                continue
            rows.append({"date": d,
                         "open": _num(r.get("TDD_OPNPRC")), "high": _num(r.get("TDD_HGPRC")),
                         "low": _num(r.get("TDD_LWPRC")), "close": _num(r.get("TDD_CLSPRC")),
                         "volume": _num(r.get("ACC_TRDVOL")) or 0.0})
    df = _finish(rows, "KRX")
    _cache_put(key, df)
    return df


# ---------------------------------------------------------------------------
# V코스피200 (현물 변동성지수)
# ---------------------------------------------------------------------------
# 지수 통계 카탈로그(170종)에는 변동성지수가 없다 — 전수로 확인했다. 대신 변동성지수 **선물**
#  응답의 SPOT_PRC 가 현물 지수값이다. 계약이 달라도 같은 값임을 겹치는 88일에서 확인했으므로
#  여러 계약을 합쳐 이력을 늘려도 안전하다(불일치가 나오면 로그로 알린다).
# 계약은 월물이고 상장 창이 짧아, 지금 살아있는 계약만 모으면 약 113 거래일에서 멈춘다.
#  과거 스냅샷 날짜로 그때 살아있던(이미 만기된) 계약까지 끌어오면 더 뒤로 늘어난다.
#
# [왜 스냅샷마다 근월물 하나만 받나] 실측상 **만기가 가까운 계약일수록 이력이 길다**
#  (202609=113일 · 202610=88 · 202611=70 · 202612=52 — 먼저 상장됐기 때문이다).
#  스냅샷의 계약을 전부 받으면 짧은 것들이 이미 받은 구간을 덮어쓸 뿐인데 호출만 늘어난다
#  (전부 받던 최초 구현은 21콜 34.6초였다). 근월물만 이어받고, 요청 구간을 덮는 순간 멈춘다.
_VKOSPI_SNAPSHOT_BACK_DAYS = (0, 150, 300, 450)
_VKOSPI_MAX_CONTRACTS = 6


def _expiry_key(name):
    """'변동성지수 F 202609' → 202609. 표기를 못 읽으면 None(정렬에서 뒤로 민다)."""
    for token in str(name or "").split():
        if len(token) == 6 and token.isdigit():
            return int(token)
    return None


def get_vkospi_daily(days=400, use_cache=True):
    """V코스피200(코스피200 변동성지수) 일봉 — **종가만** 있다(시·고·저는 종가로 평탄화).

    선물 응답의 SPOT_PRC 를 모아 만든다. 거래량은 없다(현물 지수라 애초에 없다).
    """
    key = ("vkospi", int(days))
    if use_cache:
        hit = _cache_get(key)
        if hit is False:
            return None
        if hit is not None:
            return hit

    want_from, _ = _clamp_range(days)       # 이 날짜까지 덮으면 더 받을 이유가 없다
    spot = {}
    seen = set()
    for back in _VKOSPI_SNAPSHOT_BACK_DAYS:
        if len(seen) >= _VKOSPI_MAX_CONTRACTS:
            break
        if spot and min(spot) <= want_from:
            break                            # 요청 구간을 이미 덮었다
        snap_from = datetime.now() - timedelta(days=back)
        cons = []
        for _ in range(10):
            cons = _live_contracts(PROD_VKOSPI_FUTURES, snap_from.strftime("%Y%m%d"))
            if cons:
                break
            snap_from -= timedelta(days=1)
        # 만기가 가까운 것부터 — 그게 가장 오래 상장돼 있어 이력이 길다.
        cons.sort(key=lambda c: (_expiry_key(c[1]) is None, _expiry_key(c[1]) or 0))
        for isu, _nm, _vol in cons:
            if isu in seen:
                continue
            seen.add(isu)
            for r in _contract_series(isu, PROD_VKOSPI_FUTURES, days):
                d = _date8(r.get("TRD_DD"))
                val = _num(r.get("SPOT_PRC"))
                if not d or val is None:
                    continue
                prev = spot.get(d)
                if prev is not None and abs(prev - val) > 1e-9:
                    # 계약마다 현물값이 다르면 SPOT_PRC 를 현물로 본 전제가 깨진 것이다.
                    logger.warning(f"[KRXDATA] V코스피200 현물값이 계약별로 어긋난다({d}: "
                                   f"{prev} vs {val}) — 소스 가정 재확인 필요")
                spot[d] = val
            break                            # 스냅샷당 근월물 하나면 충분하다

    rows = [{"date": d, "close": v, "open": v, "high": v, "low": v, "volume": 0.0}
            for d, v in spot.items()]
    df = _finish(rows, "KRX")
    _cache_put(key, df)
    return df
