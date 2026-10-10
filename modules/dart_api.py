# modules/dart_api.py
"""OpenDART (전자공시) 연동 계층 - 국내 배당/실적/공시 조회.

api.py에서 분리된 구현. 기존 호출부와의 호환을 위해 api.py가 동일 이름으로
재수출(re-export)하므로, 호출·테스트 patch 는 계속 api.call_dart 방식으로 동작한다.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests

import config

logger = logging.getLogger(__name__)


def _api():
    """호출 시점의 api 모듈 반환.

    내부 상호 호출(get_dart_dividend → call_dart 등)도 api.* 네임스페이스를
    경유시켜, 테스트의 patch.object(api, "call_dart") 등이 분리 전과 동일하게
    내부 호출까지 적용되도록 유지하기 위한 접근자다. (지연 import라 순환 없음)
    """
    import api
    return api


DART_BASE_URL = "https://opendart.fss.or.kr/api"
_dart_corp_map_cache = None  # 프로세스 메모리 캐시
_dart_corp_map_lock = threading.Lock()  # [중요] 동시 다운로드 방지용 락


class DartQueryError(Exception):
    """DART 조회가 **실패**했다 — '데이터가 없다'와 다르다.

    [왜 예외인가 · 2026-09-05] 종전 call_dart 는 한도초과(020)·네트워크 오류·JSON 파손을
     모두 None 으로 돌려줬고, 위의 get_dart_* 들은 그 None 을 `[]` 로 바꿔 내놨다. 그래서
     DART 일일 한도(20,000건)를 소진한 상태로 메뉴 6-7(수급·물량)을 열면 화면은
     **"최근 90일간 수급·물량 관련 보고가 없습니다"** 를 찍는다 — 아무것도 조회하지 못한
     채로 무결점 진단서를 내주는 것이다(실측 재현). 관심종목 하나당 여러 건을 부르는
     화면이라 한도 소진은 드문 일이 아니다.

     오버할(잠재 매도물량)·자기주식은 '없다'로 읽는 순간 판단이 반대로 간다. 같은 파일의
     rem_estimated 가 이미 '모르면 위험 쪽'을 택하는데, 그보다 앞단인 조회 실패가 조용했다.
     실패를 예외로 올리면 modules/manage/scan.ScanFailures 가 이미 깔아 둔 수집 경로를 타고
     화면 맨 위에 밝혀진다. **013(데이터 없음)만 None 이다.**
    """


class DartBlockedError(DartQueryError):
    """DART 호출을 **보내지 않고** 막았다 — 접속 차단 의심·한도 초과로 차단기가 열려 있다.

    한 종목씩 도는 호출부는 이 예외를 보면 남은 종목도 같은 결과이므로 바로 멈추면 된다.
    """


# ── DART 호출 관문: 속도 제한 + 차단기 ─────────────────────────────────────────────
#  [왜 · 2026-10-07] 업종 조사에서 company.json 을 상장사 2,600곳에 4스레드로 쉬지 않고 불렀더니
#   몇 분 만에 opendart.fss.or.kr 가 이 IP 의 TCP 연결 자체를 끊었다(ConnectionReset, 웹 dart.fss.or.kr
#   은 정상). 일일 한도(20,000건, 응답 020)와 별개로 **순간 빈도**로 IP 를 막는다. 막힌 동안 같은
#   공유기 뒤의 모든 DART 기능(6-5~6-8, 텔레그램 공시 알림)이 함께 죽었다.
#   그래서 DART 로 나가는 HTTP 는 전부 _dart_get 하나를 지난다:
#    · 프로세스 전체(모든 스레드 합산) 토큰 버킷 — 짧은 몰림은 DART_BURST 건까지 바로 보내고, 그 뒤로는
#      초당 DART_REFILL_PER_SEC 건씩만 채운다. 메뉴 6(공시·수급·재무·캘린더)은 관심종목마다 여러 건을
#      8스레드로 몰아 부르고(6-7 은 41종목에 369건 이상), 이 몰림은 몇 달 동안 차단된 적이 없다. 막힌 것은
#      10분 넘게 쉬지 않고 이어진 대량 호출이었다. [2026-10-08] 처음엔 고정 간격 0.25초로 막았는데, 그러면
#      6-7 하나가 1분 반 넘게 걸려 운용자가 체감할 만큼 느려졌다 — 몰림은 살리고 지속량만 묶는다.
#    · 연결이 연달아 끊기면(차단 신호) DART_BLOCK_COOLDOWN_SEC 동안 아예 보내지 않는다 — 막힌 상태에서
#      두드리면 차단이 길어질 수 있다. 020(한도 초과)은 자정까지 보내지 않는다.
DART_BURST = 1000                     # 쉬고 있다가 바로 보낼 수 있는 건수 — 메뉴 6 무거운 화면 두세 개를 연달아 열어도 기다리지 않는다
DART_REFILL_PER_SEC = 5.0             # 버킷이 빈 뒤의 지속 속도(분당 300건) — 3분 남짓이면 다시 가득
DART_BLOCK_AFTER_CONN_ERRORS = 3      # 연결 오류가 이만큼 연달아 나면 차단으로 본다
#  [2026-10-08 실측] 서버 응답은 중앙 0.16초인데 꼬리가 길다(6-7 한 번에 399건 중 3초 초과 13건·최대 39초).
#   8스레드라도 느린 몇 건이 화면 전체를 붙잡는다. 읽기 대기는 DART_READ_TIMEOUT_SEC 에서 끊고 한 번만 다시
#   보낸다(조회라 중복돼도 해가 없다). 연결은 재사용한다 — 매번 새로 TLS 를 맺으면 중앙값이 3배 늘었다(0.18→0.06초).
DART_CONNECT_TIMEOUT_SEC = 5
DART_READ_TIMEOUT_SEC = 6
DART_BLOCK_COOLDOWN_SEC = 30 * 60

_dart_gate_lock = threading.Lock()
_dart_tokens = None                   # 남은 토큰(음수 = 앞서 줄 선 몫). None 이면 가득 찬 것으로 시작
_dart_tokens_at = 0.0                 # 토큰을 마지막으로 계산한 time.monotonic()
_dart_conn_errors = 0                 # 연속 연결 오류 수
_dart_blocked_until = 0.0             # time.time() — 이 시각 전에는 보내지 않는다
_dart_blocked_reason = ""


def dart_blocked_reason():
    """차단기가 열려 있으면 사유 문자열, 아니면 None."""
    with _dart_gate_lock:
        if time.time() < _dart_blocked_until:
            left = int((_dart_blocked_until - time.time()) // 60) + 1
            return f"{_dart_blocked_reason} — 약 {left}분 뒤 다시 시도"
        return None


def _open_breaker(until, reason):
    """(잠금 보유 상태에서) 차단기를 연다."""
    global _dart_blocked_until, _dart_blocked_reason
    _dart_blocked_until, _dart_blocked_reason = until, reason
    logger.warning(f"[DART] 호출 중단: {reason} (재개 {datetime.fromtimestamp(until):%m-%d %H:%M})")


def _next_midnight_ts():
    now = datetime.now()
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def _take_token():
    """토큰 하나를 가져가고, 기다려야 할 초를 돌려준다(잠금 밖에서 잔다)."""
    global _dart_tokens, _dart_tokens_at
    with _dart_gate_lock:
        now = time.monotonic()
        if _dart_tokens is None:
            _dart_tokens = float(DART_BURST)
        else:
            _dart_tokens = min(float(DART_BURST),
                               _dart_tokens + (now - _dart_tokens_at) * DART_REFILL_PER_SEC)
        _dart_tokens_at = now
        _dart_tokens -= 1.0
        return 0.0 if _dart_tokens >= 0 else -_dart_tokens / DART_REFILL_PER_SEC


_dart_session = None
_dart_session_lock = threading.Lock()


def _send(url, params=None, timeout=None):
    """실제 전송 — 프로세스 공용 연결 풀(keep-alive). 테스트는 이 자리를 갈아끼운다."""
    global _dart_session
    if _dart_session is None:
        with _dart_session_lock:
            if _dart_session is None:
                from requests.adapters import HTTPAdapter
                s = requests.Session()
                s.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=16))
                _dart_session = s
    return _dart_session.get(url, params=params, timeout=timeout)


def _dart_get(path, params, timeout):
    """DART 로 나가는 유일한 HTTP 자리. 속도를 지키고, 차단기가 열려 있으면 보내지 않는다."""
    global _dart_conn_errors
    reason = dart_blocked_reason()
    if reason:
        raise DartBlockedError(f"DART 호출 중단 중({reason})")
    wait = _take_token()
    if wait > 0:
        time.sleep(wait)
        reason = dart_blocked_reason()      # 기다리는 사이 차단기가 열렸으면 줄 선 호출도 보내지 않는다
        if reason:
            raise DartBlockedError(f"DART 호출 중단 중({reason})")
    url = f"{DART_BASE_URL}/{path}"
    limits = (DART_CONNECT_TIMEOUT_SEC, min(float(timeout or DART_READ_TIMEOUT_SEC), DART_READ_TIMEOUT_SEC))
    try:
        try:
            res = _send(url, params=params, timeout=limits)
        except requests.exceptions.ReadTimeout:
            #  서버 꼬리 지연 — 한 번만 다시(토큰도 다시 낸다). 그 사이 차단기가 열렸으면 보내지 않는다.
            logger.debug(f"[DART] {path} 읽기 {limits[1]}초 초과 — 한 번 다시 보냄")
            wait = _take_token()
            if wait > 0:
                time.sleep(wait)
            reason = dart_blocked_reason()
            if reason:
                raise DartBlockedError(f"DART 호출 중단 중({reason})")
            res = _send(url, params=params, timeout=limits)
    except requests.exceptions.ConnectionError:
        with _dart_gate_lock:
            _dart_conn_errors += 1
            if _dart_conn_errors >= DART_BLOCK_AFTER_CONN_ERRORS:
                _dart_conn_errors = 0
                _open_breaker(time.time() + DART_BLOCK_COOLDOWN_SEC,
                              f"연결이 {DART_BLOCK_AFTER_CONN_ERRORS}번 연달아 끊김(IP 차단 의심)")
        raise
    with _dart_gate_lock:
        _dart_conn_errors = 0
    return res


def call_dart(endpoint, params, timeout=10):
    """OpenDART OpenAPI 공통 호출 래퍼.

    반환: 성공 시 응답 JSON의 'list'(없으면 dict 전체), **데이터 없음(013)이면 None**.
    실패(한도초과·오류·네트워크·JSON 파손)는 DartQueryError 를 던진다 — 호출부가
    '없음'과 구분할 수 있어야 하기 때문이다(DartQueryError 주석 참조).
    API 키 미설정도 조회를 못 한 것이므로 None 이 아니라 예외다.
    차단기가 열려 있으면 보내지 않고 DartBlockedError(DartQueryError 의 하위)를 던진다.
    """
    if not config.DART_API_KEY:
        raise DartQueryError("DART API 키가 설정되지 않았습니다(환경변수 DART_API_KEY)")
    try:
        p = dict(params)
        p["crtfc_key"] = config.DART_API_KEY
        res = _dart_get(endpoint, p, timeout)
        data = res.json()
    except DartBlockedError:
        raise
    except Exception as e:
        logger.error(f"[DART] {endpoint} 호출 오류: {e}")
        raise DartQueryError(f"{endpoint}: {e}") from e
    status = data.get("status")
    if status == "000":
        return data.get("list", data)
    if status == "013":  # 조회된 데이터 없음 (정상 케이스)
        return None
    if status == "020":  # 요청 제한 초과 — 오늘은 더 보내 봐야 같은 답이다
        with _dart_gate_lock:
            _open_breaker(_next_midnight_ts(), "DART 요청 한도 초과(020)")
    logger.warning(f"[DART] {endpoint} 응답 코드 {status}: {data.get('message')}")
    raise DartQueryError(f"{endpoint}: 응답 코드 {status} ({data.get('message')})")


def get_dart_corp_map(force_refresh=False):
    """종목코드(6자리) -> DART 고유번호(corp_code, 8자리) 매핑.

    corpCode.xml(ZIP) 1회 다운로드 후 json 파일로 캐시(30일 TTL).

    맵을 **못 받으면 DartQueryError** 를 던진다. 종전에는 빈 dict 를 돌려줬는데, 위의
    get_dart_* 들이 전부 `corp = map.get(code)` → `if not corp: return []` 이라 맵 하나가
    비면 관심종목 전부가 조용히 '해당 없음'이 된다 — 화면 한 장이 통째로 거짓말한다.
    맵은 받았는데 그 코드가 없는 것(비상장·폐지)은 실패가 아니므로 종전대로 빈 값이다.
    """
    global _dart_corp_map_cache
    if _dart_corp_map_cache is not None and not force_refresh:
        return _dart_corp_map_cache

    if not config.DART_API_KEY:
        raise DartQueryError("DART API 키가 설정되지 않았습니다(환경변수 DART_API_KEY)")

    # [중요] 동시 다운로드 방지: 여러 워커 스레드(공시 수집 등)가 동시에 진입하면
    # 각자 DART 기업코드 ZIP(수십 MB XML+10만건 dict)을 중복 다운로드/파싱해 메모리가
    # 수배로 폭증(OOM)한다. 락으로 직렬화하여 한 스레드만 받고 나머지는 캐시를 재사용한다.
    with _dart_corp_map_lock:
        # 락 획득 후 재확인 (대기 중 다른 스레드가 이미 채웠을 수 있음)
        if _dart_corp_map_cache is not None and not force_refresh:
            return _dart_corp_map_cache

        return _load_dart_corp_map_locked(force_refresh)


_DART_CORP_MAP_TTL_DAYS = 30
_dart_corp_map_asof = 0.0       # 지금 쥔 맵의 자료 시점(time.time) — 파일 mtime 또는 받은 시각
_dart_corp_map_last_error = None  # 마지막 다시 받기 실패 사유(성공하면 비운다)


def _dart_error_text(content):
    """ZIP 대신 온 오류 응답(XML/JSON)에서 'status 메시지'를 뽑는다. 못 뽑으면 앞부분 그대로."""
    import re
    txt = content[:400].decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) else str(content)[:400]
    st = re.search(r'<status>(\w+)</status>|"status"\s*:\s*"(\w+)"', txt)
    msg = re.search(r'<message>(.*?)</message>|"message"\s*:\s*"(.*?)"', txt, re.S)
    if st:
        code = st.group(1) or st.group(2)
        text = (msg.group(1) or msg.group(2)).strip() if msg else ""
        hint = " — DART 시스템 점검 중" if code == "800" else ""
        return f"DART 응답 status {code} {text}{hint}".strip()
    return f"ZIP 이 아닌 응답: {txt[:120]!r}"


def _save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _load_dart_corp_map_locked(force_refresh):
    """락 보유 상태에서 DART 기업코드 맵을 파일캐시/다운로드로 로드한다.

    [만료된 파일도 버리지 않는다 · 2026-10-10] 종전엔 30일이 지난 뒤 다시 받기가 실패하면 갖고 있던
     파일을 쓰지 않고 바로 실패했다 — DART 점검(status 800, 2026-10-09 아침 실측)·IP 차단과 겹치면
     6-5~6-8·텔레그램 공시 알림·탐색 업종이 한꺼번에 멈춘다. 종목코드↔고유번호 대응은 거의 바뀌지
     않으므로(새로 상장한 종목만 빠진다) 옛 파일로 계속 쓰고 경고를 남긴다. 오염 값 폴백이 아니다 —
     옛 맵에 **있는** 대응은 지금도 맞는 값이다. 빠진 종목은 corp_code_for 가 '모름'으로 다룬다.
    """
    global _dart_corp_map_cache, _dart_corp_map_asof, _dart_corp_map_last_error
    cache_path = os.path.join(config.JSON_DIR, "dart_corp_map.json")

    stale = None            # (맵, mtime) — 다시 받기가 실패하면 쓴다
    if os.path.exists(cache_path):
        try:
            mtime = os.path.getmtime(cache_path)
            with open(cache_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and loaded:
                if not force_refresh and (time.time() - mtime) / 86400.0 < _DART_CORP_MAP_TTL_DAYS:
                    _dart_corp_map_cache, _dart_corp_map_asof = loaded, mtime
                    return _dart_corp_map_cache
                stale = (loaded, mtime)
        except Exception as e:      # noqa: BLE001 - 깨진 파일은 새로 받는다
            logger.warning(f"[DART] corp_map 캐시 읽기 실패({cache_path}): {e}")

    # 신규 다운로드 (ZIP 안에 CORPCODE.xml)
    try:
        import zipfile, io
        import xml.etree.ElementTree as ET
        res = _dart_get("corpCode.xml", {"crtfc_key": config.DART_API_KEY}, timeout=20)
        if not res.content.startswith(b"PK"):
            raise DartQueryError(_dart_error_text(res.content))

        # [메모리 최적화] 전체 XML(수십 MB)을 트리로 올리지 않고 스트리밍 파싱(iterparse)으로
        # <list> 요소를 하나씩 처리 후 즉시 비워(clear) 메모리 피크를 최소화한다. (저사양 보호)
        corp_map = {}
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            with zf.open(zf.namelist()[0]) as xmlf:
                for _evt, item in ET.iterparse(xmlf, events=("end",)):
                    if item.tag != "list":
                        continue
                    stock_code = (item.findtext("stock_code") or "").strip()
                    corp_code = (item.findtext("corp_code") or "").strip()
                    if stock_code and corp_code:  # 상장사만 (비상장은 stock_code 공란)
                        corp_map[stock_code] = corp_code
                    item.clear()  # 처리한 요소 즉시 해제

        if corp_map:
            _dart_corp_map_cache, _dart_corp_map_asof = corp_map, time.time()
            _dart_corp_map_last_error = None
            try:
                _save_json_atomic(cache_path, corp_map)
            except Exception as e:
                logger.warning(f"[DART] corp_map 캐시 저장 실패: {e}")
            return corp_map
        raise DartQueryError("DART 기업코드 맵이 비어 있습니다(파싱 결과 0건)")
    except Exception as e:
        reason = str(e)
        _dart_corp_map_last_error = reason
        if _dart_corp_map_cache:
            logger.warning(f"[DART] corp_map 다시 받기 실패 — 쥐고 있던 맵을 계속 씁니다: {reason}")
            return _dart_corp_map_cache
        if stale is not None:
            age = (time.time() - stale[1]) / 86400.0
            logger.warning(f"[DART] corp_map 다시 받기 실패 — 만료된 파일({age:.0f}일 전)을 계속 씁니다: {reason}")
            _dart_corp_map_cache, _dart_corp_map_asof = stale
            return _dart_corp_map_cache
        logger.error(f"[DART] corp_map 다운로드 오류: {reason}")
        raise DartQueryError(f"DART 기업코드 맵을 받지 못했습니다: {reason}") from e


#  [2026-10-10] 맵에 없는 종목 — 맵은 기동 때 한 번 읽고 최대 30일 그대로 쓰므로, 그 사이 상장해 관심종목에
#   넣은 종목은 맵에 없다. 종전엔 12곳이 전부 '해당 없음(빈 값)'으로 답했다 — 조회를 못 한 것인데 '공시 없음'
#   으로 보였다. 맵이 하루보다 오래됐으면 하루 한 번 다시 받아 확인하고, 다시 받기가 실패하면 '모름'(예외)이다.
_corp_miss_lock = threading.Lock()
_corp_miss_refresh_day = None   # 다시 받기를 시도한 날짜(하루 한 번)
_corp_miss_refresh_error = None # 그날 다시 받기가 실패했으면 사유


def _is_watchlist_etf(code):
    """관심종목에 ETF 로 등록된 코드인가. (6-6 공시는 국내 ETF 도 훑는다 — 24개가 매일 맵 재확인을 부르면 안 된다)"""
    try:
        sd = getattr(config.session, "stock_data", None) or {}
        return any(s.get("code") == code for key in ("etfs_kr", "etfs_us") for s in sd.get(key, []))
    except Exception:           # noqa: BLE001
        return False


def corp_code_for(stock_code):
    """종목코드 → DART 고유번호. 상장사가 아니면(새 맵에도 없음) None, 확인할 수 없으면 DartQueryError."""
    global _corp_miss_refresh_day, _corp_miss_refresh_error
    corp = _api().get_dart_corp_map().get(stock_code)
    if corp:
        return corp
    if _is_watchlist_etf(stock_code):
        return None             # ETF 는 DART 공시 주체가 아니다 — 맵에 없는 게 정상(다시 받을 일이 아니다)
    if not _dart_corp_map_asof or time.time() - _dart_corp_map_asof < 86400:
        return None             # 하루 안에 받은 맵에도 없다 — 진짜로 없다(ETF·비상장)
    today = datetime.now().strftime("%Y%m%d")
    with _corp_miss_lock:
        if _corp_miss_refresh_day != today:
            _corp_miss_refresh_day, _corp_miss_refresh_error = today, None
            before = _dart_corp_map_asof
            logger.info(f"[DART] 기업코드 맵에 없는 종목({stock_code}) — 맵을 다시 받아 확인합니다")
            try:
                _api().get_dart_corp_map(force_refresh=True)
            except DartQueryError as e:
                _corp_miss_refresh_error = str(e)
            if _dart_corp_map_asof == before and not _corp_miss_refresh_error:
                _corp_miss_refresh_error = _dart_corp_map_last_error or "기업코드 맵 다시 받기 실패(옛 맵 유지)"
        err = _corp_miss_refresh_error
    corp = _api().get_dart_corp_map().get(stock_code)
    if corp:
        return corp
    if err:
        raise DartQueryError(f"{stock_code}: 기업코드 맵에 없고 맵을 새로 받지 못했습니다 — {err}")
    return None


def get_dart_dividend(stock_code, year=None, reprt_code="11011"):
    """국내 종목의 '배당에 관한 사항' 조회 (정기보고서 기준).

    반환: {'주당배당금': float, '시가배당률': float, '결산월': str, 'year': str} 또는 None.
    reprt_code: 11011=사업보고서(연간), 11012=반기, 11013=1분기, 11014=3분기.
    """
    if year is None:
        # 사업보고서는 다음 해 3월경 공시되므로 직전 회계연도를 우선 조회
        year = datetime.now().year - 1

    corp = corp_code_for(stock_code)
    if not corp:
        return None

    rows = _api().call_dart("alotMatter.json", {
        "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt_code
    })
    if not rows or not isinstance(rows, list):
        return None

    def _to_num(s):
        try:
            return float(str(s).replace(",", "").strip())
        except Exception:
            return 0.0

    result = {"year": str(year), "주당배당금": 0.0, "시가배당률": 0.0}
    for row in rows:
        se = (row.get("se") or "").strip()          # 항목명
        val = row.get("thstrm")                       # 당기 값
        # 주당 현금배당금(원) / 현금배당수익률(%) 추출 (보통주 기준)
        if "주당 현금배당금" in se or ("주당배당금" in se and "현금" in se):
            num = _to_num(val)
            if num > result["주당배당금"]:
                result["주당배당금"] = num
        elif "현금배당수익률" in se or "시가배당" in se:
            num = _to_num(val)
            if num > result["시가배당률"]:
                result["시가배당률"] = num

    if result["주당배당금"] <= 0 and result["시가배당률"] <= 0:
        return None
    return result


_dart_acc_month_cache = {}  # 종목코드 -> 결산월


def get_dart_acc_month(stock_code):
    """종목의 결산월('12' 등) 조회 (company.json). 프로세스 메모리 캐시."""
    if stock_code in _dart_acc_month_cache:
        return _dart_acc_month_cache[stock_code]

    acc = None
    corp = corp_code_for(stock_code)
    if corp:
        data = _api().call_dart("company.json", {"corp_code": corp})
        if isinstance(data, dict):
            acc = (data.get("acc_mt") or "").strip() or None
    _dart_acc_month_cache[stock_code] = acc
    return acc


_DISCLOSURE_MAX_PAGES = 20     # 100건 × 20 = 2,000건 — 대형사 2년치도 덮는다


def get_dart_disclosures(stock_code, days=30, pblntf_ty=None, page_count=100):
    """종목의 최근 공시 목록 조회 (list.json).

    반환: [{rcept_no, report_nm, flr_nm, rcept_dt, rm, corp_name}, ...] (최신순).
    맵에 없는 종목·데이터 없음(013)은 [] — 조회 **실패**는 DartQueryError(call_dart 참조).
    pblntf_ty: 공시유형 코드(A정기/B주요사항/C발행/D지분 등). None이면 전체.

    [페이지 · 2026-09-20] list.json 은 한 페이지 최대 100건이다. 종전에는 첫 페이지만 받아
    **창이 조용히 최신 100건으로 잘렸다** — 호출부는 730일(전환청구권행사, insider)·400일
    (지난해 잠정실적으로 다음 실적일 추정, events)을 달라고 하는데, 대형사는 한 해 공시가
    150~250건이라 창의 앞부분(가장 오래된 쪽)이 통째로 빠졌다. 실적 예상일은 하필 그
    오래된 쪽(1년 전 공시)에서 나온다. 한 페이지가 꽉 차면 다음 페이지를 이어 받는다.
    """
    corp = corp_code_for(stock_code)
    if not corp:
        return []
    end = datetime.now()
    bgn = end - timedelta(days=int(days))
    params = {
        "corp_code": corp,
        "bgn_de": bgn.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_count": str(page_count),
        "sort": "date", "sort_mth": "desc",
    }
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty
    out, seen = [], set()
    for page_no in range(1, _DISCLOSURE_MAX_PAGES + 1):
        rows = _api().call_dart("list.json", dict(params, page_no=str(page_no)))
        if not rows or not isinstance(rows, list):
            break
        added = 0
        for r in rows:
            rcept_no = r.get("rcept_no", "")
            if rcept_no and rcept_no in seen:      # 같은 페이지를 되풀이 받으면 여기서 멈춘다
                continue
            seen.add(rcept_no)
            added += 1
            out.append({
                "rcept_no": rcept_no,
                "report_nm": (r.get("report_nm") or "").strip(),
                "flr_nm": (r.get("flr_nm") or "").strip(),
                "rcept_dt": (r.get("rcept_dt") or "").strip(),
                "rm": (r.get("rm") or "").strip(),
                "corp_name": (r.get("corp_name") or "").strip(),
            })
        if added == 0 or len(rows) < int(page_count):
            break
    else:
        #  [2026-10-10] 상한까지 꽉 찼다 — 창의 오래된 쪽이 잘렸을 수 있다(관심 41종목 중 삼성전자만 해당:
        #   730일 3,708건). 조용히 넘기지 않고 남긴다. 거래소공시 등 유형(pblntf_ty)을 좁히면 대개 풀린다.
        logger.warning(f"[DART] {stock_code} 공시 목록이 {_DISCLOSURE_MAX_PAGES}쪽 상한에 닿았다 — "
                       f"{days}일 창의 오래된 쪽이 빠졌을 수 있음(유형 {pblntf_ty or '전체'}, {len(out)}건)")
    return out


def _rcept_date(row):
    """접수일자(YYYYMMDD). rcept_dt가 없으면 접수번호 앞 8자리로 복원한다.

    DART API는 계열에 따라 날짜 필드 제공 여부가 다르다(실측 2026-07-22, 삼성전자):
      - 공시목록 계열(list/elestock/majorstock): rcept_dt 제공 ✅
      - 주요사항보고서 '결정' 계열(자기주식·메자닌·무상증자·감자): rcept_dt **미제공** ❌
        → 응답 키에 아예 없고 접수번호(14자리) 앞 8자리가 접수일자다.
          예: rcept_no=20260713000395 → 2026-07-13
    이 복원이 없으면 화면의 '일자' 칸이 공백이 되고, rcept_dt 기준 정렬도 전부 빈 문자열
    비교가 되어 최신순 정렬이 무효화된다.
    """
    dt = str(row.get("rcept_dt") or "").replace("-", "").strip()
    if len(dt) == 8 and dt.isdigit():
        return dt
    head = str(row.get("rcept_no") or "").strip()[:8]
    return head if len(head) == 8 and head.isdigit() else ""


def _fill_rcept_dt(rows):
    """결정 계열 응답에 rcept_dt를 주입해 호출측이 날짜 필드를 그대로 쓰게 한다."""
    if not isinstance(rows, list):
        return []
    for r in rows:
        if isinstance(r, dict):
            r["rcept_dt"] = _rcept_date(r)
    return rows


def _dart_num(s):
    """DART 숫자 문자열('1,234', '△12' 등) -> float. 파싱 불가 시 None."""
    if s is None:
        return None
    t = str(s).replace(",", "").replace("△", "-").replace("▲", "-").strip()
    if t in ("", "-", "0-"):
        return None
    try:
        return float(t)
    except Exception:
        return None


def _norm_insider_row(r):
    return {
        "rcept_no": r.get("rcept_no", ""),
        "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
        "repror": (r.get("repror") or "").strip(),
        "ofcps": (r.get("isu_exctv_ofcps") or "").strip(),
        "main_shrholdr": (r.get("isu_main_shrholdr") or "").strip(),
        "qty": _dart_num(r.get("sp_stock_lmp_cnt")),
        "chg": _dart_num(r.get("sp_stock_lmp_irds_cnt")),
        "rate": _dart_num(r.get("sp_stock_lmp_rate")),
        "rate_chg": _dart_num(r.get("sp_stock_lmp_irds_rate")),
        "baseline": False,
    }


def get_dart_insider_trades(stock_code, since=None, keep_baseline=False):
    """임원·주요주주 특정증권등 소유상황 보고 (elestock.json, 최신순).

    반환: [{rcept_no, rcept_dt, repror, ofcps, main_shrholdr, qty, chg, rate, rate_chg,
            baseline}, ...]
    qty=보유 특정증권 수, chg=증감 수량(+매수/-매도), rate=보유비율(%).
    since: 'YYYYMMDD' — 응답이 전체 이력(수천 건)이라 이 날짜 이전은 정규화 전에 버려
           저사양 환경의 메모리 사용을 줄인다.
    keep_baseline: since 이전 구간에서 보고자별 '가장 최근 1건'만 baseline=True로 남긴다.
           DART의 증감 칸은 신규·재보고 시 보유 전량이 그대로 들어와(chg == qty) 지분
           유지가 대량 취득으로 보이므로, 직전 보유수량을 알아야 실제 증감을 차분으로
           복원할 수 있다. 보고자당 1건이라 메모리 부담은 거의 없다.
    """
    corp = corp_code_for(stock_code)
    if not corp:
        return []
    rows = _api().call_dart("elestock.json", {"corp_code": corp})
    if not rows or not isinstance(rows, list):
        return []
    out = []
    base = {}
    for r in rows:
        dt = (r.get("rcept_dt") or "").replace("-", "")
        if since and dt < since:
            if keep_baseline:
                who = (r.get("repror") or "").strip()
                prev = base.get(who)
                if prev is None or dt >= prev[0]:
                    base[who] = (dt, r)
            continue
        out.append(_norm_insider_row(r))
    for _dt, r in base.values():
        row = _norm_insider_row(r)
        row["baseline"] = True
        out.append(row)
    return out


def get_dart_major_holdings(stock_code):
    """대량보유(5%) 상황 보고 (majorstock.json, 최신순).

    반환: [{rcept_no, rcept_dt, repror, reason, qty, chg, rate, rate_chg}, ...]
    """
    corp = corp_code_for(stock_code)
    if not corp:
        return []
    rows = _api().call_dart("majorstock.json", {"corp_code": corp})
    if not rows or not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        out.append({
            "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "repror": (r.get("repror") or "").strip(),
            "reason": " ".join((r.get("report_resn") or "").split()),
            "qty": _dart_num(r.get("stkqy")),
            "chg": _dart_num(r.get("stkqy_irds")),
            "rate": _dart_num(r.get("stkrt")),
            "rate_chg": _dart_num(r.get("stkrt_irds")),
        })
    return out


def get_dart_financials(stock_code, year, reprt_code):
    """단일회사 주요계정 (fnlttSinglAcnt.json) 원본 rows. 없으면 None.

    reprt_code: 11011=사업, 11012=반기, 11013=1분기, 11014=3분기.
    """
    corp = corp_code_for(stock_code)
    if not corp:
        return None
    rows = _api().call_dart("fnlttSinglAcnt.json", {
        "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt_code
    })
    return rows if isinstance(rows, list) else None


def get_dart_paid_increase_detail(stock_code, bgn_de, end_de):
    """유상증자 결정 세부내역 (piicDecsn.json). 없으면 [].

    주요 필드: nstk_ostk_cnt(신주 보통주), nstk_estk_cnt(신주 기타주),
    bfic_tisstk_ostk(증자 전 발행주식총수), ic_mthn(증자방식), fdpp_*(자금 목적).
    """
    corp = corp_code_for(stock_code)
    if not corp:
        return []
    rows = _api().call_dart("piicDecsn.json", {
        "corp_code": corp, "bgn_de": bgn_de, "end_de": end_de
    })
    return rows if isinstance(rows, list) else []


_BOND_ENDPOINTS = {
    "CB": "cvbdIsDecsn.json",   # 전환사채
    # [Fix] 신주인수권부사채는 bdwtIsDecsn. 기존 'bwbdIsDecsn'는 존재하지 않는 URL이라
    #  DART가 status 101(잘못된 URL)을 돌려주었고, BW 오버행이 조회 자체가 되지 않았다.
    #  (실측 2026-07-22: bwbdIsDecsn→101 / bdwtIsDecsn→013 '조회된 데이타가 없습니다')
    "BW": "bdwtIsDecsn.json",   # 신주인수권부사채
    "EB": "exbdIsDecsn.json",   # 교환사채
}


def get_dart_bond_issue_detail(stock_code, bgn_de, end_de, kind="CB"):
    """메자닌(CB/BW/EB) 발행 결정 세부내역. 없으면 [].

    주요 필드: bd_fta(권면총액), cv_prc/ex_prc(전환/행사가액), bdis_mthn(발행방법).
    """
    endpoint = _BOND_ENDPOINTS.get(kind)
    if not endpoint:
        return []
    corp = corp_code_for(stock_code)
    if not corp:
        return []
    rows = _api().call_dart(endpoint, {
        "corp_code": corp, "bgn_de": bgn_de, "end_de": end_de
    })
    # [Fix] 이 계열은 rcept_dt를 주지 않으므로 접수번호에서 복원해 주입한다(_rcept_date 참조)
    return _fill_rcept_dt(rows)


def _decsn_rows(stock_code, endpoint, bgn_de, end_de):
    """주요사항보고서 결정 계열(기간 조회) 공통 래퍼. 없으면 [].

    이 계열(자기주식·무상증자·감자 등)은 rcept_dt를 주지 않아 접수번호에서 복원해 주입한다.
    """
    corp = corp_code_for(stock_code)
    if not corp:
        return []
    rows = _api().call_dart(endpoint, {
        "corp_code": corp, "bgn_de": bgn_de, "end_de": end_de
    })
    return _fill_rcept_dt(rows)


def get_dart_treasury_decisions(stock_code, bgn_de, end_de):
    """자기주식 취득/처분/신탁계약 체결 결정 (수급 신호 — 회사 단위 매수는 내부자 개인 매매보다 강함).

    반환: [{kind, rcept_no, rcept_dt, qty, amount, bgd, edd, note}, ...] (최신순)
    kind: '취득'|'처분'|'신탁체결'. amount=예정금액(원), qty=예정주식수(보통주).
    """
    out = []
    # 1) 직접 취득 결정
    for r in _decsn_rows(stock_code, "tsstkAqDecsn.json", bgn_de, end_de):
        out.append({
            "kind": "취득", "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "qty": _dart_num(r.get("aqpln_stk_ostk")),
            "amount": _dart_num(r.get("aqpln_prc_ostk")),
            "bgd": (r.get("aqexpd_bgd") or "").strip(),
            "edd": (r.get("aqexpd_edd") or "").strip(),
            "note": " ".join((r.get("aq_pp") or "").split()),  # 취득목적
        })
    # 2) 처분 결정
    for r in _decsn_rows(stock_code, "tsstkDpDecsn.json", bgn_de, end_de):
        out.append({
            "kind": "처분", "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "qty": _dart_num(r.get("dppln_stk_ostk")),
            "amount": _dart_num(r.get("dppln_prc_ostk")),
            "bgd": (r.get("dpprpd_bgd") or "").strip(),
            "edd": (r.get("dpprpd_edd") or "").strip(),
            "note": " ".join((r.get("dp_pp") or "").split()),  # 처분목적
        })
    # 3) 신탁계약 체결 결정 (간접 취득)
    for r in _decsn_rows(stock_code, "tsstkAqTrctrCnsDecsn.json", bgn_de, end_de):
        out.append({
            "kind": "신탁체결", "rcept_no": r.get("rcept_no", ""),
            "rcept_dt": (r.get("rcept_dt") or "").replace("-", "").strip(),
            "qty": None,
            "amount": _dart_num(r.get("ctr_prc")),
            "bgd": (r.get("ctr_pd_bgd") or "").strip(),
            "edd": (r.get("ctr_pd_edd") or "").strip(),
            "note": "신탁계약",
        })
    out.sort(key=lambda r: r["rcept_dt"], reverse=True)
    return out


def get_dart_free_increase_detail(stock_code, bgn_de, end_de):
    """무상증자 결정 세부내역 (fricDecsn.json). 없으면 [].

    주요 필드: nstk_ostk_cnt(신주 보통주 수), nstk_ascnt_ps_ostk(1주당 배정 주식수),
    nstk_asstd(신주배정기준일), bfic_tisstk_ostk(증자 전 발행주식총수).
    """
    return _decsn_rows(stock_code, "fricDecsn.json", bgn_de, end_de)


def get_dart_capital_reduction_detail(stock_code, bgn_de, end_de):
    """감자 결정 세부내역 (crDecsn.json). 없으면 [].

    주요 필드: cr_rt_ostk(감자비율 %), cr_std(감자기준일), cr_mth(감자방법), cr_rs(감자사유).
    """
    return _decsn_rows(stock_code, "crDecsn.json", bgn_de, end_de)


_dart_shares_cache = {}  # 종목코드 -> (발행주식총수, 유통주식수) — 정기보고서 기준이라 프로세스 캐시로 충분


def get_dart_shares_outstanding(stock_code):
    """주식의 총수 현황 (stockTotqySttus.json) — (발행주식총수, 유통주식수) 또는 (None, None).

    최근 사업/분기보고서 순으로 조회. 오버행(전환물량) 비중 계산 등에 사용.
    """
    if stock_code in _dart_shares_cache:
        return _dart_shares_cache[stock_code]
    corp = corp_code_for(stock_code)
    result = (None, None)
    if corp:
        y = datetime.now().year
        for year, reprt in ((y - 1, "11011"), (y - 2, "11011")):
            rows = _api().call_dart("stockTotqySttus.json", {
                "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt})
            if not isinstance(rows, list):
                continue
            for r in rows:
                se = (r.get("se") or "").replace(" ", "")
                if "보통주" in se or "합계" in se:
                    tot = _dart_num(r.get("istc_totqy"))
                    distb = _dart_num(r.get("distb_stock_co"))
                    if tot:
                        result = (tot, distb)
                        break
            if result[0]:
                break
    _dart_shares_cache[stock_code] = result
    return result


# 재무지표 분류코드 (fnlttSinglIndx.json idx_cl_code)
DART_INDEX_CLASSES = {
    "M210000": "수익성", "M220000": "안정성", "M230000": "성장성", "M240000": "활동성",
}


def get_dart_financial_index(stock_code, year, reprt_code, idx_cl_code):
    """단일회사 주요 재무지표 (fnlttSinglIndx.json) — DART가 계산한 지표 원본 rows.

    idx_cl_code: M210000 수익성 / M220000 안정성 / M230000 성장성 / M240000 활동성.
    row 필드: idx_nm(지표명), idx_val(값), bsns_year, stlm_dt. 없으면 None.
    """
    corp = corp_code_for(stock_code)
    if not corp:
        return None
    rows = _api().call_dart("fnlttSinglIndx.json", {
        "corp_code": corp, "bsns_year": str(year), "reprt_code": reprt_code,
        "idx_cl_code": idx_cl_code,
    })
    return rows if isinstance(rows, list) else None


# ---------------------------------------------------------------------------
# 공시 원문(document.xml) 기반 잠정실적 파싱
# ---------------------------------------------------------------------------
def get_dart_document_text(rcept_no):
    """공시 원문(document.xml ZIP)을 내려받아 태그를 제거한 텍스트 반환. 실패 시 None."""
    if not config.DART_API_KEY or not rcept_no:
        return None
    try:
        import io
        import re
        import zipfile
        res = _dart_get("document.xml", {"crtfc_key": config.DART_API_KEY, "rcept_no": rcept_no},
                        timeout=15)
        if not res.content.startswith(b"PK"):  # ZIP이 아니면 오류 JSON
            return None
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            raw = zf.read(zf.namelist()[0])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp949", errors="replace")
        text = re.sub(r"<[^>]+>", "\n", text)
        import html as _html
        return _html.unescape(text)
    except Exception as e:
        logger.debug(f"[DART] document.xml({rcept_no}) 조회 실패: {e}")
        return None


# 잠정실적 표의 숫자/증감 셀 토큰 (숫자, %, 흑전·적전 등)
_EARNINGS_TOKEN = None  # 모듈 임포트 시점 컴파일 비용 회피 (지연 컴파일)


def _earnings_token_re():
    global _EARNINGS_TOKEN
    if _EARNINGS_TOKEN is None:
        import re
        _EARNINGS_TOKEN = re.compile(
            r"^[-+△▲(]?\s*[\d,]+(?:\.\d+)?\s*[)%]?$|^(?:-|흑전|적전|흑자전환|적자전환|적자지속|흑자지속)$")
    return _EARNINGS_TOKEN


def parse_earnings_brief(text):
    """잠정실적/손익구조변동 공시 텍스트에서 매출·영업이익·순이익을 추출 (best-effort).

    반환: {"unit": 배수(원), "rows": {지표명: (당기, 전년동기/전기, 증감률str|None)}} 또는 None.
    '-'(빈 셀) 제거 후 남은 열 수로 레이아웃 판별:
      5+열=[당기,직전,QoQ,전년동기,YoY], 4열=[당기,전기,증감액,증감률],
      3열=[당기,전기,증감률], 2열=[당기,비교값(증감률은 직접 계산)].
    """
    if not text:
        return None
    import re
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    unit = 1.0
    for ln in lines[:400]:
        m = re.search(r"단위\s*[::]?\s*(조원|백만원|천만원|억원|천원|원)", ln.replace(" ", ""))
        if m:
            unit = {"조원": 1e12, "백만원": 1e6, "천만원": 1e7,
                    "억원": 1e8, "천원": 1e3, "원": 1.0}[m.group(1)]
            break

    token_re = _earnings_token_re()
    metrics = (("매출액", "매출액"), ("영업이익", "영업이익"), ("당기순이익", "당기순이익"))
    rows = {}
    for i, ln in enumerate(lines):
        compact = ln.replace(" ", "")
        for key, label in metrics:
            if label in rows:
                continue
            # 셀 라벨 형태만 매칭 (문장 속 언급 제외)
            if compact == key or (compact.startswith(key) and len(compact) <= len(key) + 6
                                  and "율" not in compact and "액또는" not in compact):
                toks, skipped = [], 0
                for nxt in lines[i + 1:i + 12]:
                    t = nxt.replace(" ", "")
                    if token_re.match(t):
                        toks.append(t)
                    elif toks:
                        break
                    else:  # 라벨과 숫자 사이 헤더 셀 등은 소량 허용
                        skipped += 1
                        if skipped > 2:
                            break
                toks = [t for t in toks if t != "-"]
                if not toks:
                    continue
                cur = _dart_num(toks[0])
                base = pct = None
                if len(toks) >= 5:
                    base, pct = _dart_num(toks[3]), toks[4]
                elif len(toks) == 4:
                    base, pct = _dart_num(toks[1]), toks[3]
                elif len(toks) == 3:
                    base, pct = _dart_num(toks[1]), toks[2]
                elif len(toks) == 2:
                    base = _dart_num(toks[1])
                if cur is not None:
                    rows[label] = (cur, base, pct)
    if not rows:
        return None
    return {"unit": unit, "rows": rows}


def get_dart_earnings_brief(rcept_no):
    """잠정실적 공시 원문에서 주요 수치 추출. 실패 시 None."""
    return parse_earnings_brief(_api().get_dart_document_text(rcept_no))


# ---------------------------------------------------------------------------
# 배당 결정 공시(현금ㆍ현물배당결정 — 거래소 수시공시) 원문 파싱
#  주요사항보고서 구조화 API가 없어(DS005 36종에 배당 없음) 원문에서 추출한다.
# ---------------------------------------------------------------------------
_DIV_DECISION_TITLE = ("현금ㆍ현물배당", "현금·현물배당", "현금배당", "현물배당")


def _find_date_after(lines, i, limit=6):
    """라벨 라인 이후 limit줄 안에서 날짜(YYYY-MM-DD류) 탐색 → 'YYYYMMDD'."""
    import re
    for nxt in lines[i + 1:i + 1 + limit]:
        m = re.search(r"(20\d{2})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})", nxt)
        if m:
            return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return None


def parse_dividend_decision(text):
    """배당결정 공시 텍스트에서 1주당 배당금·배당기준일·지급예정일 추출 (best-effort).

    반환: {"dps": float|None, "record_date": 'YYYYMMDD'|None,
           "pay_date": 'YYYYMMDD'|None, "yield": float|None} 또는 None.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {"dps": None, "record_date": None, "pay_date": None, "yield": None}
    for i, ln in enumerate(lines):
        compact = ln.replace(" ", "")
        if out["dps"] is None and "1주당배당금" in compact:
            for nxt in lines[i + 1:i + 7]:
                num = _dart_num(nxt.replace(" ", ""))
                if num is not None and num > 0:
                    out["dps"] = num
                    break
        elif out["record_date"] is None and "배당기준일" in compact:
            out["record_date"] = _find_date_after(lines, i)
        elif out["pay_date"] is None and ("지급예정" in compact and "일" in compact):
            out["pay_date"] = _find_date_after(lines, i)
        elif out["yield"] is None and "시가배당" in compact:
            for nxt in lines[i + 1:i + 5]:
                num = _dart_num(nxt.replace(" ", "").replace("%", ""))
                if num is not None and 0 < num < 100:
                    out["yield"] = num
                    break
    if out["dps"] is None and out["record_date"] is None:
        return None
    return out


