"""토스증권(mode 3) 계층 + 국내 일봉 폴백.

토스는 KIS 와 응답 형태·제공 항목이 다르고(체결강도 미제공, 웹소켓 없음),
캔들에 NXT 장전·장후 체결이 섞여 들어온다. 그래서 일봉은 pykrx/FDR 로 따로 받고,
기준가는 랭킹→검증저장→yfinance→캡처→NXT 순으로 여러 단을 거쳐 정한다.
이 파일이 큰 이유는 그 '다름'을 전부 여기서 흡수해 위층에 같은 모양으로 넘기기 때문이다.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import config
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

# =========================================================================
# [추가] 토스증권(mode 3) 어댑터
#   토스 응답을 KIS 화면 코드가 기대하는 형태(output1/output2 등)로 변환한다.
#   토스가 제공하지 않는 필드는 0/공란으로 채운다.
# =========================================================================
def _toss_int(v, default=0):
    try:
        if v is None: return default
        return int(float(str(v).replace(',', '')))
    except Exception:
        return default


def _toss_float(v, default=0.0):
    try:
        if v is None: return default
        return float(str(v).replace(',', ''))
    except Exception:
        return default


def _toss_cached_daily_chart(code):
    """기준가 판별용 국내 일봉을 '캐시 우선'으로 조회한다 (메모리→디스크→최초 1회 네트워크).

    _toss_base_price는 현재가 조회마다 불리므로, 차트는 오늘자 캐시를 재사용하고
    캐시가 전혀 없을 때만(하루 최대 1회) 오버레이 없는 과거봉을 새로 받는다.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    ck = _api()._chart_cache_key(code, False, False)  # 브로커 네임스페이스(T_/K_)로 KIS↔TOSS 교차오염 차단
    with _api()._CHART_CACHE_LOCK:
        c = _api()._CHART_CACHE.get(ck)
        if c and c.get('date') == today_str:
            return c['df']
    df = _api()._chart_disk_get(ck, today_str)
    if df is not None:
        return df
    if config.session.is_toss:
        try:
            return _api().get_chart_data(code, False, 'daily', False)
        except Exception as e:
            logger.debug(f"[Toss] 기준가 판별용 일봉 조회 실패({code}): {e}")
    return None


# --- NXT(대체거래소) 거래 여부 — 토스 캔들이 'KRX 단독'인지 판정하는 근거 ---
#  토스 캔들·현재가는 KRX+NXT 통합 체결이라 거래소를 골라낼 수 없다. 다만 NXT에서 거래되지
#  않는 종목(ETF/ETN 대부분 + 일부 주식)은 통합값이 곧 KRX 값이다. stocks API의
#  koreanMarketDetail.nxtSupported가 이를 직접 알려준다.
#  이 정보는 영업일 단위(또는 그 이상)로만 바뀌므로 거래일 단위로 캐시한다(스펙 권고).
_toss_nxt_map = {}          # {code: bool}
_toss_nxt_miss = {}         # {code: 마지막 실패 시각} — 조회 실패 재시도 폭주 방지
_toss_nxt_day = None        # 캐시 유효 거래일(YYYYMMDD)
_toss_nxt_lock = threading.Lock()
_TOSS_NXT_RETRY_SEC = 600


def _toss_nxt_supported(code):
    """종목이 NXT에서 거래되는가. True/False, 판정 불가 시 None(호출부가 폴백).

    비토스 모드이거나 국내 6자리 코드가 아니면 None. 조회 실패는 쿨다운(10분) 동안 None을
    반환해, 시세 갱신 주기마다 stocks API를 두드리지 않게 한다.
    """
    global _toss_nxt_map, _toss_nxt_miss, _toss_nxt_day
    if not config.session.is_toss or not code or not str(code).isdigit():
        return None
    today = datetime.now().strftime('%Y%m%d')
    with _toss_nxt_lock:
        if _toss_nxt_day != today:      # 날짜가 바뀌면 전체 무효화(상장·NXT 편입 변경 반영)
            _toss_nxt_map = {}
            _toss_nxt_miss = {}
            _toss_nxt_day = today
        if code in _toss_nxt_map:
            return _toss_nxt_map[code]
        last = _toss_nxt_miss.get(code)
        if last and (time.time() - last) < _TOSS_NXT_RETRY_SEC:
            return None
    try:
        rows = toss_api.get_stocks([code]) or []
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] NXT 지원 여부 조회 실패({code}): {e}")
        with _toss_nxt_lock:
            _toss_nxt_miss[code] = time.time()
        return None
    for r in rows:
        if r.get('symbol') != code:
            continue
        detail = r.get('koreanMarketDetail') or {}
        val = detail.get('nxtSupported')
        if isinstance(val, bool):
            with _toss_nxt_lock:
                _toss_nxt_map[code] = val
            return val
    with _toss_nxt_lock:      # 응답에 종목/필드가 없음(해외 종목 등) — 쿨다운 후 재시도
        _toss_nxt_miss[code] = time.time()
    return None


def _toss_krx_only(code):
    """이 종목의 토스 체결값을 'KRX 단독'으로 봐도 되는가(= NXT 미거래).

    True면 토스 분봉·현재가에 NXT 체결이 섞이지 않아 KRX 값과 동일하다.
    nxtSupported(직접 근거)를 우선하고, 판정 불가 시 ETF/ETN 휴리스틱으로 폴백한다.
    """
    nxt = _toss_nxt_supported(code)
    if nxt is not None:
        return not nxt
    try:
        return bool(_api().is_domestic_etf_etn(code))
    except Exception:
        return False


# --- KRX 정규장 세션 경계 (market-calendar/KR) ---
#  분봉에서 정규장 구간만 잘라내려면 시작·종료 시각이 필요하다. 09:00~15:30 하드코딩은
#  임시 지연·단축 개장(수능일 등)에 어긋나므로 캘린더 값을 쓰고, 조회 실패 시에만 기본값을 쓴다.
#  integrated.regularMarket은 KRX∪NXT 합집합이지만 두 거래소의 정규장 시간이 같아
#  (NXT의 프리 08:00~09:00 / 애프터 15:30~20:00은 별도 필드) 경계값으로 그대로 쓸 수 있다.
_TOSS_KRX_SESSION_DEFAULT = ((9, 0), (15, 30))
_toss_kr_cal_map = {}       # {'YYYYMMDD': ((h,m),(h,m))}
_toss_kr_cal_day = None     # 캘린더를 받아온 달력일(YYYYMMDD)
_toss_kr_cal_fail = 0.0     # 마지막 조회 실패 시각(재시도 쿨다운)
_toss_kr_cal_lock = threading.Lock()
_TOSS_KR_CAL_RETRY_SEC = 300


def _toss_parse_hm(ts):
    """ISO8601 문자열에서 (시, 분) 추출. 실패 시 None."""
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt.hour, dt.minute
    except Exception:
        return None


def _toss_krx_regular_bounds(date_str=None):
    """해당 거래일 KRX 정규장의 (시작, 종료) 시각 ((시,분),(시,분)).

    market-calendar/KR 1회 호출로 전/당/익 영업일 3건을 받아 날짜별로 캐시하므로
    캘린더 호출은 하루 1회다. 비토스 모드·조회 실패·미수록 날짜는 기본값(09:00~15:30).
    """
    global _toss_kr_cal_map, _toss_kr_cal_day, _toss_kr_cal_fail
    if not config.session.is_toss:
        return _TOSS_KRX_SESSION_DEFAULT
    today = datetime.now().strftime('%Y%m%d')
    key = str(date_str or today)
    with _toss_kr_cal_lock:
        if _toss_kr_cal_day == today:
            return _toss_kr_cal_map.get(key, _TOSS_KRX_SESSION_DEFAULT)
        # 실패 직후 재조회 억제 — 시세 갱신마다 캘린더를 두드리지 않게 한다
        if _toss_kr_cal_fail and (time.time() - _toss_kr_cal_fail) < _TOSS_KR_CAL_RETRY_SEC:
            return _TOSS_KRX_SESSION_DEFAULT

    parsed = {}
    try:
        res = toss_api.get_market_calendar("KR") or {}
        for slot in ("previousBusinessDay", "today", "nextBusinessDay"):
            day = res.get(slot) or {}
            d = str(day.get('date') or '').replace('-', '')
            reg = ((day.get('integrated') or {}).get('regularMarket')) or {}
            start = _toss_parse_hm(reg.get('startTime'))
            end = _toss_parse_hm(reg.get('endTime'))
            if d and start and end:
                parsed[d] = (start, end)
    except Exception as e:
        logger.debug(f"[Toss] 장 운영시간 조회 실패: {e}")

    with _toss_kr_cal_lock:
        if parsed:
            _toss_kr_cal_map = parsed
            _toss_kr_cal_day = today
            _toss_kr_cal_fail = 0.0
        else:                           # 실패: 쿨다운 후 재시도(그 동안은 기본값 사용)
            _toss_kr_cal_fail = time.time()
    return parsed.get(key, _TOSS_KRX_SESSION_DEFAULT)


# --- KRX 정규장 마감가(15:30) 캡처·저장 (mode 3 등락률 기준가) ---
#  TOSS는 전일 KRX 정규장 종가 필드를 안 준다. 역산·yfinance는 불안정해 폐기했고, 대신
#  '거래일 마감 후 정규장 분봉의 15:30 종가'(=KRX 마감가)를 그날 1회 캡처해 영속 저장한다.
#  역산·계산 없이 '저장된 값을 그대로' 읽어 다음 거래일의 등락률 기준가로 쓴다(HTS와 일치).
#  캡처 못한 날(앱 미구동 등)은 전일 NXT 종가(일봉 직전 캔들)로 안전 폴백한다.
_toss_krx_close_store = None
_toss_krx_close_lock = threading.Lock()


def _toss_krx_close_path():
    return os.path.join(config.JSON_DIR, "toss_krx_close.json")


def _toss_krx_close_load_locked():
    global _toss_krx_close_store
    if _toss_krx_close_store is None:
        try:
            with open(_toss_krx_close_path(), 'r', encoding='utf-8') as f:
                _toss_krx_close_store = json.load(f)
            if not isinstance(_toss_krx_close_store, dict):
                _toss_krx_close_store = {}
        except Exception:
            _toss_krx_close_store = {}
    return _toss_krx_close_store


