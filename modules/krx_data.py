"""KRX 공식 시세 — 국내 지수·KRX 금현물·파생(코스피200 선물·변동성지수).

[현황 · 2026-10-05] 값은 전부 modules/krx_openapi(KRX Open API, 인증키 KRX_OPENAPI_KEY 하나)가 준다.
 이 모듈은 그 앞에 붙은 **호출부용 입구 + 캐시**다 — 호출부(analysis·api.charts)가 원천을 몰라도
 되게 하고, 같은 시계열을 화면이 되풀이해 부를 때 저장소를 다시 읽지 않게 한다.

[걷어낸 것 · 2026-10-05] data.krx.co.kr 웹 로그인 스크래핑 경로(pykrx 세션을 빌려 bld 를 직접 POST
 하던 금·파생 조회, pykrx 지수·시장 수급 조회)와 그 게이트 KRX_WEB_SCRAPING_ALLOWED, 자격증명
 KRX_ID/KRX_PW 를 지웠다. 약관 제10조 제2호 위반으로 IP 를 차단당한(2026-09-17) 경로라 기본 꺼져
 있었는데, pykrx 는 **import 만으로** 환경변수의 계정으로 로그인했다 — 게이트가 꺼진 채로도 일봉
 조회(krx_daily)가 패키지를 적재하는 순간 그 사이트를 두드렸다. 그래서 게이트가 아니라 패키지를 뺐다.
 시장 단위 수급·공매도(지수 표의 '수급(개/외/기)'·'공매도' 컬럼)는 Open API 에 대응 서비스가 없어
 원천이 없어졌다 — 2026-09-18 부터 이미 빈 칸이었다.

[한계 — 호출부가 반드시 알아야 한다]
 · **마감 후 확정 봉만 준다.** 장중 현재가는 없다. 지수 화면처럼 현재값이 필요한 곳은
   이 모듈로 '이력'을 받고 당일 값은 실시간 소스가 덮어야 한다(국내 일봉의 krx_daily +
   오버레이와 같은 구조).
 · 조회 불가(키 없음·저장소 결손·오류)면 None — 호출부가 종전 소스(네이버·tvDatafeed·KIS)로 폴백한다.
"""
import logging
import threading
import time
from datetime import datetime

import config

logger = logging.getLogger(__name__)

# 이 입구가 받는 국내 지수(analysis 의 market_type 표기). V코스피200·선물은 전용 함수가 따로 있다.
INDEX_TYPES = ("KOSPI", "KOSDAQ", "KOSPI200", "KOSDAQ150")

_CACHE_MAX = 60

_CACHE = {}                    # key -> {'df': df, 'ts': epoch, 'day': 'YYYYMMDD'}
_CACHE_LOCK = threading.RLock()


def status_text():
    """기동 점검에 찍을 (사용 가능 여부, 한 줄 문구) — Open API 상태가 곧 이 모듈의 상태다.

    [왜 · 2026-08-25] 원천이 꺼져 있으면 이 모듈의 모든 함수가 조용히 None 을 돌려주고
     호출부가 종전 소스로 폴백한다. 동작은 이어지지만 **판단의 원천이 달라진다** — 금현물은
     시·고·저가 종가로 평탄화되고(ATR·ADX 왜곡), 지수는 확정 봉 뼈대 없이 실시간 소스만
     남는다. 화면에 흔적이 없으면 운영자가 '켜져 있다'고 믿는 동안 꺼져 있을 수 있다.
    """
    try:
        from modules import krx_openapi
        return krx_openapi.status_text()
    except Exception as e:      # noqa: BLE001 - 상태 문구가 기동을 막으면 안 된다
        return False, f"KRX Open API 상태 확인 실패 — 종전 소스로 폴백 ({e})"


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
    return None


def _cache_put(key, df):
    now = time.time()
    with _CACHE_LOCK:
        _CACHE[key] = {"df": df, "ts": now, "day": datetime.now().strftime("%Y%m%d")}
        if len(_CACHE) > _CACHE_MAX:
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1]["ts"])[:len(_CACHE) - _CACHE_MAX]
            for k, _ in oldest:
                _CACHE.pop(k, None)


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def _openapi(fn_name, *args, **kwargs):
    """Open API 결과. 키가 없거나 실패하면 None(호출부가 종전 경로로)."""
    try:
        from modules import krx_openapi
        if not krx_openapi.is_configured():     # 쿨다운 중에도 받아 둔 확정분은 읽는다
            return None
        return getattr(krx_openapi, fn_name)(*args, **kwargs)
    except Exception as e:      # noqa: BLE001 - 어떤 실패든 종전 경로로 넘긴다
        logger.debug(f"[KRXDATA] Open API {fn_name} 실패: {e}")
        return None


def _cached(key, use_cache, fetch):
    """캐시 → fetch() → 성공분만 캐시. 실패는 캐시하지 않는다.

    Open API 실패의 대부분은 저장소 결손을 백그라운드가 채우는 중인 경우다(krx_openapi
    KRX_OPENAPI_BACKGROUND_FILL). 음성 캐시를 걸면 채워진 뒤에도 그만큼 종전 소스에 머문다.
    네트워크 재시도 폭주는 krx_openapi 의 쿨다운이 따로 막는다.
    """
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit
    df = fetch()
    if df is None or getattr(df, "empty", True):
        return None
    _cache_put(key, df)
    return df


def get_gold_daily(days=400, use_cache=True):
    """KRX 금현물(원/g) 일봉 — ['date','open','high','low','close','volume']. 조회 불가 시 None.

    네이버 경로와 달리 **시·고·저와 거래량이 실제 값**이다.

    [기준선 주의 · 2026-08-25] 종전 네이버 경로는 시·고·저를 종가로 평탄화했다. 그래서
     True Range 가 종가 차분이었고 ATR·ADX 가 실제보다 작게 나왔으며 OBV 는 아예 불가였다.
     **KRXGOLD 로 돌린 2026-08-25 이전 백테스트·감사 수치와 직접 비교하면 안 된다.**
    """
    return _cached(("gold", int(days)), use_cache, lambda: _openapi("gold_daily", days))


def get_index_daily(market_type, days=400, use_cache=True):
    """KRX 공식 지수 일봉. 거래량을 함께 주므로 지수 OBV 가 성립한다.

    market_type 은 analysis 의 표기('KOSPI'/'KOSDAQ'/'KOSPI200'/'KOSDAQ150')를 그대로 받는다.
    지원하지 않는 지수(V코스피200·선물)는 None — 그건 전용 함수가 따로 있다.
    """
    if market_type not in INDEX_TYPES:
        return None
    return _cached(("index", market_type, int(days)), use_cache,
                   lambda: _openapi("index_daily", market_type, days))


def get_k200_futures_daily(session="F", days=400, use_cache=True):
    """코스피200 선물 근월물 일봉. session='F'(주간) / 'CM'(야간)."""
    want = "야간" if str(session).upper() in ("CM", "야간", "NIGHT") else "주간"
    return _cached(("k200fut", want, int(days)), use_cache,
                   lambda: _openapi("k200_futures_daily", session, days))


def get_vkospi_daily(days=400, use_cache=True):
    """V코스피200(코스피200 변동성지수) 일봉.

    Open API 파생상품지수의 '코스피 200 변동성지수'라 **시·고·저까지** 있다 — 선물 응답의
    SPOT_PRC 로 종가만 모으던 종전 웹 경로보다 낫다. 거래량은 없다(현물 지수라 애초에 없다).
    """
    return _cached(("vkospi", int(days)), use_cache, lambda: _openapi("index_daily", "VKOSPI", days))