#  배당결정·잠정실적은 둘 다 거래소공시(I)다. [2026-10-09] 전체 유형으로 받으면 공시가 많은 대형사는
#   400일 목록이 페이지 상한(20쪽·2,000건)에서 잘렸다 — 삼성전자는 지난 배당결정 2건과 잠정실적 공시가
#   빠져 실적 예상일이 사라졌다. 거래소공시로 좁히면 잘리지 않고, 호출도 41종목 100→45건으로 준다.
DART_EXCHANGE_FILING = "I"


def get_dart_dividend_decision(stock_code, days=200, rows=None):
    """최근 배당결정 공시(현금ㆍ현물배당결정)를 찾아 원문에서 확정 배당 정보를 추출.

    반환: {"dps", "record_date", "pay_date", "yield", "rcept_dt", "rcept_no"} 또는 None.
    분기·결산 배당의 '확정' 기준일을 제공한다 (캘린더의 추정 배당락일을 확정값으로 대체).
    rows: 이미 받아 둔 거래소공시 목록(최신순, days 보다 넓어도 된다) — 주면 목록을 다시 받지 않는다.
    """
    if rows is None:
        rows = _api().get_dart_disclosures(stock_code, days=days, pblntf_ty=DART_EXCHANGE_FILING)
    else:
        cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y%m%d")
        rows = [r for r in rows if str(r.get("rcept_dt") or "") >= cutoff]
    for r in rows:  # 최신순 — 첫 매칭이 최근 결정
        nm = r.get("report_nm", "")
        if not any(k in nm for k in _DIV_DECISION_TITLE):
            continue
        parsed = parse_dividend_decision(_api().get_dart_document_text(r["rcept_no"]))
        if parsed:
            parsed["rcept_dt"] = r.get("rcept_dt", "")
            parsed["rcept_no"] = r.get("rcept_no", "")
            return parsed
    return None