#  저장 형식: {code: {date: {"c": 종가, "s": 출처}}} — 출처 'yf'는 KRX 종가로 검증된 값,
#  'cap'은 분봉 캡처값(주식은 NXT 혼입으로 부정확, 아래 _toss_capture_krx_close 주석 참조).
#  구버전은 종가만 실수로 저장했으므로, 태그 없는 값은 'cap'으로 간주해 신뢰하지 않는다.
def _toss_krx_close_unpack(v):
    """저장값 → (종가, 출처). 구형식(실수)은 ('cap')으로 본다."""
    if isinstance(v, dict):
        try:
            c = float(v.get("c") or 0)
        except Exception:
            return None, None
        return (c if c > 0 else None), (v.get("s") or "cap")
    try:
        c = float(v) if v else 0
    except Exception:
        return None, None
    return (c if c > 0 else None), "cap"


# KRX 정규장 종가로 검증된 출처. 'krx'(pykrx/FDR = KRX 공식)가 'yf'보다 정확하다 —
# yfinance는 특정일에 공식 종가와 다른 값을 준다(실측 237거래일 중 2~4일, 최대 1.59%).
_TOSS_CLOSE_VERIFIED_SOURCES = ("krx", "yf")
_TOSS_CLOSE_SOURCE_RANK = {"krx": 2, "yf": 1, "cap": 0}


def _toss_krx_close_trusted(code, source):
    """이 출처의 저장값을 KRX 정규장 종가로 믿어도 되는가.

    'krx'(pykrx/FDR)·'yf'는 일봉(KRX 기준) 조회로 얻은 값이라 신뢰한다. 'cap'(분봉 캡처)은 **NXT에서
    거래되지 않는 종목**만 신뢰한다 — 그 종목의 분봉은 KRX 단독이라 오염이 없기 때문이다
    (실측 2026-07-16: ETF 15/15 정확, 주식 0/10 정확).

    NXT 거래 여부는 토스 stocks API의 koreanMarketDetail.nxtSupported로 직접 판정한다.
    조회 불가(비토스·API 실패) 시에만 종전의 ETF/ETN 휴리스틱으로 폴백한다 — 휴리스틱은
    관심목록 등록·종목명 브랜드에 의존해 NXT 미지원 '주식'을 놓치고 오탐 여지도 있다.
    """
    if source in _TOSS_CLOSE_VERIFIED_SOURCES:
        return True
    return _toss_krx_only(code)


def _toss_krx_close_get(code, date_str, trusted_only=False):
    """저장된 KRX 정규장 마감가 조회 (없으면 None).

    trusted_only=True면 KRX 종가로 믿을 수 있는 값만 돌려준다(_toss_krx_close_trusted).
    """
    if not date_str:
        return None
    with _toss_krx_close_lock:
        store = _toss_krx_close_load_locked()
        try:
            close, source = _toss_krx_close_unpack((store.get(code) or {}).get(date_str))
        except Exception:
            return None
    if close is None:
        return None
    if trusted_only and not _toss_krx_close_trusted(code, source):
        return None
    return close


def _toss_krx_close_put(code, date_str, close, source="cap"):
    """KRX 정규장 마감가 저장 (종목당 최근 10거래일 유지, 원자적 저장).

    source: 'krx'(pykrx/FDR = KRX 공식) / 'yf'(yfinance 일봉) / 'cap'(분봉 캡처).
    정확도 순위(krx > yf > cap)가 높은 값만 낮은 값을 덮어쓴다 — 이미 저장된 정확한 값이
    부정확한 출처로 퇴행하지 않게 하고, yf로 저장된 옛 값은 krx로 자동 교정되게 한다.
    """
    if not date_str or not close or close <= 0:
        return
    with _toss_krx_close_lock:
        store = _toss_krx_close_load_locked()
        per = store.setdefault(code, {})
        if not isinstance(per, dict):
            per = {}
            store[code] = per
        cur_close, cur_source = _toss_krx_close_unpack(per.get(date_str))
        if cur_close is not None:
            cur_rank = _TOSS_CLOSE_SOURCE_RANK.get(cur_source, 0)
            new_rank = _TOSS_CLOSE_SOURCE_RANK.get(source, 0)
            if new_rank < cur_rank:
                return                       # 더 부정확한 출처로 퇴행하지 않는다
            if cur_close == close and cur_source == source:
                return
        per[date_str] = {"c": float(close), "s": source}
        for k in sorted(per.keys())[:-10]:
            del per[k]
        try:
            tmp = _toss_krx_close_path() + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(store, f, ensure_ascii=False)
            os.replace(tmp, _toss_krx_close_path())
        except Exception as e:
            logger.debug(f"[Toss] KRX 마감가 저장 실패: {e}")


def _toss_after_krx_close():
    """오늘이 거래일이고 KRX 정규장 마감 + 5분(종가 확정 여유) 이후면 True.

    마감 시각은 캘린더 기준(기본 15:30 → 15:35)이라 단축장에도 어긋나지 않는다.
    """
    now = datetime.now()
    if _api().market_today(False) != now.strftime('%Y%m%d'):
        return False
    (_sh, _sm), (eh, em) = _toss_krx_regular_bounds(now.strftime('%Y%m%d'))
    return now >= now.replace(hour=eh, minute=em, second=0, microsecond=0) + timedelta(minutes=5)


def _before_nxt_premarket_open():
    """다음 NXT(대체거래소) 프리마켓 개장(08:00) 전이면 True.

    NXT 미지원(또는 장전 무체결) 종목의 '마지막 정규장 등락률'은 다음 NXT 개장(08:00)
    전까지 유지한다. 휴장일(주말·공휴일)은 하루 종일 개장 전이므로 항상 True이고,
    거래일엔 08:00 전까지만 True다. 08:00 이후엔 NXT 거래시간(프리 08~09시)이라
    체결이 시작되므로, 무체결 종목은 0%로 노출한다. (토스/KIS 공통 판정)
    """
    now = datetime.now()
    if _api().market_today(False) != now.strftime('%Y%m%d'):
        return True  # 휴장일 — 다음 거래일 NXT 개장까지 유지
    return now.strftime('%H%M') < '0800'


def _toss_before_nxt_open():
    """(호환 별칭) 토스 경로에서 쓰는 장전(NXT 개장 전) 판정. _before_nxt_premarket_open 위임."""
    return _before_nxt_premarket_open()


def _before_krx_regular_open():
    """오늘이 거래일이고 KRX 정규장 개장(09:00) 전이면 True.

    국내 지수는 NXT 연장거래가 없어 09:00 개장 전까지 현재가=전일 종가로 등락률이 0%가 된다.
    이 구간엔 직전 정규장 최종 등락률(전일 vs 전전일)을 대신 표시하기 위한 판정.
    """
    now = datetime.now()
    return _api().market_today(False) == now.strftime('%Y%m%d') and now.strftime('%H%M') < '0900'


def _toss_capture_krx_close(code):
    """마감 후, 오늘 정규장 분봉의 마지막(15:30) 종가를 하루 1회 저장한다.

    ETF/ETN은 이 값이 곧 KRX 마감가다. 그러나 **주식은 KRX 마감가가 아니다** —
    NXT(넥스트레이드)가 정규장 시간대에도 KRX와 병행 체결되고 토스 분봉은 두 거래소를
    섞어 주므로, 마지막 봉 종가가 KRX 종가 단일가와 어긋난다. ETF는 NXT 미거래라 오염이 없다.
    실측(2026-07-16, KIS 일봉 대조): ETF 15/15 정확, 주식은 52종목 중 26종목이 불일치
    (중앙값 0.45%, 최대 1.89%). 시간 필터로는 고칠 수 없어 'cap' 태그로 저장하고,
    주식 기준가로는 _toss_yf_krx_close(일봉=KRX 기준) 값을 우선한다.

    이미 오늘분이 저장돼 있으면(store 적중) 재조회하지 않아 마감 직후 1회 버스트로 끝난다.
    """
    if not (config.session.is_toss and _toss_after_krx_close()):
        return
    today = datetime.now().strftime('%Y%m%d')
    if _toss_krx_close_get(code, today):  # 이미 캡처됨 → 네트워크 재조회 없음
        return
    try:
        df = _toss_chart_data(code, period_type='intraday', is_overseas=False)
        if df is None or len(df) == 0:
            return
        close = _toss_float(df.iloc[-1]['close'])  # 정규장 필터 → 마지막 봉 = 15:30
        if close > 0:
            _toss_krx_close_put(code, today, close, source="cap")
    except Exception as e:
        logger.debug(f"[Toss] KRX 마감가 캡처 실패({code}): {e}")


# --- 랭킹 basePrice = 전일 KRX 정규장 종가(기준가) 라이브 조회 (1순위 소스) ---
#  /api/v1/rankings 의 price.basePrice(MARKET_* 타입)는 '전일 기준가'(=HTS 등락률 기준가)다.
#  거래대금+거래량 상위 각 100종목을 하루 1회 받아 {symbol: basePrice} 맵을 만든다(대형주 커버).
#  basePrice는 전일 종가라 장중 불변 → 거래일 단위 캐시. 랭킹 밖(중소형) 종목은 없음→하위순위로.
_toss_rank_base_map = None      # {code: basePrice}
_toss_rank_base_day = None      # 캐시 유효 거래일(YYYYMMDD)
_toss_rank_lock = threading.Lock()


def _toss_ranking_base(code):
    """랭킹 basePrice(전일 KRX 정규장 종가). 랭킹 상위 종목만 존재, 없으면 None."""
    global _toss_rank_base_map, _toss_rank_base_day
    if not config.session.is_toss:
        return None
    today = datetime.now().strftime('%Y%m%d')
    with _toss_rank_lock:
        if _toss_rank_base_map is not None and _toss_rank_base_day == today:
            return _toss_rank_base_map.get(code)
        # 하루 1회 적재 (거래대금·거래량 상위 각 100)
        mp = {}
        for rank_type in ("MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME"):
            try:
                res = toss_api.get_rankings(rank_type=rank_type, market_country="KR",
                                            duration="realtime", count=100)
            except toss_api.TossApiError as e:
                logger.debug(f"[Toss] 랭킹({rank_type}) 조회 실패: {e}")
                continue
            for item in (res or {}).get('rankings', []) or []:
                sym = item.get('symbol')
                bp = _toss_float(((item.get('price') or {}).get('basePrice')))
                if sym and bp > 0:
                    mp.setdefault(sym, bp)
        # 조회 자체가 전부 실패하면(빈 맵) 캐시를 세팅하지 않아 다음 호출에서 재시도한다.
        if mp:
            _toss_rank_base_map, _toss_rank_base_day = mp, today
        return mp.get(code)


