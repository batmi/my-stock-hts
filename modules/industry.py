# modules/industry.py
"""국내 종목 업종 — DART 기업개황(company.json)의 표준산업분류 코드(induty_code).

[왜 DART 인가 · 2026-10-07] 탐색 메뉴(7-4)의 업종은 KIND 상장법인목록(KRX-DESC)을 FDR 캐시 저장소에서
 받아 왔는데, 그 저장소가 2026-09-17 에서 갱신을 멈췄다(KRX 스크래핑 차단). KIND 직접 수집은 같은 약관
 문제를 다시 부르고, KRX Open API 에는 업종이 없다. DART 기업개황은 이미 쓰는 키로 모든 모드에서 열리고,
 KIND 업종명과 같은 표준산업분류(11차) 코드를 준다 — 소분류(3자리) 이름이 KIND 업종명과 94% 글자까지
 같고(나머지는 가운뎃점·띄어쓰기 차이), 방어주·지주회사 규칙이 쓰는 이름은 전부 같다(modules/ksic11.py).

[호출 예산] 상장사 하나에 한 번씩 불러야 한다. 처음 한 번 수백 건이 들고, 그 뒤로는 캐시
 (json/dart_industry.json)에서 끝난다 — 업종은 거의 안 바뀌므로 REFRESH_DAYS 마다 다시 묻는다.
 속도는 dart_api 의 관문(토큰 버킷 DART_BURST·DART_REFILL_PER_SEC)이 정하고, 차단기가 열리면 그 자리에서 멈춘다.
 대량 조회라 관문보다 더 묶는다(FAST_CALLS·PACE_SEC·MAX_PER_RUN·REFRESH_JITTER_DAYS 주석 참조).
 **여기서 스레드를 늘리지 말 것** — 2026-10-07 4스레드 일괄 조회가 DART IP 차단을 불렀다.
"""
import json
import logging
import os
import threading
import time
import zlib
from datetime import date, datetime

import config
from modules.ksic11 import KSIC11_NAMES

logger = logging.getLogger(__name__)

CACHE_FILE = "dart_industry.json"
REFRESH_DAYS = 180          # 이보다 오래된 항목은 다시 묻는다(실패하면 옛 값을 계속 쓴다)
SAVE_EVERY = 20             # 조회 도중 끊겨도 받은 만큼은 남게 이만큼마다 저장
#  [2026-10-15] 대량 조회 예산. 관문(DART_BURST 1000건 즉시·이후 초당 5건)은 메뉴 6의 짧은 몰림에 맞춘 것이라,
#   탐색 메뉴가 시가총액 상위 2,000위(후보 약 1,500건)를 처음 채우면 2~3분 동안 쉬지 않고 이어진다 —
#   10-07 IP 차단을 부른 것과 같은 꼴(수천 건을 몇 분 동안 연달아)이다. 그래서 이 모듈만 따로 더 묶는다:
#    · 한 번 실행에 처음 FAST_CALLS 건은 관문 속도로, 그 뒤로는 PACE_SEC 간격(분당 60건)으로 보낸다
#    · 한 번 실행에 MAX_PER_RUN 건까지만 묻고 나머지는 '업종 미상'으로 두어 다음 실행에 이어 받는다
#    · 만료일을 종목마다 REFRESH_JITTER_DAYS 안에서 흩는다 — 첫날 한꺼번에 받은 항목이 180일 뒤 같은 날
#      한꺼번에 만료되어 같은 대량 조회가 되풀이되지 않게. 새로 받을 종목을 만료된 종목보다 먼저 묻는다.
FAST_CALLS = 300
PACE_SEC = 1.0
MAX_PER_RUN = 600
REFRESH_JITTER_DAYS = 60

_lock = threading.Lock()


def _cache_path():
    return os.path.join(config.JSON_DIR, CACHE_FILE)


def _load():
    """{종목코드: {"ksic": str, "at": "YYYY-MM-DD"}}. 파일이 없거나 깨졌으면 빈 dict(처음부터 다시 받는다)."""
    path = _cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:      # noqa: BLE001 - 깨진 캐시는 다시 받으면 된다
        logger.warning(f"[업종] 캐시 읽기 실패({path}): {e} — 처음부터 다시 받습니다")
        return {}