# --- yfinance 국내 일봉 = KRX 정규장 종가 (3순위 소스, 과거 소급 조회 가능) ---
#  캡처(2순위)는 '그날 15:35 이후 프로그램이 mode 3로 떠 있어야' 저장되므로, 하루라도 안 띄우면
#  그날의 KRX 종가가 영구히 비어 다음 날 기준가가 NXT 종가로 폴백된다(실측 2026-07-22 한미약품:
#  KIS 기준가 372,500[KRX] vs 토스 371,500[NXT] → 등락률 -1.21% vs -0.94%).
#  yfinance 국내 일봉 종가는 KRX 정규장 기준이라 KIS 기준가와 일치하고(실측 07-21 = 372,500),
#  인증 없이 과거 아무 날짜나 조회되므로 이 커버리지 구멍을 메운다.
_toss_yf_base_miss = {}          # {(code, date): 마지막 실패 시각} — 실패 재조회 폭주 방지
_toss_yf_base_lock = threading.Lock()
_TOSS_YF_BASE_RETRY_SEC = 1800   # 실패 후 재시도 쿨다운(초)


def _toss_krx_lib_close(code, ref_date):
    """pykrx/FDR 일봉에서 ref_date(YYYYMMDD)의 KRX 정규장 종가를 얻는다. 없으면 None.

    [yfinance보다 우선하는 이유] yfinance 국내 일봉은 특정일에 공식 종가가 아닌 값을 준다.
     실측(2026-07-25, pykrx 대조 237거래일): 삼성전자·SK하이닉스 각 4일, GS건설·에코프로비엠 각 2일
     불일치(최대 +1.59%, SK하이닉스 2026-06-24 KRX 2,580,000 vs yf 2,621,000).
     불일치 날짜가 종목 간 동일(2025-09-18·2026-03-27·2026-04-01)해 배당 조정이 아니라
     yfinance 쪽 데이터 품질 문제다. 이 값이 '검증값'으로 영속 저장되면 오차가 고착된다.
    [부수 효과] krx_daily는 6시간 캐시를 재사용하므로 추가 네트워크 호출이 없고,
     yfinance 미사용으로 numpy 2.x DeprecationWarning 출력도 사라진다.
    """
    if not code or not ref_date:
        return None
    try:
        from modules import krx_daily
        df = krx_daily.get_daily(code)
    except Exception as e:      # noqa: BLE001 - 실패 시 yfinance 폴백으로 넘어간다
        logger.debug(f"[Toss] KRX 기준가 조회 실패({code} {ref_date}): {e}")
        return None

    if df is None or df.empty:
        return None
    hit = df[df['date'] == ref_date]
    if hit.empty:
        return None
    close = float(hit.iloc[-1]['close'])
    if close <= 0:
        return None
    # 검증값으로 저장 → 다음부터 2순위(신뢰 조회)에서 즉시 종료
    _toss_krx_close_put(code, ref_date, close, source="krx")
    return close


def _toss_yf_krx_close(code, ref_date):
    """yfinance 국내 일봉에서 ref_date(YYYYMMDD)의 KRX 정규장 종가를 얻는다. 없으면 None.

    pykrx/FDR(_toss_krx_lib_close)이 모두 실패했을 때의 마지막 폴백이다.
    조회 성공 시 캡처 저장소에 넣어 다음부터는 2순위에서 즉시 끝나게 한다(네트워크 1회로 종료).
    실패는 쿨다운 캐시로 묶어 주기적 시세 갱신마다 yfinance를 두드리지 않게 한다.
    """
    if not code or not ref_date:
        return None
    key = (code, ref_date)
    with _toss_yf_base_lock:
        last = _toss_yf_base_miss.get(key)
        if last and (time.time() - last) < _TOSS_YF_BASE_RETRY_SEC:
            return None
    try:
        # 국내는 KOSPI(.KS)/KOSDAQ(.KQ) 접미사를 모두 시도한다(거래소 정보가 없어도 동작).
        start = (datetime.strptime(ref_date, "%Y%m%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        end = (datetime.strptime(ref_date, "%Y%m%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        for suffix in (".KS", ".KQ"):
            ticker = f"{code}{suffix}"
            df = _api().fetch_yfinance_data(ticker, start=start, end=end)
            if df is None or getattr(df, 'empty', True):
                continue
            if isinstance(df.columns, pd.MultiIndex):
                try:
                    df = df.xs(ticker, axis=1, level=1)
                except Exception:
                    pass
            df.columns = [str(c).lower() for c in df.columns]
            if 'close' not in df.columns:
                continue
            for idx, val in df['close'].items():
                d = idx.strftime('%Y%m%d') if hasattr(idx, 'strftime') else str(idx).replace('-', '')[:8]
                if d == ref_date:
                    close = float(val)
                    if close > 0:
                        # 검증값으로 저장 → 다음부터 2순위(신뢰 조회)에서 즉시 종료
                        _toss_krx_close_put(code, ref_date, close, source="yf")
                        return close
    except Exception as e:
        logger.debug(f"[Toss] yfinance 기준가 조회 실패({code} {ref_date}): {e}")
    with _toss_yf_base_lock:
        _toss_yf_base_miss[key] = time.time()
    return None


def _toss_base_price(code, chart_df=None):
    """국내 등락률 기준가. 역산·계산 없이 '확보된 값을 그대로' 쓴다(단락 평가).

    우선순위(위에서 값이 나오면 즉시 반환, 아래는 실행 안 함):
      1) 랭킹 basePrice — 거래대금/거래량 상위(대형주)의 전일 KRX 정규장 종가(라이브, HTS 일치)
      2) 저장된 값 중 '검증된' 것 — KRX 공식/yfinance로 확인된 값, 또는 ETF의 분봉 캡처값
      3) KRX 공식 일봉 종가(pykrx/FDR) — 확보 즉시 검증값으로 저장돼 다음부터 2)에서 끝난다
      3-1) yfinance 일봉 종가 — 3)이 모두 실패했을 때의 폴백(특정일 공식 종가와 어긋나는 사례 있음)
      4) 저장된 분봉 캡처값(주식) — NXT 혼입으로 부정확하나 NXT 종가보다는 가깝다
      5) 폴백: 전일 NXT(대체거래소) 종가 = 일봉 직전 거래일 캔들 종가

    주식의 분봉 캡처값을 2)에서 쓰지 않는 이유는 _toss_capture_krx_close 주석 참조 —
    NXT가 정규장 시간대에 병행 체결돼 토스 분봉 종가가 KRX 단일가와 어긋난다. 캡처값을
    믿으면 3)이 영원히 호출되지 않아 오차가 고정된다(2026-07-22 실측 6종목).
    4)·5)는 KRX 기준 HTS 등락률과 소폭 다를 수 있다(기동 시 안내).
    TOSS는 전일 KRX 종가 필드를 직접 주지 않는다.

    ref_date(전일 종가의 거래일)는 일봉 '날짜'만으로 결정한다:
      마지막 캔들일이 오늘보다 과거면 그 캔들(=장중), 오늘이면 그 직전 캔들(=마감 후).

    chart_df: 호출자가 일봉을 이미 들고 있으면 전달(재조회 방지). 미전달 시 캐시 우선 조회.
    """
    today = datetime.now().strftime('%Y%m%d')

    # 1) 랭킹 basePrice (대형주, 라이브·HTS 일치) — 있으면 여기서 종료
    rb = _toss_ranking_base(code)
    if rb:
        return rb

    try:
        df = chart_df if chart_df is not None else _toss_cached_daily_chart(code)
        if df is None or len(df) < 2:
            return None
        last_date = str(df.iloc[-1]['date']).replace('-', '')[:8]
        prev_date = str(df.iloc[-2]['date']).replace('-', '')[:8]
        ref_date = last_date if last_date < today else prev_date

        # 2) 저장된 값 중 검증된 것만 (yfinance 확인분 또는 ETF 캡처) — 네트워크 없음
        trusted = _toss_krx_close_get(code, ref_date, trusted_only=True)
        if trusted:
            return trusted

        # 3) KRX 공식 일봉 종가 (pykrx/FDR). 성공 시 검증값으로 적재되어 2)에서 종료된다.
        #    6시간 캐시를 재사용하므로 추가 네트워크 호출이 없다.
        krx_close = _toss_krx_lib_close(code, ref_date)
        if krx_close:
            return krx_close

        # 3-1) 위가 모두 실패했을 때만 yfinance (특정일 공식 종가와 어긋나는 사례가 있어 후순위)
        yf_close = _toss_yf_krx_close(code, ref_date)
        if yf_close:
            return yf_close

        # 4) 저장된 분봉 캡처값 (주식: NXT 혼입으로 부정확) — 위 소스 실패 시 근사 폴백
        stored = _toss_krx_close_get(code, ref_date)
        if stored:
            return stored

        # 5) 폴백: 전일 NXT 종가 (일봉 직전 캔들 종가)
        row = df.iloc[-1] if last_date < today else df.iloc[-2]
        base = _toss_float(row['close'])
        return base if base > 0 else None
    except Exception as e:
        logger.debug(f"[Toss] 기준가 산출 실패({code}): {e}")
        return None


def _toss_prev_prev_close(code):
    """전전일(직전 정규장의 하루 전) 종가. 프리마켓 '최종 등락률' 기준가로 쓴다.

    NXT 미지원 종목은 개장 전 체결이 없어 현재가=전일 종가=기준가 → 등락률 0%가 된다.
    이때 '기준가'를 전전일 종가로 바꿔 직전 정규장의 최종 등락률(전일 vs 전전일)을
    개장 전까지 유지 표시한다. 일봉 마지막 캔들이 전일(과거)이면 그 직전(-2)이 전전일,
    이미 오늘 캔들이 있으면(-3)이 전전일이다.
    """
    today = datetime.now().strftime('%Y%m%d')
    try:
        df = _toss_cached_daily_chart(code)
        if df is None or len(df) < 2:
            return None
        last_date = str(df.iloc[-1]['date']).replace('-', '')[:8]
        idx = -2 if last_date < today else -3
        if len(df) < abs(idx):
            return None
        pp = _toss_float(df.iloc[idx]['close'])
        return pp if pp > 0 else None
    except Exception as e:
        logger.debug(f"[Toss] 전전일 종가 산출 실패({code}): {e}")
        return None


def _toss_krw_deposit():
    """토스 매수 가능 금액(KRW, 현금)을 예수금 근사치로 사용한다."""
    try:
        bp = toss_api.get_buying_power("KRW")
        return _toss_int(bp.get('cashBuyingPower')) if bp else 0
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] buying-power 조회 실패: {e}")
        return 0


def _toss_domestic_balance():
    """토스 보유주식(KR) → KIS get_domestic_balance (output1, output2) 형태."""
    try:
        ov = toss_api.get_holdings()
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] 보유주식 조회 실패: {e}")
        return None, None

    items = (ov or {}).get('items', []) or []
    output1 = []
    tot_eval = 0
    for it in items:
        if it.get('marketCountry') != 'KR':
            continue
        mv = it.get('marketValue', {}) or {}
        pl = it.get('profitLoss', {}) or {}
        evlu_amt = _toss_int(mv.get('amount'))
        tot_eval += evlu_amt
        qty = _toss_int(it.get('quantity'))
        avg_pric = _toss_float(it.get('averagePurchasePrice'))
        output1.append({
            'pdno': it.get('symbol', ''),
            'prdt_name': it.get('name', ''),
            'hldg_qty': str(qty),
            #  [필수 · 2026-09-05] 매도 워커(trader._sell_worker)는 이 값으로 게이트한다 —
            #   `qty = safe_int(item.get('ord_psbl_qty'))` 가 0이면 그 종목은
            #   '[분석스킵] 주문 가능 수량 0' 으로 빠져 **손절·트레일링 판정이 통째로
            #   건너뛰어진다**. 종전에는 이 키가 없어(해외 어댑터에는 있었다) 토스 모드의
            #   국내 보유 종목이 전부 무방비였다.
            #   보유수량을 그대로 쓴다(해외 어댑터와 같은 규약). 미체결 매도로 묶인 물량은
            #   발주 직전 api.fetch_sellable_quantity 가 다시 확인해 줄여 준다.
            'ord_psbl_qty': str(qty),
            'pchs_avg_pric': str(avg_pric),
            # 토스는 매입금액을 주지 않는다 → KIS 형태를 맞추려 평단×수량으로 채운다(화면 0원 방지)
            'pchs_amt': str(int(qty * avg_pric)),
            'evlu_amt': str(evlu_amt),
            'evlu_pfls_amt': str(_toss_int(pl.get('amount'))),
            'evlu_pfls_rt': str(round(_toss_float(pl.get('rate')) * 100, 2)),
            'prpr': str(_toss_int(it.get('lastPrice'))),
        })

    deposit = _toss_krw_deposit()
    summary = {
        'tot_evlu_amt': str(tot_eval + deposit),
        'scts_evlu_amt': str(tot_eval),
        'dnca_tot_amt': str(deposit),
        'nxdy_excc_amt': str(deposit),
        'prvs_rcdl_excc_amt': str(deposit),
        'bfdy_sll_amt': '0', 'bfdy_buy_amt': '0',
        'bfdy_tlex_amt': '0', 'thdt_tlex_amt': '0',
    }
    logger.info(f"[Toss] 잔고 조회 결과: 종목수={len(output1)}, 총평가금(주식)={tot_eval:,}원")
    return output1, [summary]


def _toss_overseas_balance():
    """토스 보유주식(US) → KIS get_overseas_balance 형태(list)."""
    try:
        ov = toss_api.get_holdings()
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] 해외 보유주식 조회 실패: {e}")
        return []

    items = (ov or {}).get('items', []) or []
    out = []
    for it in items:
        if it.get('marketCountry') != 'US':
            continue
        pl = it.get('profitLoss', {}) or {}
        qty = _toss_float(it.get('quantity'))
        out.append({
            'ovrs_pdno': it.get('symbol', ''),
            'ovrs_item_name': it.get('name', ''),
            'prdt_name': it.get('name', ''),
            'ovrs_cblc_qty': str(qty),
            'ord_psbl_qty': str(qty),
            'pchs_avg_pric': str(_toss_float(it.get('averagePurchasePrice'))),
            'frcr_evlu_pfls_amt': str(_toss_float(pl.get('amount'))),
            'evlu_pfls_rt': str(round(_toss_float(pl.get('rate')) * 100, 2)),
            'now_pric2': str(_toss_float(it.get('lastPrice'))),
            'ovrs_now_pric': str(_toss_float(it.get('lastPrice'))),  # [추가] 화면 코드가 읽는 현재가 필드 (KIS 호환)
            '_exchange': it.get('market', 'NASD'),
        })
    return out


def _toss_daily_chart_with_tv_fallback(code, is_overseas):
    """토스 일봉을 조회하되, 해외 종목에서 토스 캔들이 비었거나 지표 계산에 부족하면
    TradingView(tvDatafeed) 일봉으로 폴백한다.

    SK hynix(ADR) 등 일부 해외 종목/ETF는 토스 캔들이 비어(또는 EMA120 산출에 못 미쳐)
    EMA/RSI/CCI/ADX가 표에서 '-'로 비는데, 이때 TV 시세로 보강한다. 국내 종목은 폴백하지 않는다.
    (tradingview_screener(스캐너)는 스냅샷만 제공하므로 시계열은 OHLC를 주는 tvDatafeed로 조회)

    [국내] 토스 캔들은 SOR 통합값이라 NXT 장전·장후 체결이 OHLC에 섞인다(ADX 왜곡 최대 9.45).
     KRX 정규장 기준 일봉을 pykrx/FDR로 먼저 조회하고, 실패 시에만 토스 캔들로 폴백한다.
     당일 봉은 이 소스들이 주지 않으므로 _get_cached_chart의 실시간 오버레이가 채운다.
    """
    if not is_overseas:
        krx_df = _krx_daily_chart(code)
        if krx_df is not None and not krx_df.empty:
            return krx_df

    df = _toss_chart_data(code, 'daily', is_overseas)
    if not is_overseas:
        return df
    have = 0 if (df is None or df.empty) else len(df)
    # 120봉 미만이면 EMA(120)가 계산되지 않아 지표가 빈다 → TV 폴백 시도.
    if have >= 120:
        return df
    # [추가] 단, 토스가 커서를 끝까지 밀어 '더 받을 과거 봉이 없음'을 확인했다면 그게 그 종목의
    #  전체 이력이다. 신규 상장 ETF는 TradingView에도 더 긴 이력이 없어 폴백이 매번 실패하는데,
    #  그 호출이 전역 락(_TVDATAFEED_LOCK)을 쥐고 있어 나머지 종목까지 함께 멈춘다.
    #  (실측 2026-07-29 RasPi3: NASA 82봉에 33.5초, DRAM 80봉에 16.8초 — 둘 다 소득 없음.
    #   같은 표의 다른 ETF는 5~6초. 메뉴 2 '데이터 수신'이 88%에서 정지하던 원인)
    if df is not None and not df.empty and df.attrs.get('exhausted'):
        logger.debug(f"[API] {code} 토스 일봉 {have}봉이 전체 이력 — TV 폴백 생략")
        return df
    try:
        from modules import analysis as _analysis
        tv_df = _analysis.fetch_overseas_daily_via_tvdatafeed(code)
    except Exception as e:
        logger.debug(f"[API] 토스 해외 일봉 TV 폴백 실패({code}): {e}")
        tv_df = None
    # 토스가 일부라도 줬으면 더 긴 쪽을 채택(TV도 실패하면 기존 토스 결과 유지).
    if tv_df is not None and not tv_df.empty and len(tv_df) > have:
        return tv_df
    return df


def _toss_long_daily(code, is_overseas, lookback_days=1100):
    """주봉 리샘플링용 **긴** 일봉(기본 ~3년). 화면 일봉과 목적이 다르다.

    [왜 따로 받나 · 2026-09-04] 토스는 주봉 API 가 없어 일봉을 주 단위로 묶는다. 그런데
    그 재료로 화면 일봉을 그대로 쓰면 주봉도 화면 일봉의 창을 물려받는다 — 화면 일봉은
    '52주 위치·EMA120' 기준으로 250봉(≈1년)에 맞춰 잘려 있으므로, 주봉이 52주밖에
    안 나온다. KIS 주봉은 lookback_days=1100(≈3년)으로 받으므로 같은 창을 맞춘다.

    국내는 KRX 정규장 기준(pykrx/FDR)을 그대로 쓴다 — 토스 캔들은 NXT 체결이 섞여
    OHLC 가 흔들리고, 주봉은 고·저를 그대로 물려받는다.
    """
    # 거래일 환산(연 ≈ 250거래일). 최소 250봉은 확보해 지표가 비지 않게 한다.
    bars = max(int(lookback_days * 250 / 365), 250)
    if not is_overseas:
        try:
            from modules import krx_daily
            if krx_daily.is_domestic_code(code):
                df = krx_daily.get_daily(code, lookback_days=lookback_days)
                if df is not None and not df.empty and len(df) >= 120:
                    src = df.attrs.get('source', '?')
                    df = df.reset_index(drop=True)
                    df.attrs['source'] = f"KRX/{src}"
                    return df
        except Exception as e:      # noqa: BLE001 - 외부 소스 장애가 차트를 막지 않게 한다
            logger.debug(f"[Toss] 주봉용 KRX 일봉 조회 실패({code}): {e}")
    return _toss_chart_data(code, 'daily', is_overseas, target_bars=bars)


# --- KRX 공식 일봉 실패 → 토스 캔들(NXT 포함) 폴백 추적 ---
#  폴백하면 일봉 OHLC에 NXT 장전·장후 체결이 섞여 ATR이 6~15% 부풀고 ADX가 최대 9.45 어긋난다
#  (손절폭·포지션 크기까지 영향). 조용히 넘어가면 사용자가 알 수 없으므로 화면에 경고를 띄운다.
_krx_fallback_lock = threading.Lock()
_krx_fallback = {}          # {code: 사유}


def note_krx_fallback(code, reason):
    """해당 종목이 토스 캔들(NXT 포함)로 계산됐음을 기록한다."""
    if not code:
        return
    with _krx_fallback_lock:
        if _krx_fallback.get(code) != reason:
            logger.warning(f"[KRX] {code} 공식 일봉 확보 실패({reason}) → 토스 캔들(NXT 포함)로 대체")
        _krx_fallback[code] = reason


def clear_krx_fallback(code=None):
    """복구된 종목(또는 전체)을 폴백 목록에서 제거한다."""
    with _krx_fallback_lock:
        if code is None:
            _krx_fallback.clear()
        else:
            _krx_fallback.pop(code, None)