def _save(cache):
    path = _cache_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:      # noqa: BLE001 - 저장 실패는 다음에 다시 받을 뿐이다
        logger.warning(f"[업종] 캐시 저장 실패({path}): {e}")


def _refresh_days(code):
    """종목마다 고정된 만료 일수(REFRESH_DAYS ~ +REFRESH_JITTER_DAYS-1) — 실행마다 같은 값이어야 한다(hash 는 실행마다 바뀐다)."""
    return REFRESH_DAYS + zlib.crc32(str(code).encode()) % REFRESH_JITTER_DAYS


def _is_stale(entry, today, code=""):
    try:
        at = datetime.strptime(str(entry.get("at", ""))[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - at).days >= _refresh_days(code)


def name(ksic):
    """표준산업분류 코드 → 업종명(11차 소분류, 없으면 중분류). 코드가 비면 None."""
    k = str(ksic or "").strip()
    if not k:
        return None
    return KSIC11_NAMES.get(k[:3]) or KSIC11_NAMES.get(k[:2]) or f"업종코드 {k}"


def lookup(codes, progress=None, today=None):
    """종목들의 업종 코드를 돌려준다 — 캐시에 없거나 오래된 것만 DART 에 묻는다.

    반환: (known, fail_reason)
      known       {종목코드: 표준산업분류 코드}. '' 는 DART 가 업종 칸을 비워 둔 것(알려진 빈 값).
                  **known 에 없는 종목은 모른다**(조회 실패·DART 기업코드 맵에 없음) — 호출부가 구분한다.
      fail_reason 조회를 다 못 했으면 그 사유(화면 안내용), 다 했으면 None.
    progress: progress(done, total) — 새로 묻는 건수 기준.
    """
    import api
    from modules.dart_api import DartBlockedError, DartQueryError, corp_code_for

    today = today or date.today()
    with _lock:
        cache = _load()
        known = {c: cache[c].get("ksic", "") for c in codes if c in cache}
        #  새로 받을 종목이 먼저(업종 미상을 줄인다), 만료된 종목은 옛 값이 있으니 뒤로.
        todo = ([c for c in codes if c not in cache]
                + [c for c in codes if c in cache and _is_stale(cache[c], today, c)])
        if not todo:
            return known, None
        deferred = max(0, len(todo) - MAX_PER_RUN)
        todo = todo[:MAX_PER_RUN]

        try:
            api.get_dart_corp_map()
        except DartQueryError as e:
            return known, str(e)

        reason, dirty, failed, sent = None, 0, 0, 0
        for i, code in enumerate(todo):
            if progress:
                progress(i, len(todo))
            if sent >= FAST_CALLS and PACE_SEC > 0:
                time.sleep(PACE_SEC)
            try:
                #  맵에 없으면 하루 한 번 맵을 다시 받아 확인한다(새로 상장한 종목). 그래도 없으면 모름으로 둔다.
                corp = corp_code_for(code)
                if not corp:
                    continue
                sent += 1
                data = api.call_dart("company.json", {"corp_code": corp})
            except DartBlockedError as e:
                reason = str(e)
                break
            except DartQueryError as e:
                failed += 1
                reason = f"{failed}건 조회 실패(마지막: {e})"
                continue
            ksic = str((data or {}).get("induty_code") or "").strip() if not isinstance(data, list) else ""
            cache[code] = {"ksic": ksic, "at": today.isoformat()}
            known[code] = ksic
            dirty += 1
            if dirty % SAVE_EVERY == 0:
                _save(cache)
        if progress:
            progress(len(todo), len(todo))
        if dirty:
            _save(cache)
        if deferred and not (reason and "중단" in reason):
            note = f"이번 실행 상한 {MAX_PER_RUN}건 — 남은 {deferred}건은 다음 실행에서"
            reason = f"{reason} · {note}" if reason else note
        logger.info(f"[업종] DART 조회 {dirty}/{len(todo)}건 저장" + (f" · {reason}" if reason else ""))
        return known, reason