def get_krx_fallback():
    """{종목코드: 사유} 사본. 비어 있으면 모든 종목이 KRX 공식 일봉으로 계산된 것이다."""
    with _krx_fallback_lock:
        return dict(_krx_fallback)


def _krx_daily_chart(code):
    """국내 일봉을 KRX 정규장 기준(pykrx/FDR)으로 조회한다. 실패·미지원이면 None.

    지표 산출에 못 미치는 짧은 결과(신규 상장 등)는 채택하지 않고 토스 캔들에 맡긴다 —
    EMA120이 계산되지 않으면 화면 지표가 통째로 비기 때문이다.
    """
    try:
        from modules import krx_daily
        # [Fix] 종전 가드가 isdigit()이라 문자가 섞인 코드('0080G0' 등)를 조용히 배제했고,
        #  note_krx_fallback도 거치지 않아 'KRX 일봉 미확보' 경고조차 뜨지 않았다.
        #  그 종목은 NXT 체결이 섞인 토스 캔들로 지표가 계산된다(ATR 6~15% 부풀림).
        if not krx_daily.is_domestic_code(code):
            note_krx_fallback(code, "국내 6자리 코드 아님")
            return None
        df = krx_daily.get_daily(code)
    except Exception as e:      # noqa: BLE001 - 외부 소스 장애가 시세 경로를 막지 않게 한다
        logger.debug(f"[API] KRX 일봉 조회 실패({code}): {e}")
        note_krx_fallback(code, "조회 오류")
        return None

    if df is None or df.empty:
        note_krx_fallback(code, "pykrx·FDR 모두 실패")
        return None
    if len(df) < 120:
        logger.debug(f"[API] KRX 일봉({code}) {len(df)}봉으로 부족 → 토스 캔들 사용")
        note_krx_fallback(code, f"{len(df)}봉뿐(지표 산출 부족)")
        return None

    clear_krx_fallback(code)        # 복구되면 경고에서 빠진다

    source = df.attrs.get('source', '?')
    df = df.tail(250).reset_index(drop=True)
    df = _append_today_bar_from_price(df, code)
    df.attrs['source'] = f"KRX/{source}"
    return df


def _append_today_bar_from_price(df, code):
    """pykrx·FDR이 주지 않는 '당일 봉'을 현재가로 채워 붙인다.

    _get_cached_chart의 실시간 오버레이는 '캐시 적중' 경로에서만 돈다. 캐시 미스(6시간마다 1회)
    직후의 첫 반환에는 당일 봉이 없어 그 사이클의 지표·신호가 전일 기준으로 계산된다.
    여기서 미리 붙여 두면 미스/히트 어느 경로든 당일 봉이 존재한다(이후 오버레이가 갱신).
    """
    try:
        today = _api().market_today(False)
        if str(df.iloc[-1]['date']) >= today:
            return df
        # 모든 장 종료 후에는 현재가가 마지막 NXT 체결가로 굳어 있어 '당일 봉'으로 쓸 수 없다.
        # (그 시간대엔 pykrx/FDR이 이미 확정 종가를 주므로 보강 자체가 불필요하다)
        if not _api().chart_overlay_enabled(is_overseas=False):
            return df

        cp = _api().get_current_price_data(code, False)
        if not cp or cp.get('rt_cd') != '0':
            return df
        out = cp.get('output', {}) or {}

        def _f(key):
            try:
                return float(str(out.get(key, 0) or 0).replace(',', ''))
            except (TypeError, ValueError):
                return 0.0

        curr = _f('stck_prpr')
        if curr <= 0:
            return df

        open_p = _f('stck_oprc') or curr
        high_p = max(_f('stck_hgpr'), curr)
        low_p = _f('stck_lwpr')
        low_p = min(low_p, curr) if low_p > 0 else curr

        row = pd.DataFrame([{'date': today, 'open': open_p, 'high': high_p,
                             'low': low_p, 'close': curr, 'volume': _f('acml_vol')}])
        return pd.concat([df, row], ignore_index=True)
    except Exception as e:      # noqa: BLE001 - 당일 봉 보강 실패가 과거봉 반환을 막지 않게 한다
        logger.debug(f"[API] KRX 일봉 당일 봉 보강 실패({code}): {e}")
        return df


# 저가 이상치 판정 폭 — '이웃(전·익일) 저가' 대비 얼마나 아래로 고립됐는지.
# 실측(2026-07-22, KIS 국내 33종목·8,250봉)의 정상 봉 최대 고립도는 -13.2%였고 오염은 -29.0%였다.
# 20%는 그 사이의 여유 있는 경계다. (전일종가 대비·시가/종가 대비 축은 정상 봉이 각각 +29.9%,
#  +33.3%까지 나와 오염과 완전히 겹쳤다 — 이 축들로는 판별할 수 없다.)
_TOSS_DAILY_ISOLATION_GUARD = 0.20


def _toss_sanitize_daily_ohlc(df, code=""):
    """토스 국내 일봉에 섞인 '고립된 저가 이상치'를 제거한다.

    토스 캔들은 NXT 프리마켓(08:00) 개장 직후의 하한가 체결을 일봉 시가/저가에 그대로 섞는다.
      KT 2025-09-03      시가·저가 36,500 (KRX 실제 52,000 / 종가 52,500 / 이웃 저가 51,400)
      HD현대중공업 09-29  저가 344,500     (KRX 실제 486,500 / 종가 491,500)
    이 값은 52주 밴드를 넓혀 위치를 과대평가하고(KT 47.2% ← 실제 19.7%), ATR·변동성·샹들리에
    트레일링 스톱까지 부풀린다.

    판정 축은 '이웃 봉 대비 저가의 고립도' 하나뿐이다. 실측상 이 축만이 정상 봉(최대 -13.2%)과
    오염(-29.0%)을 안전하게 가른다. 종가가 그 저가 근처까지 내려간 봉은 실제로 그 가격대에서
    거래된 것이므로 건드리지 않는다(진짜 하한가 마감일 보존).

    고가 오염(삼성SDI 2026-04-22 820,000 등)은 의도적으로 손대지 않는다. 정상 봉의 고가 고립도가
    +21.3%까지 나와 오염(+21.5%)과 구분되지 않으며, 무리하게 걸러내면 진짜 급등 봉의 고가를 깎아
    52주 밴드를 되레 왜곡한다(초기 구현에서 LG이노텍·SK텔레콤이 실제로 훼손됐다).

    보정값은 임의의 클램프가 아니라 그 봉의 신뢰 가능한 실제 체결가(시가/종가)와 이웃 저가 중
    최소값으로 접는다 — 밴드를 넓히는 방향으로는 절대 보정하지 않는다. 종가는 어떤 경우에도
    수정하지 않는다.
    """
    if df is None or df.empty or len(df) < 3:
        return df
    g = _TOSS_DAILY_ISOLATION_GUARD
    fixed = 0
    try:
        io, ih, il, ic = (df.columns.get_loc(c) for c in ('open', 'high', 'low', 'close'))
        # 마지막 봉(당일)은 장중 갱신 중이라 이웃이 한쪽뿐이고 값도 계속 바뀐다 → 대상에서 제외.
        for i in range(len(df) - 1):
            neighbors = [float(df.iat[i + 1, il] or 0)]
            if i > 0:
                neighbors.append(float(df.iat[i - 1, il] or 0))
            nb_lo = min([v for v in neighbors if v > 0], default=0.0)
            if nb_lo <= 0:
                continue
            floor = nb_lo * (1 - g)
            l = float(df.iat[i, il] or 0)
            c = float(df.iat[i, ic] or 0)
            # 종가까지 floor 아래면 진짜 급락일이다(하한가 마감 등) → 원본 보존.
            if not (0 < l < floor <= c):
                continue
            o = float(df.iat[i, io] or 0)
            trusted = [v for v in (o, c) if v >= floor]   # 시가도 같은 이상치면 후보에서 빠진다
            new_l = min(trusted + [nb_lo])
            df.iat[i, il] = new_l
            if o < floor:
                df.iat[i, io] = new_l
            if float(df.iat[i, ih] or 0) < new_l:         # 정합성 방어
                df.iat[i, ih] = new_l
            fixed += 1
        if fixed:
            logger.debug(f"[Toss] 일봉 저가 이상치 보정({code}): {fixed}봉")
    except Exception as e:
        logger.debug(f"[Toss] 일봉 저가 이상치 보정 실패({code}): {e}")
    return df


def _toss_daily_df(candles):
    """토스 일봉 캔들 리스트 → DataFrame(date=YYYYMMDD 문자열, 오름차순).

    시드 대조와 최종 반환이 같은 형태를 쓰도록 한 곳에 모은다 — 형식이 갈라지면
    겹침 구간 종가 대조가 매번 실패해 시드가 조용히 무력화된다.
    """
    rows = [{
        'date': str(c.get('timestamp', ''))[:10].replace('-', ''),  # KIS 일봉과 동일 형식
        'open': _toss_float(c.get('openPrice')),
        'high': _toss_float(c.get('highPrice')),
        'low': _toss_float(c.get('lowPrice')),
        'close': _toss_float(c.get('closePrice')),
        'volume': _toss_float(c.get('volume')),
    } for c in candles]
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset=['date'])
    return df.sort_values('date', ascending=True).reset_index(drop=True)


def _toss_chart_data(code, period_type='daily', is_overseas=False, target_bars=None):
    """토스 캔들 → KIS get_chart_data 형태의 DataFrame.
    columns=['date','open','high','low','close','volume'] (date=YYYYMMDD, 오름차순).

    일봉은 52주 위치/EMA120 정확도를 위해 nextBefore 커서로 ~250개 이상 모은다
    (토스 캔들은 호출당 최대 200개). 분봉은 단일 호출(200개).

    target_bars 는 일봉을 더 길게 받아야 하는 호출부(주봉 리샘플링)가 쓴다. 화면 일봉은
    250봉이면 충분하지만 주봉 3년은 ~750봉이 필요하다. 기본값(None)이면 종전과 같다.
    """
    if period_type == 'hourly':
        # 토스는 1분/일봉만 제공 → 시봉 미지원
        return pd.DataFrame()
    interval = '1m' if period_type == 'intraday' else '1d'
    # 일봉: 52주(≈250거래일) 확보.
    # 분봉: KIS와 동일하게 "당일 정규장(09:00~15:30)"만 표시한다. 토스 1분봉은 NXT(대체거래소)
    # 연장시간(08:00~20:00)까지 포함하므로, 장후(NXT 20:00)에 조회해도 당일 09:00까지 닿도록
    # 하루 분량(≈720)을 커서로 모은다. (토스 count 최대 200 → 400은 [400] invalid-request)
    per_call = 200
    if interval == '1d':
        target = int(target_bars) + 10 if target_bars else 260
        # 페이지 예산은 목표 봉 수에서 나온다(콜당 200개). 종전 상수 4는 260봉 기준이었다.
        max_pages = max(4, -(-target // per_call) + 1)
    else:
        target, max_pages = 720, 5

    candles = []
    before = None
    prev_cursor = None
    # [추가] 커서를 끝까지 밀어 '더 받을 과거 봉이 없음'을 확인했는가.
    #  받은 봉 수가 적은 것과 '그게 전부인 것'은 다르다 — 신규 상장 종목은 후자다.
    #  해외 폴백(_toss_daily_chart_with_tv_fallback)이 이 값을 보고 헛수고를 건너뛴다.
    exhausted = False
    seeded_df = None  # [최적화] 시드로 과거 구간을 채워 2페이지 요청을 생략한 경우의 병합 결과
    page_log = []  # [진단] 분봉 페이징 추적
    try:
        for _ in range(max_pages):
            res = toss_api.get_candles(code, interval=interval, count=per_call, before=before)
            batch = (res or {}).get('candles', []) or []
            nb = (res or {}).get('nextBefore')
            if interval == '1m':
                _tsb = [str(c.get('timestamp', '')) for c in batch if c.get('timestamp')]
                page_log.append(
                    f"before={before} → {len(batch)}건"
                    f"[{min(_tsb) if _tsb else '-'}~{max(_tsb) if _tsb else '-'}] nextBefore={nb}")
            if not batch:
                exhausted = True     # 더 받을 과거 봉이 없다
                break
            candles.extend(batch)
            if len(candles) >= target:
                break
            # [최적화] 일봉: 최신 페이지를 받은 뒤 저장해 둔 시드로 앞을 채울 수 있으면 여기서 멈춘다.
            #  과거 봉은 불변이라 재조회가 낭비이고, /candles는 5 RPS라 콜 1회가 곧 소요다.
            #  (수정주가 검증에 실패하면 None → 아래 정상 페이징을 그대로 탄다)
            if interval == '1d':
                seeded_df = _api()._toss_seed_extend(code, _toss_daily_df(candles), target)
                if seeded_df is not None:
                    break
            # 다음 페이지 커서: nextBefore 우선, 없으면 이번 배치의 가장 오래된 timestamp로 폴백.
            # (분봉은 nextBefore가 1페이지 후 끊기는 경우가 있어 09:00까지 못 가는 문제를 보완)
            oldest_ts = min((str(c.get('timestamp', '')) for c in batch if c.get('timestamp')),
                            default=None)
            before = nb or oldest_ts
            if not before or before == prev_cursor:  # 커서가 더 진행 못하면 중단(무한루프 방지)
                exhausted = True
                break
            prev_cursor = before
    except toss_api.TossApiError as e:
        cand_err = e
        logger.debug(f"[Toss] 캔들 조회 실패({code}): {e}")
    else:
        cand_err = None

    if interval == '1m' and (config.FILE_DEBUG_LEVEL == "DEBUG" or cand_err):
        # [진단] 페이지별 건수/시간범위/커서 + 에러를 1줄로 남긴다(에러는 항상 기록).
        logger.warning(f"[Toss] 분봉({code}) 페이징 {len(page_log)}회: "
                       + (" || ".join(page_log) if page_log else "(요청 결과 없음)")
                       + (f" | ERROR={cand_err}" if cand_err else ""))

    if not candles:
        # [진단] 일봉이 통째로 비면 화면엔 '차트 없음'·지표 전체 '-'로만 나타나 원인이 안 남는다.
        #  (토큰 만료·레이트리밋 등) 실패 사유를 항상 로그에 남긴다. 분봉은 위에서 이미 기록.
        if interval == '1d':
            logger.warning(f"[Toss] 일봉({code}) 조회 결과 없음"
                           + (f" | ERROR={cand_err}" if cand_err else " (요청은 성공했으나 캔들 0건)"))
        return pd.DataFrame()
    if interval == '1d':
        df = _toss_daily_df(candles)
    else:
        rows = []
        for c in candles:
            ts = str(c.get('timestamp', ''))
            # 분봉: KIS(_get_intraday_chart_data)와 동일하게 Timestamp로 보관해야
            # chart.format_date가 strftime 경로를 타 'MM-DD HH:MM' 라벨을 동일하게 출력한다.
            # (12자리 문자열로 두면 format_date 어느 분기에도 안 걸려 raw 숫자가 X축에 찍힘)
            date = pd.to_datetime(ts, errors='coerce')
            if pd.notna(date) and getattr(date, 'tzinfo', None) is not None:
                date = date.tz_convert('Asia/Seoul').tz_localize(None)
            rows.append({
                'date': date,
                'open': _toss_float(c.get('openPrice')),
                'high': _toss_float(c.get('highPrice')),
                'low': _toss_float(c.get('lowPrice')),
                'close': _toss_float(c.get('closePrice')),
                'volume': _toss_float(c.get('volume')),
            })
        df = pd.DataFrame(rows).drop_duplicates(subset=['date'])
        df = df.sort_values('date', ascending=True).reset_index(drop=True)
    if interval == '1d':
        # 시드 병합에 성공했으면 그 결과가 곧 전체 구간이다(받은 페이지는 최신 1장뿐).
        if seeded_df is not None:
            df = seeded_df
        # 다음 조회가 최신 1페이지만으로 끝나도록, 정제 전 원본을 시드로 남긴다.
        #  (정제 후를 저장하면 다음 번 겹침 대조가 원본 종가와 어긋나 매번 폐기된다)
        _api()._toss_seed_set(code, df)
        if not is_overseas:
            # NXT 프리마켓 상·하한가 체결이 섞인 봉을 먼저 정제한다(tail 이전 = 첫 봉도 전일 종가 확보).
            # 해외는 가격제한폭이 없어 '제한폭에 붙은 값' 판정이 성립하지 않으므로 제외.
            df = _toss_sanitize_daily_ohlc(df, code)
        df = df.tail(int(target_bars) if target_bars else 250).reset_index(drop=True)
        df.attrs['source'] = 'TOSS'
        # 커서를 끝까지 밀어 확인한 경우에만 True — 호출부가 '이게 전체 이력'으로 신뢰한다
        df.attrs['exhausted'] = exhausted
        # 국내 일봉 종가는 NXT 연장(~20:00) 체결까지 포함한 값 그대로 둔다.
        # (과거엔 직전 봉 종가를 KRX 기준가로 역산 보정했으나 역산 로직을 폐기 —
        #  mode 3 등락률은 전일 NXT 종가 기준으로 계산하며, 기동 시 안내한다.)
        return df

    # 분봉: KIS 당일분봉과 동일하게 "당일(가장 최근 거래일)의 KRX 정규장"만 표시.
    # 토스의 시간외/NXT 연장(08:00~/~20:00) 캔들과 날짜 교차를 모두 제거한다.
    # 장전에 조회하면 당일 정규장 데이터가 없어 빈 값이 되고(→ 호출부에서 장전 안내),
    # 장중이면 개장~현재, 장후면 정규장 전체가 된다.
    # 경계는 market-calendar/KR 값을 쓴다(기본 09:00~15:30) — 임시 지연·단축 개장 대응.
    last_day = df['date'].dt.normalize().max()
    (sh, sm), (eh, em) = _toss_krx_regular_bounds(last_day.strftime('%Y%m%d'))
    minutes = df['date'].dt.hour * 60 + df['date'].dt.minute
    in_session = (minutes >= sh * 60 + sm) & (minutes <= eh * 60 + em)
    return df[(df['date'].dt.normalize() == last_day) & in_session].reset_index(drop=True)


def _toss_current_price_data(code, is_overseas):
    """토스 현재가 → KIS get_current_price_data 형태({rt_cd,output})."""
    try:
        row = toss_api.get_price(code)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 현재가 조회 실패({code}): {e}")
        row = None
    # [폴백] 토스 해외 현재가 조회 실패 시 TradingView 시세로 대체한다(mode 1/2와 동일 경로).
    #  국내는 KRX/NXT 전용이라 TV 폴백 대상이 아니다.
    price = (row or {}).get('lastPrice', '0')
    if is_overseas and (not row or _toss_float(price) <= 0):
        res_tv = _api()._overseas_tv_fallback_price(code)
        if res_tv is not None:
            return res_tv
    if not row:
        return {'rt_cd': '9999'}
    # 국내(stck_prpr)/해외(last) 양쪽 경로를 모두 채운다. 외인비율 등은 토스 미제공.
    output = {
        'stck_prpr': str(_toss_int(price)),
        'last': str(_toss_float(price)),
    }
    # [추가] 국내: 기준가 대비 전일대비/등락률 필드를 채운다. 기준가=저장된 KRX 정규장 마감가
    # (있으면 HTS 일치), 없으면 전일 NXT 종가로 폴백. 마감 후엔 오늘 KRX 마감가를 1회 캡처해 저장.
    if not is_overseas:
        _toss_capture_krx_close(code)  # 마감(15:35+) 후 오늘 KRX 마감가 1회 저장(이미 있으면 즉시 반환)
        p = _toss_float(price)

        # [모드 정합성] ETF/ETN은 마감 후 현재가를 'KRX 정규장 종가'로 고정한다.
        #  ETF는 NXT 연장거래 대상이 아니라 15:30 이후 체결은 전부 KRX 시간외단일가(16:00~18:00)다.
        #  KIS 경로(mode 1/2)는 시간외단일가를 별도 TR(FHPST02300000)로만 제공해 반영하지 않으므로
        #  정규장 종가를 보여주는데, 토스 lastPrice는 시간외 체결을 그대로 반영해 두 모드가 어긋났다.
        #  (실측 2026-07-22 16:07 KODEX 코스닥150: KIS·HTS 12,525 / 토스 12,530 = 시간외 체결가.
        #   같은 시각 시간외 거래량이 있는 ETF만 값이 갈리고, 거래가 없던 ETF는 정확히 일치했다.)
        #  일봉 종가와 지표(EMA/RSI/CCI·52주 위치)의 기준은 정규장 종가이고 시간외단일가는 거래량이
        #  극히 적어(수 주~수십 주) 노이즈에 가까우므로, 마감 후에는 캡처해 둔 KRX 마감가로 고정한다.
        #  ※ NXT 거래 종목은 연장가를 mode 1/2도 함께 노출하므로(get_multi_current_prices_nxt)
        #    여기서 고정하면 오히려 어긋난다 → NXT 미거래 종목(_toss_krx_only)에만 적용한다.
        #    (종전엔 ETF/ETN 휴리스틱이었으나 nxtSupported로 판정해 NXT 미지원 주식까지 포함)
        if _toss_after_krx_close() and _toss_krx_only(code):
            krx_close = _toss_krx_close_get(code, datetime.now().strftime('%Y%m%d'))
            if krx_close and krx_close > 0:
                p = float(krx_close)
                output['stck_prpr'] = str(int(round(p)))
                output['last'] = str(p)

        base = _toss_base_price(code)
        if base and base > 0 and p > 0:
            output['stck_sdpr'] = str(int(base))
            output['prdy_vrss'] = str(int(round(p - base)))
            output['prdy_ctrt'] = str(round((p - base) / base * 100, 2))
            # [마감 후 최종 등락률 유지] NXT 미지원 종목은 체결이 없어 현재가(lastPrice)=전일 종가
            #  =기준가 → 등락률 0%가 된다. 다음날 NXT 개장(08:00) 전까지는 기준가를 전전일 종가로
            #  대체해 직전 정규장 최종 등락률(전일 vs 전전일)을 유지한다. 08:00(NXT 프리마켓) 이후엔
            #  대체하지 않아 KRX 개장 전까지 약 1시간은 0%로 노출된다.
            if int(round(p)) == int(round(base)) and _toss_before_nxt_open():
                pp = _toss_prev_prev_close(code)
                if pp and pp > 0:
                    output['stck_sdpr'] = str(int(pp))
                    output['prdy_vrss'] = str(int(round(p - pp)))
                    output['prdy_ctrt'] = str(round((p - pp) / pp * 100, 2))
    return {'rt_cd': '0', 'output': output}


def _toss_order_book(code):
    """토스 호가 → KIS get_order_book 형태({rt_cd,output1})."""
    try:
        ob = toss_api.get_orderbook(code)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 호가 조회 실패({code}): {e}")
        return {'rt_cd': '9999'}
    if not ob:
        return {'rt_cd': '9999'}
    asks = ob.get('asks', []) or []   # 낮은 가격순(최우선 매도호가가 [0])
    bids = ob.get('bids', []) or []   # 높은 가격순(최우선 매수호가가 [0])
    out1 = {}
    total_ask = 0
    total_bid = 0
    for i in range(10):
        a = asks[i] if i < len(asks) else {}
        b = bids[i] if i < len(bids) else {}
        av = _toss_int(a.get('volume'))
        bv = _toss_int(b.get('volume'))
        total_ask += av
        total_bid += bv
        out1[f'askp{i+1}'] = str(_toss_int(a.get('price')))
        out1[f'bidp{i+1}'] = str(_toss_int(b.get('price')))
        out1[f'askp_rsqn{i+1}'] = str(av)
        out1[f'bidp_rsqn{i+1}'] = str(bv)
    out1['total_askp_rsqn'] = str(total_ask)
    out1['total_bidp_rsqn'] = str(total_bid)
    return {'rt_cd': '0', 'output1': out1}


# -------------------------------------------------------------------------
# [추가] 토스 주문(메뉴 8) 어댑터: 미체결 조회 / 주문 / 정정·취소 / 주문가능수량
# -------------------------------------------------------------------------
def _toss_name_map(symbols):
    """심볼 목록 → {symbol: name} (종목명 표시용). 실패 시 빈 dict."""
    syms = [s for s in dict.fromkeys(symbols) if s]
    if not syms:
        return {}
    try:
        rows = toss_api.get_stocks(syms)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 종목명 조회 실패: {e}")
        return {}
    return {r.get('symbol'): r.get('name', '') for r in (rows or []) if r.get('symbol')}


def _toss_open_orders(market):
    """토스 미체결(OPEN) 주문 → KIS 미체결 형태.
    market: 'domestic'(KRW) | 'overseas'(USD)."""
    try:
        res = toss_api.get_orders(status="OPEN")
    except toss_api.TossApiError as e:
        #  조회 실패는 '미체결 없음'이 아니다 — None 이어야 호출부가 중복 주문 게이트를
        #  내린다(api/account.get_domestic_open_orders 주석 참조).
        logger.error(f"[Toss] 미체결 조회 실패 — '없음'이 아니라 '모름'으로 돌려준다: {e}")
        return None

    orders = (res or {}).get('orders', []) or []
    want_krw = (market == 'domestic')
    picked = []
    for o in orders:
        is_krw = (o.get('currency') == 'KRW')
        if is_krw != want_krw:
            continue
        picked.append(o)

    name_map = _toss_name_map([o.get('symbol') for o in picked])
    out = []
    for o in picked:
        symbol = o.get('symbol', '')
        side = o.get('side')  # BUY / SELL
        sll_buy_cd = '02' if side == 'BUY' else '01'  # KIS: 01=매도, 02=매수
        sll_buy_name = '매수' if side == 'BUY' else '매도'
        qty = _toss_int(o.get('quantity'))
        filled = _toss_int((o.get('execution') or {}).get('filledQuantity'))
        rmn = max(qty - filled, 0)
        # orderedAt: '2026-03-29T09:30:00+09:00' → ord_dt(YYYYMMDD) / ord_tmd(HHMMSS)
        ts = str(o.get('orderedAt', ''))
        ord_dt = ts[:10].replace('-', '') if len(ts) >= 10 else ''
        ord_tmd = ts[11:19].replace(':', '') if len(ts) >= 19 else ''
        name = name_map.get(symbol) or symbol

        if want_krw:
            out.append({
                'odno': o.get('orderId', ''),
                'pdno': symbol,
                'prdt_name': name,
                'sll_buy_dvsn_cd': sll_buy_cd,
                'sll_buy_dvsn_cd_name': sll_buy_name,
                'ord_qty': str(qty),
                'ord_unpr': str(_toss_int(o.get('price'))),
                'rmn_qty': str(rmn),
                'ord_tmd': ord_tmd,
                '_toss_order_type': o.get('orderType', 'LIMIT'),
            })
        else:
            out.append({
                'odno': o.get('orderId', ''),
                'pdno': symbol,
                'prdt_name': name,
                'sll_buy_dvsn_cd': sll_buy_cd,
                'ft_ord_qty': str(qty),
                'nccs_qty': str(rmn),
                'ft_ord_unpr3': str(_toss_float(o.get('price'))),
                'ord_unpr': str(_toss_float(o.get('price'))),
                'ord_dt': ord_dt,
                'ord_tmd': ord_tmd,
                'ovrs_excg_cd': o.get('_market', 'NASD'),
                '_toss_order_type': o.get('orderType', 'LIMIT'),
            })
    logger.info(f"[Toss] 미체결({market}) 조회 결과: {len(out)}건")
    return out


def _toss_today_closed_orders():
    """오늘(KST) 종료된(CLOSED) 토스 주문 목록(페이지네이션). 2초 마이크로 캐시.
    체결 감시(ConclusionMonitor)가 국내/해외 두 번 호출하므로 캐시로 중복 호출을 줄인다."""
    cache_key = "toss_closed_today"
    cached = _api()._get_micro_cache(cache_key, ttl=2.0)
    if cached is not None:
        return cached

    today = datetime.now().strftime("%Y-%m-%d")

    def _fetch(use_range):
        out = []
        cursor = None
        for _ in range(10):  # 최대 10페이지(=1000건) 방어
            kwargs = {"status": "CLOSED", "limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            if use_range:
                kwargs["from_date"] = today
                kwargs["to_date"] = today
            res = toss_api.get_orders(**kwargs)
            out.extend((res or {}).get('orders', []) or [])
            if not (res or {}).get('hasNext'):
                break
            cursor = (res or {}).get('nextCursor')
            if not cursor:
                break
        return out

    orders = []
    try:
        orders = _fetch(use_range=True)
    except toss_api.TossApiError as e:
        # from/to 형식 등으로 실패하면 기간 없이 재조회 후 클라이언트 필터에 의존
        logger.debug(f"[Toss] 체결이력 기간조회 실패, 전체조회로 폴백: {e}")
        try:
            orders = _fetch(use_range=False)
        except toss_api.TossApiError as e2:
            logger.error(f"[Toss] 당일 체결이력 조회 실패: {e2}")
            orders = []

    # 오늘(KST) filledAt(없으면 orderedAt) 기준으로 한 번 더 필터(방어)
    todays = []
    for o in orders:
        ts = str((o.get('execution') or {}).get('filledAt') or o.get('orderedAt') or '')
        if ts[:10] == today:
            todays.append(o)
    _api()._set_micro_cache(cache_key, todays)
    return todays


def _toss_period_entry_dates(codes, qty_map=None, months=12):
    """토스 CLOSED 주문 이력에서 현 포지션의 진입일을 찾는다. {code: 'YYYYMMDD'}

    KIS의 get_period_entry_dates와 같은 역할(수량 흐름 재생). 토스는 기간(from/to)
    조회를 한 번에 받으므로 3개월씩 끊을 필요가 없다.

    [조회 실패는 올린다 · 2026-09-05] 종전에는 `(res or {})` 로 None 을 삼켜, 요청이
     실패하면 **'이 기간에 주문이 없다'와 똑같이 빈 dict** 를 돌려줬다. 호출부
     (api.account.get_period_entry_dates)는 그 답을 15분 마이크로 캐시에 굳히므로,
     토스가 잠깐 흔들리면 그 종목들의 보유일수가 15분 동안 **0일**로 남는다 — 시간청산
     (15일)의 시계가 그만큼 밀리고, 그 사이 재조회는 0회다(실측).
     KIS 경로는 같은 결함을 2026-09-05 에 `ok` 플래그로 고쳤다(_fetch_period_executions).
     토스는 호출부가 예외를 이미 잡아 캐시하지 않고 빠지므로, 여기서는 올리면 된다.
     페이지 상한(20p=2000건)에 걸린 것도 '다 읽었다'가 아니므로 같이 올린다 —
     반쪽 이력으로 수량 흐름을 재생하면 엉뚱한 날짜가 나온다.
    """
    wanted = set(codes)
    rows = {c: [] for c in wanted}

    today = datetime.now()
    start = today - timedelta(days=int(months * 30.5))
    cursor = None
    exhausted = False

    for _ in range(20):  # 최대 20페이지(=2000건) 방어
        kwargs = {"status": "CLOSED", "limit": 100,
                  "from_date": start.strftime("%Y-%m-%d"),
                  "to_date": today.strftime("%Y-%m-%d")}
        if cursor:
            kwargs["cursor"] = cursor

        res = toss_api.get_orders(**kwargs)
        if not isinstance(res, dict):
            raise RuntimeError(
                f"토스 주문 이력을 조회하지 못했습니다(status=CLOSED, from={kwargs['from_date']}) "
                f"— '주문이 없다'가 아닙니다")
        for o in ((res or {}).get('orders') or []):
            side = str(o.get('side') or '').upper()
            if side not in ('BUY', 'SELL'):
                continue
            code = str(o.get('symbol') or '').strip()
            if code not in wanted:
                continue
            ex = o.get('execution') or {}
            qty = _toss_int(ex.get('filledQuantity'))
            if qty <= 0:
                continue  # 취소·미체결 주문은 수량 흐름에 영향이 없다
            ts = str(ex.get('filledAt') or o.get('orderedAt') or '')
            date = ts[:10].replace('-', '')
            if len(date) == 8:
                rows[code].append((date, side == 'BUY', qty))

        if not (res or {}).get('hasNext'):
            exhausted = True
            break
        cursor = (res or {}).get('nextCursor')
        if not cursor:
            exhausted = True
            break
    if not exhausted:
        raise RuntimeError("토스 주문 이력이 페이지 상한(2000건)에서 끊겼습니다 "
                           "— 반쪽 이력으로 진입일을 재생하지 않습니다")

    window_start = start.strftime("%Y%m%d")
    found = {}
    for code, r in rows.items():
        d = _api()._replay_entry_date(r, (qty_map or {}).get(code), window_start)
        if d:
            found[code] = d
    return found


def _toss_history_item(o, overseas, name_map):
    """토스 CLOSED 주문 1건 → KIS 당일 체결이력 항목."""
    symbol = o.get('symbol', '')
    side = o.get('side')
    sll_buy_cd = '02' if side == 'BUY' else '01'
    sll_buy_name = '매수' if side == 'BUY' else '매도'
    qty = _toss_int(o.get('quantity'))
    ex = o.get('execution') or {}
    filled = _toss_int(ex.get('filledQuantity'))
    status = str(o.get('status', ''))
    is_canceled = bool(o.get('canceledAt')) or status in ('CANCELED', 'EXPIRED', 'REJECTED')
    cncl = max(qty - filled, 0) if is_canceled else 0
    avg = _toss_float(ex.get('averageFilledPrice'))
    ts = str(ex.get('filledAt') or o.get('orderedAt') or '')
    ord_dt = ts[:10].replace('-', '') if len(ts) >= 10 else ''
    ord_tmd = ts[11:19].replace(':', '') if len(ts) >= 19 else ''
    name = name_map.get(symbol) or symbol

    item = {
        'odno': o.get('orderId', ''),
        'pdno': symbol,
        'prdt_name': name,
        'sll_buy_dvsn_cd': sll_buy_cd,
        'sll_buy_dvsn_cd_name': sll_buy_name,
        'ord_dt': ord_dt,
        'ord_tmd': ord_tmd,
        'cncl_cfrm_qty': str(cncl),
    }
    if overseas:
        # 해외는 ft_* 필드 존재로 판별되므로 국내 항목에는 절대 넣지 않는다.
        item.update({
            'ovrs_item_name': name,
            'ft_ord_qty': str(qty),
            'ft_ccld_qty': str(filled),
            'nccs_qty': '0',  # CLOSED 주문은 잔량 0
            'ft_ccld_unpr3': str(avg),
        })
    else:
        item.update({
            'ord_qty': str(qty),
            'tot_ccld_qty': str(filled),
            'rmn_qty': '0',
            'avg_prvs': str(avg),
        })
    return item


def _toss_today_history(overseas):
    """KIS get_today_history / get_overseas_today_history 토스 어댑터."""
    orders = _toss_today_closed_orders()
    want_krw = not overseas
    picked = [o for o in orders if (o.get('currency') == 'KRW') == want_krw]
    name_map = _toss_name_map([o.get('symbol') for o in picked])
    items = [_toss_history_item(o, overseas, name_map) for o in picked]
    logger.info(f"[Toss] 당일 체결이력({'해외' if overseas else '국내'}) 조회 결과: {len(items)}건")
    if overseas:
        return {'rt_cd': '0', 'output': items}
    return {'rt_cd': '0', 'output1': items}


def _toss_place_order(market, action, code, qty, price, ord_dvsn):
    """토스 주문 생성 → KIS place_order 형태({rt_cd,msg1,output:{ODNO}})."""
    side = 'BUY' if action == 'buy' else 'SELL'
    # KIS ord_dvsn: '01'=국내 시장가 → 토스 MARKET, 그 외 지정가
    is_market = (market == 'domestic' and str(ord_dvsn) == '01')
    order_type = 'MARKET' if is_market else 'LIMIT'
    order_price = None if is_market else price
    try:
        r = toss_api.create_order(
            symbol=code, side=side, order_type=order_type,
            quantity=qty, price=order_price,
        )
        odno = str((r or {}).get('orderId') or '').strip()
        if not odno:
            #  [Fix 2026-09-05] 주문번호 없는 '성공'은 성공이 아니다. 서버는 접수했는데
            #   우리는 그것을 가리킬 수단이 없다 — 추적 키가 '' 가 되면 체결 대사도,
            #   미체결 자동 취소도 그 주문을 못 찾고, 그 종목은 is_pending 인 채로
            #   손절·트레일링 판정에서 통째로 빠진다. 결과 불명으로 올려 조회로 대사한다.
            raise _api().OrderOutcomeUnknown(
                "토스 주문 응답에 주문번호가 없습니다(접수 여부 불명)")
        logger.info(f"[Toss] 주문 접수: {side} {code} {qty}주 @{order_price} ({order_type}) → {odno}")
        return {'rt_cd': '0', 'msg_cd': '0000', 'msg1': '주문 접수 완료',
                'output': {'ODNO': odno, 'KRX_FWDG_ORD_ORGNO': '', 'ORD_TMD': ''}}
    except toss_api.TossOrderOutcomeUnknown as e:
        # 응답이 유실됐다 — 실패가 아니라 '모름'이다. KIS 경로와 같은 예외로 올려
        #  place_order 가 재전송 대신 조회로 대사하게 한다.
        raise _api().OrderOutcomeUnknown(str(e.message or e)) from e
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] 주문 실패: {e}")
        return {'rt_cd': '1', 'msg_cd': str(e.code or ''), 'msg1': str(e.message or e), 'output': {}}


def _toss_revise_cancel(market, action, org_no, code, qty, price, ord_dvsn):
    """토스 정정/취소 → KIS revise_cancel_order 형태({rt_cd,msg1,output:{ODNO}})."""
    try:
        if action == 'cancel':
            r = toss_api.cancel_order(org_no)
        else:  # modify (정정)
            is_market = (market == 'domestic' and str(ord_dvsn) == '01')
            order_type = 'MARKET' if is_market else 'LIMIT'
            # 토스 정정은 수량이 필수(누락 시 '주문 수량이 유효하지 않습니다' 오류).
            # KIS의 0=전량정정 sentinel이 들어오면 미체결 잔량을 조회해 실제 수량으로 명시한다.
            mod_qty = qty if (qty and int(qty) > 0) else None
            if mod_qty is None:
                try:
                    od = toss_api.get_order(org_no) or {}
                    rem = _toss_int(od.get('quantity')) - _toss_int((od.get('execution') or {}).get('filledQuantity'))
                    if rem > 0:
                        mod_qty = rem
                except toss_api.TossApiError as e:
                    logger.debug(f"[Toss] 정정 잔량 조회 실패({org_no}): {e}")
            r = toss_api.modify_order(
                org_no, order_type=order_type,
                quantity=mod_qty, price=(None if is_market else price),
            )
        odno = str((r or {}).get('orderId') or '').strip()
        if not odno:
            #  정정·취소도 같다 — 새 주문번호를 모르면 그 주문을 추적할 수 없다.
            raise _api().OrderOutcomeUnknown(
                f"토스 {action} 응답에 주문번호가 없습니다(처리 여부 불명)")
        logger.info(f"[Toss] {action} 완료: 원주문={org_no} → 신규={odno}")
        return {'rt_cd': '0', 'msg_cd': '0000', 'msg1': f'{action} 완료',
                'output': {'ODNO': odno, 'KRX_FWDG_ORD_ORGNO': ''}}
    except toss_api.TossOrderOutcomeUnknown as e:
        raise _api().OrderOutcomeUnknown(str(e.message or e)) from e
    except toss_api.TossApiError as e:
        logger.error(f"[Toss] {action} 실패: {e}")
        return {'rt_cd': '1', 'msg_cd': str(e.code or ''), 'msg1': str(e.message or e), 'output': {}}


def _toss_buyable_qty(code, price, currency="KRW"):
    """토스 매수가능수량 = 매수가능현금 / 단가."""
    try:
        bp = toss_api.get_buying_power(currency)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 매수가능금액 조회 실패({code}): {e}")
        return 0
    cash = _toss_float((bp or {}).get('cashBuyingPower'))
    if price and float(price) > 0:
        return int(cash / float(price))
    return 0


def _toss_sellable_qty(code):
    """토스 매도가능수량. **조회 실패는 None**(=알 수 없음)이다.

    [실패와 '못 판다'를 가른다 · 2026-09-05] 종전에는 실패도 0을 돌려줬다. 호출부
     (trader._sell_worker)는 0을 '팔 수 없는 상태'로 읽어 **매도를 중단**한다 —
     일시적 조회 실패가 손절을 거르는 결과가 된다. KIS 경로는 같은 이유로 이미
     None 을 돌려주도록 고쳐져 있었는데(api/quotes/price.fetch_sellable_quantity 주석)
     토스 어댑터만 그대로였다. 추세추종에서 못 파는 비용은 못 사는 비용보다 훨씬 크다.
     None 을 받으면 호출부가 잔고 수량으로 진행하고, 정말 못 파는 상태면 증권사가 거부한다.
    """
    try:
        sq = toss_api.get_sellable_quantity(code)
    except toss_api.TossApiError as e:
        logger.debug(f"[Toss] 매도가능수량 조회 실패({code}): {e}")
        return None
    if sq is None:
        return None
    return _toss_int(sq.get('sellableQuantity'))
